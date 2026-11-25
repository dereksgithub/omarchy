"""Round 2: one test per defect the audit found in round 1 (docs/07-audit.md, B-numbers),
plus the scoping and transport hardening that came with fixing them."""

from __future__ import annotations

import asyncio
import json

import pytest

from omarchy_fabric import console as console_mod
from omarchy_fabric.api import Session, dispatch
from omarchy_fabric.approvals import KEEP_FINAL
from omarchy_fabric.audit import AuditLog, verify_file
from omarchy_fabric.config import RpcConfig, parse
from omarchy_fabric.fabric import Fabric
from omarchy_fabric.mcp_server import RemoteBackend
from omarchy_fabric.model import Assurance, InvalidRequest, InvokeRequest, NotFound, PermissionDenied, Thing
from omarchy_fabric.rpc import RpcClient, RpcError, RpcServer

from .conftest import BASE, merge

DESKTOP = Assurance.DESKTOP
DIMMER_PLUG = {"adapters": [{"type": "simulator", "id": "sim", "tick_s": 0, "things": [
    {"id": "den.dimmer", "name": "Den dimmer plug", "area": "den", "kind": "plug",
     "capabilities": ["on_off", "level"], "state": {"on": False, "level": 0}},
    {"id": "den.radiator", "name": "Den radiator", "area": "den", "kind": "heater",
     "capabilities": ["on_off", "level"], "state": {"on": True, "level": 60}}]}]}


# ---------------------------------------------------------------- B03 / B04: what an agent may see
async def test_B03_a_denied_thing_is_invisible_not_just_untouchable(home):
    ids = {t["id"] for t in home.fabric.list_things(viewer="intern")["things"]}
    assert "sim:entrance.front_door" not in ids and "sim:office.blinds" in ids
    with pytest.raises(NotFound):
        home.fabric.get_state("sim:entrance.front_door", viewer="intern")
    with pytest.raises(NotFound):
        home.fabric.describe("intern", "sim:entrance.front_door", viewer="intern")
    home.sim.external("entrance.front_door", lock_state="unlocked")
    seen = home.fabric.recent_events(viewer="intern")
    assert not any(e.get("thing", "").startswith("sim:entrance.") for e in seen)
    assert any(e.get("thing") == "sim:entrance.front_door" for e in home.fabric.recent_events())  # consoles do


async def test_B03_reaching_for_a_hidden_thing_is_recorded_and_counts(home):
    for _ in range(10):  # the answer is the same as for a thing that does not exist...
        with pytest.raises(NotFound):
            await home.do("intern", "entrance.front_door", "lock")
    last = [r for r in home.fabric.audit.tail(40) if r["kind"] == "denied"][-1]
    assert last["agent"] == "intern" and last["thing"] == "sim:entrance.front_door"  # ...but it is on record
    assert "intern" in home.fabric.status()["suspended_agents"]  # and the breaker tripped


async def test_B03_observe_narrows_what_an_agent_reads(make_home):
    h = await make_home({"agents": {"clerk": {"max_tier": "T1", "observe": ["area:office"]}}})
    ids = {t["id"] for t in h.fabric.list_things(viewer="clerk")["things"]}
    assert ids and all(i.startswith("sim:office.") for i in ids)
    with pytest.raises(NotFound):
        h.fabric.get_state("sim:kitchen.kettle", viewer="clerk")


async def test_B04_an_agent_gets_no_events_about_another_agents_approvals(home):
    pending = await home.do("claude", "kitchen.kettle", "turn_on", reason="tea")
    mine = [e for e in home.fabric.recent_events(viewer="claude") if e["type"] == "approval_requested"]
    theirs = [e for e in home.fabric.recent_events(viewer="codex") if e["type"] == "approval_requested"]
    assert [e["approval"]["id"] for e in mine] == [pending["approval_id"]] and theirs == []


# ---------------------------------------------------------------- B05: tiers that follow what a command does
async def test_B05_dimming_a_plug_is_not_cheaper_than_switching_it_on(make_home):
    h = await make_home(DIMMER_PLUG)
    dim = await h.do("claude", "den.dimmer", "set_level", level=40)
    assert dim["tier"] == "T2" and dim["status"] == "pending_approval"  # was T1: straight through
    described = {c["command"]: c for c in h.fabric.describe("claude", "sim:den.dimmer")["commands"]}
    assert described["level.set_level"]["tier"] == "T2"
    heat = await h.fabric.invoke(InvokeRequest("claude", "sim:den.radiator", "set_level", {"level": 80}, dry_run=True))
    assert heat["tier"] == "T3"
    off = await h.do("claude", "den.radiator", "set_level", level=0)  # turning a heater down to nothing is safer
    assert off["tier"] == "T1" and off.get("safer") and off["status"] == "executed"


