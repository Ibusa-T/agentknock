# agentknock

> **Your terminal agents, knocking each other awake while you watch —
> without stealing focus. It rings the doorbell; it never gives the orders.**

**English** | [日本語](./README.ja.md)

**agentknock** is a **transport layer for desktop multi-agent orchestration**.
A sender agent calls `send(recipient, content, sender=...)`; the receiver's
local watcher wakes the target CLI/TUI AI agent (e.g. Antigravity `agy.exe`,
and — via adapters — other terminal agents) by injecting a keystroke into the
target process's console input buffer (`CONIN$`) through the Windows kernel
API (`AttachConsole` + `WriteConsoleInputW`). The target's window never gains
focus, so the human owner keeps working undisturbed. The destination process
is resolved **by PID**, reverse-looked-up from a presence lock the target
itself published — never by window title, never by process enumeration.

This is **not** keystroke automation. It is the missing delivery channel for
agents that (a) you cannot modify, (b) only read `stdin`, (c) are idle or
backgrounded, and must be reached (d) without disturbing the human's foreground
session. A message queue assumes a *cooperative subscriber*; RPA/`pyautogui`
assumes a *foreground window*. This tool assumes neither.

---

## Who this README is for

This document addresses **two distinct readers**, with one section targeted at
each. Pick yours:

