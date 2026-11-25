"""Rooms and presence: which rooms are in use and where each person is.

The fabric does not locate anyone itself. It keeps a small model that sources
report into, and answers two questions from it: "which room is this person in?"
and "is anyone in this room?".

Sources, weakest claim last:

  sightings   "derek is in the office", reported by something that knows: an
              Omarchy desktop whose owner is at it (bound to the console's
              connection, so it ends when the desktop sleeps or logs out), a BLE
              anchor on a room terminal, a Home Assistant room-presence sensor.
  occupancy   motion / mmWave sensors: someone is in the room, nobody knows who.

A room is a thing's `area`, compared by slug ("Living Room" == living_room).
Where a person is counts as personal data: agents see it only when their policy
says so (`[agents.x] presence`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import InvalidRequest
from .registry import Registry
from .util import Clock, slug, valid_name


@dataclass
class Sighting:
    person: str
    room: str
    source: str
    since: float
    expires_at: float | None = None  # None: until the source clears it or its session ends
    session: int | None = None
    order: int = 0  # reports in the same instant: the later one is the newer

    @property
    def newness(self) -> tuple[float, int]:
        return (self.since, self.order)

    def live(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now


class Presence:
    def __init__(self, clock: Clock, registry: Registry, default_person: str | None = None):
        self.clock = clock
        self.registry = registry
        self.default_person = default_person
        self._sightings: dict[tuple[str, str], Sighting] = {}  # (person, source) -> sighting
        self._last_room: dict[str, str | None] = {}  # what we last told listeners
        self._reports = 0

    # ------------------------------------------------------------ sources
    def report(self, person: str, room: str, source: str, ttl_s: float | None = None,
               session: int | None = None) -> dict[str, Any]:
        if not valid_name(person):
            raise InvalidRequest("person must be a short name: letters, digits, '.', '_' or '-'")
        room_id = slug(room)
        if not room_id:
            raise InvalidRequest("room must be the name of a room, e.g. office")
        now = self.clock.now()
        if ttl_s is not None and ttl_s <= 0:
            raise InvalidRequest("ttl_s must be positive")
        source = slug(source, keep=":.-") or "unknown"
        self._reports += 1
        self._sightings[(person, source)] = Sighting(
            person, room_id, source, now, None if ttl_s is None else now + ttl_s, session, self._reports)
        self._settle(person, source)
        return {"person": person, "room": self.room_of(person), "source": source}

    def clear(self, person: str | None = None, source: str | None = None, session: int | None = None) -> int:
        """Withdraw sightings: by source, by person, or everything a closed session reported."""
        source = slug(source, keep=":.-") if source else None
        gone = [key for key, s in self._sightings.items()
                if (person is None or s.person == person)
                and (source is None or s.source == source)
                and (session is None or s.session == session)]
        for key in gone:
            del self._sightings[key]
        for who in sorted({key[0] for key in gone}):
            self._settle(who, source or "cleared")
        return len(gone)

    def expire_due(self) -> None:
        now = self.clock.now()
        gone = [key for key, s in self._sightings.items() if not s.live(now)]
        for key in gone:
            del self._sightings[key]
        for who in sorted({key[0] for key in gone}):
            self._settle(who, "expired")

    def _settle(self, person: str, source: str) -> None:
        """Tell listeners when the room a person is in has changed."""
        room = self.room_of(person)
        old = self._last_room.get(person)
        if room == old:
            return
        if room is None:
            self._last_room.pop(person, None)
        else:
            self._last_room[person] = room
        self.registry.emit({"type": "presence", "person": person, "room": room, "old_room": old,
                            "source": source})

    # ------------------------------------------------------------ answers
    def _live(self) -> list[Sighting]:
        now = self.clock.now()
        return [s for s in self._sightings.values() if s.live(now)]

    def person(self, name: str | None = None) -> str | None:
        """The person meant when none is named: the configured default, or the only one known."""
        if name:
            return name
        if self.default_person:
            return self.default_person
        known = {s.person for s in self._live()}
        return next(iter(known)) if len(known) == 1 else None

    def room_of(self, person: str | None = None) -> str | None:
        """The room of the most recent sighting, or None when nobody has seen them."""
        who = self.person(person)
        if who is None:
            return None
        seen = [s for s in self._live() if s.person == who]
        return max(seen, key=lambda s: s.newness).room if seen else None

    def room_for_session(self, session: int) -> str | None:
        """The room a connection has vouched for, e.g. the room a desktop console is in."""
        seen = [s for s in self._live() if s.session == session]
        return max(seen, key=lambda s: s.newness).room if seen else None

    def people(self) -> dict[str, dict[str, Any]]:
        now = self.clock.now()
        out: dict[str, dict[str, Any]] = {}
        for s in sorted(self._live(), key=lambda s: s.newness):
            out[s.person] = {"room": s.room, "source": s.source, "for_s": round(now - s.since)}
        return out

    def people_in(self, room: str) -> list[str]:
        room_id = slug(room)
        return sorted(who for who in {s.person for s in self._live()} if self.room_of(who) == room_id)

    def occupied(self, room: str) -> bool | None:
        """True: a person or an occupancy sensor says so. False: a sensor says it is
        empty. None: nothing in that room can tell."""
        room_id = slug(room)
        if self.people_in(room_id):
            return True
        sensed = None
        for thing in self.registry.things.values():
            if "occupancy" not in thing.capabilities or slug(thing.area) != room_id:
                continue
            if not self.registry.online.get(thing.id):
                continue
            value = self.registry.values(thing.id).get("occupied")
            if value is True:
                return True
            if value is False:
                sensed = False
        return sensed

    def rooms(self, visible=None, with_people: bool = True) -> list[dict[str, Any]]:
        """Every room that has things (or a person) in it. `visible` filters the things
        that count, so an agent is not told about rooms it cannot see into."""
        rooms: dict[str, dict[str, Any]] = {}
        for thing in self.registry.things.values():
            room_id = slug(thing.area)
            if not room_id or not thing.expose or (visible is not None and not visible(thing)):
                continue
            entry = rooms.setdefault(room_id, {"room": room_id, "name": thing.area, "things": 0})
            entry["things"] += 1
        if with_people:
            for s in self._live():
                rooms.setdefault(s.room, {"room": s.room, "name": s.room, "things": 0})
        for room_id, entry in rooms.items():
            if with_people:
                entry["occupied"] = self.occupied(room_id)
                people = self.people_in(room_id)
                if people:
                    entry["people"] = people
        return [rooms[key] for key in sorted(rooms)]