async def test_B06_a_toggle_is_settled_before_anyone_approves_it(home):
    asked = await home.do("claude", "office.plug", "toggle", reason="save power")  # the plug is on
    assert asked["command"] == "on_off.turn_off" and asked["asked"] == "on_off.toggle"
    approval = home.fabric.approvals.get(asked["approval_id"])
    assert "TURN OFF" in approval.summary
    home.sim.sensor("office.plug", on=False)  # it switches itself off meanwhile
    home.fabric.resolve_approval(approval.id, True, "console:desktop", DESKTOP)
    await home.fabric.approval_status(approval.id, wait_s=1)
    assert home.value("office.plug", "on") is False  # a bare "toggle" would have switched it back on
    assert ("office.plug", "on_off", "turn_on", {}) not in home.sim.commands


# ---------------------------------------------------------------- B07 / B08: the approval queue
async def test_B07_asking_again_returns_the_waiting_approval(home):
    first = await home.do("claude", "kitchen.kettle", "turn_on", reason="tea")
    again = await home.do("claude", "kitchen.kettle", "turn_on", reason="tea, please")
    assert again["approval_id"] == first["approval_id"] and again["duplicate"] is True
    assert len(home.fabric.approvals.pending()) == 1
    other = await home.do("codex", "kitchen.kettle", "turn_on")  # someone else's request is its own
    assert other["approval_id"] != first["approval_id"]


async def test_B08_the_button_refuses_a_request_that_replaced_a_lapsed_one(home):
    unlock = await home.do("claude", "entrance.front_door", "unlock", reason="courier")
    home.clock.advance(121)  # nobody pressed in time
    assert (await home.fabric.approval_status(unlock["approval_id"]))["status"] == "expired"
    garage = await home.do("claude", "garage.door", "open", reason="courier")  # a different request takes its place
    pressed = home.fabric.presence_press("gpio:approve")  # the person arrives, thinking of the door
    assert pressed["result"] == "guarded" and home.value("garage.door", "position") == 0
    assert (await home.fabric.approval_status(garage["approval_id"]))["status"] == "pending_approval"
    home.fabric.resolve_approval(garage["approval_id"], False, "console:desktop", DESKTOP)
    retry = await home.do("claude", "entrance.front_door", "unlock", reason="courier")  # the same request again
    assert home.fabric.presence_press("gpio:approve")["result"] == "approved"
    assert (await home.fabric.approval_status(retry["approval_id"], wait_s=1))["status"] == "executed"


async def test_B08_the_guard_ends_by_itself(home):
    first = await home.do("claude", "entrance.front_door", "unlock")
    home.clock.advance(121)
    await home.fabric.approval_status(first["approval_id"])
    home.clock.advance(121)  # the guard lasts one approval timeout by default
    await home.do("claude", "garage.door", "open")
    assert home.fabric.presence_press("gpio:approve")["result"] == "approved"


async def test_B09_finished_approvals_do_not_pile_up(home):
    for _ in range(KEEP_FINAL + 30):
        approval = home.fabric.approvals.create(
            agent="claude", thing_id="sim:kitchen.kettle", thing_name="Kettle", capability="on_off",
            command="turn_on", args={}, reason="", tier=2, requires=DESKTOP, timeout_s=120)
        home.fabric.approvals.resolve(approval.id, approve=False, by="console:desktop", assurance=DESKTOP)
    assert len(home.fabric.approvals.recent(limit=10_000)) <= KEEP_FINAL + 1


# ---------------------------------------------------------------- B10 / B11: freeze and leases
async def test_B10_a_second_freeze_does_not_rewrite_the_first(home):
    home.fabric.freeze("gpio:kill", "physical kill switch")
    await dispatch(home.fabric, Session("agent", "claude"), "freeze", {"reason": "nothing to see here"})
    assert home.fabric.frozen["by"] == "gpio:kill" and home.fabric.frozen["reason"] == "physical kill switch"
    assert sum(1 for e in home.fabric.recent_events() if e["type"] == "freeze") == 1


async def test_B11_a_lease_needs_the_right_to_control(home):
    with pytest.raises(PermissionDenied):
        home.fabric.acquire_lease("painter", "sim:kitchen.kettle", 300)
    with pytest.raises(NotFound):
        home.fabric.acquire_lease("intern", "sim:entrance.front_door", 300)
    assert home.fabric.acquire_lease("painter", "sim:office.desk_light", 300)["agent"] == "painter"


