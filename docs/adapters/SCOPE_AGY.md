# SCOPE_AGY.md — Scope contract of `AgyDeliveryAdapter`

**English** | [日本語](#日本語)

This document is the scope contract (**B4**) of the reference adapter
`agentknock.adapters.agy.AgyDeliveryAdapter`. It states explicitly what the
adapter supports, what it does not, and why — so users and audit reviewers can
tell by reading alone whether their setup is in scope, without diagnosing
"mysteriously flaky" behavior.

Every adapter author must publish a document of this shape next to their
adapter (see `ADAPTER_AUTHORING.md`). This file is that doc for the agy
adapter.

---

## Install form

Opt-in extra: `pip install "agentknock[win32]"`. Pulls in the native Cython
extension (`_win_console.pyd`) and the `AgyLockLivenessProbe`. **The agentknock
core is not touched by this extra** — installing it only *adds* the agy
adapter; removing it does not break anything else.

## Capability declaration

```yaml
target: agy.exe (Antigravity CLI, Go)
surface_custom_json: No           # agy.exe does not surface custom JSON fields into <SYSTEM_MESSAGE>
honor_sender_locale: No           # follows from surface_custom_json: No + mirroring override
injected_input_trust_label: USER_EXPLICIT   # injected text == human-owner keystroke
consumes_marker_on_wake: False    # the agy.exe target process reads undelivered/<id> itself
```

**Consequence (deduction, not preference)**: because the sender's custom JSON
is not reachable from the agent's context, and because an LLM running inside
`agy.exe` strongly mirrors the language of the preceding `<USER_REQUEST>` (=
the injected nudge), the adapter's output language is **100 % local-sovereign
by construction**. `force-local` is the only correct default here; it falls
out of the implementation facts.

## Supported targets

A target is in scope when **every one** of these holds:

- **OS**: Windows 10 or Windows 11.
- **Agent binary**: `agy.exe` (Antigravity CLI, Go) — not Antigravity web, not
  Antigravity IDE plugin.
- **Console surface**: the target **owns a real console** (classic conhost,
  Windows Terminal / ConPTY, `cmd.exe`-launched, `pwsh`-launched, …) and reads
  `stdin` as **console input records** (not as a byte stream).
- **Session & integrity**: the target runs in the **same user session** and at
  the **same integrity level** as the receiver-side watcher (the process that
  calls `AgyDeliveryAdapter.wake(...)`).
- **Opt-in**: the target has published its own entry in `AliasRegistry`
  (`register_self`) *and* held a live presence lock
  (`presence/<cid>.lock`) — the `AgyLockLivenessProbe` reverse-looks-up its
  PID from that lock via the Windows Restart Manager API.

ConPTY-hosted terminals are **supported**. The folk belief that "ConPTY
breaks `WriteConsoleInputW`" is not accurate — ConPTY apps have a real
console input buffer and are reachable just like classic conhost.

## Out of scope — will not work, by design

| Scenario | Why | What to use instead |
|:---|:---|:---|
| `agy.exe` launched with `stdin` as a **pipe or redirect** | The agent reads a byte stream, not `INPUT_RECORD`s — `WriteConsoleInputW` does not reach it. (Single largest real limitation.) | Launch `agy.exe` interactively from a real console. |
| Target has **no console** (GUI app, `DETACHED_PROCESS`, `CREATE_NO_WINDOW`) | Nothing to attach to; no `CONIN$`. | Not addressable by `AgyDeliveryAdapter`. Consider `OutboxDeliveryAdapter` + a receiver that polls the outbox. |
| Target is **elevated** (runs as a different integrity level than the watcher) | `AttachConsole` across integrity levels is denied by Windows (security property, not a bug). | Run both watcher and target at the same integrity level. |
| Target runs in a **different session** (RDP / SwitchUser) | `AttachConsole` cross-session is denied. | Run the watcher in the same session as the target. |
| **WSL** target (the Linux side) | Not a Win32 console. | `OutboxDeliveryAdapter` over a path visible to both Windows and WSL. |
| **Remote** target (another machine) | No Win32 console reachable. | `OutboxDeliveryAdapter` over a shared filesystem, or a transport MQ. |
| The target process has *not* opt-in-registered via `register_self()` | R1 — no process enumeration ever. | Have the target call `register_self()` on startup. |

## R-M1 contract — marker ownership (v0.3)

`AgyDeliveryAdapter.consumes_marker_on_wake == False`. The target `agy.exe`
process reads `undelivered/<Message-ID>` itself as part of its wake handling.
The receiver-side `ResidentWatcher` therefore **MUST NOT** unlink the marker
after `wake()` returns — ACK is `agy.exe`'s job, not the watcher's.

The in-memory cooldown (`rewake_cooldown_sec`, default 60 s) prevents double
wake in the window between `wake()` and the target's actual dequeue. A
background Reaper may clean truly orphan markers after `stale_marker_ttl_sec`
(default 300 s).

See `README.md` § Marker lifecycle for the full state machine.

## i18n (adapter-internal, B3)

The adapter owns its wake-nudge catalog:

```python
AgyDeliveryAdapter(wake_locale="ja", output_locale="ja")
```

Resolution order (sender input is never consulted):

1. `AGENTKNOCK_WAKE_LOCALE` / `AGENTKNOCK_OUTPUT_LOCALE` / `AGENTKNOCK_LOCALE`
   (legacy env names `CONINJECT_*` are also honored for v0.2 compatibility)
2. Constructor arguments (`wake_locale=`, `output_locale=`)
3. `"en"` — fail-closed on any unknown or invalid value.

Supported locales: `en, ja, zh, ko, es, fr, de`.

## R2 test (B1)

Shipped alongside the adapter as
`tests/test_r2_agy_adapter.py::test_r2_send_cannot_reach_agy_adapter`:

```python
from pathlib import Path
from agentknock.testing import assert_send_cannot_reach

def test_r2_send_cannot_reach_agy_adapter():
    assert_send_cannot_reach(
        Path("agentknock/core/mailbox.py"),       # module hosting send()
        entry_func_name="send",
        forbidden_symbols=frozenset({
            "AgyDeliveryAdapter",
            "_win_console",
            "AttachConsole",
            "WriteConsoleInputW",
            "find_pid_by_lock_path",
            "inject_console_input",
        }),
    )
```

The test fails closed on dynamic imports and on any transitive reach into
the adapter. Treat a failure as a stop-ship: any new path from `send()` into
Win32 territory is an R2 violation by construction.

## Security notes specific to this adapter

- The `CONIN$` channel is **non-attributable** at the receiver: injected text
  arrives tagged `source: USER_EXPLICIT`, indistinguishable from the human
  owner's own keystrokes. This is why R2 is type-level here and the wake
  nudge must be a local constant — see README § R2 and `AGENTS.md`.
- The receiver-side `AGENTS.md` convention is mandatory. A target agent that
  does not follow it (treating console input on a wake turn as untrusted,
  reading instructions only from the signed JSON) is out of scope regardless
  of the OS/console preconditions above.
- Operations trio (audit log, visible indicator, kill switch via
  `unregister_self`) are required for this adapter, not optional — see
  README § Operations guide.

---

## 日本語

本ドキュメントはリファレンスアダプタ `agentknock.adapters.agy.AgyDeliveryAdapter` の scope contract（**B4**）です。アダプタが何をサポートし、何を*しない*のか、そしてその理由を明示することで、利用者・監査レビュー者がドキュメントを読むだけで「自分の環境が scope 内か」を判別でき、"なぜか不安定" な挙動を解析せずに済むことを目的とします。

すべてのアダプタ作者は、自身のアダプタの隣にこの形式のドキュメントを公開します（`ADAPTER_AUTHORING.md` 参照）。本ファイルは agy アダプタ版です。

### インストール形態

opt-in extra: `pip install "agentknock[win32]"`。ネイティブ Cython 拡張（`_win_console.pyd`）と `AgyLockLivenessProbe` を導入します。**agentknock の core はこの extra によって一切変化しません**——インストールは agy アダプタを*追加*するだけで、アンインストールしても他に影響しません。

### Capability 宣言

```yaml
target: agy.exe (Antigravity CLI, Go)
surface_custom_json: No           # agy.exe は <SYSTEM_MESSAGE> にカスタム JSON フィールドを surface しない
honor_sender_locale: No           # surface_custom_json: No ＋ mirroring 上書きから演繹
injected_input_trust_label: USER_EXPLICIT   # 注入テキスト == 人間オーナーのキー入力
consumes_marker_on_wake: False    # agy.exe 本体が自分で undelivered/<id> を読む
```

**帰結（好みでなく演繹）**: 送信者の custom JSON はエージェント文脈に到達せず、かつ `agy.exe` 内 LLM が直前 `<USER_REQUEST>`（＝注入ナッジ）の言語に強く同調するため、このアダプタの出力言語は**構造上 100 % ローカル専権**です。`force-local` が唯一正しい既定であり、これは実装事実から演繹されます。

### サポート対象

ターゲットが scope 内となるのは、**次のすべて**が成立するときです:

- **OS**: Windows 10 または Windows 11。
- **エージェント binary**: `agy.exe`（Antigravity CLI, Go 製）——Antigravity Web でも Antigravity IDE プラグインでもない。
- **コンソール面**: ターゲットが**実コンソール**（従来 conhost・Windows Terminal / ConPTY・`cmd.exe` 起動・`pwsh` 起動等）を所有し、`stdin` を**コンソール入力レコード**として読むこと（バイトストリームとしてではない）。
- **セッションとインテグリティ**: ターゲットが、受信側ウォッチャ（`AgyDeliveryAdapter.wake(...)` を呼ぶプロセス）と**同一ユーザーセッション**・**同一インテグリティレベル**で動作していること。
- **Opt-in**: ターゲットが `AliasRegistry` に自己広告エントリを公開している（`register_self`）、*かつ* 生きた presence ロック（`presence/<cid>.lock`）を保持していること——`AgyLockLivenessProbe` が Windows Restart Manager API 経由でそのロックから PID を逆引きします。

ConPTY ホストのターミナルは**サポート対象**です。「ConPTY は `WriteConsoleInputW` を壊す」というのは誤認です——ConPTY アプリも実コンソール入力バッファを持ち、従来の conhost 同様に到達可能です。

### 対象外（設計上、動作しない）

| シナリオ | 理由 | 代替 |
|:---|:---|:---|
| `agy.exe` が `stdin` を**パイプ/リダイレクト**で起動されている | エージェントがバイトストリームを読むため `INPUT_RECORD` が届かない——`WriteConsoleInputW` は到達しない。（**唯一最大の実制約**。） | `agy.exe` を実コンソールから対話的に起動する。 |
| ターゲットに**コンソールが無い**（GUI アプリ、`DETACHED_PROCESS`、`CREATE_NO_WINDOW`） | attach 対象がない；`CONIN$` が無い。 | `AgyDeliveryAdapter` では到達不能。`OutboxDeliveryAdapter` ＋ outbox をポーリングする受信側で代替。 |
| ターゲットが**昇格**されている（ウォッチャと別インテグリティ） | クロスインテグリティの `AttachConsole` は Windows が拒否（セキュリティ特性、バグではない）。 | ウォッチャとターゲットを同一インテグリティで動かす。 |
| ターゲットが**別セッション**で動作（RDP／SwitchUser） | クロスセッションの `AttachConsole` は拒否される。 | ウォッチャをターゲットと同一セッションで動かす。 |
| **WSL** ターゲット（Linux 側） | Win32 コンソールではない。 | Windows と WSL の両方から見える path を使った `OutboxDeliveryAdapter`。 |
| **リモート**ターゲット（別マシン） | Win32 コンソールが到達不能。 | 共有 FS 上の `OutboxDeliveryAdapter` またはトランスポート MQ。 |
| ターゲットプロセスが `register_self()` で opt-in 登録していない | R1 — プロセス列挙は決して行わない。 | ターゲット起動時に `register_self()` を呼ぶ。 |

### R-M1 契約 — マーカー所有権（v0.3）

`AgyDeliveryAdapter.consumes_marker_on_wake == False`。ターゲットの `agy.exe` プロセスは起床処理の一環として自分で `undelivered/<Message-ID>` を読みます。したがって受信側 `ResidentWatcher` は `wake()` 後に**決してマーカーを unlink しては*ならない***——ACK は `agy.exe` の仕事であり、ウォッチャの仕事ではありません。

In-Memory cooldown（`rewake_cooldown_sec`、既定 60 秒）が `wake()` からターゲットの実 dequeue までの窓での二重起床を防ぎます。本当に孤児となったマーカーは `stale_marker_ttl_sec`（既定 300 秒）経過後に Background Reaper が清掃する場合があります。

完全な状態機械は README.md § マーカー・ライフサイクル参照。

### i18n（アダプタ内部、B3）

アダプタは自身の起床ナッジカタログを保持します:

```python
AgyDeliveryAdapter(wake_locale="ja", output_locale="ja")
```

解決順（送信者入力は決して参照しない）:

1. `AGENTKNOCK_WAKE_LOCALE` / `AGENTKNOCK_OUTPUT_LOCALE` / `AGENTKNOCK_LOCALE`
   （v0.2 互換のため legacy env `CONINJECT_*` も honor）
2. コンストラクタ引数（`wake_locale=`, `output_locale=`）
3. `"en"` — 未知／不正な値には fail-closed。

対応ロケール: `en, ja, zh, ko, es, fr, de`。

### R2 テスト（B1）

アダプタと併置して `tests/test_r2_agy_adapter.py::test_r2_send_cannot_reach_agy_adapter` として同梱:

```python
from pathlib import Path
from agentknock.testing import assert_send_cannot_reach

def test_r2_send_cannot_reach_agy_adapter():
    assert_send_cannot_reach(
        Path("agentknock/core/mailbox.py"),       # send() を host するモジュール
        entry_func_name="send",
        forbidden_symbols=frozenset({
            "AgyDeliveryAdapter",
            "_win_console",
            "AttachConsole",
            "WriteConsoleInputW",
            "find_pid_by_lock_path",
            "inject_console_input",
        }),
    )
```

このテストは動的 import とアダプタへの推移的到達に対して fail-closed します。失敗は stop-ship 扱い: `send()` から Win32 領域への新たな経路は、構造的に R2 違反です。

### このアダプタ固有のセキュリティ注意

- `CONIN$` チャネルは受信側で**帰属不能**です: 注入テキストは `source: USER_EXPLICIT` としてタグ付けされ、人間オーナー自身のキー入力と区別できません。これが本アダプタで R2 が型レベルであり、起床ナッジがローカル定数でなければならない理由です——README § R2 と `AGENTS.md` を参照。
- 受信側 `AGENTS.md` 規約は必須です。これに従わないターゲットエージェント（起床ターンのコンソール入力を untrusted として扱わず、指示を署名付き JSON からのみ読む規約を守らないもの）は、上記 OS／コンソール前提が満たされていても scope 外です。
- 運用三点セット（監査ログ、可視インジケータ、`unregister_self` 経由のキルスイッチ）はこのアダプタで必須であり、オプションではありません——README § 運用ガイド参照。
