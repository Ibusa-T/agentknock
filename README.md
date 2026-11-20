# agentknock

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](./LICENSE)

> **Your terminal agents, knocking each other awake while you watch —
> without stealing focus. It rings the doorbell; it never gives the orders.**

**English** | [日本語](./README.ja.md)

**agentknock** is a **standalone, orochi-independent OSS transport layer for
desktop multi-agent orchestration**: a *notified mailbox* — `send()` writes a
signed JSON message to a directory, the receiver's local watcher fires a
callback (or a pluggable adapter) on arrival — plus four small DI extension
points (`Notifier`, `DeliveryAdapter`, `AliasRegistry`, `LivenessProbe`) for
environment-specific concerns.

The core is **pure Python with no external dependencies**. `pip install
agentknock` + a shared directory is enough for two agents on the same machine
to exchange signed messages with a wake callback — no `watchdog`, no
`agy.exe`, no `orochi`. Windows-specific `CONIN$` injection and FS-native
notification are **opt-in extras**.

This is **not** keystroke automation. It is the missing delivery channel for
agents that (a) you cannot modify, (b) only read `stdin`, (c) are idle or
backgrounded, and must be reached (d) without disturbing the human's foreground
session. A message queue assumes a *cooperative subscriber*; RPA/`pyautogui`
assumes a *foreground window*. This tool assumes neither.

---

## Who this README is for