# ---------------------------------------------------------------- B12 / B13: who changed it
async def test_B12_stopping_a_door_the_fabric_is_moving_is_a_persons_doing(make_home):
    h = await make_home({"policy": {"tiers": {"T3": "allow"}}})
    assert (await h.do("claude", "garage.door", "open"))["status"] == "executed"
    h.sim.step(3)  # it travels: those changes are the fabric's
    assert "human_hold" not in h.fabric.get_state("sim:garage.door")
    h.sim.external("garage.door", cover_state="stopped")  # someone hits the wall button
    assert h.fabric.registry.state["sim:garage.door"]["cover_state"].origin == "external"
    assert (await h.do("claude", "garage.door", "open"))["code"] == "human_hold"


async def test_B13_a_change_after_the_command_finished_is_not_the_fabrics(home):
    await home.do("claude", "living_room.lamp", "set_level", level=40)
    home.sim.external("living_room.lamp", level=20)  # a person dims it further, seconds later
    assert home.fabric.registry.state["sim:living_room.lamp"]["level"].origin == "external"
    assert (await home.do("claude", "living_room.lamp", "set_level", level=40))["code"] == "human_hold"


async def test_B14_a_backend_coming_back_is_announced(home):
    home.fabric.adapter_status("sim", False, "gone")
    thing = home.fabric.registry.get("sim:living_room.lamp")
    home.fabric.adapter_status("sim", True, "back")
    home.fabric.add_thing(thing, state={"on": False}, online=True)
    last = [e for e in home.fabric.recent_events() if e.get("thing") == thing.id][-1]
    assert last["type"] == "availability" and last["online"] is True


# ---------------------------------------------------------------- B15 / B16: the audit log
async def test_B15_a_torn_last_line_does_not_stop_the_daemon(home, tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, home.clock, fsync=False)
    for n in range(3):
        log.append("executed", n=n)
    with open(path, "ab") as fh:
        fh.write(b'{"seq": 4, "ts": "2026-11-18T12:00:00+00:00", "kind": "exec')  # the power went out here
    reopened = AuditLog(path, home.clock, fsync=False)  # used to raise JSONDecodeError
    assert reopened.seq == 4 and reopened.tail(1)[0]["kind"] == "recovered"
    assert verify_file(path).ok and (tmp_path / "audit.jsonl.torn").read_bytes().startswith(b'{"seq": 4')
    reopened.append("executed", n=9)
    assert verify_file(path).ok and verify_file(path).records == 5


async def test_B16_a_console_notices_a_log_that_was_cut_short_or_rewritten(home, tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, home.clock, key=b"node-key", fsync=False)
    for n in range(5):
        log.append("executed", n=n)
    seen_seq, seen_head = log.seq, log.prev  # what a console remembers
    assert log.witness(seen_seq, seen_head)["status"] == "match"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:3]) + "\n")  # someone removes the last two records
    cut = AuditLog(path, home.clock, key=b"node-key", fsync=False)
    assert verify_file(path, b"node-key").ok  # the chain alone cannot tell
    assert cut.witness(seen_seq, seen_head)["status"] == "rolled_back"
    cut.append("executed", n="x")
    cut.append("executed", n="y")  # ...and writes two others in their place
    assert cut.witness(seen_seq, seen_head)["status"] == "rewritten"


# ---------------------------------------------------------------- B17: a config typo must not relax policy
@pytest.mark.parametrize("extra, complaint", [
    ({"policy": {"bound": [{"param": "temperature", "max": 24}]}}, "unknown key(s) bound"),
    ({"polcy": {}}, "unknown key(s) polcy"),
    ({"agents": {"claude": {"max_teir": "T1"}}}, "max_teir"),
    ({"things": {"sim:office.plug": {"kind": "heatr"}}}, "unknown kind 'heatr'"),
    ({"policy": {"quiet_hours": {"start": "23:00", "end": "07:00", "from": "T1"}}}, "unknown key(s) from"),
    ({"adapters": [{"type": "simulator", "id": "sim"}, {"type": "simulator", "id": "sim"}]}, "share the id"),
    ({"rules": [{"id": "x", "on": {"gesture": "double"}, "do": [{"thing": "*", "command": "fly"}]}]}, "'fly'"),
    ({"rules": [{"id": "x", "on": {"state": "leak"}, "run_as": "human", "do": [{"thing": "*", "command": "close"}]}]},
     "only a gesture"),
    ({"rules": [{"id": "x", "max_tier": "T3", "on": {"gesture": "double"}, "do": [{"thing": "*", "command": "unlock"}]}]},
     "rules_max_tier"),
])
def test_B17_config_mistakes_are_errors(extra, complaint):
    with pytest.raises(ValueError) as err:
        parse(merge(BASE, extra))
    assert complaint in str(err.value)


