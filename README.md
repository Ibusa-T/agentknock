# agentknock

> **Your terminal agents, knocking each other awake while you watch —
> without stealing focus. It rings the doorbell; it never gives the orders.**

**English** | [日本語](./README.ja.md)

**agentknock** (module: `console_injector`) is a **transport layer for desktop
multi-agent orchestration**. It wakes an interactive CLI/TUI AI agent (e.g.
Antigravity `agy.exe`, and — via adapters — other terminal agents) that is
blocked on `stdin` while idle, by injecting a keystroke into the target
process's console input buffer (`CONIN$`) through the Windows kernel API
(`AttachConsole` + `WriteConsoleInputW`). The target's window never gains focus,
so the human owner keeps working undisturbed. The destination process is
resolved **by PID**, reverse-looked-up from a presence lock the target itself
published — never by window title, never by process enumeration.

This is **not** keystroke automation. It is the missing delivery channel for
agents that (a) you cannot modify, (b) only read `stdin`, (c) are idle or
backgrounded, and must be reached (d) without disturbing the human's foreground
session. A message queue assumes a *cooperative subscriber*; RPA/`pyautogui`
assumes a *foreground window*. This tool assumes neither.

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
follow, see [`AGENTS.md`](./AGENTS.md).

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

## Security invariants

These hold across all tasks and all adapters. (Enforcement lives in the code and
its tests; this section is the contract they implement.)

### R2 — no sender-controlled bytes on `CONIN$`
The bytes written to `CONIN$` are **never** derived from any field of an inbound
message. A locally-sourced *language* may ride the nudge (see i18n below), but a
remote *sender* contributes **zero bytes and zero selectors** to the injected
string. `wake_prompt` is resolved from a local catalog only.

> The low-level `inject_console_input(target_pid, text=None, …)` still accepts a
> `text` parameter, but on the wake path it only ever receives this
> locally-resolved nudge — the free-string CLI option is gone. R2 is a
> **structural** guarantee: `on_undelivered_file_created` passes only locally
> resolved text and never a value derived from `msg_data`.

### Wake-signal / payload separation
`CONIN$` delivers the wake nudge and nothing else. Sender-controlled content
(the task, the title, any metadata) lives in the JSON file and is consumed by
the agent through the attributable `<SYSTEM_MESSAGE>` channel.

### Two layers, not one problem
- **Governance layer — separation of powers.** Wake-nudge i18n and output-language
  i18n sit on opposite sides of the trust boundary. R2 fires on the *wake nudge
  only*; an output-language preference does not change the injected bytes, so it
  does not touch R2.
- **Mechanism layer — coupling by language mirroring.** An LLM strongly mirrors
  the language of the immediately preceding `<USER_REQUEST>` (= the injected
  nudge). So the nudge's language *mechanically* drives the output language.
  This is why the output-language directive is **bundled into the local nudge
  constant**, not left to chance or to the sender.

---

## Capability contract (first-class)

The mechanism generalizes beyond `agy.exe`, but its *safety* does not transfer
for free: it depends on how each target harness tags and surfaces input. Every
target adapter therefore **must** declare these first-class fields. They are not
documentation — they are load-bearing, and they determine what is even possible.

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
surface custom JSON fields *and* language mirroring defeats any out-of-band hint,
the output language of `agy.exe` is effectively **100 % local-sovereign** — it is
set by the local nudge constant and nothing else. `force-local` is therefore the
only correct default here; it falls out of the implementation facts.

---

## Scope contract (applicability)

The mechanism rests on `AttachConsole` + `WriteConsoleInputW`. Its boundary is
**not** "ConPTY breaks it" — ConPTY-hosted apps have a real console input buffer
and *are* reachable. State the real boundary explicitly, as a first-class spec,
or the tool degrades into a "mysteriously flaky" one.

### Supported targets
- Interactive CLI/TUI processes that **own a real console** (classic conhost,
  Windows Terminal / ConPTY, `cmd`, `pwsh`, …) and read `stdin` as **console
  input**.
- Running in the **same user session** and at the **same integrity level** as the
  injector.