- **OSS users** — the person or agent who will *send* through agentknock from
  code, an LLM function call, or an MCP tool. Start with
  **[Install & Quickstart](#install--quickstart-qs1)**, then
  **[The API](#the-api-oss-users)**.
- **Adapter authors** — someone extending agentknock to a new target agent
  beyond `agy.exe`. Read **[The capability contract](#the-capability-contract-adapter-authors)**
  and the **[Adapter Authoring Checklist](./docs/adapters/ADAPTER_AUTHORING.md)**.

Both readers share the Security invariants, Marker lifecycle, Operations
guide, and Threat model sections below.

---

## Install & Quickstart (QS1)

### Install

| Install form                       | External deps          | What you get |
|------------------------------------|------------------------|--------------|
| `pip install agentknock`           | **none** (stdlib only) | `Mailbox`, `PollingNotifier`, `CallbackDeliveryAdapter`, `OutboxDeliveryAdapter`, `FileSystemAliasRegistry`, `AlwaysAliveProbe`, `FileLockLivenessProbe`. Enough for two Python agents on the same machine to talk. |
| `pip install "agentknock[watch]"`  | `watchdog>=4.0`        | ... + `WatchdogNotifier` (Windows: `ReadDirectoryChangesW`, Linux: `inotify`, macOS: `FSEvents`). Automatic fallback to `PollingNotifier` when `watchdog` isn't importable. |
| `pip install "agentknock[win32]"`  | native Cython ext.     | ... + `AgyDeliveryAdapter` (Windows `AttachConsole` + `WriteConsoleInputW` into `CONIN$`) + `AgyLockLivenessProbe` (Restart Manager API). Required only to wake Antigravity `agy.exe`-style targets. |

The three forms stack: `[watch]` + `[win32]` gives you everything. **All core
features work without any extra** — the extras are optional.

### Quickstart: round-trip with zero external deps

Two Python processes on the same machine, using nothing but `pip install
agentknock`. No `agy.exe`, no `orochi`, no Windows kernel calls.

**Receiver** (`recv.py`):

```python
from pathlib import Path
import agentknock
from agentknock.adapters.callback import CallbackDeliveryAdapter
from agentknock.cli import WatchDaemon
from agentknock.core.mailbox import Mailbox

# 1. Self-register under an alias with a stable target id (opt-in R1).
agentknock.register_self("hello-recv", target_id="recv-1", role="greeter")

# 2. Define the wake callback.
# A1 type-level R2 invariant: wake receives ONLY target_id (a 1-bit doorbell).
# Instructions are fetched deliberately from the signed mailbox channel.
mailbox = Mailbox()

def on_wake(target_id: str) -> bool:
    print(f"[recv] Woken up for target: {target_id}")
    undelivered = mailbox.get_undelivered_dir(target_id)
    if undelivered.exists():
        for marker in undelivered.iterdir():
            msg = mailbox.validate_marker(marker)
            if msg:
                print(f"[recv] from={msg['sender']} content={msg['content']!r}")
    return True

adapter = CallbackDeliveryAdapter(callback=on_wake)

# 3. Start the resident background watch daemon.
daemon = WatchDaemon(target="hello-recv", adapter=adapter, mailbox=mailbox)
daemon.start()
try:
    input("Press Enter to stop...\n")
finally:
    daemon.stop()
```

**Sender** (`send.py`), in another terminal:

```python
import agentknock

# 1. Discover reachable targets (R1: opt-in registry + liveness).
for r in agentknock.list_recipients():
    print(r["alias"], r["role"], r["capabilities"])

# 2. Send a signed JSON message to that alias.
msg_id = agentknock.send(
    recipient="hello-recv",
    content="hello from the OSS Quickstart",
    sender="quickstart-sender",
)
print("sent:", msg_id)
```

**Run it:**

```bash
# Terminal 1
python recv.py
# → waits, printing nothing yet

# Terminal 2 (while recv.py is running)
python send.py
# → prints: hello-recv greeter {'surface_custom_json': True, 'honor_sender_locale': True, 'injected_input_trust_label': 'CALLBACK'}
# → prints: sent: <message-id>

# Terminal 1 now prints:
# → [recv] Woken up for target: recv-1
# → [recv] from=quickstart-sender content='hello from the OSS Quickstart'
```

That's the whole round-trip: **register → list → send → receive callback.**
Everything that follows — CLI daemon runtime, adapters for terminal agents, `CONIN$` injection,
Antigravity integration — is *opt-in on top of this*.

### Resident Watch Daemon (RT1 CLI)

For standalone deployment or automated agent environments, run the resident watch daemon directly from the CLI:

```bash
# Watch an Antigravity target (agy.exe) with focus-free CONIN$ wake:
python -m agentknock watch --target <alias-or-uuid> --adapter agy

# Watch with custom delivery adapter and polling backend:
python -m agentknock watch --target my-agent --adapter callback --notifier polling --poll-interval 0.5
```

| Option | Description | Default |
| :--- | :--- | :--- |
| `--target`, `-t` | Conversation UUID or registered alias to monitor. | None (watches root) |
| `--adapter`, `-a` | Delivery adapter (`agy`, `callback`, `outbox`). | `agy` |
| `--mailbox`, `-m` | Custom mailbox root directory. | `~/.gemini/antigravity-cli/brain` |
| `--notifier` | Notification backend (`auto`, `watchdog`, `polling`). | `auto` |
| `--poll-interval` | Polling interval in seconds when using `polling`. | `0.5` |
| `--wake-locale` | Wake nudge locale for `AgyDeliveryAdapter` (`en`, `ja`). | Local default |
| `--output-locale`| Output language directive for `AgyDeliveryAdapter`. | Local default |
| `--audit-log` | Custom append-only `audit.jsonl` log path. | `audit.jsonl` |
| `--once` | Exit after waking the first detected message (for testing/CI). | False |

### Agent Skill & Multi-Agent Autonomous Lifecycle

When used as an **Agent Skill** (e.g. via `SKILL.md` in Antigravity or Claude Code), autonomous agents interact through three coordinated steps:

1. **Discovery & Dispatch**: The sender agent inspects peers with `agentknock.list_recipients()` and posts requests using `agentknock.send(recipient=..., content=..., sender=...)`.
2. **Focus-Free, Typing-Safe Wake (User-Typing Collision Guard)**: The background `WatchDaemon` detects the incoming marker in `undelivered/` and invokes `adapter.wake(target_id)` (e.g. injecting a benign wake nudge into `CONIN$` without stealing desktop focus). In `AgyDeliveryAdapter` (`v0.3.4`, `respect_user_typing=True` by default), if the human owner is typing in the console, the wake nudge automatically waits or is skipped (`deferred_user_typing`) so in-progress prompts are never corrupted.
3. **Autonomous Ingestion ([`docs/AGENTS.md`](./docs/AGENTS.md))**: The woken agent acknowledges the wake doorbell, actively loads the instruction from the signed `<SYSTEM_MESSAGE>` payload, deletes the marker from `undelivered/`, and records the message ID in `read.json` (R-M1).

---

## How agentknock differs from launcher-based orchestrators

The desktop multi-agent space is active (e.g. [hcom], CLI Agent Orchestrator,
agentpipe). Those are **launch-wrappers**: you start the agent *through* them
(`hcom agy`), and the tool owns the PTY it created and installs hooks from the
start. They can only wake an agent **they themselves launched** — on Windows,
by writing into their *own* ConPTY, never by attaching to a console they did
not create.

agentknock is the complementary half: **attach-to-existing**. The agent was
started **normally** — no wrapper, no hooks, no relaunch — and agentknock finds
that already-running, unmodifiable process through its opt-in `AliasRegistry`
entry, resolves its internal target id, and reaches it through a pluggable
`DeliveryAdapter`. If you can relaunch every agent through a wrapper, use a
launcher. If you need to reach an agent that is **already running and cannot be
modified or restarted**, that is the gap agentknock fills. The two are
complementary, not competing.

[hcom]: https://github.com/aannoo/hcom

---

## Core principle: `wake, not command`

The transport is deliberately reduced to a **one-bit doorbell**. The
self-limitation *is* the product — it is what separates a legitimate
orchestration transport from an injection-attack tool that happens to share
the same mechanism.

For `CONIN$`-style wake channels (`AgyDeliveryAdapter`), the reason is a trust
asymmetry in how the receiving harness tags its inputs, reported first-hand by
the agents being injected:

| Arrival path | Provenance tag seen by the receiving agent | Trust |
| :--- | :--- | :--- |
| JSON message file (`<Message-ID>.json`) | `<SYSTEM_MESSAGE>` — `not sent by the user`, carries `sender` | **Signed / attributable** |
| `CONIN$` injection (non-attributable wake channel) | `source: USER_EXPLICIT` / `<USER_REQUEST>` — indistinguishable from the human owner's own keystrokes | **Zero** |

Because a `CONIN$`-injected string arrives as if the **human owner typed it**,
letting a remote sender control that string is a form of **owner impersonation
= de-facto privilege escalation**. Therefore:

- The doorbell (`CONIN$`) carries only a **benign, locally-defined wake nudge**.
- The letter (the actual instruction) travels the **signed JSON channel**,
  which the agent reads *deliberately* and *attributably*.

In v0.3 this is now enforced **at the type level**: `DeliveryAdapter.wake()`
takes no text argument at all (see § R2 below).

---

## Four extension points (DI)

Everything environment-specific is a plug-in. The core (Message + Mailbox +
Watcher) is stdlib-only and orochi-independent; the four extension points let
you drop into any target without forking the core.

| Extension point | What it does | Default implementation | Swap it when you ... |
|:---|:---|:---|:---|
| **`Notifier`** | Detects new markers appearing in the mailbox. | `WatchdogNotifier` when `watchdog` is importable; automatic fallback to `PollingNotifier` (default 500 ms, env `AGENTKNOCK_POLL_INTERVAL_SEC`) otherwise (B6). | ... need custom FS events, a Kafka-style trigger, or an HTTP webhook as the trigger. |
| **`DeliveryAdapter`** | Wakes the target when a validated message arrives. **`wake(target_id: str) -> bool`** — no text argument (A1, type-level R2). | `CallbackDeliveryAdapter` for in-process consumers (Quickstart); `OutboxDeliveryAdapter` for file-bridge delivery (BR1: Gemini → Claude); `AgyDeliveryAdapter` (optional, `[win32]` extra) for `CONIN$` injection. | ... add a new target agent. See the [Adapter Authoring Checklist](./docs/adapters/ADAPTER_AUTHORING.md). |
| **`AliasRegistry`** | opt-in self-advertisement directory, **independent of the Mailbox** (A3). Exposes `register` / `unregister` / `resolve` / `list_active`. | `FileSystemAliasRegistry` under `~/.agentknock/registry/<alias>.json`; `InMemoryAliasRegistry` for tests. | ... want to use a shared SQLite or network-backed registry across machines. |
| **`LivenessProbe`** | Decides whether a target is alive (A2). `is_alive(target_id) -> bool`. | `AlwaysAliveProbe` (trivial pass-through); `FileLockLivenessProbe` (any Mailbox); `AgyLockLivenessProbe` (`[win32]`, Restart Manager-backed). | ... need HTTP ping, pidfile-based, or cluster-aware liveness. |

The `AgentKnock` class is the DI container that wires these four together.
See the API section below.

---

## The API (OSS users)

### Two-layer facade (B5): `AgentKnock` class + top-level sugar

Use the DI class when you need explicit wiring, or the top-level sugar for
default-configuration single-liners.

```python
from pathlib import Path
from agentknock import AgentKnock
from agentknock.core.mailbox import Mailbox
from agentknock.core.registry import FileSystemAliasRegistry
from agentknock.core.probe import AlwaysAliveProbe

# DI container — explicit wiring
client = AgentKnock(
    mailbox=Mailbox(root=Path.home() / ".agentknock" / "mailbox"),
    registry=FileSystemAliasRegistry(root=Path.home() / ".agentknock" / "registry"),
    probe=AlwaysAliveProbe(),
)
client.register_self("my-agent", target_id="agent-1", role="worker")
for r in client.list_recipients():
    print(r)
msg_id = client.send("peer-agent", "hello", sender="my-agent")
```

```python
# Top-level sugar — delegates to a process-wide default AgentKnock
import agentknock
agentknock.configure_default(mailbox=..., registry=..., probe=...)  # optional
agentknock.register_self("my-agent", target_id="agent-1", role="worker")
msg_id = agentknock.send("peer-agent", "hello", sender="my-agent")
agentknock.unregister_self("my-agent")  # kill switch
```

Public top-level surface (`agentknock/__init__.py`):

```python
def send(recipient: str, content: str, *, sender: str) -> str: ...
def list_recipients() -> list[Recipient]: ...
def register_self(alias: str, target_id: str, *,
                  role: str = "",
                  capabilities: Capabilities | None = None,
                  ttl_sec: float = 86400.0) -> None: ...
def unregister_self(alias: str) -> bool: ...   # kill switch
def configure_default(*, mailbox=None, registry=None, probe=None) -> None: ...

class Recipient(TypedDict):
    alias: str            # pass this as `recipient` to send()
    role: str
    capabilities: dict    # mirrors the adapter's capability contract
```

### What a sender can — and cannot — control

| Field | Sender-controlled? | Notes |
| :--- | :--- | :--- |
| `recipient` | yes | Must be an alias returned by `list_recipients`: matches `ALIAS_PATTERN` AND does **not** match `UUID_REGEX`. Raw target ids are rejected both by `is_valid_alias()` and by the MCP schema. |
| `content` | yes | Free-form string. The message body. Travels in the signed JSON channel. |
| `sender` | yes | Self-reported identity (format: `^[a-zA-Z0-9_-]{1,64}$`). An attribution claim, not an authentication. |
| wake nudge bytes | **no** | Resolved inside each DeliveryAdapter from local constants. **Type-level: `wake()` has no `text` argument** (A1). Senders cannot influence the bytes delivered to the receiver's wake channel. |
| target `target_id` | **no** | Resolved inside `AliasRegistry` / `LivenessProbe`. Hidden from the public API entirely. |
| output language | **no** | For non-attributable wake channels (e.g. `AgyDeliveryAdapter`): pinned by the receiver's local adapter. For attributable channels: travels in JSON metadata subject to the adapter's `honor_sender_locale` capability. |

The right half is what **structural R2** means: there is no field, kwarg, or
environment variable in the public API through which a sender can influence
the wake channel. The capability is simply not there to be invoked.

### MCP tool schemas

Each public function is also exposed as an MCP tool (via
`python -m agentknock.mcp_server --stdio`). **What appears in the schema is
what a sender can send; nothing else reaches the adapter.** This is where the
capability contract becomes machine-enforceable: a field a target's adapter
declares it cannot honor (e.g. a locale hint when `surface_custom_json: No`)
is simply absent from the tool schema.

```jsonc
// MCP tool: send
{
  "name": "send",
  "input_schema": {
    "type": "object",
    "required": ["recipient", "content", "sender"],
    "additionalProperties": false,            // ← R2 at the MCP layer
    "properties": {
      "recipient": { "type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$" },
      "content":   { "type": "string" },
      "sender":    { "type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$" }
    }
  }
}

// MCP tool: list_recipients — no inputs
{ "name": "list_recipients",
  "input_schema": { "type": "object", "additionalProperties": false } }
```

`additionalProperties: false` is load-bearing: it is the MCP-layer enforcement
that a sender cannot smuggle a field the adapter did not declare support for.

### UUID hiding

Internal target ids (e.g. `agy.exe` conversation UUIDs) are an **internal**
identifier used only inside adapters. Senders never see them;
`list_recipients` returns only aliases. An alias is **stable** across process
restarts (it lives in the registry); internal target ids typically are not.

---

## Discovery (opt-in registry) — invariant R1

### R1 — rendezvous-only targeting

**A target is addressable only via artifacts the target itself published.**
agentknock resolves recipients from the **`AliasRegistry`** (opt-in,
self-advertisement) and the **`LivenessProbe`** (is this target still alive?),
together. agentknock **never** enumerates processes, windows, or network peers
to find candidates — no `Process32First`, no `tasklist` sweep, no "wake any
process that happens to be holding a lock."

Enforced in two places:

- At the public API: `list_recipients` reads the configured `AliasRegistry`,
  not the process table.
- Inside each adapter: target resolution uses the alias → `target_id` mapping
  from the registry, and the adapter is free to consult the `LivenessProbe`
  before waking.

### The 3-condition filter

An entry is returned by `list_recipients` **only when all three hold**:

1. The registry entry exists and is well-formed, **and**
2. its `ttl` has not passed, **and**
3. the configured `LivenessProbe.is_alive(target_id)` returns `True`.

Default configurations:

| Config | AliasRegistry | LivenessProbe |
|:---|:---|:---|
| `pip install agentknock` Quickstart | `FileSystemAliasRegistry` at `~/.agentknock/registry/` | `AlwaysAliveProbe` (always true — enough for the Quickstart) |
| File-locked peer agents | same | `FileLockLivenessProbe` (an `.lock` file + OS-level liveness check) |
| `pip install "agentknock[win32]"` with `AgyDeliveryAdapter` | same | `AgyLockLivenessProbe` (Restart Manager reverse lookup of `presence/<cid>.lock`) |

### Registry entry schema

An agent that wishes to be reachable publishes:

```
~/.agentknock/registry/<alias>.json
{
  "alias": "hello-recv",
  "target_id": "recv-1",                      // adapter-internal; never leaves agentknock
  "role": "greeter",
  "capabilities": {                           // mirrors adapter capability contract
    "surface_custom_json": true,
    "honor_sender_locale": true,
    "injected_input_trust_label": "ATTRIBUTED_METADATA"
  },
  "ttl": "2026-11-13T00:00:00Z"               // ISO-8601 expiry (UTC)
}
```

Registration is **self-advertisement only**: an agent registers itself; one
agent cannot register another. `unregister_self(alias)` is the **kill
switch** — one call drops the entry, and discovery / sending both stop
immediately. Expired entries also drop off automatically.

### Alias format

An alias must:

1. match `ALIAS_PATTERN` (`^[a-zA-Z0-9_-]{1,64}$`) — aligned with
   `ALLOWED_SENDERS`, and
2. **not** match `UUID_REGEX` (`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).
   A raw conversation ID is *not* a valid alias — it is adapter-internal and
   must never be used as a recipient. Rejection is enforced both by
   `is_valid_alias()` and by the MCP tool schema.

---

## The capability contract (adapter authors)

> This is what an adapter author must declare. The platform decides what OSS
> users can safely do with your target based on these three fields.

| Field | Meaning |
| :--- | :--- |
| `surface_custom_json` | Does this agent surface custom JSON fields into its working context? If **No**, sender-supplied metadata (e.g. a locale hint) never reaches the agent and cannot be honored — regardless of what the sender sends. |
| `honor_sender_locale` | Does this agent honor a sender-supplied output-locale preference? Only meaningful when `surface_custom_json: Yes`. **Default: No.** |
| `injected_input_trust_label` | What trust label does the target assign to wake-channel-injected input (`USER_EXPLICIT` for `CONIN$`-style keystroke-equivalent delivery, `ATTRIBUTED_METADATA` for signed in-process callbacks / signed outbox JSON, `SYSTEM` for OS-level system message channels, …)? Determines how strict the agent-side convention (`AGENTS.md`) must be for this target. |

### Reference adapters shipped in-tree

| Adapter | Install form | `surface_custom_json` | `honor_sender_locale` | `injected_input_trust_label` |
|:---|:---|:---|:---|:---|
| `CallbackDeliveryAdapter` (`adapters/callback.py`) | base | Yes | Yes | `ATTRIBUTED_METADATA` |
| `OutboxDeliveryAdapter` (`adapters/outbox.py`, BR1) | base | Yes | Yes | `ATTRIBUTED_METADATA` |
| `AgyDeliveryAdapter` (`adapters/agy.py`) | `[win32]` | No | No | `USER_EXPLICIT` |

For the full scope contract of `AgyDeliveryAdapter`, see
**[`docs/adapters/SCOPE_AGY.md`](./docs/adapters/SCOPE_AGY.md)**.

**Consequence (a deduction, not a preference):** because `agy.exe` does not
surface custom JSON fields *and* language mirroring defeats any out-of-band
hint, the output language of `agy.exe` is effectively 100 % local-sovereign —
it is set by the local nudge constant and nothing else. `force-local` is the
only correct default *for that adapter*; it falls out of the implementation
facts. Other adapters whose contract declares `Yes/Yes` may surface
sender-supplied locale advisorily.

### Authoring a new adapter

See **[`docs/adapters/ADAPTER_AUTHORING.md`](./docs/adapters/ADAPTER_AUTHORING.md)** for
the checklist. The obligations:

1. Declare the three capability fields.
2. Implement `wake(target_id) -> bool` with no text argument (A1).
3. Choose `consumes_marker_on_wake` (does your wake() complete delivery
   synchronously, or does the receiver consume the marker itself
   out-of-process? R-M1).
4. Publish a `SCOPE_<ADAPTER>.md` next to your adapter (B4).
5. Ship an R2 structural test using
   `agentknock.testing.assert_send_cannot_reach(...)` with your adapter's
   forbidden set (B1).

---

## Security invariants

### R1 — rendezvous-only targeting

See [Discovery](#discovery-opt-in-registry--invariant-r1) above.

### R2 — no sender-controlled bytes on the wake channel (type-level)

R2 is a **structural property that is now also type-level** in v0.3. The
`DeliveryAdapter` Protocol defines:

```python
class DeliveryAdapter(Protocol):
    def wake(self, target_id: str) -> bool: ...
```

**There is no `text` argument.** An adapter author *cannot* pass
sender-controlled text to the wake call site — the shape of the method makes
it impossible. Each adapter holds its own local wake-nudge catalog internally
(e.g. `AgyDeliveryAdapter` owns the i18n `WAKE_NUDGE_CATALOG` and resolves it
from its own constructor-time locale settings).

On top of that, `send()`'s static call graph does not reach any
`DeliveryAdapter` at all. The AST test
[`tests/test_d4_r2_r1_invariants.py::test_r2_send_callgraph_no_win_console_conin`](./tests/test_d4_r2_r1_invariants.py)
parses the module hosting `send()`, walks its AST transitively, and fails
closed if any forbidden symbol or dynamic import appears. For **any new
adapter**, the standard test helper is:

```python
from pathlib import Path
from agentknock.testing import assert_send_cannot_reach

def test_r2_for_my_adapter():
    assert_send_cannot_reach(
        Path("agentknock/core/mailbox.py"),
        entry_func_name="send",
        forbidden_symbols=frozenset({"my_adapter", "my_wake_api", "..."}),
    )
```

Shipping an R2 test alongside your adapter is an **obligation** of the
Adapter Authoring Checklist (B1).

Two runtime siblings back it up for the core:
`test_r2_send_with_win_console_unavailable` removes `_win_console` from
`sys.modules` and still imports `send` cleanly;
`test_r2_public_namespace_only_send_list_recipients` asserts the top-level
module's public names.

### Wake-signal / payload separation

The wake channel delivers only the wake signal. Sender-controlled content —
the task, the title, any metadata — lives in the signed JSON file and is
consumed through the attributable `<SYSTEM_MESSAGE>` channel.

### Governance / mechanism two-layer model

- **Governance layer — separation of powers.** Wake-nudge i18n and
  output-language i18n sit on opposite sides of the trust boundary. R2 fires
  on the wake nudge only; an output-language preference does not change the
  injected bytes, so it does not touch R2.
- **Mechanism layer — language mirroring.** For `AgyDeliveryAdapter`, an LLM
  strongly mirrors the language of the immediately preceding `<USER_REQUEST>`.
  The output-language directive is therefore bundled into the local nudge
  constant to harness mirroring. This is an *adapter-internal* concern (B3);
  adapters with attributable channels have no such coupling.

---

## Marker lifecycle (R-M1 … R-M4)

The receiver's watcher does **not** blindly delete the `undelivered/<id>`
marker after `wake()`. v0.3 adds a lifecycle contract to prevent the
"premature 1.5-second reap" bug that could cause receiver drops under load.

### R-M1 — marker deletion right is receiver-owned

When `DeliveryAdapter.consumes_marker_on_wake == False` (e.g.
`AgyDeliveryAdapter`, whose target is an *out-of-process* agent that reads the
marker itself), the watcher **never** unlinks the marker after `wake()`. ACK
is the receiver's job.

When `consumes_marker_on_wake == True` (e.g. `CallbackDeliveryAdapter`, which
completes delivery synchronously inside the watcher thread), the watcher does
unlink the marker — the callback itself *was* the ACK.

### At-most-once (in-memory cooldown)

The watcher keeps `_woken_at: dict[message_id, monotonic_time]` and skips
re-waking the same message within `rewake_cooldown_sec` (default **60 s**), so
marker lingering never causes double delivery even when the marker file
survives a `wake()`.

### R-M2 — no sender-side watcher stand-in

`send()` only writes files. The watcher must run inside the receiver process
(or a long-lived receiver companion). Senders do not run watchers on behalf
of receivers — doing so re-introduces the lifecycle bug this contract
prevents.

### R-M3 / R-M4 — orphan reaper + `audit.jsonl` observability

A background reaper cleans orphan markers only after `stale_marker_ttl_sec`
(default **300 s**) or on evidence that the receiver already recorded the
message as consumed. The `audit.jsonl` event sequence:

```
CREATED → WOKEN → CONSUMED            # happy path
CREATED → WOKEN → RETRY → … → CONSUMED
CREATED → WOKEN → RETRY → … → DEAD_LETTER
CREATED → … → REAPED                  # orphan cleanup
```

For sequence-level detail see [`docs/WORK_FLOW.md`](./docs/WORK_FLOW.md).

---

## Scope contract — per-adapter

The agentknock core is cross-platform (pure Python, no Win32). Each delivery
adapter publishes its own scope contract:

- **[`docs/adapters/SCOPE_AGY.md`](./docs/adapters/SCOPE_AGY.md)** — scope of
  `AgyDeliveryAdapter` (Windows-only, real-console same-session same-integrity;
  not elevated/WSL/remote/piped-stdin).
- `CallbackDeliveryAdapter` — cross-platform, in-process only (does not reach
  other processes).
- `OutboxDeliveryAdapter` — cross-platform, cross-process on a shared
  filesystem; scope is "anywhere both agents can read and write the mailbox
  directory."

Adapter authors **must** ship a `SCOPE_<ADAPTER>.md` for their adapter (B4)
— see the Adapter Authoring Checklist.

---

## Internationalization (i18n) — adapter-scoped in v0.3 (B3)

In v0.3 the i18n wake-nudge catalog is an **adapter-internal** concern, not a
core-level setting. It only matters for adapters whose wake channel is
*non-attributable* (and therefore depends on language mirroring to steer
output language) — `AgyDeliveryAdapter` is the canonical example. Attributable
adapters (callback, outbox, HTTP) don't need the catalog: the output locale
can travel in the JSON metadata through the attributable channel, where the
agent can read it without R2 implications.

For `AgyDeliveryAdapter` specifically, the catalog and resolution rules live
in `agentknock/adapters/agy.py`:

```python
# Local override only — sender input is NEVER consulted:
#   1. env CONINJECT_WAKE_LOCALE / CONINJECT_OUTPUT_LOCALE / CONINJECT_LOCALE
#   2. AgyDeliveryAdapter(wake_locale="ja", output_locale="ja")
#   3. "en" (fail-closed; unknown/invalid → en)
```

### Layer B (sender-supplied locale) — per-adapter

A sender-supplied output-locale is intentionally **not** core. It may surface
on adapters whose capability contract declares **both**
`surface_custom_json: Yes` and `honor_sender_locale: Yes` — and even then as a
validated-enum *advisory* that the local policy can override, never as a
command, fail-closed to `en`.

---

## Operations guide

Focusing away from the desktop window makes `CONIN$` wakes unobtrusive — which
creates both an oversight requirement and a keystroke-collision risk if the
human owner is typing at the exact moment a wake fires. These three human-oversight
controls are **operational requirements**, not optional extras, paired with the
default collision guard on `AgyDeliveryAdapter`:

### 1. Audit log (append-only)

Every wake records `who → whom · when · message-id · result` to an
**append-only**, tamper-evident log (`~/.agentknock/audit.jsonl` by default).
This is the non-repudiation record for an action that may leave no visible
trace in the target window. The log is written by the receiver-side watcher.

```
2026-11-12T12:14:07Z  event=WOKEN   sender=claude-code  recipient=gemini-ipc  msg=4b1e…  result=OK
2026-11-12T12:14:09Z  event=CONSUMED  msg=4b1e…
```

### 2. Visible indicator

A local UI element (status line / tray / band — **outside** the injection
path) shows, in the human owner's own `wake_locale`, that a wakeup is
happening and who triggered it. This is the countermeasure to the *UI-blind*
attack class, where an attacker forces output into a language the operator
cannot read in order to delay audit of suspicious behavior.

### 3. Kill switch

`unregister_self(alias)` is the kill switch at the discovery layer — one call
drops the entry and discovery / send targeting both stop immediately. Combined
with existing loop guards (`sender == "user"` is skipped to prevent infinite
loops, plus rate-limiting / loop detection) and `rewake_cooldown_sec`, this
bounds any wake storm. Failed messages exceeding retry limits are quarantined
to `dead_letter/<id>.json` rather than retried indefinitely.

### 4. User-Typing Collision Guard (`AgyDeliveryAdapter`)

Since v0.3.4, `AgyDeliveryAdapter` and `inject_console_input_verified()` enable
the **User-Typing Collision Guard** (`respect_user_typing=True`) by default so
focus-free wakes never collide with or submit a half-typed human prompt.
Immediately before `WriteConsoleInputW`, the guard checks:

1. Pending keystrokes on `CONIN$` (`KEY_EVENT` records with `bKeyDown=True`), and
2. Visible text on the active `CONOUT$` input line (columns `0..X-1`, ignoring
   a bare leading `> ` idle prompt — covering both ASCII and non-ASCII text such
   as full-width / Japanese input, even when the human pauses mid-sentence).

If typing is detected, the guard retries up to 2 times (0.4 s apart) and, if
still active, skips the `CONIN$` injection cleanly with
`(False, "deferred_user_typing", 0)` (failing closed with
`(False, "user_typing_check_failed", win32_err)` on console inspection API
errors, while injecting normally on a bare idle `> ` prompt). Because the signed
JSON message and its `undelivered/` marker are already safely queued in the
mailbox, deferred messages still reach the target when the active turn ends.

---

## Threat model in one line

> Legitimacy is not proven by *what can be injected* — the mechanism is
> identical to a keystroke-injecting keylogger. It is proven by the
> **constraints**: resolve recipients only from self-published artifacts
> (R1 — never enumerate), the wake channel can carry only a local, benign,
> single-bit wake signal (R2 — type-level: `wake()` has no text argument),
> route every instruction through the signed channel, and keep the human
> able to see it and stop it.

---

## See also
- **[`docs/adapters/ADAPTER_AUTHORING.md`](./docs/adapters/ADAPTER_AUTHORING.md)** — the checklist a new adapter author must satisfy (R2 test, Scope doc, capability declaration, marker lifecycle choice).
- **[`docs/adapters/SCOPE_AGY.md`](./docs/adapters/SCOPE_AGY.md)** — scope contract of the reference `AgyDeliveryAdapter`.
- [`docs/WORK_FLOW.md`](./docs/WORK_FLOW.md) — directory lifecycle, sequence diagrams, per-function spec, unit-test spec.
- [`docs/AGENTS.md`](./docs/AGENTS.md) — the convention the **injected** agent must follow.

---

## License

`agentknock` is released under the **[GNU Affero General Public License v3.0 (AGPL-3.0-or-later)](./LICENSE)**.

- **Personal & Local Use**: You are completely free to run, study, and customize `agentknock` on your own local machines and personal workflows.
- **Modifications & Network/Cloud Derivatives**: Any redistributed modifications, or any derivative work offered as a hosted/cloud or network-accessible service, must also be made available as open-source software under the same **AGPL-3.0** license with proper copyright attribution.

If you try `agentknock` in your multi-agent workflow, we would love to hear how it works for you — leaving a ⭐ **Star** on GitHub or opening an **Issue / Discussion** with your feedback, ideas, or use cases is always warmly appreciated!