def test_B17_half_configured_quiet_hours_are_an_error():
    with pytest.raises(ValueError) as err:
        parse({"policy": {"quiet_hours": {"start": "23:00"}}})
    assert "needs start and end" in str(err.value)


def test_B17_adapter_options_are_checked_too():
    cfg = parse(merge(BASE, {"adapters": [{"type": "homeassistant", "id": "ha", "exclude_integration": ["matter"]}]}))
    with pytest.raises(ValueError) as err:
        Fabric(cfg)
    assert "exclude_integration" in str(err.value)


def test_B17_adapters_are_named_after_their_type():
    cfg = parse({"adapters": [{"type": "homeassistant"}, {"type": "matter"}], "audit": {"enabled": False}})
    assert [a.id for a in cfg.adapters] == ["homeassistant", "matter"]  # was "homeassistant", "matter1"


async def test_B18_a_bound_on_a_text_argument_is_ignored_not_fatal(make_home):
    h = await make_home({"policy": {"bounds": [{"param": "mode", "max": 3}], "tiers": {"T2": "allow"}}})
    assert (await h.do("claude", "living_room.thermostat", "set_mode", mode="heat"))["status"] == "executed"


# ---------------------------------------------------------------- B19: transports
async def test_B19_an_identity_is_for_the_life_of_a_connection(home, short_tmp):
    rpc = RpcConfig(socket=short_tmp / "fabric.sock", local_assurance=DESKTOP)
    server = RpcServer(home.fabric, rpc)
    await server.start()
    try:
        agent = await RpcClient.connect(rpc.socket, role="agent", agent="claude")
        with pytest.raises(RpcError) as err:
            await agent.call("hello", role="console", agent="desktop")  # used to switch roles
        assert err.value.code == "forbidden"
        with pytest.raises(RpcError):
            await agent.call("thaw")
        for name in ("human:desktop", "--exec", "rule:x", "a" * 65, "two words"):
            with pytest.raises(RpcError):
                await RpcClient.connect(rpc.socket, role="agent", agent=name)
        with pytest.raises(RpcError) as err:
            await agent.call("invoke", thing="sim:living_room.lamp", command="turn_on", operator=True)
        assert err.value.code == "forbidden"  # only a console is where a person sits
        with pytest.raises(RpcError) as err:
            await agent.call("lease", thing="sim:living_room.lamp", ttl_s="soon")
        assert err.value.code == "invalid"
        await agent.close()
    finally:
        await server.stop()


async def test_B20_the_mcp_proxy_survives_a_daemon_restart(home, short_tmp):
    rpc = RpcConfig(socket=short_tmp / "fabric.sock", local_assurance=DESKTOP)
    server = RpcServer(home.fabric, rpc)
    await server.start()
    backend = RemoteBackend(lambda: RpcClient.connect(rpc.socket, role="agent", agent="claude"))
    assert (await backend.call("status"))["name"] == "test-home"
    await server.stop()
    backend.client.writer.close()
    await asyncio.sleep(0.05)
    with pytest.raises(RpcError) as err:
        await backend.call("status")
    assert err.value.code == "unavailable"
    server = RpcServer(home.fabric, rpc)
    await server.start()
    try:
        assert (await backend.call("status"))["name"] == "test-home"  # used to stay dead until the agent restarted
        await backend.client.close()
    finally:
        await server.stop()


# ---------------------------------------------------------------- B21 - B24: the approval card
@pytest.fixture
def omarchy_desktop(monkeypatch):
    monkeypatch.setattr(console_mod.shutil, "which", lambda name: f"/usr/bin/{name}")


def _card(**overrides):
    approval = {"id": "K7QMZP", "summary": "claude wants to TURN ON 'Kettle'", "agent": "claude", "tier": "T2",
                "requires": "desktop", "reason": "tea", **overrides}
    return console_mod.Console(None, None)._approval_card(approval)


