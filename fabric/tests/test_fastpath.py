"""Round 2: people, rules and rooms. Scenario ids match docs/04-scenarios.md
(P: a person acting through the fabric, F: the fast path, R: rooms and presence)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from omarchy_fabric.api import Session, dispatch, end_session
from omarchy_fabric.config import parse
from omarchy_fabric.fabric import Fabric
from omarchy_fabric.model import (
    KIND_HUMAN,
    Assurance,
    InvalidRequest,
    InvokeRequest,
    PermissionDenied,
    Principal,
    Thing,
)

from .fakes import HA_TOKEN, FakeHomeAssistant

DESKTOP = Assurance.DESKTOP
ME = Principal(KIND_HUMAN, "desktop", assurance=DESKTOP)
CONSOLE = Session("console", "desktop", DESKTOP)

BUTTON = {"id": "office-button", "on": {"gesture": "double", "thing": "sim:office.button"},
          "do": [{"thing": "area:{room}&kind:light", "command": "toggle"}], "run_as": "human"}
LEAK = {"id": "leak-shutoff", "on": {"state": "leak", "thing": "kind:sensor", "to": True},
        "do": [{"thing": "kind:water_main", "command": "close"}], "notify": "critical"}


async def by_hand(home, thing: str, command: str, **args) -> dict:
    return await home.fabric.invoke(InvokeRequest("", f"sim:{thing}", command, args, "by hand", principal=ME))


async def fired(home, rule_id: str, times: int = 1) -> None:
    ok = await home.fabric.registry.wait_for(lambda: home.fabric.rules.fired.get(rule_id, 0) >= times, 2)
    assert ok, f"rule {rule_id} fired {home.fabric.rules.fired.get(rule_id, 0)} time(s), expected {times}"


async def settle() -> None:
    """Let a rule that should NOT fire have its chance to."""
    for _ in range(5):
        await asyncio.sleep(0)


# ================================================================ P: a person acting through the fabric
async def test_P01_a_person_is_not_held_back_by_the_guards_meant_for_agents(home):
    home.sim.external("living_room.lamp", on=True)  # they just used the wall switch: agents must yield...
    assert (await home.do("claude", "living_room.lamp", "set_level", level=10))["code"] == "human_hold"
    assert (await by_hand(home, "living_room.lamp", "set_level", level=10))["status"] == "executed"  # ...they need not
    home.clock.set_local_time(23, 30)  # quiet hours are about agents acting at night
    assert (await by_hand(home, "office.desk_light", "turn_on"))["status"] == "executed"
    home.fabric.freeze("gpio:kill", "kill switch")  # and the emergency stop never locks people out
    assert (await by_hand(home, "living_room.lamp", "turn_off"))["status"] == "executed"
    assert (await by_hand(home, "office.blinds", "close"))["status"] == "executed"
    for _ in range(12):  # no rate limit either
        assert (await by_hand(home, "office.desk_light", "toggle"))["status"] == "executed"
    assert home.fabric.audit.tail(1)[0]["agent"] == "human:desktop"


async def test_P02_a_persons_channel_counts_toward_the_approval_a_tier_needs(home):
    kettle = await by_hand(home, "kitchen.kettle", "turn_on")  # T2 needs desktop; they are at the desktop
    assert kettle["status"] == "executed" and kettle["code"] == "operator"
    unlock = await by_hand(home, "entrance.front_door", "unlock")  # T3 needs the button, whoever asks
    assert unlock["status"] == "pending_approval" and unlock["requires"] == "presence"
    with pytest.raises(PermissionDenied):
        home.fabric.resolve_approval(unlock["approval_id"], True, "console:desktop", DESKTOP)
    home.fabric.presence_press("gpio:approve")
    assert (await home.fabric.approval_status(unlock["approval_id"], wait_s=1))["status"] == "executed"


async def test_P03_limits_that_protect_the_home_apply_to_everyone(home):
    assert (await by_hand(home, "living_room.thermostat", "set_target_temperature", temperature=30))["code"] == "out_of_bounds"
    home.sim.set_offline("office.blinds")
    assert (await by_hand(home, "office.blinds", "close"))["code"] == "offline"


async def test_P03_a_deny_rule_can_cover_people_too(make_home):
    h = await make_home({"policy": {"rules": [{"thing": "kind:garage_door", "command": "open", "effect": "deny",
                                               "note": "the garage only opens by hand"}]}})
    assert (await by_hand(h, "garage.door", "open"))["status"] == "denied"


async def test_P04_what_a_person_does_through_the_fabric_is_still_a_persons_doing(home):
    await by_hand(home, "living_room.lamp", "turn_on")
    assert home.fabric.registry.state["sim:living_room.lamp"]["on"].origin == "human"
    blocked = await home.do("claude", "living_room.lamp", "set_level", level=5, reason="save energy")
    assert blocked["code"] == "human_hold"  # exactly as after a wall switch


async def test_P05_only_a_console_can_act_as_a_person_and_only_when_it_says_so(home):
    kettle = {"thing": "sim:kitchen.kettle", "command": "turn_on"}
    with pytest.raises(PermissionDenied):
        await dispatch(home.fabric, Session("agent", "claude"), "invoke", {**kettle, "operator": True})
    with pytest.raises(PermissionDenied):
        await dispatch(home.fabric, Session("agent", "claude"), "group", {"selector": "*", "command": "turn_off"})
    scripted = await dispatch(home.fabric, CONSOLE, "invoke", kettle)  # a script or an agent shelling out to ofab
    assert scripted["code"] == "tier_exceeds_agent"
    assert (await dispatch(home.fabric, CONSOLE, "invoke", {**kettle, "operator": True}))["status"] == "executed"


async def test_P06_toggle_a_room_from_the_desktop(home):
    office = {"selector": "area:office&kind:light", "command": "toggle"}
    out = await dispatch(home.fabric, CONSOLE, "group", office)
    assert out["command"] == "on_off.turn_off" and [r["status"] for r in out["results"]] == ["executed"]
    assert home.value("office.desk_light", "on") is False
    await dispatch(home.fabric, CONSOLE, "group", office)
    assert home.value("office.desk_light", "on") is True
    nothing = await dispatch(home.fabric, CONSOLE, "group", {"selector": "area:attic", "command": "toggle"})
    assert nothing["matched"] == 0 and nothing["results"] == []


# ================================================================ F: the fast path
async def test_F01_a_button_press_acts_without_a_model(make_home):
    h = await make_home({"rules": [BUTTON]})
    h.sim.gesture("office.button", "double")
    await fired(h, "office-button")
    assert h.value("office.desk_light", "on") is False  # it was on: the room's lights go off together
    assert h.fabric.registry.state["sim:office.desk_light"]["on"].origin == "human"
    h.sim.gesture("office.button", "single")  # a different gesture is not this rule's
    h.sim.gesture("office.button", "double")  # and a bouncing button is ignored for a second
    await settle()
    assert h.fabric.rules.fired["office-button"] == 1
    h.clock.advance(2)
    h.sim.gesture("office.button", "multi_press_2")  # what Matter calls a double press
    await fired(h, "office-button", 2)
    assert h.value("office.desk_light", "on") is True
    fired_record = [r for r in h.fabric.audit.tail(20) if r["kind"] == "rule_fired"][-1]
    assert fired_record["rule"] == "office-button" and fired_record["executed"] == 1


async def test_F02_a_gesture_is_a_persons_so_it_works_through_a_freeze(make_home):
    h = await make_home({"rules": [BUTTON]})
    h.fabric.freeze("gpio:kill", "kill switch")
    h.sim.gesture("office.button", "double")
    await fired(h, "office-button")
    assert h.value("office.desk_light", "on") is False


async def test_F03_a_leak_closes_the_water_main(make_home):
    h = await make_home({"rules": [LEAK]})
    h.sim.sensor("utility.leak", leak=True)
    await fired(h, "leak-shutoff")
    assert h.value("utility.water_main", "open") is False
    executed = [r for r in h.fabric.audit.tail(20) if r["kind"] == "executed"][-1]
    assert executed["agent"] == "rule:leak-shutoff" and "leak became True" in executed["cause"]
    event = [e for e in h.fabric.recent_events() if e["type"] == "rule"][-1]
    assert event["notify"] == "critical" and event["things"] == ["sim:utility.water_main"]
    h.fabric.freeze("console:desktop")
    h.sim.sensor("utility.leak", leak=False)
    reopen = await by_hand(h, "utility.water_main", "open")  # T3: even its owner needs the button
    h.fabric.presence_press("gpio:approve")
    assert (await h.fabric.approval_status(reopen["approval_id"], wait_s=1))["status"] == "executed"
    h.clock.advance(5)
    h.sim.sensor("utility.leak", leak=True)  # closing a valve is safer: the rule still protects a frozen home
    await fired(h, "leak-shutoff", 2)
    assert h.value("utility.water_main", "open") is False


async def test_F04_a_rule_cannot_do_more_than_it_was_approved_for(make_home):
    kettle = {"id": "morning-kettle", "on": {"signal": "alarm.rang"},
              "do": [{"thing": "sim:kitchen.kettle", "command": "turn_on"}]}
    h = await make_home({"rules": [kettle]})  # max_tier defaults to T1; the kettle is T2
    h.fabric.signal("alarm.rang", {}, "console:desktop")
    await fired(h, "morning-kettle")
    assert h.value("kitchen.kettle", "on") is False
    denied = [r for r in h.fabric.audit.tail(20) if r["kind"] == "denied"][-1]
    assert denied["code"] == "tier_exceeds_rule" and denied["agent"] == "rule:morning-kettle"

    h2 = await make_home({"rules": [{**kettle, "max_tier": "T2"}]})  # written into the config on purpose
    h2.fabric.signal("alarm.rang", {}, "console:desktop")
    await fired(h2, "morning-kettle")
    assert h2.value("kitchen.kettle", "on") is True and h2.fabric.approvals.pending() == []


async def test_F05_rules_yield_to_people_and_to_the_emergency_stop(make_home):
    lamp_on = {"id": "dusk", "on": {"signal": "sun.set"}, "do": [{"thing": "sim:living_room.lamp", "command": "turn_on"}]}
    h = await make_home({"rules": [lamp_on]})
    h.sim.external("living_room.lamp", on=True)
    h.sim.external("living_room.lamp", on=False)  # someone wants it off
    h.fabric.signal("sun.set", {}, "console:desktop")
    await fired(h, "dusk")
    assert h.value("living_room.lamp", "on") is False
    assert [r for r in h.fabric.audit.tail(20) if r["kind"] == "denied"][-1]["code"] == "human_hold"
    h.clock.advance(601)
    h.fabric.freeze("console:desktop")
    h.fabric.signal("sun.set", {}, "console:desktop")
    await fired(h, "dusk", 2)
    assert h.value("living_room.lamp", "on") is False
    assert [r for r in h.fabric.audit.tail(20) if r["kind"] == "denied"][-1]["code"] == "frozen"


async def test_F06_a_desktop_signal_dims_the_office_within_working_hours(make_home):
    dim = {"id": "screenshare", "on": {"signal": "desktop.screenshare", "where": {"state": "on"}},
           "when": {"time": "08:00-22:00"},
           "do": [{"thing": "sim:office.desk_light", "command": "set_level", "args": {"level": 20}}]}
    h = await make_home({"rules": [dim]})
    with pytest.raises(PermissionDenied):  # a signal can set off a rule, so agents cannot send one
        await dispatch(h.fabric, Session("agent", "claude"), "emit", {"signal": "desktop.screenshare"})
    await dispatch(h.fabric, CONSOLE, "emit", {"signal": "desktop.screenshare", "data": {"state": "off"}})
    await settle()
    assert h.value("office.desk_light", "level") == 80
    await dispatch(h.fabric, CONSOLE, "emit", {"signal": "desktop.screenshare", "data": {"state": "on"}})
    await fired(h, "screenshare")
    assert h.value("office.desk_light", "level") == 20
    h.clock.set_local_time(23, 0)
    await by_hand(h, "office.desk_light", "set_level", level=80)
    await dispatch(h.fabric, CONSOLE, "emit", {"signal": "desktop.screenshare", "data": {"state": "on"}})
    await settle()
    assert h.value("office.desk_light", "level") == 80 and h.fabric.rules.fired["screenshare"] == 1
    assert not any(e["type"] == "signal" for e in h.fabric.recent_events(viewer="claude"))
    with pytest.raises(InvalidRequest):
        h.fabric.signal("Bad Name!", {}, "console:desktop")


async def test_F07_a_rule_does_not_set_itself_off(make_home):
    echo = {"id": "cap-level", "on": {"state": "level", "thing": "sim:living_room.lamp"}, "cooldown_s": 0,
            "do": [{"thing": "sim:living_room.lamp", "command": "set_level", "args": {"level": 30}}]}
    h = await make_home({"rules": [echo]})
    h.clock.advance(700)
    await h.do("claude", "living_room.lamp", "set_level", level=80)  # an agent sets it; the rule caps it
    await fired(h, "cap-level")
    await settle()
    assert h.value("living_room.lamp", "level") == 30 and h.fabric.rules.fired["cap-level"] == 1


async def test_F08_a_threshold_fires_on_the_crossing_and_can_just_tell_you(make_home):
    hot = {"id": "too-warm", "on": {"state": "temperature", "thing": "sim:living_room.climate", "above": 25},
           "notify": "normal"}
    h = await make_home({"rules": [hot]})
    h.sim.sensor("living_room.climate", temperature=24.0)
    await settle()
    assert "too-warm" not in h.fabric.rules.fired
    h.sim.sensor("living_room.climate", temperature=26.0)
    await fired(h, "too-warm")
    h.clock.advance(5)
    h.sim.sensor("living_room.climate", temperature=27.5)  # still above: nothing new to say
    await settle()
    assert h.fabric.rules.fired["too-warm"] == 1
    h.sim.sensor("living_room.climate", temperature=22.0)
    h.sim.sensor("living_room.climate", temperature=25.5)
    await fired(h, "too-warm", 2)


async def test_F09_an_agent_proposes_a_rule_and_a_person_approves_it(make_home, tmp_path):
    h = await make_home({"fabric": {"state_dir": str(tmp_path)}})
    proposal = {"id": "welcome-light", "on": {"presence": "*", "enters": "living_room"},
                "do": [{"thing": "sim:living_room.lamp", "command": "turn_on"}]}
    asked = await dispatch(h.fabric, Session("agent", "claude"), "rule_propose",
                           {"rule": proposal, "reason": "you ask for this every evening"})
    assert asked["status"] == "pending_approval" and asked["requires"] == "desktop" and asked["tier"] == "T1"
    assert "welcome-light" not in h.fabric.rules.rules  # nothing runs on an agent's say-so
    h.fabric.presence.report("derek", "living_room", "test")
    await settle()
    assert h.value("living_room.lamp", "on") is False
    h.fabric.presence.clear("derek")
    again = await dispatch(h.fabric, Session("agent", "claude"), "rule_propose", {"rule": proposal, "reason": "?"})
    assert again["approval_id"] == asked["approval_id"]
    card = h.fabric.approvals.get(asked["approval_id"])
    assert card.kind == "rule" and "ADD RULE 'welcome-light'" in card.summary and "turn_on" in card.detail
    h.fabric.resolve_approval(card.id, True, "console:desktop", DESKTOP)
    done = await h.fabric.approval_status(card.id, wait_s=1)
    assert done["status"] == "executed"
    rule = h.fabric.rules.get("welcome-light")
    assert rule.author == "claude" and rule.source == "runtime" and str(rule.max_tier) == "T1"
    h.fabric.presence.report("derek", "living_room", "test")
    await fired(h, "welcome-light")
    assert h.value("living_room.lamp", "on") is True

    restarted = Fabric(h.fabric.config, clock=h.clock)  # the rule is kept across a restart
    await restarted.start()
    try:
        kept = restarted.rules.get("welcome-light")
        assert kept.author == "claude" and kept.approved_by == "console:desktop"
        assert (await dispatch(restarted, CONSOLE, "rule_remove", {"id": "welcome-light"})) == {"removed": "welcome-light"}
    finally:
        await restarted.stop()


async def test_F10_a_proposal_cannot_reach_past_its_author(make_home):
    h = await make_home({"agents": {"clerk": {"max_tier": "T2", "things": ["sim:office.desk_light"]}}})

    async def propose(agent: str, rule: dict) -> dict:
        return await dispatch(h.fabric, Session("agent", agent), "rule_propose", {"rule": rule, "reason": "test"})

    base = {"id": "r", "on": {"signal": "go"}}
    with pytest.raises(PermissionDenied) as err:  # T3 is beyond what any proposed rule may hold
        await propose("claude", {**base, "do": [{"thing": "sim:entrance.front_door", "command": "unlock"}]})
    assert "T3" in str(err.value)
    with pytest.raises(PermissionDenied):  # beyond the proposing agent's own ceiling
        await propose("intern", {**base, "do": [{"thing": "sim:kitchen.kettle", "command": "turn_on"}]})
    with pytest.raises(PermissionDenied):  # a thing it may not control
        await propose("clerk", {**base, "do": [{"thing": "area:office", "command": "turn_off"}]})
    with pytest.raises(PermissionDenied):  # only a person can make a gesture act as a person
        await propose("claude", {"id": "r", "on": {"gesture": "double", "thing": "sim:office.button"},
                                 "run_as": "human", "do": [{"thing": "sim:office.desk_light", "command": "toggle"}]})
    with pytest.raises(InvalidRequest):
        await propose("claude", {**base, "do": [{"thing": "sim:office.desk_light", "command": "levitate"}]})
    assert h.fabric.approvals.pending() == []


async def test_F10_a_proposed_rule_keeps_its_authors_limits_when_it_fires(make_home):
    h = await make_home({"agents": {"clerk": {"max_tier": "T1", "things": ["sim:office.desk_light"]}}})
    rule = {"id": "office-off", "on": {"signal": "leaving"}, "do": [{"thing": "area:office&kind:light", "command": "turn_off"}]}
    asked = await dispatch(h.fabric, Session("agent", "clerk"), "rule_propose", {"rule": rule, "reason": "tidy"})
    h.fabric.resolve_approval(asked["approval_id"], True, "console:desktop", DESKTOP)
    await h.fabric.approval_status(asked["approval_id"], wait_s=1)
    # A second light appears in the office later. The clerk could never touch it; neither can its rule.
    h.fabric.add_thing(Thing("sim:office.spot", "Spot", "sim", "office.spot", ("on_off",), area="office", kind="light"),
                       state={"on": True})
    h.fabric.signal("leaving", {}, "console:desktop")
    await fired(h, "office-off")
    assert h.value("office.desk_light", "on") is False and h.value("office.spot", "on") is True
    assert [r for r in h.fabric.audit.tail(20) if r["kind"] == "denied"][-1]["code"] == "not_permitted"


async def test_F11_rules_are_managed_from_a_console(home):
    agent = Session("agent", "claude")
    with pytest.raises(PermissionDenied):
        await dispatch(home.fabric, agent, "rule_add", {"rule": LEAK})
    added = await dispatch(home.fabric, CONSOLE, "rule_add", {"rule": LEAK})
    assert added["id"] == "leak-shutoff" and added["source"] == "runtime"
    listed = (await dispatch(home.fabric, agent, "rules", {}))["rules"]
    assert [r["id"] for r in listed] == ["leak-shutoff"] and "close kind:water_main" in listed[0]["does"]
    await dispatch(home.fabric, CONSOLE, "rule_enable", {"id": "leak-shutoff", "enabled": False})
    home.sim.sensor("utility.leak", leak=True)
    await settle()
    assert home.value("utility.water_main", "open") is True
    with pytest.raises(InvalidRequest):
        await dispatch(home.fabric, CONSOLE, "rule_add", {"rule": LEAK})  # the name is taken


async def test_F12_a_home_assistant_button_sets_off_a_rule():
    async with FakeHomeAssistant() as ha:
        ha.states["event.hall_button"] = {
            "entity_id": "event.hall_button", "state": "unknown",
            "attributes": {"friendly_name": "Hall button", "device_class": "button",
                           "event_types": ["single_press", "double_press"]},
            "context": {"id": "x", "user_id": None, "parent_id": None}}
        cfg = parse({"fabric": {"confirm_timeout_s": 1}, "audit": {"enabled": False},
                     "adapters": [{"type": "homeassistant", "id": "ha", "url": ha.url, "token": HA_TOKEN}],
                     "agents": {"claude": {"max_tier": "T3"}},
                     "rules": [{"id": "hall", "on": {"gesture": "double", "thing": "ha:event.hall_button"},
                                "do": [{"thing": "ha:light.living_lamp", "command": "turn_on"}], "run_as": "human"}]})
        fabric = Fabric(cfg)
        await fabric.start()
        try:
            assert await fabric.registry.wait_for(lambda: fabric.adapter_health.get("ha", {}).get("connected"), 5)
            button = {t["id"]: t for t in fabric.list_things()["things"]}["ha:event.hall_button"]
            assert button["kind"] == "button" and button["capabilities"] == ["button"]
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            await ha.broadcast_change("event.hall_button", yesterday, {"event_type": "double_press"})
            await asyncio.sleep(0.2)  # a timestamp restored after a restart is not a press
            assert not fabric.rules.fired
            await ha.broadcast_change("event.hall_button", datetime.now(timezone.utc).isoformat(),
                                      {"event_type": "double_press"})
            assert await fabric.registry.wait_for(lambda: fabric.registry.values("ha:light.living_lamp").get("on"), 3)
            assert fabric.registry.state["ha:light.living_lamp"]["on"].origin == "human"
        finally:
            await fabric.stop()


async def test_F13_a_gpio_button_tells_single_double_and_long_apart(make_home):
    h = await make_home({"adapters": [
        {"type": "simulator", "id": "sim", "tick_s": 0},
        {"type": "gpio", "id": "gpio", "pin_factory": "mock", "devices": [
            {"id": "desk", "kind": "button", "pin": 5, "area": "office", "double_s": 0.15, "hold_s": 0.25}]}]})
    seen: list[str] = []
    h.fabric.registry.listeners.append(lambda e: seen.append(e["gesture"]) if e["type"] == "gesture" else None)
    pin = h.fabric.adapters["gpio"].devices["desk"].pin

    async def press(hold: float = 0.02) -> None:
        pin.drive_low()
        await asyncio.sleep(hold)
        pin.drive_high()
        await asyncio.sleep(0.03)

    await press()
    await asyncio.sleep(0.3)
    await press()
    await press()
    await asyncio.sleep(0.3)
    await press(hold=0.45)
    await asyncio.sleep(0.3)
    assert seen == ["single", "double", "long"]
    assert {t["id"]: t for t in h.fabric.list_things()["things"]}["gpio:desk"]["kind"] == "button"


# ================================================================ R: rooms and presence
async def test_R01_the_fabric_knows_which_room_a_person_is_in(make_home):
    h = await make_home({"presence": {"default_person": "derek"}})
    assert h.fabric.where()["room"] is None
    h.fabric.presence.report("derek", "Office", "desktop:laptop")
    where = h.fabric.where()
    assert where["person"] == "derek" and where["room"] == "office"
    assert "sim:office.desk_light" in {t["id"] for t in where["things"]}
    rooms = {r["room"]: r for r in h.fabric.rooms()["rooms"]}
    assert rooms["office"]["people"] == ["derek"] and rooms["office"]["occupied"] is True
    assert rooms["hallway"]["occupied"] is False and rooms["kitchen"]["occupied"] is None  # a sensor / nothing to tell
    h.sim.sensor("hallway.motion", occupied=True)
    assert h.fabric.presence.occupied("hallway") is True
    h.fabric.presence.report("derek", "kitchen", "phone", ttl_s=60)  # the newest sighting wins
    assert h.fabric.where()["room"] == "kitchen"
    h.clock.advance(61)
    h.fabric.presence.expire_due()
    assert h.fabric.where()["room"] == "office"
    moves = [(e["old_room"], e["room"]) for e in h.fabric.recent_events() if e["type"] == "presence"]
    assert moves == [(None, "office"), ("office", "kitchen"), ("kitchen", "office")]


async def test_R02_where_people_are_is_shown_only_to_agents_allowed_to_know(home):
    home.fabric.presence.report("derek", "entrance", "badge")
    assert home.fabric.where("derek", viewer="claude")["room"] == "entrance"
    with pytest.raises(PermissionDenied):
        home.fabric.where("derek", viewer="painter")  # its policy says presence = false
    for room in home.fabric.rooms(viewer="painter")["rooms"]:
        assert "people" not in room and "occupied" not in room
    assert not any(e["type"] == "presence" for e in home.fabric.recent_events(viewer="painter"))
    assert any(e["type"] == "presence" for e in home.fabric.recent_events(viewer="claude"))
    hidden = home.fabric.where("derek", viewer="intern")  # may know the room, sees nothing in it
    assert hidden["room"] == "entrance" and hidden["things"] == []


async def test_R03_a_desktop_vouches_for_its_room_while_it_is_connected(make_home):
    h = await make_home({"presence": {"default_person": "derek"}})
    desk = Session("console", "desktop", DESKTOP)
    with pytest.raises(PermissionDenied):
        await dispatch(h.fabric, Session("agent", "claude"), "presence_report", {"room": "office"})
    reported = await dispatch(h.fabric, desk, "presence_report", {"room": "office", "source": "desktop:laptop",
                                                                 "bind": True})
    assert reported == {"person": "derek", "room": "office", "source": "desktop:laptop"}
    emitted = await dispatch(h.fabric, desk, "emit", {"signal": "desktop.focus"})  # signals carry the desk's room
    assert [e for e in h.fabric.recent_events() if e["type"] == "signal"][-1]["room"] == "office" and emitted
    end_session(h.fabric, desk)  # the laptop sleeps, or the session ends
    assert h.fabric.where()["room"] is None


async def test_R04_lights_follow_a_person_into_a_room(make_home):
    follow = {"id": "follow-me", "on": {"presence": "derek", "enters": "*"}, "when": {"time": "06:00-23:00"},
              "do": [{"thing": "area:{room}&kind:light", "command": "turn_on"}]}
    leave = {"id": "leave-off", "on": {"presence": "derek", "leaves": "living_room"},
             "do": [{"thing": "area:{room}&kind:light", "command": "turn_off"}]}
    h = await make_home({"rules": [follow, leave]})
    h.fabric.presence.report("derek", "living_room", "anchor")
    await fired(h, "follow-me")
    assert h.value("living_room.lamp", "on") is True
    h.clock.advance(5)
    h.fabric.presence.report("derek", "office", "anchor")
    await fired(h, "leave-off")
    assert h.value("living_room.lamp", "on") is False


async def test_R05_a_wearable_acts_on_the_room_its_wearer_is_in(make_home):
    h = await make_home({"rules": [BUTTON], "things": {"sim:office.button": {"tags": ["wearer:derek"]}}})
    h.sim.gesture("office.button", "double")  # nobody knows where derek is: nothing to act on
    await fired(h, "office-button")
    assert h.value("office.desk_light", "on") is True and h.value("living_room.lamp", "on") is False
    h.clock.advance(2)
    h.fabric.presence.report("derek", "living_room", "anchor")
    h.sim.gesture("office.button", "double")
    await fired(h, "office-button", 2)
    assert h.value("living_room.lamp", "on") is True and h.value("office.desk_light", "on") is True
    assert h.fabric.audit.tail(1)[0]["principal"] == "human:derek"


async def test_R06_agents_get_rooms_and_rules_over_mcp(make_home):
    from mcp import Client

    from omarchy_fabric.mcp_server import LocalBackend, build_server

    h = await make_home({"presence": {"default_person": "derek"}, "rules": [LEAK]})
    h.fabric.presence.report("derek", "office", "desktop:laptop")
    async with Client(build_server(LocalBackend(h.fabric, "claude"))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {"list_rooms", "where_is", "list_rules", "propose_rule"} <= set(tools)
        assert tools["where_is"].annotations.read_only_hint is True
        assert not {"approve", "thaw", "emit", "rule_add"} & set(tools)
        where = (await client.call_tool("where_is", {})).structured_content
        assert where["room"] == "office"
        rules = (await client.call_tool("list_rules", {})).structured_content["rules"]
        assert rules[0]["id"] == "leak-shutoff"
        asked = (await client.call_tool("propose_rule", {"reason": "asked twice today", "rule": {
            "id": "desk-dim", "on": {"signal": "desktop.screenshare", "where": {"state": "on"}},
            "do": [{"thing": "area:office&kind:light", "command": "set_level", "args": {"level": 20}}]}})).structured_content
        assert asked["status"] == "pending_approval" and asked["tier"] == "T1"
        refused = (await client.call_tool("propose_rule", {"reason": "x", "rule": {
            "id": "let-me-in", "on": {"presence": "*", "enters": "entrance"},
            "do": [{"thing": "kind:lock", "command": "unlock"}]}})).structured_content
        assert refused["error"]["code"] == "forbidden"
