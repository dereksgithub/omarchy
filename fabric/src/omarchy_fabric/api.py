"""One method table for every transport (unix socket, TCP, SSH stdio, embedded MCP).

Roles: agent < console < admin.
  agent    observe (what its policy lets it see), invoke (through policy), simulate,
           leases, its own approvals, propose a rule, emergency stop
  console  + list / approve / deny approvals, thaw, audit, act as the person at the
           console, rules, signals, presence reports, simulator controls
  admin    + Matter commissioning
An agent can always make things safer (freeze) but never approve its own requests.

A console session is where a person sits, so it may act as one: `invoke` with
operator = true, or `group`. Without that flag a console's invoke is treated like
an agent's, which keeps a script or an agent that merely shells out to `ofab do`
under the same policy as every other agent.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .model import (
    KIND_AGENT,
    KIND_HUMAN,
    Assurance,
    InvalidRequest,
    InvokeRequest,
    NotFound,
    PermissionDenied,
    Principal,
)
from .util import clean_text, valid_name

ROLES = {"agent": 0, "console": 1, "admin": 2}
_SESSION_IDS = itertools.count(1)


@dataclass
class Session:
    role: str
    agent: str
    max_assurance: Assurance = Assurance.NONE  # strongest approval this session can give
    transport: str = "local"
    sid: int = field(default_factory=lambda: next(_SESSION_IDS))

    def at_least(self, role: str) -> bool:
        return ROLES[self.role] >= ROLES[role]

    @property
    def who(self) -> str:
        return f"{self.role}:{self.agent}"

    @property
    def viewer(self) -> str | None:
        """Whose eyes a read is for: an agent's (scoped by its policy) or a console's (None)."""
        return None if self.at_least("console") else self.agent


Handler = Callable[[Any, Session, dict[str, Any]], Awaitable[Any]]
METHODS: dict[str, tuple[str, Handler]] = {}


def method(name: str, role: str = "agent"):
    def register(fn: Handler) -> Handler:
        METHODS[name] = (role, fn)
        return fn
    return register


async def dispatch(fabric, session: Session, name: str, params: dict[str, Any] | None) -> Any:
    entry = METHODS.get(name)
    if entry is None:
        raise InvalidRequest(f"unknown method {name!r}")
    role, handler = entry
    if not session.at_least(role):
        raise PermissionDenied(f"{name} needs the {role} role; this session is {session.role}")
    if params is not None and not isinstance(params, dict):
        raise InvalidRequest("params must be an object")
    try:
        return await handler(fabric, session, dict(params or {}))
    except KeyError as exc:
        raise InvalidRequest(f"missing parameter {exc.args[0]!r}") from None


def _num(p: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = p.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidRequest(f"{key} must be a number")
    try:
        number = float(value)
    except ValueError:
        raise InvalidRequest(f"{key} must be a number, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidRequest(f"{key} must be a finite number")
    return number


def end_session(fabric, session: Session) -> None:
    """A connection closed: whatever it vouched for while open is withdrawn."""
    fabric.presence.clear(session=session.sid)


def _hidden_ok(s: Session) -> bool:
    return s.at_least("console")


def _principal(s: Session, p: dict[str, Any]) -> Principal:
    if p.get("operator"):
        if not s.at_least("console"):
            raise PermissionDenied("only a console can act as the person at it (operator)")
        return Principal(KIND_HUMAN, s.agent, assurance=s.max_assurance)
    return Principal(KIND_AGENT, s.agent)


def _args(p: dict[str, Any]) -> dict[str, Any]:
    args = p.get("args") or {}
    if not isinstance(args, dict):
        raise InvalidRequest("args must be an object, e.g. {\"level\": 40}")
    return args


@method("status")
async def _status(f, s, p):
    return f.status()


@method("things")
async def _things(f, s, p):
    return f.list_things(p.get("area"), p.get("capability"), p.get("query"), include_hidden=_hidden_ok(s),
                         viewer=s.viewer)


@method("state")
async def _state(f, s, p):
    return f.get_state(p["thing"], include_hidden=_hidden_ok(s), viewer=s.viewer)


@method("describe")
async def _describe(f, s, p):
    return f.describe(s.agent, p["thing"], include_hidden=_hidden_ok(s), viewer=s.viewer)


def _request(s: Session, p: dict[str, Any], dry_run: bool = False) -> InvokeRequest:
    return InvokeRequest(agent=s.agent, thing_id=p["thing"], command=p["command"], args=_args(p),
                         reason=str(p.get("reason", "")), dry_run=dry_run, wait_s=_num(p, "wait_s"),
                         principal=_principal(s, p))


@method("invoke")
async def _invoke(f, s, p):
    return await f.invoke(_request(s, p))


@method("simulate")
async def _simulate(f, s, p):
    return await f.invoke(_request(s, p, dry_run=True))


@method("approval_status")
async def _approval_status(f, s, p):
    return await f.approval_status(str(p["id"]), agent=s.viewer, wait_s=_num(p, "wait_s"))


@method("events")
async def _events(f, s, p):
    return {"events": f.recent_events(int(_num(p, "limit", 50)), p.get("thing"), int(_num(p, "since_seq")),
                                      include_hidden=_hidden_ok(s), viewer=s.viewer)}


@method("rooms")
async def _rooms(f, s, p):
    return f.rooms(viewer=s.viewer)


@method("where")
async def _where(f, s, p):
    return f.where(p.get("person"), viewer=s.viewer)


@method("lease")
async def _lease(f, s, p):
    return f.acquire_lease(s.agent, p["thing"], _num(p, "ttl_s", 300), str(p.get("purpose", "")))


@method("release")
async def _release(f, s, p):
    return f.release_lease(s.agent, p["thing"])


@method("freeze")
async def _freeze(f, s, p):
    return f.freeze(s.who, str(p.get("reason", "")))


@method("rules")
async def _rules(f, s, p):
    return {"rules": f.rules.list()}


@method("rule_propose")
async def _rule_propose(f, s, p):
    return f.rules.propose(s.agent, p["rule"], str(p.get("reason", "")))


# ---------------------------------------------------------------- console
@method("group", "console")
async def _group(f, s, p):
    """The person at this console acts on everything a selector matches."""
    return await f.invoke_group(Principal(KIND_HUMAN, s.agent, assurance=s.max_assurance), str(p["selector"]),
                                str(p["command"]), _args(p), reason=str(p.get("reason", "")))


@method("emit", "console")
async def _emit(f, s, p):
    data = p.get("data") or {}
    if not isinstance(data, dict):
        raise InvalidRequest("data must be an object")
    return f.signal(p["signal"], data, s.who, p.get("room") or f.presence.room_for_session(s.sid))


@method("presence", "console")
async def _presence(f, s, p):
    return {"people": f.presence.people(), **f.rooms()}


@method("presence_report", "console")
async def _presence_report(f, s, p):
    """Say where a person is. bind = true ties it to this connection: it is
    withdrawn when the connection closes (the desktop slept, the session ended)."""
    person = p.get("person") or f.presence.default_person
    if not person:
        raise InvalidRequest("name a person, or set presence.default_person in the fabric's config")
    return f.presence.report(str(person), str(p["room"]), str(p.get("source") or s.who),
                             None if p.get("ttl_s") is None else _num(p, "ttl_s"), s.sid if p.get("bind") else None)


@method("presence_clear", "console")
async def _presence_clear(f, s, p):
    if p.get("person") is None and p.get("source") is None:
        return {"cleared": f.presence.clear(session=s.sid)}
    return {"cleared": f.presence.clear(p.get("person"), p.get("source"))}


@method("rule_add", "console")
async def _rule_add(f, s, p):
    return f.rules.add(p["rule"], by=s.who).view()


@method("rule_remove", "console")
async def _rule_remove(f, s, p):
    return f.rules.remove(str(p["id"]), by=s.who)


@method("rule_enable", "console")
async def _rule_enable(f, s, p):
    return f.rules.set_enabled(str(p["id"]), bool(p.get("enabled", True)), by=s.who)


@method("approvals", "console")
async def _approvals(f, s, p):
    return {"approvals": f.list_approvals(bool(p.get("recent", False)))}


@method("approve", "console")
async def _approve(f, s, p):
    return f.resolve_approval(p["id"], True, s.who, s.max_assurance, str(p.get("note", "")))


@method("deny", "console")
async def _deny(f, s, p):
    return f.resolve_approval(p["id"], False, s.who, s.max_assurance, str(p.get("note", "")))


@method("thaw", "console")
async def _thaw(f, s, p):
    return f.thaw(s.who)


@method("lift_suspension", "console")
async def _lift(f, s, p):
    f.rates.lift(p["agent"])
    f.audit.append("suspension_lifted", agent=clean_text(p["agent"]), by=s.who)
    return {"lifted": p["agent"]}


@method("audit_tail", "console")
async def _audit_tail(f, s, p):
    return {"records": f.audit.tail(int(_num(p, "n", 20)))}


@method("audit_verify", "console")
async def _audit_verify(f, s, p):
    return f.audit.verify().to_dict()


@method("audit_check", "console")
async def _audit_check(f, s, p):
    """Does the log still contain the record a console saw earlier? See audit.witness()."""
    return f.audit.witness(int(_num(p, "seq")), str(p["head"]))


@method("sim", "console")
async def _sim(f, s, p):
    """Scenario controls for simulator adapters: play the human, break things, advance time."""
    adapter = f.adapters.get(p.get("adapter", "sim"))
    if adapter is None or adapter.type != "simulator":
        raise NotFound("no simulator adapter (sim controls only work on simulated things)")
    action = p["action"]
    native = str(p.get("thing", ""))
    if native.startswith(f"{adapter.id}:"):
        native = native.split(":", 1)[1]
    attrs = p.get("attrs") or {}
    if action in ("human", "external", "sensor"):
        if native not in adapter.state:
            raise NotFound(f"simulator has no {native!r}")
        getattr(adapter, action)(native, **attrs)
    elif action == "gesture":
        if native not in adapter.state:
            raise NotFound(f"simulator has no {native!r}")
        adapter.gesture(native, str(p.get("gesture") or "single"))
    elif action in ("offline", "online"):
        adapter.set_offline(native, action == "offline")
    elif action == "fault":
        adapter.inject_fault(native, str(p.get("fault", "error")))
    elif action == "step":
        adapter.step(_num(p, "seconds", 1))
    else:
        raise InvalidRequest(f"unknown sim action {action!r}")
    return {"ok": True, "action": action, "thing": native}


@method("matter_commission", "admin")
async def _commission(f, s, p):
    adapter = f.adapters.get(p.get("adapter", "matter"))
    if adapter is None or adapter.type != "matter":
        raise NotFound("no matter adapter configured")
    result = await adapter.commission(str(p["code"]), bool(p.get("network_only", False)))
    f.audit.append("matter_commission", by=s.who, result=result)
    return result


__all__ = ["METHODS", "ROLES", "Session", "dispatch", "end_session", "valid_name"]
