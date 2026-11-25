# Audit: the original design and the round-1 build

Written 2026-11-22 on branch `fabric/round-2`. Three things were audited: the vision in `system-architecture.md`, the round-1 code, and the Omarchy glue (checked statically against upstream Omarchy at `c1e3553`, never run on an Omarchy machine). This round was cut short by a usage limit, so the last section lists what is done and what is not.

## Verdict

- **The vision is sound in its layering and too big as a first build.** Not running Linux on wearables, delegating drivers to Home Assistant and Matter, a policy gate between agents and actuators, a deterministic fast path, and compiling automations into rules are all right. It names at least five products (wearables, room terminals, hub daemon, voice, desktop integration) with no phasing and no first slice.
- **Round 1 picked the right first slice** (policy gate, HA and Matter adapters, MCP, desktop glue) and built the gate far deeper than the vision specified. It is worth keeping. It skipped the vision's fast path, presence and human-direct control entirely, and it had real defects (below).
- **Keep Python.** A Rust or Go rewrite of about 4,000 tested lines buys nothing now; the wire protocol is language-neutral, and the libraries the next phases need (BLE, wake word, GPIO) are Python.

## What the vision gets wrong or leaves open

| # | Issue | What the design should say |
|---|---|---|
| D1 | The policy gate is three tiers by device class | Tier per command, adjusted by what the device is and by direction (round 1 does this) |
| D2 | Rules and the gate are unrelated; "agents compile rules into fabricd" is a way around the gate | A rule is a principal with a tier ceiling. An agent's rule needs a person's approval, never exceeds its author, and every action it takes still goes through the pipeline (built this round) |
| D3 | No notion of who is asking | Three kinds of principal: agent, person, rule (built this round). Voice is a low-assurance person: T1 only |
| D4 | A Quickshell prompt on the machine the agent runs on is not out of band | Assurance levels; T3 needs a physical button (round 1). A bonded wearable can become a level between desktop and presence |
| D5 | `fabric.{room}.{entity}.cmd` on a shared bus lets any client with a credential command a device | Clients send requests to the daemon; only the daemon talks to adapters. Rooms do not belong in subject names (devices move; HA ids contain dots) |
| D6 | NATS JetStream with a replicated KV for one hub | Not for v1: a unix socket plus SSH is simpler and pins identities. Keep the transport abstract; add a NATS or MQTT bridge when there are several nodes. MCUs speak MQTT or BLE, not NATS |
| D7 | A new presence engine from BLE RSSI | Reuse Bermuda / ESPresense through HA. The fabric keeps a small model that sources report into (built this round). The Omarchy desktop is a free presence anchor |
| D8 | Custom wearables first | Define the gesture contract; start with off-the-shelf buttons (HA `event` entities, GPIO), which now work. Wearable links need bonding and replay protection before they may confirm anything |
| D9 | `agents/skills/fabric.md` | That tree is contributor guides. User-facing skills are `default/agents/skills/<name>/SKILL.md`, linked into `~/.agents/skills` |
| D10 | No audit, no failure modes, no privacy model | Round 1 has the audit log; presence is now scoped per agent |

## Defects found in round 1

Each fixed item has a test named after it in `tests/test_round2.py`. B25–B32 are leads from an automated review (Greptile) of round 1 that round 2 left open. Each was checked against the code at `f2fb136` and fixed on `fabric/round-3`, except B31, which round 2 had already fixed.

| # | Defect | State |
|---|---|---|
| B01 | `pyproject.toml` pinned `mcp<2` but the code needs SDK 2.x: a fresh install resolved 1.30.0 and `ofab mcp` could not import | Fixed (by the other session, in `1e9a858`) |
| B02 | No way for a person to act: `ofab do` ran as agent "cli" with a T1 ceiling, subject to human hold | Fixed: `ofab do --me`, `ofab toggle` |
| B03 | An agent's `deny` scope blocked control but not reading: state, descriptions and events were visible | Fixed: hidden things do not exist for that agent; attempts are audited and count toward the breaker |
| B04 | Any agent received other agents' approval events, ids and reasons included | Fixed |
| B05 | `set_level` on a plug or heater was T1 although it switches the load on | Fixed |
| B06 | An approved `toggle` could go the opposite way from what the approver saw | Fixed: settled to on or off at request time |
| B07 | A looping agent could queue unlimited identical approvals, jamming the approve button | Fixed: a repeat returns the waiting approval |
| B08 | The blind approve button approved whatever replaced a request that had just lapsed | Fixed: swap guard |
| B09 | Resolved approvals were never pruned | Fixed |
| B10 | A later freeze overwrote who froze the fabric and why | Fixed |
| B11 | Any agent could lease any thing, including ones it may not control | Fixed |
| B12 | A person stopping a door the fabric was moving got no human hold | Fixed |
| B13 | A person's change after a fabric command finished was credited to the fabric (HA and simulator) | Fixed |
| B14 | Things coming back online after a reconnect emitted no event | Fixed |
| B15 | A half-written last audit line (power cut) stopped the daemon from starting | Fixed: moved to `.torn`, noted in a `recovered` record |
| B16 | Cutting records off the end of the audit log left a chain that still verified | Mitigated: consoles remember the newest record and check it (`audit.witness`) |
| B17 | Unknown config keys, kinds and adapter options were ignored, so a typo could silently drop bounds | Fixed: they are errors |
| B18 | A bound on a text argument raised inside the policy engine | Fixed |
| B19 | A connection could say `hello` twice and change role; agent names were free text | Fixed |
| B20 | The MCP proxy stayed dead after a daemon restart | Fixed: reconnects on the next call |
| B21 | A plain click on an approval notification approved it (Omarchy runs the first action on click) | Fixed in `console.py`: Review is first; ids go after `--with` |
| B22 | Approvals, freezes and suspensions were silenced by Do Not Disturb | Fixed, configurable (`console.through_dnd`) |
| B23 | Agent text could add markup and links to the card, or make Omarchy offer "copy code" buttons that renumber Approve | Fixed |
| B24 | Cards were dismissed by a truncated substring that rarely matched, and stale Approve buttons survived a shell restart | Fixed: cards are transient and replaced in place |
| B25 | The emergency stop lived only in memory: a restart, crash or power cut lifted it | Fixed: kept in `emergency-stop.json` in the state directory and restored before adapters start or RPC takes requests; an unreadable file counts as a stop |
| B26 | An agent suspended by the breaker still had its already-approved requests run: the approval re-check cleared the suspension along with the rate budget | Fixed: only the rate budget counted at request time is skipped |
| B27 | The HA adapter's default URL was `ws://homeassistant.local`, sending the long-lived token across the LAN in plaintext | Fixed: defaults to `127.0.0.1`; plaintext to any other host needs `allow_plaintext = true`, otherwise `wss://` |
| B28 | A failed HA registry read (a non-admin token) disabled `exclude_integrations`, and entities created after startup skipped it too, so Matter devices also appeared under an `ha:` id that dodged the other id's lease, hold and load class | Fixed: with exclusions set, the adapter stays unavailable until it can read the registry, and looks up each new entity before admitting it |
| B29 | The console took its snapshot before it subscribed, so a request or freeze in between got no card, nor did requests made while it was disconnected | Fixed: subscribes first, then reconciles cards and the freeze notice against the snapshot |
| B30 | `console.notify = false` was ignored: only `--quiet` turned notifications off | Fixed |
| B31 | A clean WebSocket close left HA, and Matter, marked connected | Not a defect at `f2fb136`: round 2 already reports the adapter down when a session ends cleanly, in both adapters |
| B32 | Every Matter attribute update re-sent the whole endpoint, so a reading that had stopped arriving looked fresh whenever anything else on the endpoint changed | Fixed: an update refreshes only the attributes the reported Matter attribute feeds |

