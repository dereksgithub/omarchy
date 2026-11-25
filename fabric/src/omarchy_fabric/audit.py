"""Tamper-evident audit log: append-only JSONL with a SHA-256 hash chain.

Every decision, approval and execution is recorded with who asked, what they
asked for, why they said they wanted it, and what happened. Each line carries
the hash of the previous line, so editing or deleting a record breaks the chain
from that point on (`ofab audit verify`). With an HMAC key (kept where agents
cannot read it, e.g. on the Pi node) a rewrite of the whole file is detectable
too.

What a chain cannot show by itself is its own end: cutting records off the tail,
or putting back an older copy of the file, leaves a chain that still verifies.
That takes a witness. Every console remembers the newest (seq, hash) it has seen
and asks `witness()` whether the log still contains it.

A power cut can leave half a line at the end of the file. On start that fragment
is moved to a `.torn` file next to the log and noted in a `recovered` record,
instead of stopping the daemon.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import Clock, iso

GENESIS = "0" * 64


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def _digest(prev: str, record: dict[str, Any], key: bytes | None) -> str:
    payload = prev.encode() + b"\n" + _canonical(record)
    if key:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    records: int
    first_bad_seq: int | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "records": self.records, "first_bad_seq": self.first_bad_seq,
                "message": self.message}


class AuditLog:
    def __init__(self, path: Path | None, clock: Clock, key: bytes | None = None, fsync: bool = True,
                 memory: int = 200):
        self.path = Path(path) if path else None
        self.clock = clock
        self.key = key
        self.fsync = fsync
        self._memory: list[dict[str, Any]] = []
        self._memory_max = memory
        self.seq = 0
        self.prev = GENESIS
        if self.path and self.path.exists():
            torn = 0
            while moved := _recover_tail(self.path):  # one unfinished or unreadable line at a time
                torn += moved
            last = _last_line(self.path)
            if last:
                record = json.loads(last)
                self.seq = int(record["seq"])
                self.prev = record["hash"]
            if torn:
                self.append("recovered", torn_bytes=torn, kept=f"{self.path.name}.torn")

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        self.seq += 1
        now = self.clock.now()
        record: dict[str, Any] = {"seq": self.seq, "ts": iso(now), "kind": kind}
        record.update({k: v for k, v in fields.items() if v is not None})
        record["prev"] = self.prev
        record["hash"] = _digest(self.prev, record, self.key)
        self.prev = record["hash"]
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                if self.fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
        self._memory.append(record)
        del self._memory[: -self._memory_max]
        return record

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        if self.path and self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
            return [json.loads(line) for line in lines if line.strip()]
        return self._memory[-n:]

    def verify(self) -> VerifyResult:
        if self.path is None:
            return verify_records(self._memory, self.key, partial=True)
        return verify_file(self.path, self.key)

    def witness(self, seq: int, head: str) -> dict[str, Any]:
        """Check the log against a (seq, hash) a console saw earlier. A log that is now
        shorter was cut or rolled back; one that holds a different record at that seq
        was rewritten."""
        if seq <= 0:
            return {"ok": True, "status": "nothing_witnessed", "seq": self.seq}
        if seq > self.seq:
            return {"ok": False, "status": "rolled_back", "seq": self.seq, "witnessed_seq": seq,
                    "message": f"the log ends at record {self.seq}, but record {seq} was seen before"}
        found = None
        if self.path and self.path.exists():
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("seq") == seq:
                        found = record.get("hash")
                        break
        else:
            found = next((r["hash"] for r in self._memory if r["seq"] == seq), None)
            if found is None:  # older than what is kept in memory: nothing to compare against
                return {"ok": True, "status": "not_kept", "seq": self.seq}
        if found != head:
            return {"ok": False, "status": "rewritten", "seq": self.seq, "witnessed_seq": seq,
                    "message": f"record {seq} is not the record that was seen before"}
        return {"ok": True, "status": "match", "seq": self.seq}


def verify_records(records: list[dict[str, Any]], key: bytes | None, partial: bool = False) -> VerifyResult:
    prev = records[0]["prev"] if (partial and records) else GENESIS
    expected_seq = records[0]["seq"] if (partial and records) else 1
    for record in records:
        body = {k: v for k, v in record.items() if k != "hash"}
        if record.get("seq") != expected_seq:
            return VerifyResult(False, expected_seq - 1, record.get("seq"),
                                f"sequence gap: expected {expected_seq}, found {record.get('seq')}")
        if body.get("prev") != prev:
            return VerifyResult(False, expected_seq - 1, expected_seq, f"record {expected_seq} does not chain to the previous one")
        if _digest(prev, body, key) != record.get("hash"):
            return VerifyResult(False, expected_seq - 1, expected_seq, f"record {expected_seq} was modified")
        prev = record["hash"]
        expected_seq += 1
    return VerifyResult(True, len(records), None, "chain intact")


def verify_file(path: Path, key: bytes | None = None) -> VerifyResult:
    records = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                return VerifyResult(False, len(records), len(records) + 1, f"line {n} is not valid JSON")
    return verify_records(records, key)


def _recover_tail(path: Path) -> int:
    """Cut an unfinished last line (a write interrupted by a power cut) off the log
    and keep it in `<log>.torn`. Returns how many bytes were moved."""
    size = path.stat().st_size
    if size == 0:
        return 0
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 65536))
        tail = fh.read()
    cut = tail.rfind(b"\n") + 1  # everything after the last newline is unfinished
    fragment = tail[cut:]
    if not fragment:
        try:  # ends with a newline: still make sure the last full line is a record
            json.loads(tail.splitlines()[-1])
            return 0
        except (json.JSONDecodeError, IndexError, UnicodeDecodeError):
            lines = tail.splitlines(keepends=True)
            fragment = lines[-1] if lines else b""
            cut = len(tail) - len(fragment)
    if len(fragment) == len(tail) and size > len(tail):
        raise ValueError(f"{path}: the last 64 KiB hold no complete record; refusing to guess")
    with open(path.with_name(path.name + ".torn"), "ab") as torn:
        torn.write(fragment + (b"" if fragment.endswith(b"\n") else b"\n"))
    with open(path, "r+b") as fh:
        fh.truncate(size - len(fragment))
        fh.flush()
        os.fsync(fh.fileno())
    return len(fragment)


def _last_line(path: Path) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        block = min(size, 65536)
        fh.seek(size - block)
        lines = [line for line in fh.read().splitlines() if line.strip()]
    return lines[-1].decode() if lines else ""
