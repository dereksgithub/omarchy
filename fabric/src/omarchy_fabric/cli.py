"""ofab: the omarchy-fabric command line.

    ofab serve                         run the fabric daemon
    ofab mcp --agent claude-code       MCP server for an agent (stdio)
    ofab console                       desktop notifications + bar status
    ofab things | get | describe | do | simulate
    ofab toggle [room | selector]      the person at this desktop flips the lights
    ofab rooms | where | room [name] | presence [here | away]
    ofab rules | rule add FILE | rule rm ID | emit SIGNAL key=value
    ofab approvals | approve | deny | freeze | thaw
    ofab events [-f] | audit verify | audit tail
    ofab sim human sim:entrance.front_door lock_state=unlocked
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import __version__
from . import config as config_mod
from .rpc import RpcClient, RpcError
from .util import slug, valid_name


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _kv(pairs: list[str]) -> dict[str, Any]:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"ofab: expected key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        out[key] = _value(value)
    return out


def _print(obj: Any, as_json: bool = False) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    print("  ".join(h.upper().ljust(w) for h, w in zip(headers, widths)))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def _target(args, cfg) -> str | None:
    return args.connect or os.environ.get("OFAB_CONNECT") or cfg.console.connect or (
        str(cfg.rpc.socket) if cfg.rpc else None)


async def _client(args, cfg, role: str = "console") -> RpcClient:
    agent = getattr(args, "as_agent", None) or "cli"
    if not valid_name(agent):
        raise SystemExit("ofab: an agent name is letters, digits, '.', '_' or '-'")
    return await RpcClient.connect(_target(args, cfg), role=role, agent=agent,
                                   token=os.environ.get("OFAB_TOKEN"))


# ------------------------------------------------------------------ this desktop's room
def room_file(cfg) -> Path:
    """One line naming the room this desktop is in, next to the config file. It is
    separate from config.toml so `ofab room office` can set it without rewriting TOML."""
    base = cfg.source.parent if cfg.source else config_mod.DEFAULT_CONFIG.expanduser().parent
    return base / "room"


def desktop_room(cfg) -> str | None:
    try:
        text = room_file(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return slug(text) or cfg.console.room


def desktop_source() -> str:
    return f"desktop:{slug(socket.gethostname().split('.')[0], keep='-') or 'host'}"


def _nudge_console(sig: str) -> bool:
    """Tell the running `ofab console` service to re-read the room or change presence.
    Best effort: off Omarchy (or with no console running) there is nothing to tell."""
    if not shutil.which("systemctl"):
        return False
    done = subprocess.run(["systemctl", "--user", "kill", f"--signal={sig}", "omarchy-fabric-console.service"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return done.returncode == 0


def _light_selector(target: str | None, cfg) -> str:
    """`ofab toggle` with nothing: the lights of this desktop's room. With a bare word:
    the lights of that room. Anything with ':' or a glob is a selector as written."""
    if not target:
        room = desktop_room(cfg)
        if not room:
            raise SystemExit("ofab: say which room this desktop is in first: ofab room office")
        return f"area:{room}&kind:light"
    if any(ch in target for ch in ":*?[&"):
        return target
    return f"area:{slug(target)}&kind:light"


def _load_rules(path: str) -> list[dict[str, Any]]:
    """Rules from a TOML file ([[rules]] tables, or one rule at the top level) or JSON."""
    file = Path(path).expanduser()
    try:
        raw = file.read_bytes()
    except OSError as exc:
        raise SystemExit(f"ofab: {exc}") from None
    try:
        data = json.loads(raw) if file.suffix == ".json" else tomllib.loads(raw.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise SystemExit(f"ofab: {file}: {exc}") from None
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        return data["rules"]
    return data if isinstance(data, list) else [data]


# ------------------------------------------------------------------ commands
async def cmd_serve(args, cfg) -> int:
    from .fabric import Fabric
    from .rpc import RpcServer

    logging.basicConfig(level=logging.INFO if not args.verbose else logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        fabric = Fabric(cfg)
    except ValueError as exc:  # parses, but cannot be served: an unknown adapter option, say
        print(f"ofab: {exc}", file=sys.stderr)
        return 2
    await fabric.start()
    server = RpcServer(fabric, cfg.rpc)
    await server.start()
    source = cfg.source or "built-in simulator config"
    print(f"ofab {__version__}: serving fabric {cfg.name!r} on {cfg.rpc.socket} ({source})", file=sys.stderr, flush=True)
    status = fabric.status()
    if status.get("warning"):
        print(f"ofab: warning: {status['warning']}", file=sys.stderr)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop()
    await fabric.stop()
    return 0


async def cmd_mcp(args, cfg) -> int:
    try:
        from .mcp_server import LocalBackend, RemoteBackend, build_server
        import mcp.server.mcpserver  # noqa: F401 - the optional dependency, at the version we need
    except ImportError:
        print("ofab: the MCP server needs the mcp extra (MCP SDK 2.3 or newer): "
              "pip install 'omarchy-fabric[mcp]'", file=sys.stderr)
        return 2
    if not valid_name(args.agent):
        print("ofab mcp: --agent is letters, digits, '.', '_' or '-'", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    fabric = None
    if args.embedded:
        from .fabric import Fabric

        try:
            fabric = Fabric(cfg)
        except ValueError as exc:
            print(f"ofab: {exc}", file=sys.stderr)
            return 2
        await fabric.start()
        backend = LocalBackend(fabric, args.agent)
    else:
        target, token = _target(args, cfg), os.environ.get("OFAB_TOKEN")

        async def connect() -> RpcClient:
            return await RpcClient.connect(target, role="agent", agent=args.agent, token=token)

        backend = RemoteBackend(connect)
        try:
            await backend.ensure()
        except RpcError as exc:
            # Stay up: the daemon may start after the agent does (login order, a node
            # that is rebooting). Tools answer "unavailable" until it is reachable.
            print(f"ofab mcp: {exc}; will keep trying on each call "
                  "(or pass --embedded for a private simulator)", file=sys.stderr)
    try:
        await build_server(backend).run_stdio_async()
    finally:
        if fabric is not None:
            await fabric.stop()
    return 0


async def cmd_rpc_stdio(args, cfg) -> int:
    from .rpc import stdio_bridge

    await stdio_bridge(cfg.rpc.socket, args.role, args.agent)
    return 0


async def cmd_console(args, cfg) -> int:
    from .console import Console

    console = Console(_target(args, cfg), cfg.console.status_file, notify=cfg.console.notify and not args.quiet,
                      notify_executed_from=cfg.console.notify_executed_from,
                      room=lambda: desktop_room(cfg), person=cfg.console.person, source=desktop_source(),
                      witness_file=cfg.state_dir / "witness.json", through_dnd=cfg.console.through_dnd)
    await console.run()
    return 0


def cmd_room(args, cfg) -> int:
    """Which room this desktop is in. While someone is at it, they are in that room."""
    path = room_file(cfg)
    if args.name is None:
        room = desktop_room(cfg)
        print(room or "no room set (ofab room office)")
        return 0 if room else 1
    room = slug(args.name)
    if not room:
        print("ofab: a room name is a word such as office or living_room", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(room + "\n", encoding="utf-8")
    _nudge_console("SIGHUP")
    print(room)
    return 0


async def cmd_remote(args, cfg) -> int:
    """Every command that talks to a running daemon."""
    c = args.cmd
    role = "agent" if c in ("do", "simulate") and args.as_agent else "console"
    if c == "matter":
        role = "admin"
    if c == "presence" and args.presence_cmd and not (args.person or args.room or args.ttl):
        # This desktop's own presence belongs to the console service: it is bound to
        # that service's connection, so it ends by itself when the desktop sleeps.
        here = args.presence_cmd == "here"
        if _nudge_console("SIGUSR2" if here else "SIGUSR1"):
            print("told the console you are " + ("here" if here else "away"))
            return 0
        print("ofab: no console service is running here (`ofab console`); to report by hand use "
              "`ofab presence here ROOM --ttl 600`", file=sys.stderr)
        return 1
    client = await _client(args, cfg, role)
    try:
        if c == "status":
            result = await client.call("status")
            if args.json:
                _print(result)
            else:
                frozen = result.get("frozen")
                print(f"fabric {result['name']} (ofab {result['version']}): {result['online']}/{result['things']} things online"
                      + (f", FROZEN by {frozen['by']}: {frozen['reason']}" if frozen else ""))
                for name, a in result["adapters"].items():
                    print(f"  {name:<14} {'connected' if a['connected'] else 'DISCONNECTED'}  {a.get('detail', '')}")
                print(f"  pending approvals: {result['pending_approvals']}   rules: {result.get('rules', 0)}")
                if result.get("warning"):
                    print(f"  warning: {result['warning']}")
        elif c == "things":
            result = await client.call("things", area=args.area, capability=args.capability, query=args.query)
            if args.json:
                _print(result)
            else:
                _table([[t["id"], t["name"], t.get("area") or "", t.get("kind") or "", ",".join(t["capabilities"]),
                         "yes" if t["online"] else "NO"] for t in result["things"]],
                       ["id", "name", "area", "kind", "capabilities", "online"])
        elif c == "get":
            _print(await client.call("state", thing=args.thing))
        elif c == "describe":
            _print(await client.call("describe", thing=args.thing))
        elif c in ("do", "simulate"):
            method = "invoke" if c == "do" else "simulate"
            operator = bool(getattr(args, "me", False))
            if operator and args.as_agent:
                raise SystemExit("ofab: --me and --as are different people; pick one")
            result = await client.call(method, thing=args.thing, command=args.command, args=_kv(args.args),
                                       reason=getattr(args, "reason", "") or "operator via ofab",
                                       wait_s=getattr(args, "wait", 0), operator=operator or None)
            _print(result)
            return 0 if result.get("status") in ("executed", "simulated", "pending_approval") else 3
        elif c == "toggle":
            result = await client.call("group", selector=_light_selector(args.target, cfg), command="toggle",
                                       reason="toggled from the desktop")
            if args.json:
                _print(result)
            elif not result["results"]:
                print(f"nothing matches {result['selector']}")
                return 3
            else:
                for r in result["results"]:
                    print(f"{r['thing']}: {r.get('status')}" + (f" ({r.get('code')})" if r.get("status") != "executed" else ""))
            return 0 if any(r.get("status") == "executed" for r in result["results"]) else 3
        elif c == "rooms":
            result = await client.call("presence")
            if args.json:
                _print(result)
            else:
                _table([[r["room"], str(r["things"]), {True: "yes", False: "no"}.get(r.get("occupied"), "?"),
                         ", ".join(r.get("people", []))] for r in result["rooms"]],
                       ["room", "things", "occupied", "people"])
        elif c == "where":
            _print(await client.call("where", person=args.person))
        elif c == "presence":
            source = desktop_source().replace("desktop:", "cli:", 1)  # never the console service's own claim
            if args.presence_cmd == "here":
                room = args.room or desktop_room(cfg)
                if not room:
                    raise SystemExit("ofab: say which room: ofab presence here office (or set one: ofab room office)")
                _print(await client.call("presence_report", person=args.person or cfg.console.person, room=room,
                                         source=source, ttl_s=args.ttl))
            elif args.presence_cmd == "away":
                _print(await client.call("presence_clear", person=args.person or cfg.console.person, source=source))
            else:
                _print(await client.call("presence"))
        elif c == "emit":
            _print(await client.call("emit", signal=args.signal, data=_kv(args.data),
                                     room=args.room or desktop_room(cfg)))
        elif c == "rules":
            result = await client.call("rules")
            if args.json:
                _print(result)
            elif not result["rules"]:
                print("no rules")
            else:
                _table([[r["id"], "on" if r["enabled"] else "OFF", r["source"], r["max_tier"], str(r["fired"]),
                         r["does"]] for r in result["rules"]], ["id", "", "from", "tier", "fired", "rule"])
        elif c == "rule":
            if args.rule_cmd == "add":
                for rule in _load_rules(args.file):
                    _print(await client.call("rule_add", rule=rule))
            elif args.rule_cmd == "rm":
                _print(await client.call("rule_remove", id=args.id))
            else:
                _print(await client.call("rule_enable", id=args.id, enabled=args.rule_cmd == "enable"))
        elif c == "approvals":
            result = await client.call("approvals", recent=args.recent)
            if args.json:
                _print(result)
            elif not result["approvals"]:
                print("no pending approvals")
            else:
                _table([[a["id"], a["status"], a["tier"], a["requires"], a["summary"],
                         a.get("detail") or a.get("reason", "")]
                        for a in result["approvals"]], ["id", "status", "tier", "needs", "request", "reason"])
        elif c == "approval":
            result = await client.call("approval_status", id=args.id)
            a = result["approval"]
            if args.json:
                _print(result)
            else:
                print(a["summary"])
                if a.get("detail"):
                    print(f"  it would   {a['detail']}")
                print(f"  status     {result['status']}"
                      + (f" ({a['expires_in_s']}s left)" if "expires_in_s" in a else ""))
                print(f"  risk       {a['tier']}, needs {a['requires']} approval")
                print(f"  asked by   {a['agent']}")
                print(f"  reason     {a.get('reason') or '(none given)'}")
                if result["status"] == "pending_approval":
                    how = ("press the approve button on the fabric node" if a["requires"] == "presence"
                           else f"ofab approve {a['id']}")
                    print(f"\n  to allow: {how}\n  to refuse: ofab deny {a['id']}")
        elif c in ("approve", "deny"):
            _print(await client.call(c, id=args.id, note=args.note))
        elif c == "freeze":
            _print(await client.call("freeze", reason=" ".join(args.reason) or "operator emergency stop"))
        elif c == "thaw":
            _print(await client.call("thaw"))
        elif c == "lease":
            _print(await client.call("lease", thing=args.thing, ttl_s=args.ttl, purpose=args.purpose))
        elif c == "release":
            _print(await client.call("release", thing=args.thing))
        elif c == "events":
            if args.follow:
                async for event in client.events():
                    print(json.dumps(event, default=str, ensure_ascii=False), flush=True)
            else:
                for event in (await client.call("events", limit=args.limit, thing=args.thing))["events"]:
                    print(json.dumps(event, default=str, ensure_ascii=False))
        elif c == "audit":
            if args.audit_cmd == "forget":
                # After looking into an audit alert: stop comparing against the old log.
                witness = cfg.state_dir / "witness.json"
                witness.unlink(missing_ok=True)
                _nudge_console("SIGHUP")
                print(f"forgot what this console had seen of the audit log ({witness})")
                return 0
            if args.audit_cmd == "verify":
                result = await client.call("audit_verify")
                _print(result)
                return 0 if result["ok"] else 4
            for record in (await client.call("audit_tail", n=args.n))["records"]:
                print(json.dumps(record, ensure_ascii=False))
        elif c == "sim":
            if args.action == "gesture":
                result = await client.call("sim", action="gesture", thing=args.thing,
                                           gesture=args.attrs[0] if args.attrs else "single")
            else:
                result = await client.call("sim", action=args.action, thing=args.thing, attrs=_kv(args.attrs),
                                           seconds=args.seconds, fault=args.fault)
            _print(result)
        elif c == "matter":
            _print(await client.call("matter_commission", code=args.code, network_only=args.network_only))
        return 0
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ofab", description="omarchy-fabric: agents to the physical world, safely.")
    p.add_argument("--version", action="version", version=f"ofab {__version__}")
    p.add_argument("-c", "--config", help="config file (default ~/.config/omarchy-fabric/config.toml)")
    p.add_argument("--connect", help="fabric to talk to: socket path, ssh://user@host, tcp://host:port")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the fabric daemon")
    s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("mcp", help="MCP server for an agent, on stdio")
    s.add_argument("--agent", default=os.environ.get("OFAB_AGENT", "agent"), help="agent identity for policy and audit")
    s.add_argument("--connect", default=argparse.SUPPRESS, help="same as the global --connect")
    s.add_argument("--embedded", action="store_true", help="run a private fabric in-process instead of using the daemon")
    s = sub.add_parser("rpc-stdio", help="RPC on stdin/stdout with a fixed identity (SSH forced command)")
    s.add_argument("--role", default="agent", choices=["agent", "console", "admin"])
    s.add_argument("--agent", default="remote-agent")
    s = sub.add_parser("console", help="desktop notifications and bar status")
    s.add_argument("--quiet", action="store_true", help="status file only, no notifications")

    sub.add_parser("status", help="fabric health")
    s = sub.add_parser("things", help="list things")
    s.add_argument("--area")
    s.add_argument("--capability")
    s.add_argument("--query")
    for name in ("get", "describe"):
        sub.add_parser(name, help=f"{name} one thing").add_argument("thing")
    for name, help_text in (("do", "act on a thing (through policy)"), ("simulate", "dry run")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("thing")
        s.add_argument("command")
        s.add_argument("args", nargs="*", help="key=value")
        s.add_argument("--reason", default="")
        s.add_argument("--wait", type=float, default=0)
        s.add_argument("--as", dest="as_agent", help="act as this agent identity (agent role)")
        if name == "do":
            s.add_argument("--me", action="store_true",
                           help="act as the person at this console, not as an agent (for you, not for agents)")
    s = sub.add_parser("toggle", help="flip the lights of a room, as the person at this desktop")
    s.add_argument("target", nargs="?", help="a room, a thing id or a selector; default: this desktop's room")
    sub.add_parser("rooms", help="rooms, whether they are occupied, and who is where")
    s = sub.add_parser("where", help="which room a person is in")
    s.add_argument("person", nargs="?")
    s = sub.add_parser("room", help="show or set the room this desktop is in")
    s.add_argument("name", nargs="?")
    s = sub.add_parser("presence", help="who is where; `here` / `away` say whether you are at this desktop")
    s.add_argument("presence_cmd", nargs="?", choices=["here", "away"])
    s.add_argument("room", nargs="?")
    s.add_argument("--person")
    s.add_argument("--ttl", type=float, help="seconds until the report lapses")
    s = sub.add_parser("emit", help="send a signal that rules can trigger on, e.g. desktop.screenshare state=on")
    s.add_argument("signal")
    s.add_argument("data", nargs="*", help="key=value")
    s.add_argument("--room")
    sub.add_parser("rules", help="list rules")
    s = sub.add_parser("rule", help="add, remove, enable or disable a rule")
    r = s.add_subparsers(dest="rule_cmd", required=True)
    r.add_parser("add", help="add the rule(s) in a TOML or JSON file").add_argument("file")
    for name in ("rm", "enable", "disable"):
        r.add_parser(name).add_argument("id")
    s = sub.add_parser("approvals", help="pending approvals")
    s.add_argument("--recent", action="store_true")
    sub.add_parser("approval", help="show one request in full").add_argument("id")
    for name in ("approve", "deny"):
        s = sub.add_parser(name, help=f"{name} a pending request")
        s.add_argument("id")
        s.add_argument("--note", default="")
    s = sub.add_parser("freeze", help="emergency stop for all agent actions")
    s.add_argument("reason", nargs="*")
    sub.add_parser("thaw", help="resume after a freeze")
    s = sub.add_parser("lease", help="lease a thing (as the cli agent)")
    s.add_argument("thing")
    s.add_argument("--ttl", type=float, default=300)
    s.add_argument("--purpose", default="")
    sub.add_parser("release", help="release a lease").add_argument("thing")
    s = sub.add_parser("events", help="recent events")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--thing")
    s = sub.add_parser("audit", help="audit log")
    a = s.add_subparsers(dest="audit_cmd", required=True)
    a.add_parser("verify")
    a.add_parser("forget", help="after an audit alert: stop comparing against the log this console saw before")
    t = a.add_parser("tail")
    t.add_argument("-n", type=int, default=20)
    s = sub.add_parser("sim", help="simulator controls: human, external, sensor, gesture, offline, online, fault, step")
    s.add_argument("action", choices=["human", "external", "sensor", "gesture", "offline", "online", "fault", "step"])
    s.add_argument("thing", nargs="?", default="")
    s.add_argument("attrs", nargs="*", help="key=value, or the gesture name (single, double, long)")
    s.add_argument("--seconds", type=float, default=1)
    s.add_argument("--fault", default="error")
    s = sub.add_parser("matter", help="Matter admin")
    m = s.add_subparsers(dest="matter_cmd", required=True)
    c = m.add_parser("commission", help="commission a device with its setup code (MT:... or 11-digit)")
    c.add_argument("code")
    c.add_argument("--network-only", action="store_true", help="device is already on the network (no BLE)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ofab: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "room":
        return cmd_room(args, cfg)
    handlers = {"serve": cmd_serve, "mcp": cmd_mcp, "rpc-stdio": cmd_rpc_stdio, "console": cmd_console}
    handler = handlers.get(args.cmd, cmd_remote)
    try:
        return asyncio.run(handler(args, cfg))
    except RpcError as exc:
        print(f"ofab: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