Known limit, unchanged: on one machine an agent with shell access is the same user as you, and on a node it can still use the console's SSH key if that key is readable. `desktop` assurance is only as strong as the separation between agents and console credentials.

## Omarchy glue: found, not yet fixed

These came from the static check against upstream and are still open in `omarchy/`.

- **Installer aborts on a stock machine:** no `omarchy-shell shell rescanPlugins` before enabling the plugin (use `omarchy-bar put fabric.status --section right`); the stock menu overlay is `{}` plus comments, so the menu entries are never installed; `omarchy-pkg-add uv` is wrong (uv is a mise tool: `omarchy-mise-default-tools declares uv`); `omarchy-cmd-present claude` is always true because of lazy shims (use `omarchy-default-agent --installed claude`).
- **Bar widget:** no API errors. It never starts watching if the runtime directory is missing at load, can show stale state after quick writes (use `WatchedFile` plus a `ShellIpc` refresh that the console calls), can overflow its popup, and shows the count on vertical bars. Rename the `clip` function.
- **Units:** the console unit needs `ConditionEnvironment=WAYLAND_DISPLAY` and `OMARCHY_PATH`; drop `After=network-online.target` from the daemon unit.
- **Hook:** use `omarchy-theme-color accent` and `omarchy-hook-install`.
- **Skills:** also copy to `~/.gemini/config/skills` and `~/.hermes/skills`.
- **Routing:** `omarchy fabric status` cannot reach a command outside Omarchy's own `bin/`, so `omarchy-fabric-*` wrappers only work by full name until they live upstream.
- **Idle and lock:** upstream has no event for them (sleep has one, which the console now uses). A hook for lock, unlock and idle is a small contribution worth proposing.
- **Fit upstream:** a self-contained Python project in a top-level `fabric/` folder has no precedent in Omarchy. The closest one is `omarchy-local-ai`: engine in its own package, a `bin/` command and a shell plugin watching its `status.json` in the Omarchy tree.

## Built this round

- Principals: agent, person, rule (`model.Principal`, `policy.py`).
- The fast path: gestures, signals, rules with conditions, agent proposals with approval, a runtime store (`rules.py`).
- Rooms and presence, scoped per agent (`presence.py`); the desktop console vouches for its room while connected.
- Gestures from HA `event` entities, GPIO buttons and the simulator.
- CLI: `toggle`, `rooms`, `where`, `room`, `presence`, `emit`, `rules`, `rule`, `approval`, `do --me`, `sim gesture`.
- MCP tools: `list_rooms`, `where_is`, `list_rules`, `propose_rule`.
- 103 tests pass (37 from round 1, 39 for the defects above, 27 for people, rules and rooms). The daemon, console and CLI were also run end to end against the simulator on macOS.

## Not done

- `system-architecture.md` is not revised yet; the table of design issues above is the input for it. The original is untouched.
- The glue fixes listed above, and the `omarchy-fabric-*` commands.
- `README.md` and docs 02, 04, 05 and 06 still describe round 1 (test counts, scenarios, `ofab do` semantics).
- `examples/` and `deploy/pi5/node.toml` have no rules or presence examples.
- No independent review of this round's diff; not run on Python 3.11 (the Pi's version); nothing run on hardware or on Omarchy.
- Committed: round 2 is on `fabric/round-2` (`f5b8ba0`, `f2fb136`), under review as a PR into `fabric/main` in the fork. The B25–B32 fixes are on `fabric/round-3`, one commit each.