def test_B21_a_click_on_the_card_only_shows_the_request(omarchy_desktop):
    cmd = _card()
    labels = [cmd[i + 1] for i, word in enumerate(cmd) if word == "--action"]
    assert labels == ["Review", "Approve", "Deny"]  # Omarchy runs the first one when the card is clicked
    assert cmd[-2:] == ["--with", "K7QMZP"]  # the id is data, appended by the sender to every action
    assert "K7QMZP" not in cmd[:-1]
    presence = _card(requires="presence")
    assert [presence[i + 1] for i, w in enumerate(presence) if w == "--action"] == ["Review", "Deny"]
    assert _card(id="$(reboot)") is None  # an id that is not ours never reaches a command line


def test_B22_requests_and_alarms_get_through_do_not_disturb(omarchy_desktop):
    assert "--app-name" not in _card()  # Omarchy's own app name is what its Do Not Disturb lets through
    note = console_mod.notify_command("Fabric: claude ran on_off.turn_on", "On sim:kitchen.kettle (T2)", urgency="low")
    assert note[1:3] == ["--app-name", "omarchy-fabric"]
    quiet = console_mod.Console(None, None, through_dnd=False)._approval_card(
        {"id": "K7QMZP", "summary": "x", "agent": "claude", "tier": "T2", "requires": "desktop"})
    assert quiet[1:3] == ["--app-name", "omarchy-fabric"]


def test_B23_agent_text_cannot_reshape_the_card(omarchy_desktop):
    cmd = _card(reason="<a href='http://evil.example'>details</a> &lt;b&gt; your verification code is 482913",
                summary="claude wants to SET COLOR TEMP 'Lamp' (kelvin=2700)")
    headline, body = cmd[cmd.index("--action") - 2], cmd[cmd.index("--action") - 1]
    assert "<" not in body and ">" not in body and "&" not in body
    assert "482913" not in body and "2700" not in headline  # no digit run is left for a "copy code" button
    assert "4·8·2·9·1·3" in body
    plain = _card(summary="claude wants to SET COLOR TEMP 'Lamp' (kelvin=2700)")
    assert "kelvin=2700" in plain[plain.index("--action") - 2]  # untouched when nothing talks about codes
    dashed = console_mod.notify_command("--urgency=low", "--exec")
    assert dashed[-2:] == ["urgency=low", "exec"]  # text can never sit where an option could


async def test_B24_a_resolved_request_replaces_its_card(omarchy_desktop, monkeypatch):
    sent = []

    async def fake_run(cmd, fallback="", capture=False):
        sent.append(cmd)
        return "42" if capture else ""

    monkeypatch.setattr(console_mod, "_run", fake_run)
    console = console_mod.Console(None, None)
    console._waited = True

    async def no_refresh():
        return None

    console.refresh = no_refresh
    approval = {"id": "K7QMZP", "summary": "claude wants to TURN ON 'Kettle'", "agent": "claude", "tier": "T2",
                "requires": "desktop", "status": "pending"}
    await console.handle({"type": "approval_requested", "approval": approval})
    assert "-p" in sent[0] and "--transient" in sent[0]  # not brought back, buttons and all, by a shell restart
    await console.handle({"type": "approval_resolved", "approval": {**approval, "status": "expired"}})
    assert sent[1][sent[1].index("-r") + 1] == "42" and "--action" not in sent[1]
    assert any(word.startswith("Expired: ") for word in sent[1])
    await console.handle({"type": "approval_resolved", "approval": {**approval, "status": "expired"}})
    assert len(sent) == 2  # nothing left to replace


def test_agents_never_learn_a_thing_exists_from_a_removed_event(home):
    home.fabric.remove_thing("sim:entrance.alarm")
    assert not any(e.get("thing") == "sim:entrance.alarm" for e in home.fabric.recent_events(viewer="claude"))


def test_room_matching_ignores_how_a_room_is_spelt():
    from omarchy_fabric.util import match_thing

    lamp = Thing("ha:light.sofa", "Sofa", "ha", "light.sofa", ("on_off", "level"), area="Living Room", kind="light")
    assert match_thing("area:living_room", lamp) and match_thing("area:Living Room", lamp)
    assert match_thing("area:living*&kind:light&cap:level", lamp)
    assert not match_thing("area:living_room&kind:plug", lamp) and not match_thing("cap:door_lock", lamp)


def test_invalid_request_for_unknown_rule_command_reads_well():
    from omarchy_fabric.rules import parse_rule

    with pytest.raises(ValueError) as err:
        parse_rule({"id": "Bad Id", "on": {"gesture": "double"}, "do": []})
    assert "rule id" in str(err.value)
    assert isinstance(InvalidRequest("x"), Exception) and json.dumps({"ok": True})
