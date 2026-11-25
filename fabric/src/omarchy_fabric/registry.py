"""Thing registry: current state, availability, origin attribution, event fan-out."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from . import catalog
from .model import (
    ORIGIN_DEVICE,
    ORIGIN_EXTERNAL,
    ORIGIN_FABRIC,
    ORIGIN_INITIAL,
    AttrValue,
    Thing,
)
from .util import Clock

Listener = Callable[[dict], None]


@dataclass
class _Expected:
    deadline: float
    target: Any  # predicted value, or None for a side effect
    start: Any  # value before the command
    origin: str  # ORIGIN_FABRIC, or ORIGIN_HUMAN when a person acted through the fabric
    by: str  # principal id, so a rule can tell its own changes apart
    allowed: set | None = None  # values a side effect may take; None = any


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    return a == b


class Registry:
    def __init__(self, clock: Clock, history: int = 500):
        self.clock = clock
        self.things: dict[str, Thing] = {}
        self.state: dict[str, dict[str, AttrValue]] = {}
        self.online: dict[str, bool] = {}
        self.events: deque[dict] = deque(maxlen=history)
        self.listeners: list[Listener] = []
        # (thing, attr) -> what a command the fabric just executed is expected to change
        self._fabric_window: dict[tuple[str, str], _Expected] = {}
        self.last_human_change: dict[str, tuple[float, str, str]] = {}  # thing -> (ts, attr, origin)
        self._changed = asyncio.Event() if _has_loop() else None
        self._seq = 0

    # ------------------------------------------------------------ events
    def emit(self, event: dict) -> None:
        self._seq += 1
        event = {"seq": self._seq, "ts": self.clock.now(), **event}
        self.events.append(event)
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:  # a broken listener must never break the fabric
                pass
        self._notify()

    def _notify(self) -> None:
        if self._changed is None and _has_loop():
            self._changed = asyncio.Event()
        if self._changed is not None:
            self._changed.set()
            self._changed = asyncio.Event()

    async def wait_for(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Wait until predicate() is true or timeout passes (real time)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if self._changed is None:
                self._changed = asyncio.Event()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                return predicate()
        return True

    # ------------------------------------------------------------ things
    def upsert(self, thing: Thing, state: dict[str, Any] | None = None, online: bool = True) -> None:
        new = thing.id not in self.things
        self.things[thing.id] = thing
        self.state.setdefault(thing.id, {})
        now = self.clock.now()
        for attr, value in (state or {}).items():
            current = self.state[thing.id].get(attr)
            if current is None or not _same(current.value, value):
                self.state[thing.id][attr] = AttrValue(value, now, ORIGIN_INITIAL)
        was_online = self.online.get(thing.id)
        self.online[thing.id] = online
        if new:
            self.emit({"type": "thing_added", "thing": thing.id})
        elif was_online != online:  # a backend came back: consoles redraw on this event
            self.emit({"type": "availability", "thing": thing.id, "online": online})

    def remove(self, thing_id: str) -> None:
        if self.things.pop(thing_id, None) is not None:
            self.state.pop(thing_id, None)
            self.online.pop(thing_id, None)
            self.emit({"type": "thing_removed", "thing": thing_id})

    def get(self, thing_id: str) -> Thing | None:
        return self.things.get(thing_id)

    def set_online(self, thing_id: str, online: bool) -> None:
        if thing_id in self.things and self.online.get(thing_id) != online:
            self.online[thing_id] = online
            self.emit({"type": "availability", "thing": thing_id, "online": online})

    # ------------------------------------------------------------ state
    def values(self, thing_id: str) -> dict[str, Any]:
        return {k: v.value for k, v in self.state.get(thing_id, {}).items()}

    def expect(self, thing_id: str, attrs: set[str], window_s: float,
               predicted: dict[str, Any] | None = None, *, origin: str = ORIGIN_FABRIC, by: str = "",
               allowed: dict[str, set] | None = None) -> None:
        """Record what a command the fabric executes is expected to change. During the
        window, a change counts as that command's only if it reaches the predicted
        value or moves toward it; anything else (the person who flips the light
        straight back off) is external, so human hold still protects them.

        `origin` is who the command was for: "fabric" for an agent or a rule, "human"
        when a person acted through the fabric, whose change agents then yield to.
        `allowed` lists the values an unpredicted side effect may take."""
        deadline = self.clock.now() + window_s
        values = self.values(thing_id)
        predicted = predicted or {}
        for attr in attrs:
            self._fabric_window[(thing_id, attr)] = _Expected(
                deadline, predicted.get(attr), values.get(attr), origin, by, (allowed or {}).get(attr))

    def _expected(self, thing_id: str, attr: str, now: float) -> "_Expected | None":
        entry = self._fabric_window.get((thing_id, attr))
        if entry is not None and now > entry.deadline:
            del self._fabric_window[(thing_id, attr)]
            return None
        return entry

    def _attribute(self, thing_id: str, attr: str, value: Any, now: float) -> "_Expected | None":
        """The expectation this change fulfils, or None when it is not the command's doing."""
        entry = self._expected(thing_id, attr, now)
        if entry is None:
            return None
        target, start = entry.target, entry.start
        if target is None:  # affected but not predicted (level, power, cover_state...)
            return entry if entry.allowed is None or value in entry.allowed else None
        if _same(value, target) or (isinstance(target, (int, float)) and isinstance(value, (int, float))
                                    and not isinstance(target, bool) and abs(float(value) - float(target)) <= 1.0):
            del self._fabric_window[(thing_id, attr)]  # reached: later changes are someone else's
            return entry
        numeric = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (value, target, start))
        if numeric and min(start, target) <= value <= max(start, target):
            return entry  # in transit toward the target (a door opening, a fade)
        return None

    def update(self, thing_id: str, attrs: dict[str, Any], origin: str | None = None) -> list[dict]:
        """Apply attribute updates. `origin` comes from the adapter when it knows
        (HA context, simulator); otherwise changes inside a fabric command window
        count as fabric, and anything else on an actuated attribute is external."""
        thing = self.things.get(thing_id)
        if thing is None:
            return []
        now = self.clock.now()
        actuated = catalog.actuated_attributes(thing)
        changes = []
        current = self.state.setdefault(thing_id, {})
        for attr, value in attrs.items():
            prev = current.get(attr)
            if prev is not None and _same(prev.value, value):
                prev.updated_at = now  # still reporting: refresh freshness, no event
                continue
            who, by = origin, ""
            if who is None:
                expected = self._attribute(thing_id, attr, value, now)
                if expected is not None:
                    who, by = expected.origin, expected.by
                elif attr in actuated:
                    who = ORIGIN_EXTERNAL
                else:
                    who = ORIGIN_DEVICE
            elif who == ORIGIN_FABRIC:
                # The backend knows the change is ours (its own context id); the
                # expectation says on whose behalf, which may be a person's.
                expected = self._expected(thing_id, attr, now)
                if expected is not None:
                    who, by = expected.origin, expected.by
                    self._attribute(thing_id, attr, value, now)  # retires it once the target is reached
            current[attr] = AttrValue(value, now, who)
            change = {
                "type": "state",
                "thing": thing_id,
                "attr": attr,
                "old": None if prev is None else prev.value,
                "new": value,
                "origin": who,
            }
            if by:
                change["by"] = by
            if attr in actuated and who not in (ORIGIN_FABRIC, ORIGIN_DEVICE, ORIGIN_INITIAL):
                self.last_human_change[thing_id] = (now, attr, who)
            changes.append(change)
            self.emit(change)
        return changes


def _has_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