- **OSS users** — the person or agent who will *send* through agentknock from
  an LLM function call or an MCP tool. Read **[The API (for OSS users)](#the-api-for-oss-users)**.
  Everything you can do is in one tool schema; anything not in it, you cannot
  do.
- **Adapter authors** — someone extending agentknock to a new target agent
  beyond `agy.exe`. Read **[The capability contract (for adapter authors)](#the-capability-contract-for-adapter-authors)**.
  Your adapter declares three load-bearing fields that decide what the platform
  can safely let OSS users do with your target.

Both readers share the Security invariants, Scope contract, i18n, Operations
guide, and Threat model sections below.

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
that already-running, unmodifiable process by its presence lock, resolves its
PID, and injects into its *existing* console input buffer. If you can relaunch
every agent through a wrapper, use a launcher. If you need to wake an agent that
is **already running and cannot be modified or restarted**, that is the gap
agentknock fills. The two are complementary, not competing.

[hcom]: https://github.com/aannoo/hcom

For the end-to-end sequence and per-function reference, see
[`WORK_FLOW.md`](./WORK_FLOW.md). For the convention the **injected** agent must
follow, see [`AGENTS.md`](./AGENTS.md). For the architectural decision record
behind the current shape, see
[`../orochi/DESIGN-0001-agentknock-positioning.md`](../orochi/DESIGN-0001-agentknock-positioning.md).

---

## Core principle: `wake, not command`

The transport is deliberately reduced to a **one-bit doorbell**. The
self-limitation *is* the product — it is what separates a legitimate
orchestration transport from an injection-attack tool that happens to share the
same mechanism.

The reason is a trust asymmetry in how the receiving harness tags its inputs,
reported first-hand by the agents being injected:

| Arrival path | Provenance tag seen by the receiving agent | Trust |
| :--- | :--- | :--- |
| JSON message file (`<Message-ID>.json`) | `<SYSTEM_MESSAGE>` — `not sent by the user`, carries `sender` | **Signed / attributable** |
| `CONIN$` injection (this tool) | `source: USER_EXPLICIT` / `<USER_REQUEST>` — indistinguishable from the human owner's own keystrokes | **Zero** |

Because a `CONIN$`-injected string arrives as if the **human owner typed it**,
letting a remote sender control that string is a form of **owner impersonation
= de-facto privilege escalation**. Therefore:

- The doorbell (`CONIN$`) carries only a **benign, locally-defined wake nudge**.
- The letter (the actual instruction) travels the **signed JSON channel**, which
  the agent reads *deliberately* and *attributably*.

---

## The API (for OSS users)

> This is everything an OSS user — a person, an agent, or an MCP client — can
> do with agentknock. **Two functions. One rule: what is not in this schema
> cannot be sent.**

### Public surface

Only these two names are exported from `agentknock`. Both are also exposed as
MCP tools (`send` / `list_recipients`) for LLM harness integration. The
authoritative stubs live in `agentknock/_contract.py`.

```python
# agentknock/__init__.py

def send(recipient: str, content: str, *, sender: str) -> str:
    """Deliver a signed message to `recipient` (an alias from list_recipients).
    Returns the message-id. Internally, `send()` resolves the alias through
    the **L2 pure function** `_resolve_alias_from_registry` (a plain file
    read — no Win32, no adapter import) and then writes to the mailbox. Its
    static call graph does not reach `_adapter` / `_win_console` / CONIN$ /
    PID / inject — see Security invariants § R2."""

def list_recipients() -> list[Recipient]:
    """Opt-in discovery only (see R1). Returns alias-addressable recipients
    whose registry entry is unexpired AND whose presence lock is live.
    Never returns conversation_id."""

class Recipient(TypedDict):
    alias: str            # pass this as `recipient` to send()
    role: str
    capabilities: dict    # mirrors the adapter's capability contract
```

### What a sender can — and cannot — control

| Field | Sender-controlled? | Notes |
| :--- | :--- | :--- |
| `recipient` | yes | Must be an alias returned by `list_recipients`: matches `ALIAS_PATTERN` AND does **not** match `UUID_REGEX` (raw conversation IDs are rejected both by `is_valid_alias()` and by the MCP schema). |
| `content` | yes | Free-form string. The message body. Travels in the signed JSON channel. |
| `sender` | yes | Self-reported identity (format: `^[a-zA-Z0-9_-]{1,64}$`). An attribution claim, not an authentication. |
| wake nudge string | **no** | Resolved from the receiver's local `WAKE_NUDGE_CATALOG`. Senders cannot influence the bytes written to `CONIN$`. |
| target PID | **no** | Resolved inside the adapter from the recipient's own presence lock. Senders never see PIDs. |
| target `conversation_id` | **no** | Internal to adapters. Hidden from the public API entirely. |
| output language | **no** | Pinned by the receiver's local nudge. See [i18n § Layer B deletion](#layer-b-sender-supplied-locale--intentionally-absent-in-v1). |

The right half of that table is what **structural R2** means: there is no
field, kwarg, or environment variable in the public API through which a sender
can influence `CONIN$`. The capability is simply not there to be invoked.

### MCP tool schemas

Each public function is also exposed as an MCP tool. **What appears in the
schema is what a sender can send; nothing else reaches the adapter.** This is
where the capability contract becomes machine-enforceable: a field a target's
adapter declares it cannot honor (e.g. a locale hint when
`surface_custom_json: No`) is simply absent from the tool schema in the first
place.

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
{ "name": "list_recipients", "input_schema": { "type": "object", "additionalProperties": false } }
```

`additionalProperties: false` is load-bearing: it is the MCP-layer enforcement
that a sender cannot smuggle a field the adapter did not declare support for.

### UUID hiding

Conversation IDs are an **internal** identifier used only inside adapters.
Senders never see them; `list_recipients` returns only aliases. An alias is
**stable** across process restarts (it lives in the registry file); a
conversation ID is not. This decouples the sender from the receiver's lifecycle
and gives R1's "self-published rendezvous" principle a stable public address.

---

## Discovery (opt-in registry) — invariant R1

### R1 — rendezvous-only targeting

**A target is addressable only via artifacts the target itself published.**
agentknock resolves recipients from two of those, together: its **opt-in
registry entry** (alias → capabilities) and its **presence lock** (liveness).
agentknock **never** enumerates processes to find candidates. There is no
`Process32First`, no `tasklist` sweep, no "wake any window that happens to be
holding a presence lock."

This is R1, and it is enforced in two places:

- At the public API: `list_recipients` reads the registry directory — not the
  process table. A process that has not registered is not addressable.
- Inside the adapter: PID resolution uses the target's own presence lock
  (reverse-looked-up by path), not a scan.

### Registry schema

An agent that wishes to be reachable publishes:

```
~/.gemini/antigravity-cli/registry/<alias>.json
{
  "alias": "gemini-ipc",                      // ^[a-zA-Z0-9_-]{1,64}$
  "conversation_id": "<uuid>",                // adapter-internal; never leaves agentknock
  "role": "IPC core",
  "capabilities": {                           // mirrors adapter capability contract
    "surface_custom_json": false,
    "honor_sender_locale": false,
    "injected_input_trust_label": "USER_EXPLICIT"
  },
  "ttl": "2026-11-09T00:00:00Z"               // ISO-8601 expiry (UTC)
}
```

Registration is **self-advertisement only**: an agent registers itself; one
agent cannot register another. Expired entries drop off automatically.

### `list_recipients` filters

An entry is returned **only when all three hold**:

1. The registry file exists and is well-formed, **and**
2. `ttl` has not passed, **and**
3. The presence lock named by its `conversation_id` is held by a live process.

A registry entry alone is not addressable. A presence lock alone (unregistered)
is not addressable. The rendezvous requires both.

### Alias format

An alias must:

1. match `ALIAS_PATTERN` (`^[a-zA-Z0-9_-]{1,64}$`) — aligned with the
   `ALLOWED_SENDERS` format so that sender attribution and recipient
   addressing share one identifier regex, **and**
2. **not** match `UUID_REGEX` (`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).
   A raw conversation ID is *not* a valid alias — it is adapter-internal and
   must never be used as a recipient. Rejection is enforced both by
   `is_valid_alias()` and by the MCP tool schema.

---

## The capability contract (for adapter authors)

> This is what an adapter author must declare. The platform decides what OSS
> users can safely do with your target based on these three fields.

The wake mechanism generalizes beyond `agy.exe`, but its *safety* does not
transfer for free: it depends on how each target harness tags and surfaces
input. Every target adapter therefore **must** declare these first-class
fields. They are not documentation — they are load-bearing, and they determine
what the platform even lets a sender attempt.

| Field | Meaning |
| :--- | :--- |
| `surface_custom_json` | Does this agent surface custom JSON fields into its working context? If **No**, sender-supplied metadata (e.g. a locale hint) never reaches the agent and cannot be honored — regardless of what the sender sends. |
| `honor_sender_locale` | Does this agent honor a sender-supplied output-locale preference? Only meaningful when `surface_custom_json: Yes`. **Default: No.** |
| `injected_input_trust_label` | What trust label does the target assign to `CONIN$`-injected input (`USER_EXPLICIT`, a low-trust `SYSTEM` tag, …)? Determines how strict the agent-side convention (`AGENTS.md`) must be. |

### Reference adapter — `agy.exe` (Antigravity CLI, Go)

```yaml
target: agy.exe
surface_custom_json: No          # custom JSON fields are NOT copied into <SYSTEM_MESSAGE>
honor_sender_locale: No          # follows from surface_custom_json: No
injected_input_trust_label: USER_EXPLICIT   # injected text == human-owner keystroke
```

**Consequence (a deduction, not a preference):** because `agy.exe` does not
surface custom JSON fields *and* language mirroring defeats any out-of-band
hint, the output language of `agy.exe` is effectively **100 % local-sovereign** —
it is set by the local nudge constant and nothing else. `force-local` is
therefore the only correct default here; it falls out of the implementation
facts.

### How the contract shows up to OSS users

The `capabilities` dict in each `Recipient` returned by `list_recipients` is a
direct copy of the adapter's declared contract. An MCP client that asks "can I
send a locale hint to this recipient?" can read `honor_sender_locale` and
decide locally. The MCP tool schema *for `send`* is also derived from the
union of adapter contracts: if **no** adapter declares `honor_sender_locale:
Yes`, the `send` tool simply has no `locale` field — the OSS user cannot
request what no one can honor.

---

## Security invariants

These hold across all tasks and all adapters. (Enforcement lives in the code
and its tests; this section is the contract they implement.)

### R1 — rendezvous-only targeting

See [Discovery (opt-in registry)](#discovery-opt-in-registry--invariant-r1)
above. In summary: PID / alias resolution proceeds only from artifacts the
target itself published. No process enumeration, ever.

### R2 — no sender-controlled bytes on `CONIN$`

R2 is a **structural property of the public API surface.** The static call
graph of `send()` does not reach `_adapter` or `_win_console` — a sender
cannot invoke `CONIN$` injection, not even accidentally, not through any
field, kwarg, or environment variable. This is the point of the L2 split:
alias resolution lives in the pure file-reading function
`_resolve_alias_from_registry`, which does not import `_adapter` or any
Win32 code, so `send()`'s AST/import closure contains only the mailbox
writer and this registry reader. `CONIN$` injection is performed only by the
**receiver-side** orochi watcher, after it has validated the signed JSON;
the nudge it writes is resolved from the receiver's local
`WAKE_NUDGE_CATALOG` only, never from `msg_data`.

This is stronger than v0.1's guarantee ("convention + review"). In v0.1
`inject_console_input` was callable from the public surface and R2 relied on
`on_undelivered_file_created` never passing message-derived text; in v0.2 the
entire inject call graph is unreachable from senders *by construction*. A
dedicated test fixes this: `send`'s AST/import closure must not contain
`_adapter` or `_win_console` symbols.

### Wake-signal / payload separation

`CONIN$` delivers the wake nudge and nothing else. Sender-controlled content
(the task, the title, any metadata) lives in the JSON file and is consumed by
the agent through the attributable `<SYSTEM_MESSAGE>` channel.

### Two layers, not one problem

- **Governance layer — separation of powers.** Wake-nudge i18n and
  output-language i18n sit on opposite sides of the trust boundary. R2 fires
  on the *wake nudge only*; an output-language preference does not change the
  injected bytes, so it does not touch R2.
- **Mechanism layer — coupling by language mirroring.** An LLM strongly
  mirrors the language of the immediately preceding `<USER_REQUEST>` (= the
  injected nudge). So the nudge's language *mechanically* drives the output
  language. This is why the output-language directive is **bundled into the
  local nudge constant**, not left to chance or to the sender.

---

## Scope contract (applicability)

The mechanism rests on `AttachConsole` + `WriteConsoleInputW`. Its boundary is
**not** "ConPTY breaks it" — ConPTY-hosted apps have a real console input
buffer and *are* reachable. State the real boundary explicitly, as a
first-class spec, or the tool degrades into a "mysteriously flaky" one.

### Supported targets
- Interactive CLI/TUI processes that **own a real console** (classic conhost,
  Windows Terminal / ConPTY, `cmd`, `pwsh`, …) and read `stdin` as **console
  input**.
- Running in the **same user session** and at the **same integrity level** as
  the injector (the receiver-side watcher).

### Out of scope (will not work, by design)
- Processes whose `stdin` is a **pipe / redirect** (they read a byte stream,
  not console input records — `WriteConsoleInput` does not reach them). *This
  is the single largest real limitation.*
- Processes with **no console** (GUI apps; `DETACHED_PROCESS` /
  `CREATE_NO_WINDOW`).
- **Elevated** targets or a **different session** (no cross-integrity, no
  cross-session injection — this is a security property, not just a limit).
- **WSL** (Linux side — not a Win32 console) and **remote** targets.

This permanently fixes the tool as a **desktop-local, same-session,
console-attached** transport. It is *not* and will never be a cross-machine /
headless / CI transport — and that is fine: that territory belongs to a
conventional message queue. The scope matches the market (terminal AI agents
a human runs in windows on their own desktop), which is exactly the set that
satisfies the contract.

---

## Internationalization (i18n)

OSS is used worldwide, so the default is **English**, and all language
selection is **local-sovereign**: the sender never influences it.

```python
# English default. Key = output_locale.
# The nudge body is written IN that language AND carries an explicit directive,
# so language mirroring is harnessed rather than fought.
WAKE_NUDGE_CATALOG = {
  "en": "A new message arrived. Review your inbox and begin. Respond in English.",
  "ja": "受信メッセージを確認して作業に着手してください。日本語で応答してください。",
  # zh / ko / es / fr / de ...
}
DEFAULT_LOCALE = "en"

# Resolution order (sender input is NEVER consulted):
#   1. local override:  env CONINJECT_LOCALE  /  library arg locale="ja"
#   2. fallback:         "en"   (unknown / invalid -> unconditional en, fail-closed)
#
# wake_locale and output_locale resolve independently, each local-only:
#   env CONINJECT_WAKE_LOCALE   -> resolve_wake_locale()    (what the human reads)
#   env CONINJECT_OUTPUT_LOCALE -> resolve_output_locale()  (what the agent answers in)
#   both fall back to CONINJECT_LOCALE, then to "en".
```

- **Output language is pinned deterministically by the local directive**, not
  left to mirroring. Mirroring is a tailwind; on any mismatch the explicit
  directive wins.
- `wake_locale` (what the human owner reads) and `output_locale` (what the
  agent answers in) are **independent local settings** (both local → R2 is
  untouched); they default to the same value.
- When human-readability ≠ output language, do **not** make the nudge
  bilingual (it muddies mirroring). Keep `CONIN$` single-language and show the
  human-facing text through the **visible indicator** (a local UI *outside*
  the injection path).

### Layer B (sender-supplied locale) — intentionally absent in v1

A sender-supplied output-locale (`requestedOutputLocale` in the JSON) is
**removed in v1**, and this absence is **intentional, not an oversight**:

1. `agy.exe` does **not** surface custom JSON fields into the agent's context
   (`surface_custom_json: No`) — the field would never arrive.
2. Even if it did, **language mirroring** of the local nudge would override
   it.

So the feature would be doubly inert while adding attack surface. It may
return **later** only as **advisory-only**, **subordinate to the local
catalog**, and **only on adapters whose contract declares
`surface_custom_json: Yes` and `honor_sender_locale: Yes`** — and even then as
a validated-enum *hint* the local policy can override, never as a command,
fail-closed to `en`. When that day comes it surfaces as an optional MCP tool
field, not as a core one.

---

## Operations guide

The focus-not-stolen property is a feature *and* a risk: injection is
invisible to the human by design. These three controls restore human oversight
and are **operational requirements**, not optional extras.

### 1. Audit log (append-only)
Every wake records `who → whom · when · message-id · result` to an
**append-only**, tamper-evident log. This is the non-repudiation record for an
action that leaves no visible trace in the target window. The log is written
by the receiver-side watcher (the only component that ever invokes a wake).

```
2026-11-08T12:14:07Z  wake  sender=claude-code  recipient=gemini-ipc  msg=4b1e…  pid=13820  result=OK
```

### 2. Visible indicator
A local UI element (status line / tray / band — **outside** the injection
path) shows, in the human owner's own `wake_locale`, that a wakeup is
happening and who triggered it. This is the countermeasure to the *UI-blind*
attack class, where an attacker forces output into a language the operator
cannot read in order to delay audit of suspicious behavior. (Keeping
output-language authority local — see i18n — is the other half of that
defense.)

### 3. Kill switch
A single, immediate local disable that halts all wake injection. Combined
with the existing loop guards — `sender == "user"` is skipped to prevent
infinite loops, plus rate-limiting / loop detection — it bounds any wake
storm. When failures exceed retry limits, messages are quarantined to
`.system_generated/messages/dead_letter/<id>.json` rather than retried
indefinitely.

---

## Threat model in one line

> Legitimacy is not proven by *what can be injected* — the mechanism is
> identical to a keystroke-injecting keylogger. It is proven by the
> **constraints**: resolve recipients only from self-published artifacts
> (R1 — never enumerate), inject only a local, benign, single-bit wake nudge
> (R2 — never sender bytes), route every instruction through the signed
> channel, and keep the human able to see it and stop it.

---

## See also
- [`WORK_FLOW.md`](./WORK_FLOW.md) — directory lifecycle, sequence diagram,
  per-function spec, unit-test spec.
- [`AGENTS.md`](./AGENTS.md) — the convention the **injected** agent must
  follow.
- [`../orochi/DESIGN-0001-agentknock-positioning.md`](../orochi/DESIGN-0001-agentknock-positioning.md) —
  the architectural decision record that locked the two-reader API shape.