### Out of scope (will not work, by design)
- Processes whose `stdin` is a **pipe / redirect** (they read a byte stream, not
  console input records — `WriteConsoleInput` does not reach them). *This is the
  single largest real limitation.*
- Processes with **no console** (GUI apps; `DETACHED_PROCESS` / `CREATE_NO_WINDOW`).
- **Elevated** targets or a **different session** (no cross-integrity, no
  cross-session injection — this is a security property, not just a limit).
- **WSL** (Linux side — not a Win32 console) and **remote** targets.

This permanently fixes the tool as a **desktop-local, same-session, console-attached**
transport. It is *not* and will never be a cross-machine / headless / CI
transport — and that is fine: that territory belongs to a conventional message
queue. The scope matches the market (terminal AI agents a human runs in windows
on their own desktop), which is exactly the set that satisfies the contract.

---

## Internationalization (i18n)

OSS is used worldwide, so the default is **English**, and all language selection
is **local-sovereign**: the sender never influences it.

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

- **Output language is pinned deterministically by the local directive**, not left
  to mirroring. Mirroring is a tailwind; on any mismatch the explicit directive
  wins.
- `wake_locale` (what the human owner reads) and `output_locale` (what the agent
  answers in) are **independent local settings** (both local → R2 is untouched);
  they default to the same value.
- When human-readability ≠ output language, do **not** make the nudge bilingual
  (it muddies mirroring). Keep `CONIN$` single-language and show the
  human-facing text through the **visible indicator** (a local UI *outside* the
  injection path).

### Layer B (sender-supplied locale) — intentionally absent in v1

A sender-supplied output-locale (`requestedOutputLocale` in the JSON) is **removed
in v1**, and this absence is **intentional, not an oversight**:

1. `agy.exe` does **not** surface custom JSON fields into the agent's context
   (`surface_custom_json: No`) — the field would never arrive.
2. Even if it did, **language mirroring** of the local nudge would override it.

So the feature would be doubly inert while adding attack surface. It may return
**later** only as **advisory-only**, **subordinate to the local catalog**, and
**only on adapters whose contract declares `surface_custom_json: Yes` and
`honor_sender_locale: Yes`** — and even then as a validated-enum *hint* the local
policy can override, never as a command, fail-closed to `en`.

---

## Operations guide

The focus-not-stolen property is a feature *and* a risk: injection is invisible
to the human by design. These three controls restore human oversight and are
**operational requirements**, not optional extras.

### 1. Audit log (append-only)
Every injection records `who → whom · when · message-id` to an **append-only**,
tamper-evident log. This is the non-repudiation record for an action that leaves
no visible trace in the target window.

```
2026-11-08T12:14:07Z  wake  sender=claude-code  recipient=22222222-…  msg=4b1e…  pid=13820  result=OK
```

### 2. Visible indicator
A local UI element (status line / tray / band — **outside** the injection path)
shows, in the human owner's own `wake_locale`, that a wakeup is happening and who
triggered it. This is the countermeasure to the *UI-blind* attack class, where an
attacker forces output into a language the operator cannot read in order to delay
audit of suspicious behavior. (Keeping output-language authority local — see
i18n — is the other half of that defense.)

### 3. Kill switch
A single, immediate local disable that halts all injection. Combined with the
existing loop guards — `sender == "user"` is skipped to prevent infinite loops,
plus rate-limiting / loop detection — it bounds any wake storm.

---

## Threat model in one line

> Legitimacy is not proven by *what can be injected* — the mechanism is identical
> to a keystroke-injecting keylogger. It is proven by the **constraints**: inject
> only into a PID the target itself published (never by enumeration), inject only
> a local, benign, single-bit wake nudge (never sender bytes), route every
> instruction through the signed channel, and keep the human able to see it and
> stop it.

## See also
- [`WORK_FLOW.md`](./WORK_FLOW.md) — directory lifecycle, sequence diagram, per-function spec, unit-test spec.
- [`AGENTS.md`](./AGENTS.md) — the convention the **injected** agent must follow.
