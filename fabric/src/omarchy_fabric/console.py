"""Desktop console: the human side of the fabric on an Omarchy machine.

`ofab console` runs as a user service. It subscribes to the fabric's event
stream (local daemon or a Pi node over SSH) and

  - pops an Omarchy notification for every approval, with Review / Approve / Deny
    (PRESENCE-level requests get no Approve: that happens at the node's button),
  - tells you about executed actions at or above console.notify_executed_from,
    about rules that ask to be announced, and about anything alarming,
  - keeps $XDG_RUNTIME_DIR/omarchy-fabric/status.json current for the bar widget,
    which watches the file instead of polling (Omarchy's no-polling rule),
  - says "the person is in this room" for as long as someone is at this desktop,
  - remembers the newest audit record it has seen, so a log that was cut short or
    put back to an older copy on the node is noticed (audit.witness).

The approval card is where a person says yes, so it is built defensively:

  - A click on the card itself runs the *first* button (Omarchy's rule). That
    button is Review, which only shows the request. Approve is never the default.
  - The approval id travels after `--with`, as data, never as a word of a command.
  - Text from agents and devices is shown, but with markup characters removed, and
    with numbers broken up when the text talks about codes or PINs: Omarchy offers
    "copy code" buttons for such text, which would renumber the real buttons.
  - A card is replaced in place when its request is resolved, so no stale Approve
    button is left on screen or brought back after a shell restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .model import Tier
from .rpc import RpcClient, RpcError
from .util import clean_text

GLYPH = "\U000f048d"  # md-server_network
APP = "omarchy-fabric"
BACKOFF_S = (1, 2, 5, 10, 30)
REVIEW = "omarchy-launch-floating-terminal-with-presentation"
APPROVAL_ID = re.compile(r"[A-HJ-NP-Z2-9]{6}")
FINAL = ("denied", "expired", "executed", "failed", "cancelled")
SLEEP_MATCH = ("type='signal',sender='org.freedesktop.login1',"
               "interface='org.freedesktop.login1.Manager',member='PrepareForSleep'")

# Mirrors CODE_WORDS in Omarchy's shell/plugins/notifications/Detect.js: next to one of
# these words, a number in a notification is offered as a verification code to copy.
_CODE_WORDS = re.compile(
    r"\b(code|otp|one[\s-]?time|verification|verify|passcode|pin|2fa|two[\s-]factor|security key|confirm(?:ation)?)\b",
    re.IGNORECASE)
_DIGIT_RUN = re.compile(r"(?<=\d)(?=[A-Za-z0-9])|(?<=[A-Z])(?=\d)")


def toast_text(text: object, limit: int = 160) -> str:
    """Text from an agent or a device, made safe to show in a notification."""
    s = clean_text(text, limit)
    return s.replace("<", "‹").replace(">", "›").replace("&", "+")  # the body is rendered as markup


def _defuse(headline: str, body: str) -> tuple[str, str]:
    """Stop a notification from being read as carrying a verification code. Omarchy
    looks at the headline and the body together, so this does too."""
    if not _CODE_WORDS.search(f"{headline}\n{body}"):
        return headline, body
    return _DIGIT_RUN.sub("·", headline), _DIGIT_RUN.sub("·", body)  # "4821" -> "4·8·2·1"


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, default=str))
    os.replace(tmp, path)


def notify_command(headline: str, body: str, actions: list[tuple[str, list[str]]] | None = None, *,
                   with_args: list[str] | None = None, urgency: str = "normal", alarm: bool = False,
                   through_dnd: bool = True, transient: bool = False, replace: int | None = None,
                   want_id: bool = False) -> list[str] | None:
    """Build the notification command for this desktop. Omarchy's helper first (its
    --action runs a program when clicked), plain notify-send otherwise.

    `alarm` marks what a person must not miss: a request waiting for them, a freeze,
    a leak. With through_dnd those are sent under Omarchy's own app name, which is
    the one thing its Do Not Disturb lets through; everything else respects it."""
    actions = actions or []
    # Text must never sit where an option could: a leading dash would read as one.
    headline, body = _defuse(headline.lstrip("- ") or "Fabric", body.lstrip("- "))
    if shutil.which("omarchy-notification-send"):
        cmd = ["omarchy-notification-send"]
        if not (alarm and through_dnd):
            cmd += ["--app-name", APP]
        cmd += ["-u", urgency, "-g", GLYPH]
        if transient:
            cmd.append("--transient")
        if replace:
            cmd += ["-r", str(replace)]
        if want_id:
            cmd.append("-p")
        cmd += [headline, body]
        for label, program in actions:
            cmd += ["--action", label, *program]
        if actions and with_args:
            cmd += ["--with", *with_args]
        return cmd
    if shutil.which("notify-send"):
        return ["notify-send", "-a", APP, "-u", urgency, "--", headline, body]
    return None


async def _run(cmd: list[str] | None, fallback: str = "", capture: bool = False) -> str:
    """Send one notification. Returns what the sender printed (the id, with -p)."""
    if cmd is None:  # no notification daemon (e.g. developing on macOS): log instead
        print(f"ofab console: {fallback}", file=sys.stderr)
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
    except OSError as exc:
        print(f"ofab console: could not notify ({exc}): {fallback}", file=sys.stderr)
        return ""
    if not capture:
        asyncio.ensure_future(proc.wait())
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    if proc.returncode != 0:
        print(f"ofab console: the notification was not accepted: {fallback}", file=sys.stderr)
    return out.decode(errors="replace").strip()


class Console:
    def __init__(self, target: str | None, status_file: Path, notify: bool = True,
                 notify_executed_from: Tier = Tier.T2, ofab: str = "ofab",
                 room: Callable[[], str | None] | str | None = None, person: str | None = None,
                 source: str = "desktop", witness_file: Path | None = None, through_dnd: bool = True):
        self.target = target
        self.status_file = status_file
        self.notify = notify
        self.notify_executed_from = notify_executed_from
        self.ofab = ofab
        self._room = room
        self.person = person
        self.source = source
        self.witness_file = witness_file
        self.through_dnd = through_dnd
        self.client: RpcClient | None = None
        self.here = True  # someone is at this desktop (until told otherwise)
        self.audit_alert: str | None = None
        self._cards: dict[str, int] = {}  # approval id -> notification id, to replace the card later
        self._waited = False
        self._wants: asyncio.Queue[str] = asyncio.Queue()

    @property
    def room(self) -> str | None:
        return self._room() if callable(self._room) else self._room

    # ------------------------------------------------------------ status file
    async def refresh(self) -> None:
        assert self.client is not None
        status = await self.client.call("status")
        approvals = (await self.client.call("approvals"))["approvals"]
        self._remember(status)
        write_status(self.status_file, {
            "connected": True,
            "updated": time.time(),
            "fabric": status.get("name"),
            "frozen": status.get("frozen"),
            "pending": [{k: a.get(k) for k in ("id", "summary", "detail", "tier", "requires", "reason",
                                              "expires_in_s")} for a in approvals],
            "adapters": {k: v.get("connected") for k, v in (status.get("adapters") or {}).items()},
            "things": status.get("things"),
            "online": status.get("online"),
            "rules": status.get("rules"),
            "room": self.room if self.here else None,
            "warning": status.get("warning"),
            "audit_alert": self.audit_alert,
        })

    # ------------------------------------------------------------ audit witness
    def _witness(self) -> dict[str, Any]:
        try:
            data = json.loads(self.witness_file.read_text()) if self.witness_file else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def _remember(self, status: dict[str, Any]) -> None:
        """Keep the newest audit record this console has seen, per fabric."""
        head, name = status.get("audit") or {}, str(status.get("name"))
        if self.witness_file is None or self.audit_alert or not head.get("seq"):
            return  # after an alert the old witness stays, so the alert repeats until someone looks
        seen = self._witness()
        if (seen.get(name) or {}).get("seq") == head["seq"]:
            return
        seen[name] = {"seq": head["seq"], "head": head["head"]}
        try:
            self.witness_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.witness_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(seen))
            os.replace(tmp, self.witness_file)
        except OSError:
            pass

    async def _check_witness(self) -> None:
        assert self.client is not None
        status = await self.client.call("status")
        seen = self._witness().get(str(status.get("name")))
        self.audit_alert = None
        if not seen:
            return
        result = await self.client.call("audit_check", seq=seen.get("seq", 0), head=seen.get("head", ""))
        if result.get("ok"):
            return
        self.audit_alert = result.get("message") or result.get("status") or "the audit log changed"
        await self._send(notify_command(
            "Fabric audit log was altered", toast_text(f"On {status.get('name')}: {self.audit_alert}. "
                                                       "Look into it, then run: ofab audit forget", 240),
            urgency="critical", alarm=True, through_dnd=self.through_dnd), f"audit alert: {self.audit_alert}")

    # ------------------------------------------------------------ presence
    async def _claim(self) -> None:
        """While someone is at this desktop, they are in the room it stands in. The
        claim is bound to this connection, so the fabric drops it when we go away."""
        if self.client is None:
            return
        room = self.room
        try:
            if self.here and room:
                await self.client.call("presence_report", person=self.person, room=room, source=self.source,
                                       bind=True)
            else:
                await self.client.call("presence_clear")
        except RpcError as exc:
            print(f"ofab console: presence not reported: {exc}", file=sys.stderr)

    def _want(self, what: str) -> None:
        self._wants.put_nowait(what)

    async def _wishes(self) -> None:
        """Signals and sleep events, applied one at a time on the connection."""
        while True:
            what = await self._wants.get()
            if what in ("here", "away"):
                self.here = what == "here"
            await self._claim()
            if self.client is not None:
                try:
                    await self.refresh()
                except RpcError:
                    pass

    async def _watch_sleep(self) -> None:
        """Going to sleep ends the claim; waking restores it. Same logind signal, read
        the same way, as Omarchy's own omarchy-system-sleep-monitor."""
        if not shutil.which("dbus-monitor"):
            return
        proc = await asyncio.create_subprocess_exec("dbus-monitor", "--system", SLEEP_MATCH,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            assert proc.stdout is not None
            while line := await proc.stdout.readline():
                if b"boolean true" in line:
                    self._want("away")
                elif b"boolean false" in line:
                    self._want("here")
        finally:
            if proc.returncode is None:
                proc.terminate()

    # ------------------------------------------------------------ notifications
    async def _send(self, cmd: list[str] | None, fallback: str, capture: bool = False) -> str:
        if not self.notify:
            return ""
        if cmd is not None and not self._waited:
            # Right after login the shell may not have claimed the notification bus yet.
            self._waited = True
            if shutil.which("omarchy-notification-wait"):
                waiter = await asyncio.create_subprocess_exec(
                    "omarchy-notification-wait", "10", stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await waiter.wait()
        return await _run(cmd, fallback, capture)

    def _approval_card(self, a: dict[str, Any]) -> list[str] | None:
        approval_id = str(a.get("id", ""))
        if not APPROVAL_ID.fullmatch(approval_id):
            return None
        headline = f"Approve? {toast_text(a.get('summary', ''), 90)}"
        lines = []
        if a.get("detail"):
            lines.append(f"It would: {toast_text(a['detail'], 200)}")
        lines.append(f"{a.get('tier')} · requested by {toast_text(a.get('agent', ''), 64)}")
        if a.get("reason"):
            lines.append(f"Reason: {toast_text(a['reason'], 160)}")
        # Review comes first on purpose: a click on the card runs the first action.
        actions: list[tuple[str, list[str]]] = [("Review", [REVIEW, self.ofab, "approval"])]
        if a.get("requires") == "presence":
            lines.append("Press the approve button on the fabric node to allow it.")
        else:
            actions.append(("Approve", [self.ofab, "approve"]))
        actions.append(("Deny", [self.ofab, "deny"]))
        return notify_command(headline, "\n".join(lines), actions, with_args=[approval_id], urgency="critical",
                              alarm=True, through_dnd=self.through_dnd, transient=True, want_id=True)

    async def _approval_resolved(self, a: dict[str, Any]) -> None:
        card = self._cards.pop(str(a.get("id", "")), None)
        if card is None:
            return
        status = str(a.get("status", ""))
        word = {"approved": "Approved", "executed": "Done", "denied": "Denied", "expired": "Expired",
                "cancelled": "Cancelled", "failed": "Failed"}.get(status, "Closed")
        await self._send(notify_command(f"{word}: {toast_text(a.get('summary', ''), 90)}", "", urgency="low",
                                        transient=True, replace=card),
                         f"approval {a.get('id')} {status}")

    async def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in ("approval_requested", "approval_resolved", "freeze", "thaw", "adapter", "agent_suspended",
                    "availability", "thing_added", "thing_removed", "rule"):
            await self.refresh()
        if not self.notify:
            return
        if kind == "approval_requested":
            a = event["approval"]
            printed = await self._send(self._approval_card(a),
                                       f"approval {a.get('id')} needed ({a.get('requires')}): "
                                       f"{clean_text(a.get('summary', ''), 120)}", capture=True)
            if printed.isdigit():
                self._cards[str(a.get("id"))] = int(printed)
        elif kind == "approval_resolved":
            await self._approval_resolved(event["approval"])
        elif kind == "action" and event.get("status") == "executed":
            if Tier.parse(event.get("tier", "T0")) >= self.notify_executed_from:
                await self._send(notify_command(
                    f"Fabric: {toast_text(event.get('agent', ''), 64)} ran {toast_text(event.get('command', ''), 40)}",
                    f"On {toast_text(event.get('thing', ''), 80)} ({event.get('tier')})", urgency="low"),
                    f"{event.get('agent')} ran {event.get('command')} on {event.get('thing')}")
        elif kind == "rule" and event.get("status") == "fired" and event.get("notify"):
            level = event["notify"] if event["notify"] in ("low", "normal", "critical") else "normal"
            await self._send(notify_command(
                f"Rule {toast_text(event.get('rule', ''), 48)} fired",
                f"Because: {toast_text(event.get('trigger', ''), 160)}\nIt did: {toast_text(event.get('does', ''), 200)}",
                urgency=level, alarm=level == "critical", through_dnd=self.through_dnd),
                f"rule {event.get('rule')} fired: {event.get('trigger')}")
        elif kind == "agent_suspended":
            await self._send(notify_command(
                f"Fabric: {toast_text(event.get('agent', ''), 64)} suspended",
                f"Too many refused actions; it is paused for {round(event.get('seconds', 0))}s.",
                urgency="critical", alarm=True, through_dnd=self.through_dnd, transient=True),
                f"{event.get('agent')} suspended")
        elif kind == "presence_guarded":
            await self._send(notify_command(
                "Fabric: the approve button is paused",
                f"A different request lapsed a moment ago, so the button will not take this one for "
                f"{round(event.get('seconds', 0))}s. Check what is waiting before you press it.",
                urgency="critical", alarm=True, through_dnd=self.through_dnd, transient=True),
                "the approve button refused a request that replaced a lapsed one")
        elif kind == "freeze":
            await self._send(notify_command(
                "Fabric frozen", f"By {toast_text(event.get('by', ''), 80)}. Reason: {toast_text(event.get('reason', ''))}",
                urgency="critical", alarm=True, through_dnd=self.through_dnd, transient=True),
                f"frozen by {event.get('by')}: {event.get('reason')}")

    # ------------------------------------------------------------ main loop
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig, what in ((signal.SIGUSR1, "away"), (signal.SIGUSR2, "here"), (signal.SIGHUP, "room")):
            try:
                loop.add_signal_handler(sig, self._want, what)
            except (NotImplementedError, RuntimeError):
                pass
        helpers = [asyncio.ensure_future(self._wishes()), asyncio.ensure_future(self._watch_sleep())]
        attempt = 0
        try:
            while True:
                try:
                    self.client = await RpcClient.connect(self.target, role="console", agent="desktop")
                    attempt = 0
                    self._cards.clear()
                    await self._check_witness()
                    await self._claim()
                    await self.refresh()
                    async for event in self.client.events():
                        await self.handle(event)
                except (RpcError, OSError) as exc:
                    write_status(self.status_file, {"connected": False, "updated": time.time(), "error": str(exc)})
                finally:
                    if self.client is not None:
                        await self.client.close()
                        self.client = None
                await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                attempt += 1
        finally:
            for helper in helpers:
                helper.cancel()
