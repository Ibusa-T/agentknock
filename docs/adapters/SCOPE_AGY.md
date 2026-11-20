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

Opt-in extra: `pip install "agentknock[win32]"`. Uses the native Cython
extension (`_win_console.pyd`) when built, and **since v0.3.1 ships an integrated
pure-Python `ctypes` fallback (`agentknock.adapters._win_console_ctypes`)** inside
the package alongside `AgyLockLivenessProbe` (without runtime `pyximport` or a root
`_win_console.py` shim), so the adapter runs out-of-the-box even without a C
compiler or `.pyd` binary. If neither `_win_console` backend is available,
`AgyLockLivenessProbe.is_alive()` returns `False` (fail-closed) rather than
assuming lock presence alone indicates liveness. **The agentknock core is not
touched by this extra** — installing it only *adds* the agy adapter; removing it
does not break anything else.

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

Enforced by `agentknock.testing.assert_send_cannot_reach` and `DEFAULT_FORBIDDEN_SYMBOLS` across `tests/test_t_c_adapters.py::test_b1_assert_send_cannot_reach_passes`, `tests/test_d4_r2_r1_invariants.py::test_r2_send_callgraph_no_win_console_conin`, and `tests/test_v031_toctou_security.py::test_r2_forbidden_symbols_include_v031_toctou_symbols`:

```python
from pathlib import Path
from agentknock.testing import DEFAULT_FORBIDDEN_SYMBOLS, assert_send_cannot_reach

def test_r2_send_cannot_reach_agy_adapter():
    assert_send_cannot_reach(
        Path("agentknock/core/mailbox.py"),       # or agentknock/console_injector.py
        entry_func_name="send",
        forbidden_symbols=DEFAULT_FORBIDDEN_SYMBOLS | frozenset({
            "AgyDeliveryAdapter",
        }),
    )
```

`DEFAULT_FORBIDDEN_SYMBOLS` (v0.3.1) covers both Win32 console symbols and TOCTOU identity/process APIs (`_adapter`, `_win_console`, `_win_console_ctypes`, `CONIN$`, `AttachConsole`, `WriteConsoleInputW`, `OpenProcess`, `GetProcessTimes`, `wake`, `_inject`, `_find_pid`, `_find_process_identity`, `find_agy_pid_by_conversation_id`, `_find_agy_process_by_conversation_id`, `inject_console_input`, `inject_console_input_verified`, `find_pid_by_lock_path`, `find_process_by_lock_path`, `_default_adapter`).

The test fails closed on dynamic imports and on any transitive reach into
the adapter. Treat a failure as a stop-ship: any new path from `send()` into
Win32 territory is an R2 violation by construction.

## Security notes specific to this adapter

- **PID Reuse (TOCTOU) Protection (v0.3.1)**: Lock resolution (`find_process_by_lock_path`) binds `(pid, ProcessStartTime)` with TS session & `agy.exe` image name verification, failing closed (`None`) if 0 or >=2 distinct lock holders exist. Console injection (`inject_console_input_verified`) pins the kernel process `HANDLE` across liveness, exact 64-bit `GetProcessTimes` match, image check, `AttachConsole` / `CONIN$`, and `WriteConsoleInputW` (fail-closed `False` if `expected_start_time` is omitted or `<= 0`). `AgyLockLivenessProbe.is_alive()` also fails closed to `False` when no Win32 backend is available or verification fails.
- **User-Typing Collision Guard (v0.3.4)**: Enabled by default (`respect_user_typing=True` on `AgyDeliveryAdapter` and `inject_console_input_verified()`). Before calling `WriteConsoleInputW`, `inject_console_input_verified()` checks both (1) pending `KEY_EVENT` (`bKeyDown=True`) records on `CONIN$` (`GetNumberOfConsoleInputEvents` / `PeekConsoleInputW`) and (2) visible text on the current input line of `CONOUT$` (`GetConsoleScreenBufferInfo` / `ReadConsoleOutputCharacterW` over columns `0..X-1`). After skipping a bare idle prompt prefix (`>` at column 0 followed by spaces, such as the `agy.exe` `> ` prompt), any remaining visible character (`ord(ch) > 0x20 and ord(ch) != 0x7F` — covering both ASCII and non-ASCII text such as full-width / Japanese input, even while the user is paused mid-sentence) triggers up to 2 retries (0.4 s interval). If typing is still detected, injection is suppressed without writing to `CONIN$`, returning `(False, "deferred_user_typing", 0)` and recording `last_wake_detail["reason"] = "deferred_user_typing"` (Win32 inspection errors fail closed with `(False, "user_typing_check_failed", win32_err)`). A truly idle console (`X == 0`, whitespace-only, or the bare `> ` prompt alone) injects normally as before.
- **Structured Failure Audit**: `AgyDeliveryAdapter` / `_AgyAdapter` records `last_wake_detail` (`reason`, `win32_error`, `pid`, `verified_start_time`, `backend`), which `WatchDaemon` emits as `wake_reason` and `win32_error` in `audit.jsonl` for `RETRY` / `DEAD_LETTER` events.
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

opt-in extra: `pip install "agentknock[win32]"`。ネイティブ Cython 拡張（`_win_console.pyd`）が利用可能な場合は優先ロードし、未コンパイル環境向けに pure-Python `ctypes` フォールバック（`agentknock/adapters/_win_console_ctypes.py`）と `AgyLockLivenessProbe`（Win32 バックエンド無効時や検証不能時は `False` に fail-closed）を組み込んでいます。**agentknock の core はこの extra によって一切変化しません**——インストールは agy アダプタを*追加*するだけで、アンインストールしても他に影響しません。

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

