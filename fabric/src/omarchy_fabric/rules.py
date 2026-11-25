"""Rules: the deterministic fast path.

A rule maps an event to actions without a model in the loop: a double press on
the desk button toggles the lights of the room it is in, a leak closes the water
main, the desktop starting a screen share dims the office. Matching happens in
the event callback, so a rule fires in the same loop turn as its trigger.

    [[rules]]
    id = "desk-button"
    on = { gesture = "double", thing = "gpio:desk_button" }
    do = [ { thing = "area:{room}&kind:light", command = "toggle" } ]
    run_as = "human"

Rules are not a way around policy. Every action a rule takes goes through the
same pipeline as an agent's, as its own principal:

  run_as = "rule"   (default) the action runs as "rule:<id>" with the rule's tier
                    ceiling (`max_tier`, T1 unless raised, never above
                    policy.rules_max_tier). It yields to people, leases and the
                    emergency stop, is rate limited, and is audited.
  run_as = "human"  for gestures only: the press is a person's, so the action runs
                    as that person on a channel of the stated `assurance`.

Agents can propose rules (MCP `propose_rule`) so a repeated request becomes a
rule instead of a model call every time. A proposal is never active until a
person approves it, it can never do more than the proposing agent could, and its
ceiling is the tier that was shown to the person who approved it. Rules written
in the config file are the administrator's and need no approval.

Triggers   gesture | state | signal | presence
Conditions when = { time = "22:00-07:00", occupied = "{room}", empty = "office",
                    state = [ { thing = "...", attr = "on", equals = true } ] }
Actions    do = [ { thing = <selector>, command = "...", args = { ... } } ]
           {room}, {person} and {thing} are filled in from the trigger.

There is no schedule trigger on purpose: a systemd timer that runs
`ofab emit <signal>` does it without a clock loop in the daemon.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import catalog
from .model import (
    KIND_HUMAN,
    KIND_RULE,
    Assurance,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    Principal,
    Tier,
)
from .util import clean_text, glob, match_thing, parse_hhmm, slug, valid_name

if TYPE_CHECKING:
    from .fabric import Fabric

log = logging.getLogger("omarchy_fabric.rules")

RULE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
SIGNAL_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
MAX_ACTIONS = 8
MAX_RUNTIME_RULES = 64
MAX_TARGETS = 32
NOTIFY = ("low", "normal", "critical")
_PLACEHOLDER = re.compile(r"\{(room|person|thing)\}")
_UNSET: Any = object()

_RULE_KEYS = {"id", "on", "when", "do", "run_as", "assurance", "max_tier", "cooldown_s", "enabled", "note",
              "notify"}
_TRIGGER_KEYS = {
    "gesture": {"gesture", "thing"},
    "state": {"state", "thing", "to", "from", "above", "below", "origin"},
    "signal": {"signal", "where"},
    "presence": {"presence", "enters", "leaves"},
}
_WHEN_KEYS = {"time", "state", "occupied", "empty"}
_STATE_KEYS = {"thing", "attr", "equals", "above", "below"}
_ACTION_KEYS = {"thing", "command", "args"}


def _only(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")


def _scalar(value: Any, where: str) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"{where} must be a string, a number or true/false")


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number")
    return float(value)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    return a == b


def _crossed(new: Any, old: Any, threshold: float, upward: bool) -> bool:
    """A threshold trigger fires on the crossing, not on every reading past it."""
    if isinstance(new, bool) or not isinstance(new, (int, float)):
        return False
    was = float(old) if isinstance(old, (int, float)) and not isinstance(old, bool) else None
    if upward:
        return new > threshold and (was is None or was <= threshold)
    return new < threshold and (was is None or was >= threshold)


def _window(text: str, where: str) -> tuple[dt.time, dt.time]:
    start, sep, end = str(text).partition("-")
    if not sep:
        raise ValueError(f"{where} must look like 22:00-07:00")
    try:
        return parse_hhmm(start.strip()), parse_hhmm(end.strip())
    except ValueError:
        raise ValueError(f"{where} must look like 22:00-07:00") from None


def _in_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # crosses midnight


def _fill(text: str, values: dict[str, Any]) -> str | None:
    """Fill {room}, {person} and {thing}. None when the trigger did not provide one."""
    missing = False

    def swap(match: re.Match) -> str:
        nonlocal missing
        value = values.get(match.group(1))
        if value is None:
            missing = True
            return ""
        return str(value)

    out = _PLACEHOLDER.sub(swap, text)
    return None if missing else out


@dataclass
class Trigger:
    kind: str  # gesture | state | signal | presence
    thing: str = "*"  # selector, for gesture and state
    gesture: str = "*"
    attr: str = ""
    to: Any = _UNSET
    from_: Any = _UNSET
    above: float | None = None
    below: float | None = None
    origin: tuple[str, ...] = ()
    signal: str = ""
    where: dict[str, Any] = field(default_factory=dict)
    person: str = "*"
    enters: str = ""
    leaves: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "gesture":
            return {"gesture": self.gesture, "thing": self.thing}
        if self.kind == "state":
            out: dict[str, Any] = {"state": self.attr, "thing": self.thing}
            if self.to is not _UNSET:
                out["to"] = self.to
            if self.from_ is not _UNSET:
                out["from"] = self.from_
            for key in ("above", "below"):
                if getattr(self, key) is not None:
                    out[key] = getattr(self, key)
            if self.origin:
                out["origin"] = list(self.origin)
            return out
        if self.kind == "signal":
            return {"signal": self.signal, **({"where": dict(self.where)} if self.where else {})}
        return {"presence": self.person, **({"enters": self.enters} if self.enters else {}),
                **({"leaves": self.leaves} if self.leaves else {})}

    def describe(self) -> str:
        if self.kind == "gesture":
            return f"{self.gesture} press on {self.thing}"
        if self.kind == "state":
            parts = [f"{self.thing} {self.attr}"]
            if self.to is not _UNSET:
                parts.append(f"becomes {self.to}")
            if self.above is not None:
                parts.append(f"rises above {self.above:g}")
            if self.below is not None:
                parts.append(f"falls below {self.below:g}")
            return " ".join(parts) if len(parts) > 1 else f"{parts[0]} changes"
        if self.kind == "signal":
            where = ", ".join(f"{k}={v}" for k, v in self.where.items())
            return f"signal {self.signal}" + (f" ({where})" if where else "")
        who = "someone" if self.person == "*" else self.person
        if self.enters:
            return f"{who} enters {self.enters}"
        if self.leaves:
            return f"{who} leaves {self.leaves}"
        return f"{who} changes room"


@dataclass
class StateCondition:
    thing: str
    attr: str
    equals: Any = _UNSET
    above: float | None = None
    below: float | None = None

    def holds(self, value: Any) -> bool:
        if self.equals is not _UNSET and not _same(value, self.equals):
            return False
        number = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if self.above is not None and (number is None or number <= self.above):
            return False
        if self.below is not None and (number is None or number >= self.below):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"thing": self.thing, "attr": self.attr}
        if self.equals is not _UNSET:
            out["equals"] = self.equals
        for key in ("above", "below"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


@dataclass
class Conditions:
    time: tuple[dt.time, dt.time] | None = None
    states: tuple[StateCondition, ...] = ()
    occupied: str = ""
    empty: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.time:
            out["time"] = f"{self.time[0]:%H:%M}-{self.time[1]:%H:%M}"
        if self.states:
            out["state"] = [s.to_dict() for s in self.states]
        if self.occupied:
            out["occupied"] = self.occupied
        if self.empty:
            out["empty"] = self.empty
        return out


@dataclass
class Action:
    thing: str
    command: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"thing": self.thing, "command": self.command, **({"args": dict(self.args)} if self.args else {})}

    def describe(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.command} {self.thing}" + (f" ({args})" if args else "")


@dataclass
class Rule:
    id: str
    trigger: Trigger
    actions: tuple[Action, ...]
    when: Conditions = field(default_factory=Conditions)
    run_as: str = "rule"
    assurance: Assurance = Assurance.CLI  # run_as = "human": what the gesture proves
    max_tier: Tier = Tier.T1
    cooldown_s: float = 1.0
    enabled: bool = True
    note: str = ""
    notify: str = ""  # tell the consoles when it fires: low | normal | critical
    source: str = "config"  # config | runtime
    author: str | None = None  # the agent that proposed it
    approved_by: str | None = None

    def describe(self) -> str:
        actions = "; ".join(a.describe() for a in self.actions) or "tell the consoles"
        return clean_text(f"when {self.trigger.describe()}: {actions}", 200)

    def to_dict(self) -> dict[str, Any]:
        """The rule as it is written in a config file; parse_rule reads it back."""
        out: dict[str, Any] = {"id": self.id, "on": self.trigger.to_dict()}
        when = self.when.to_dict()
        if when:
            out["when"] = when
        out["do"] = [a.to_dict() for a in self.actions]
        if self.run_as != "rule":
            out["run_as"] = self.run_as
            out["assurance"] = str(self.assurance)
        out["max_tier"] = str(self.max_tier)
        out["cooldown_s"] = self.cooldown_s
        if not self.enabled:
            out["enabled"] = False
        if self.note:
            out["note"] = self.note
        if self.notify:
            out["notify"] = self.notify
        return out

    def view(self) -> dict[str, Any]:
        out = {"id": self.id, "does": self.describe(), "enabled": self.enabled, "source": self.source,
               "run_as": self.run_as, "max_tier": str(self.max_tier), "rule": self.to_dict()}
        if self.author:
            out["proposed_by"] = self.author
        if self.approved_by:
            out["approved_by"] = self.approved_by
        return out


def _parse_trigger(data: Any, where: str) -> Trigger:
    if not isinstance(data, dict):
        raise ValueError(f"{where}: `on` must be a table such as {{ gesture = \"double\", thing = \"gpio:desk\" }}")
    kinds = [kind for kind in _TRIGGER_KEYS if kind in data]
    if len(kinds) != 1:
        raise ValueError(f"{where}: `on` needs exactly one of gesture, state, signal or presence")
    kind = kinds[0]
    _only(data, _TRIGGER_KEYS[kind], f"{where}: on")
    if kind == "gesture":
        return Trigger("gesture", thing=str(data.get("thing", "*")),
                       gesture=catalog.canonical_gesture(data["gesture"]) if data["gesture"] != "*" else "*")
    if kind == "state":
        attr = str(data["state"])
        if not attr:
            raise ValueError(f"{where}: on.state names the attribute to watch, e.g. \"leak\"")
        trigger = Trigger("state", thing=str(data.get("thing", "*")), attr=attr)
        if "to" in data:
            trigger.to = _scalar(data["to"], f"{where}: on.to")
        if "from" in data:
            trigger.from_ = _scalar(data["from"], f"{where}: on.from")
        if "above" in data:
            trigger.above = _number(data["above"], f"{where}: on.above")
        if "below" in data:
            trigger.below = _number(data["below"], f"{where}: on.below")
        origin = data.get("origin", ())
        trigger.origin = (origin,) if isinstance(origin, str) else tuple(str(o) for o in origin)
        return trigger
    if kind == "signal":
        name = str(data["signal"])
        if not name:
            raise ValueError(f"{where}: on.signal needs a name such as desktop.screenshare")
        where_table = data.get("where", {})
        if not isinstance(where_table, dict):
            raise ValueError(f"{where}: on.where must be a table")
        return Trigger("signal", signal=name,
                       where={str(k): _scalar(v, f"{where}: on.where.{k}") for k, v in where_table.items()})
    person = str(data["presence"])
    if person != "*" and not valid_name(person):
        raise ValueError(f"{where}: on.presence must be a person's name or \"*\"")
    if "enters" in data and "leaves" in data:
        raise ValueError(f"{where}: on.presence takes enters or leaves, not both")
    return Trigger("presence", person=person, enters=str(data.get("enters", "")), leaves=str(data.get("leaves", "")))


def _parse_when(data: Any, where: str) -> Conditions:
    if data in (None, {}):
        return Conditions()
    if not isinstance(data, dict):
        raise ValueError(f"{where}: `when` must be a table")
    _only(data, _WHEN_KEYS, f"{where}: when")
    states = []
    entries = data.get("state", [])
    for i, entry in enumerate([entries] if isinstance(entries, dict) else entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: when.state entries must be tables")
        _only(entry, _STATE_KEYS, f"{where}: when.state[{i}]")
        if not entry.get("thing") or not entry.get("attr"):
            raise ValueError(f"{where}: when.state[{i}] needs thing and attr")
        condition = StateCondition(str(entry["thing"]), str(entry["attr"]))
        if "equals" in entry:
            condition.equals = _scalar(entry["equals"], f"{where}: when.state[{i}].equals")
        if "above" in entry:
            condition.above = _number(entry["above"], f"{where}: when.state[{i}].above")
        if "below" in entry:
            condition.below = _number(entry["below"], f"{where}: when.state[{i}].below")
        states.append(condition)
    return Conditions(
        time=_window(data["time"], f"{where}: when.time") if "time" in data else None,
        states=tuple(states), occupied=str(data.get("occupied", "")), empty=str(data.get("empty", "")),
    )


def _parse_actions(data: Any, where: str) -> tuple[Action, ...]:
    entries = [data] if isinstance(data, dict) else list(data or [])
    if len(entries) > MAX_ACTIONS:
        raise ValueError(f"{where}: at most {MAX_ACTIONS} actions")
    actions = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: do[{i}] must be a table with thing and command")
        _only(entry, _ACTION_KEYS, f"{where}: do[{i}]")
        thing, command = str(entry.get("thing", "")), str(entry.get("command", ""))
        if not thing or not command:
            raise ValueError(f"{where}: do[{i}] needs thing and command")
        if not catalog.knows_command(command):
            raise ValueError(f"{where}: do[{i}]: no capability has a command {command!r}")
        args = entry.get("args", {})
        if not isinstance(args, dict):
            raise ValueError(f"{where}: do[{i}].args must be a table")
        actions.append(Action(thing, command, {str(k): _scalar(v, f"{where}: do[{i}].args.{k}")
                                               for k, v in args.items()}))
    return tuple(actions)


def parse_rule(data: Any, source: str = "config", author: str | None = None) -> Rule:
    """Build a rule from a config table or a proposal. Raises ValueError saying what is wrong."""
    if not isinstance(data, dict):
        raise ValueError("a rule must be a table")
    rule_id = str(data.get("id", ""))
    if not RULE_ID.fullmatch(rule_id):
        raise ValueError(f"rule id {rule_id!r} must be lowercase letters, digits, '-' or '_' (at most 48)")
    where = f"rule {rule_id}"
    _only(data, _RULE_KEYS, where)
    trigger = _parse_trigger(data.get("on"), where)
    actions = _parse_actions(data.get("do"), where)
    notify = str(data.get("notify", ""))
    if notify and notify not in NOTIFY:
        raise ValueError(f"{where}: notify must be one of {', '.join(NOTIFY)}")
    if not actions and not notify:
        raise ValueError(f"{where}: needs at least one action in `do`, or `notify`")
    run_as = str(data.get("run_as", "rule"))
    if run_as not in ("rule", "human"):
        raise ValueError(f"{where}: run_as must be \"rule\" or \"human\"")
    if run_as == "human" and trigger.kind != "gesture":
        raise ValueError(f"{where}: only a gesture can act as a person (run_as = \"human\")")
    try:
        assurance = Assurance.parse(data.get("assurance", "cli"))
        max_tier = Tier.parse(data.get("max_tier", "T1"))
    except (KeyError, ValueError):
        raise ValueError(f"{where}: assurance is cli, desktop or presence; max_tier is T0 to T3") from None
    cooldown = _number(data.get("cooldown_s", 1.0), f"{where}: cooldown_s")
    if cooldown < 0:
        raise ValueError(f"{where}: cooldown_s cannot be negative")
    return Rule(
        id=rule_id, trigger=trigger, actions=actions, when=_parse_when(data.get("when"), where),
        run_as=run_as, assurance=assurance, max_tier=max_tier, cooldown_s=cooldown,
        enabled=bool(data.get("enabled", True)), note=clean_text(data.get("note", ""), 160),
        notify=notify, source=source, author=author,
    )


class RuleEngine:
    def __init__(self, fabric: "Fabric", rules: list[Rule] | None = None, store: Path | None = None):
        self.fabric = fabric
        self.rules: dict[str, Rule] = {}
        for rule in rules or []:
            if rule.id in self.rules:
                raise ValueError(f"two rules are named {rule.id!r}")
            self.rules[rule.id] = rule
        self.store = store
        self.fired: dict[str, int] = {}
        self._last_fired: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._listening = False

    # ================================================================ lifecycle
    def start(self) -> None:
        self._load()
        if not self._listening:
            self.fabric.registry.listeners.append(self._on_event)
            self._listening = True

    def stop(self) -> None:
        if self._listening and self._on_event in self.fabric.registry.listeners:
            self.fabric.registry.listeners.remove(self._on_event)
        self._listening = False
        for task in list(self._tasks):
            task.cancel()

    # ================================================================ store
    def _load(self) -> None:
        if self.store is None or not self.store.exists():
            return
        try:
            entries = json.loads(self.store.read_text(encoding="utf-8")).get("rules", [])
        except (OSError, ValueError, AttributeError) as exc:
            log.error("cannot read %s: %s", self.store, exc)
            self.fabric.audit.append("rule_store_unreadable", error=clean_text(exc, 200))
            return
        for entry in entries:
            try:
                rule = parse_rule(entry["rule"], source="runtime", author=entry.get("author"))
            except (KeyError, TypeError, ValueError) as exc:
                log.error("skipping a stored rule: %s", exc)
                continue
            if rule.id in self.rules:  # the config file wins
                continue
            rule.enabled = bool(entry.get("enabled", True))
            rule.approved_by = entry.get("approved_by")
            self.rules[rule.id] = rule

    def _save(self) -> None:
        if self.store is None:
            return
        entries = [{"rule": {**r.to_dict(), "enabled": True}, "enabled": r.enabled, "author": r.author,
                    "approved_by": r.approved_by}
                   for r in self.rules.values() if r.source == "runtime"]
        self.store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"rules": entries}, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.store)

    # ================================================================ management
    def list(self) -> list[dict[str, Any]]:
        out = []
        for rule in sorted(self.rules.values(), key=lambda r: r.id):
            view = rule.view()
            view["fired"] = self.fired.get(rule.id, 0)
            out.append(view)
        return out

    def get(self, rule_id: str) -> Rule:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise NotFound(f"no rule {rule_id!r}")
        return rule

    def add(self, data: Any, by: str, author: str | None = None) -> Rule:
        try:
            rule = parse_rule(data, source="runtime", author=author)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        if rule.id in self.rules:
            raise InvalidRequest(f"a rule named {rule.id!r} already exists")
        if sum(1 for r in self.rules.values() if r.source == "runtime") >= MAX_RUNTIME_RULES:
            raise InvalidRequest(f"there are already {MAX_RUNTIME_RULES} stored rules; remove one first")
        rule.approved_by = by
        self.rules[rule.id] = rule
        self._save()
        self.fabric.audit.append("rule_added", rule=rule.id, by=by, author=author, does=rule.describe(),
                                 max_tier=str(rule.max_tier), run_as=rule.run_as)
        self.fabric.registry.emit({"type": "rule", "rule": rule.id, "status": "added"})
        return rule

    def remove(self, rule_id: str, by: str) -> dict[str, Any]:
        rule = self.get(rule_id)
        if rule.source != "runtime":
            raise InvalidRequest(f"rule {rule_id!r} is written in the config file; remove it there")
        del self.rules[rule_id]
        self._save()
        self.fabric.audit.append("rule_removed", rule=rule_id, by=by)
        self.fabric.registry.emit({"type": "rule", "rule": rule_id, "status": "removed"})
        return {"removed": rule_id}

    def set_enabled(self, rule_id: str, enabled: bool, by: str) -> dict[str, Any]:
        rule = self.get(rule_id)
        rule.enabled = enabled
        if rule.source == "runtime":
            self._save()
        self.fabric.audit.append("rule_enabled" if enabled else "rule_disabled", rule=rule_id, by=by)
        self.fabric.registry.emit({"type": "rule", "rule": rule_id, "status": "enabled" if enabled else "disabled"})
        return {"rule": rule_id, "enabled": enabled,
                **({"note": "a config rule is back to what the file says on restart"}
                   if rule.source != "runtime" else {})}

    # ================================================================ proposals
    def required_tier(self, rule: Rule, agent: str) -> Tier:
        """The riskiest thing the rule would do to the things it matches today. Raises
        if it reaches for something the proposing agent may not control."""
        policy = self.fabric.config.policy
        tier = Tier.T0
        for action in rule.actions:
            pattern = _PLACEHOLDER.sub("*", action.thing)
            matched = False
            for thing in self.fabric.registry.things.values():
                if not thing.expose or not match_thing(pattern, thing):
                    continue
                try:
                    cap_id, cmd = catalog.resolve_command(thing, action.command)
                except InvalidRequest:
                    continue
                if not policy.can_control(agent, thing):
                    raise PermissionDenied(f"rule {rule.id} would act on {thing.id}, which you may not control")
                matched = True
                tier = max(tier, catalog.effective_tier(thing, cap_id, cmd, action.args)[0])
            if not matched:
                tier = max(tier, catalog.default_tier(action.command))
        return tier

    def propose(self, agent: str, data: Any, reason: str) -> dict[str, Any]:
        """An agent asks for a rule. Nothing runs until a person approves it."""
        policy = self.fabric.config.policy
        try:
            rule = parse_rule(data, source="runtime", author=agent)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        if rule.run_as != "rule":
            raise PermissionDenied("an agent cannot propose a rule that acts as a person (run_as = \"human\")")
        if rule.id in self.rules:
            raise InvalidRequest(f"a rule named {rule.id!r} already exists")
        agent_policy = policy.agent(agent)
        if agent_policy is None:
            raise PermissionDenied(f"agent {agent!r} has no policy")
        needed = self.required_tier(rule, agent)
        ceiling = min(agent_policy.max_tier, policy.rules_max_tier)
        if needed > ceiling:
            raise PermissionDenied(f"rule {rule.id} needs {needed}; rules you propose are limited to {ceiling}")
        rule.max_tier = needed  # the ceiling is exactly what the person approving is shown
        body = rule.to_dict()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]
        requires = max(Assurance.DESKTOP, policy.tier_assurance.get(needed, Assurance.PRESENCE))
        return self.fabric.request_rule(agent, rule, body, {"digest": digest}, reason, needed, requires)

    def activate(self, payload: dict[str, Any], by: str, author: str) -> Rule:
        return self.add(payload, by=by, author=author)

    # ================================================================ matching
    def _values_for(self, thing, **extra: Any) -> dict[str, Any]:
        """{room}, {person}, {thing} for a trigger that came from a thing. A thing
        tagged "wearer:<name>" is carried around: its room is where that person is."""
        presence = self.fabric.presence
        wearer = next((tag[7:] for tag in sorted(thing.tags) if tag.startswith("wearer:")), None)
        if wearer:
            return {"thing": thing.id, "person": wearer, "room": presence.room_of(wearer), **extra}
        return {"thing": thing.id, "person": presence.person(), "room": slug(thing.area) or None, **extra}

    def _match(self, rule: Rule, event: dict[str, Any]) -> dict[str, Any] | None:
        trigger = rule.trigger
        registry = self.fabric.registry
        if trigger.kind == "gesture":
            thing = registry.get(event.get("thing", ""))
            if thing is None or not match_thing(trigger.thing, thing):
                return None
            if not glob(trigger.gesture, str(event.get("gesture", ""))):
                return None
            return self._values_for(thing, what=f"{event.get('gesture')} press on {thing.id}")
        if trigger.kind == "state":
            if event.get("attr") != trigger.attr or event.get("by") == f"rule:{rule.id}":
                return None  # its own doing must not set it off again
            thing = registry.get(event.get("thing", ""))
            if thing is None or not match_thing(trigger.thing, thing):
                return None
            new, old = event.get("new"), event.get("old")
            if trigger.to is not _UNSET and not _same(new, trigger.to):
                return None
            if trigger.from_ is not _UNSET and not _same(old, trigger.from_):
                return None
            if trigger.above is not None and not _crossed(new, old, trigger.above, upward=True):
                return None
            if trigger.below is not None and not _crossed(new, old, trigger.below, upward=False):
                return None
            if trigger.origin and event.get("origin") not in trigger.origin:
                return None
            return self._values_for(thing, what=f"{thing.id} {trigger.attr} became {new}")
        if trigger.kind == "signal":
            name = str(event.get("name", ""))
            if not glob(trigger.signal, name):
                return None
            data = event.get("data") or {}
            if any(not _same(data.get(key), want) for key, want in trigger.where.items()):
                return None
            presence = self.fabric.presence
            room = event.get("room") or data.get("room")
            return {"thing": None, "person": presence.person(),
                    "room": slug(room) if room else presence.room_of(), "what": f"signal {name}"}
        if trigger.kind == "presence":
            person = event.get("person")
            if trigger.person != "*" and person != trigger.person:
                return None
            room, old = event.get("room"), event.get("old_room")
            if trigger.enters:
                if room is None or not glob(slug(trigger.enters, keep="*?[]"), room):
                    return None
                return {"thing": None, "person": person, "room": room, "what": f"{person} entered {room}"}
            if trigger.leaves:
                if old is None or not glob(slug(trigger.leaves, keep="*?[]"), old):
                    return None
                return {"thing": None, "person": person, "room": old, "what": f"{person} left {old}"}
            return {"thing": None, "person": person, "room": room, "what": f"{person} moved to {room}"}
        return None

    def _blocked(self, rule: Rule, values: dict[str, Any]) -> str | None:
        """Why the conditions do not hold right now, or None when they do."""
        when = rule.when
        if when.time and not _in_window(self.fabric.clock.local().time(), *when.time):
            return "outside its time window"
        for condition in when.states:
            thing_id = _fill(condition.thing, values)
            value = self.fabric.registry.values(thing_id).get(condition.attr) if thing_id else None
            if not condition.holds(value):
                return f"{condition.thing} {condition.attr} is {value!r}"
        if when.occupied:
            room = _fill(when.occupied, values)
            if not room or self.fabric.presence.occupied(room) is not True:
                return f"{when.occupied} is not known to be occupied"
        if when.empty:
            room = _fill(when.empty, values)
            if not room or self.fabric.presence.occupied(room) is True:
                return f"{when.empty} is occupied"
        return None

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind not in _TRIGGER_KEYS:
            return
        for rule in list(self.rules.values()):
            if not rule.enabled or rule.trigger.kind != kind:
                continue
            values = self._match(rule, event)
            if values is None:
                continue
            now = self.fabric.clock.now()
            last = self._last_fired.get(rule.id)
            if last is not None and now - last < rule.cooldown_s:
                continue
            if self._blocked(rule, values) is not None:
                continue
            self._last_fired[rule.id] = now
            try:
                task = asyncio.get_running_loop().create_task(self._run(rule, values))
            except RuntimeError:  # no event loop: nothing can execute here
                continue
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ================================================================ firing
    def _principal(self, rule: Rule, values: dict[str, Any]) -> Principal:
        if rule.run_as == "human":
            person = values.get("person")
            return Principal(KIND_HUMAN, person if valid_name(person) else "someone", assurance=rule.assurance)
        return Principal(KIND_RULE, rule.id, max_tier=rule.max_tier, author=rule.author)

    async def _run(self, rule: Rule, values: dict[str, Any]) -> None:
        who = self._principal(rule, values)
        what = str(values.get("what", ""))
        counts = {"executed": 0, "pending_approval": 0, "denied": 0, "failed": 0, "skipped": 0}
        things: list[str] = []
        try:
            for action in rule.actions:
                selector = _fill(action.thing, values)
                if selector is None:  # e.g. {room} for a button nobody has placed in a room
                    counts["skipped"] += 1
                    continue
                args = {}
                for key, value in action.args.items():
                    args[key] = _fill(value, values) if isinstance(value, str) else value
                if any(value is None for value in args.values()):
                    counts["skipped"] += 1
                    continue
                outcome = await self.fabric.invoke_group(
                    who, selector, action.command, args, reason=f"rule {rule.id}" + (f": {rule.note}" if rule.note else ""),
                    cause=f"rule {rule.id}: {what}", limit=MAX_TARGETS)
                if not outcome["results"]:
                    counts["skipped"] += 1
                for result in outcome["results"]:
                    status = result.get("status", "failed")
                    counts[status if status in counts else "failed"] += 1
                    if status == "executed":
                        things.append(result["thing"])
        except asyncio.CancelledError:
            raise
        except Exception:  # one broken rule must never take the engine down
            log.exception("rule %s failed", rule.id)
            counts["failed"] += 1
        self.fired[rule.id] = self.fired.get(rule.id, 0) + 1
        summary = {k: v for k, v in counts.items() if v}
        self.fabric.audit.append("rule_fired", rule=rule.id, trigger=clean_text(what, 160), principal=who.id,
                                 **summary)
        event: dict[str, Any] = {"type": "rule", "rule": rule.id, "status": "fired",
                                 "trigger": clean_text(what, 160), **summary}
        if things:
            event["things"] = things[:8]
        if rule.notify:
            event["notify"] = rule.notify
            event["does"] = rule.describe()
        self.fabric.registry.emit(event)