`agentknock.testing.assert_send_cannot_reach` と `DEFAULT_FORBIDDEN_SYMBOLS` を用い、`tests/test_t_c_adapters.py::test_b1_assert_send_cannot_reach_passes`、`tests/test_d4_r2_r1_invariants.py::test_r2_send_callgraph_no_win_console_conin`、`tests/test_v031_toctou_security.py::test_r2_forbidden_symbols_include_v031_toctou_symbols` で検証:

```python
from pathlib import Path
from agentknock.testing import DEFAULT_FORBIDDEN_SYMBOLS, assert_send_cannot_reach

def test_r2_send_cannot_reach_agy_adapter():
    assert_send_cannot_reach(
        Path("agentknock/core/mailbox.py"),       # または agentknock/console_injector.py
        entry_func_name="send",
        forbidden_symbols=DEFAULT_FORBIDDEN_SYMBOLS | frozenset({
            "AgyDeliveryAdapter",
        }),
    )
```

`DEFAULT_FORBIDDEN_SYMBOLS` (v0.3.1) には従来の Win32 コンソールシンボルに加え、TOCTOU 対策・プロセス検証シンボル（`_adapter`, `_win_console`, `_win_console_ctypes`, `CONIN$`, `AttachConsole`, `WriteConsoleInputW`, `OpenProcess`, `GetProcessTimes`, `wake`, `_inject`, `_find_pid`, `_find_process_identity`, `find_agy_pid_by_conversation_id`, `_find_agy_process_by_conversation_id`, `inject_console_input`, `inject_console_input_verified`, `find_pid_by_lock_path`, `find_process_by_lock_path`, `_default_adapter`）が含まれます。

このテストは動的 import とアダプタへの推移的到達に対して fail-closed します。失敗は stop-ship 扱い: `send()` から Win32 領域への新たな経路は、構造的に R2 違反です。

### このアダプタ固有のセキュリティ注意

- **PID 再利用（TOCTOU）対策（v0.3.1）**: ロック解決（`find_process_by_lock_path`）は `(pid, ProcessStartTime)` を TS セッションおよび `agy.exe` イメージ名検証付きで束縛し、保持者が 0 件または 2 件以上の場合は `None` に fail-closed します。コンソール注入（`inject_console_input_verified`）はカーネル `HANDLE` を生存確認・64bit `GetProcessTimes` 完全一致検証・イメージ検証・`AttachConsole` / `CONIN$`・`WriteConsoleInputW` 完了までピン留めします（`expected_start_time` 欠落または `<= 0` のレガシ呼び出しは `False` に fail-closed）。また `AgyLockLivenessProbe.is_alive()` も Win32 バックエンド無効時や検証不能時は `False` に fail-closed します。
- **User-Typing Collision Guard（入力衝突ガード、v0.3.4）**: 既定で有効（`AgyDeliveryAdapter` および `inject_console_input_verified()` の `respect_user_typing=True`）。`WriteConsoleInputW` の直前に、(1) `CONIN$` 上の未処理キーイベント（`GetNumberOfConsoleInputEvents` / `PeekConsoleInputW` による `KEY_EVENT` かつ `bKeyDown=True`）と、(2) `CONOUT$` の現在入力行（`GetConsoleScreenBufferInfo` / `ReadConsoleOutputCharacterW` の `0..X-1` 列）に可視文字が存在するかを検査します。行頭の静的プロンプト（先頭列の `>` と後続スペース、`agy.exe` の `> ` 等）を除外したうえで可視文字（`ord(ch) > 0x20 and ord(ch) != 0x7F` — ASCII に加え全角・日本語などの非 ASCII 入力や入力途中で思考停止している状態も保護対象）が残っている場合は最大 2 回（0.4 秒間隔）再確認し、なお入力中であれば `WriteConsoleInputW` を呼ばずに `(False, "deferred_user_typing", 0)` で注入を抑止し、`last_wake_detail["reason"] = "deferred_user_typing"` を記録します（検査 API エラー時は `(False, "user_typing_check_failed", win32_err)` に fail-closed）。一方、`agy.exe` の `> ` プロンプトのみや空行（`X == 0` / 空白のみ）といった真アイドル状態では、従来どおり起床ナッジが注入されます。
- **構造化失敗監査**: `AgyDeliveryAdapter` / `_AgyAdapter` は `last_wake_detail`（`reason`, `win32_error`, `pid`, `verified_start_time`, `backend`）を記録し、`WatchDaemon` は `RETRY` / `DEAD_LETTER` イベントの `wake_reason`・`win32_error` として `audit.jsonl` に出力します。
- `CONIN$` チャネルは受信側で**帰属不能**です: 注入テキストは `source: USER_EXPLICIT` としてタグ付けされ、人間オーナー自身のキー入力と区別できません。これが本アダプタで R2 が型レベルであり、起床ナッジがローカル定数でなければならない理由です——README § R2 と `AGENTS.md` を参照。
- 受信側 `AGENTS.md` 規約は必須です。これに従わないターゲットエージェント（起床ターンのコンソール入力を untrusted として扱わず、指示を署名付き JSON からのみ読む規約を守らないもの）は、上記 OS／コンソール前提が満たされていても scope 外です。
- 運用三点セット（監査ログ、可視インジケータ、`unregister_self` 経由のキルスイッチ）はこのアダプタで必須であり、オプションではありません——README § 運用ガイド参照。
