# Adapter Authoring Checklist

**English** | [日本語](#日本語)

This checklist lists the obligations an author of a new agentknock
`DeliveryAdapter` must meet before shipping. Every item here has a
spec-level rationale; items marked **must** are blocking — a reviewer will
refuse to merge an adapter that skips them.

See the agentknock README's capability-contract section for the user-facing
picture, and `SCOPE_AGY.md` for a worked reference example.

---

## 1. Capability declaration (must)

Declare the three load-bearing capability fields on your adapter:

```python
from agentknock._contract import Capabilities

class MyAdapter:
    @property
    def capabilities(self) -> Capabilities:
        return {
            "surface_custom_json": False,     # Does the target surface custom JSON into its context?
            "honor_sender_locale": False,     # Only meaningful when surface_custom_json is True.
            "injected_input_trust_label": "ATTRIBUTED_METADATA",
            # One of: "USER_EXPLICIT" | "ATTRIBUTED_METADATA" | "SYSTEM"
        }
```

**Rationale.** These three fields drive what the platform even *lets* OSS
users attempt (e.g. the MCP tool schema's `locale` field only appears when at
least one adapter declares `Yes/Yes`). If your target cannot honor a
sender-supplied locale, say **No** — do not claim capability you cannot
actually enforce.

### Choosing `injected_input_trust_label`

| Value | When to pick |
|:---|:---|
| `USER_EXPLICIT` | The wake channel is **non-attributable** at the receiver — the agent sees the injected bytes as if the human owner typed them (e.g. `CONIN$` for `agy.exe`). R2 type-level enforcement is critical; the receiver's `AGENTS.md` must treat console input on a wake turn as untrusted. |
| `ATTRIBUTED_METADATA` | The wake channel is **attributable** — the agent sees the message as signed JSON with a `sender` field it can verify (e.g. `CallbackDeliveryAdapter`, `OutboxDeliveryAdapter`). i18n wake nudges typically not needed. |
| `SYSTEM` | OS-level system-message channels where the OS vouches for the sender identity. Rare. |

## 2. `wake(target_id)` — A1 type-level R2 (must)

Implement **exactly** this signature:

```python
def wake(self, target_id: str) -> bool: ...
```

**No `text` parameter.** No `content`. No `payload`. No `**kwargs` that could
leak sender-controlled bytes into the wake call site. If your adapter needs a
locale/mode/variant, resolve it from **adapter-local state** (constructor
arguments, environment variables, local catalog) — never from a function
argument on `wake()`.

**Rationale.** In v0.2 R2 was a call-graph invariant that could silently
regress if a future refactor passed message-derived text into `_inject()`.
v0.3 makes this impossible at the type level: there is nowhere in the
signature to put sender-controlled text in the first place. Your adapter must
preserve this property.

## 3. `consumes_marker_on_wake` — R-M1 lifecycle (must)

Choose one — *there is no middle ground*:

```python
@property
def consumes_marker_on_wake(self) -> bool:
    return True   # OR  False
```

| Value | When | Watcher behavior |
|:---|:---|:---|
| `True` | Your `wake()` completes delivery **synchronously** inside the watcher thread (e.g. you call a user-provided callback and it returns, or you write the JSON bytes into an outbox and fsync). | Watcher unlinks `undelivered/<id>` immediately after `wake()` returns `True`. Your callback-return *was* the ACK. |
| `False` | Your `wake()` only **signals** an external process (e.g. `CONIN$` injection into `agy.exe`), which will then dequeue `undelivered/<id>` on its own schedule. | Watcher does **not** unlink the marker. ACK is the target's job. The in-memory cooldown (`rewake_cooldown_sec`, default 60 s) prevents double-wake while the marker lingers. |

**Rationale.** A premature `unlink()` after an async wake caused the "1.5-s
marker reap" bug that lost messages. R-M1 draws a clear line: the party that
actually completes delivery owns the ACK.

## 4. `LivenessProbe` compatibility (must declare, may reuse)

Document which `LivenessProbe` implementations your adapter is known to work
with. If your target needs liveness semantics that no shipped probe covers,
ship your own `LivenessProbe` implementation alongside the adapter (e.g. the
agy adapter ships `AgyLockLivenessProbe` that reverse-looks-up the PID from
`presence/<cid>.lock`).

Common pairings:

| Adapter style | Suggested LivenessProbe |
|:---|:---|
| In-process callback | `AlwaysAliveProbe` |
| File-outbox between cooperating peer processes | `FileLockLivenessProbe` |
| Native desktop agent via OS-level liveness | Custom probe (see `AgyLockLivenessProbe`) |
| HTTP / RPC target | Custom probe that pings the target |

**Rationale.** R1's three-condition filter `(registry well-formed ∧
ttl-unexpired ∧ LivenessProbe.is_alive(target_id))` is only as sharp as the
probe. A liveness implementation that always returns `True` is valid only
for in-process adapters (where you are the target).

## 5. Publish `SCOPE_<ADAPTER>.md` (must, B4)

Next to your adapter module, publish a scope document following the shape of
`SCOPE_AGY.md`. It must state:

- **Install form** — is this an optional extra? what deps?
- **Capability declaration** — the three fields from §1 verbatim.
- **Supported targets** — what counts as in-scope, as *conjoint* conditions.
- **Out of scope — will not work, by design** — a table of scenarios + why
  each one fails + what to use instead.
- **R-M1 contract** — your `consumes_marker_on_wake` value and its
  consequences for the watcher.
- **i18n** — if your adapter has a wake-nudge catalog, state the resolution
  order and env vars; otherwise state "none (attributable channel)".
- **R2 test** — the test name and its forbidden set.
- **Security notes specific to this adapter** — anything a reviewer must know
  that is not generic to agentknock.

**Rationale.** The scope contract is how an adapter earns the user's trust by
being predictable rather than mysterious. "Mysteriously flaky" is a scope-doc
failure, not a bug.

## 6. R2 structural test (must, B1)

Ship a test in `tests/test_r2_<adapter>.py` that uses the standard helper:

```python
from pathlib import Path
from agentknock.testing import assert_send_cannot_reach

def test_r2_send_cannot_reach_<adapter>():
    assert_send_cannot_reach(
        Path("agentknock/core/mailbox.py"),
        entry_func_name="send",
        forbidden_symbols=frozenset({
            # 1. Your adapter's top-level public name:
            "MyAdapter",
            # 2. Every module it imports that touches the wake channel:
            "my_native_ext",
            # 3. Any symbol that opens a path to the wake channel:
            "open_my_wake_handle",
            "write_to_my_wake",
            # 4. Any Win32 / FFI / RPC symbol you know of:
            "AttachConsole",
            # (adapt to your adapter's actual forbidden set)
        }),
    )
```

The helper walks `send()`'s AST transitively and fails closed on dynamic
imports. Treat a failure as **stop-ship**: any new path from `send()` into
your adapter's wake machinery is an R2 violation by construction, and must
be fixed at the source — do not loosen the forbidden set.

## 7. `audit.jsonl` event participation (must)

Your adapter's behavior must support the 4-event audit lifecycle:

```
CREATED → WOKEN → CONSUMED            # happy path
CREATED → WOKEN → RETRY → … → CONSUMED
CREATED → WOKEN → RETRY → … → DEAD_LETTER
CREATED → … → REAPED                  # orphan cleanup
```

`wake()` returning `True` triggers `WOKEN`. `wake()` returning `False` after
all retries exhausted triggers `DEAD_LETTER`. If your adapter is
`consumes_marker_on_wake=True`, `CONSUMED` is emitted immediately after the
unlink; otherwise it is emitted by the target when it consumes the marker, or
by the Reaper on timeout.

**Do not** emit audit events yourself — this is the watcher's job. Your only
obligation is to return `True`/`False` from `wake()` to drive the state
machine.

## 8. Checklist (PR review summary)

Before opening a PR, confirm every box:

- [ ] **§1** `capabilities` declared with all three fields.
- [ ] **§2** `wake(self, target_id: str) -> bool` signature — no `text`
      argument, no kwargs that could leak sender bytes.
- [ ] **§3** `consumes_marker_on_wake` property returns `True` or `False`.
- [ ] **§4** `LivenessProbe` compatibility documented (reused or shipped
      alongside).
- [ ] **§5** `SCOPE_<ADAPTER>.md` published next to the adapter.
- [ ] **§6** R2 test in `tests/test_r2_<adapter>.py` using
      `assert_send_cannot_reach` — passes locally.
- [ ] **§7** Behavior observed to produce `CREATED → WOKEN → CONSUMED` (and
      `… → DEAD_LETTER` on retry exhaustion) in `audit.jsonl`.
- [ ] README's § "Reference adapters shipped in-tree" table updated with your
      adapter's three-field capability row.

---

## 日本語

本チェックリストは、新しい agentknock `DeliveryAdapter` の作者が出荷前に満たすべき義務事項の一覧です。各項目に仕様上の根拠があり、**必須**とマークされた項目はブロッキングです——レビュー担当者はこれを満たさないアダプタのマージを拒否します。

利用者視点の全体像は agentknock README の capability contract セクションを、具体例は `SCOPE_AGY.md` を参照してください。

### 1. Capability 宣言（必須）

アダプタに 3 つの load-bearing capability フィールドを宣言します:

```python
from agentknock._contract import Capabilities

class MyAdapter:
    @property
    def capabilities(self) -> Capabilities:
        return {
            "surface_custom_json": False,     # ターゲットはカスタム JSON を文脈に surface するか
            "honor_sender_locale": False,     # surface_custom_json=True の時のみ意味を持つ
            "injected_input_trust_label": "ATTRIBUTED_METADATA",
            # "USER_EXPLICIT" | "ATTRIBUTED_METADATA" | "SYSTEM" のいずれか
        }
```

**根拠.** この 3 フィールドが、プラットフォームが OSS 利用者に何を*試させる*かを決めます（例: MCP ツールスキーマの `locale` フィールドは、少なくとも 1 つのアダプタが `Yes/Yes` を宣言する時のみ現れます）。ターゲットが送信者提供ロケールを honor できないなら **No** と言ってください——強制できない capability を主張しないこと。

#### `injected_input_trust_label` の選び方

| 値 | 選ぶ条件 |
|:---|:---|
| `USER_EXPLICIT` | 起床チャネルが受信側で**帰属不能**——エージェントが注入バイトを人間オーナーの打鍵と同じものとして見る（例: `agy.exe` の `CONIN$`）。R2 型レベル強制が critical、受信側 `AGENTS.md` は起床ターンのコンソール入力を untrusted として扱う必要あり。 |
| `ATTRIBUTED_METADATA` | 起床チャネルが**帰属可能**——エージェントがメッセージを検証可能な `sender` フィールド付き署名付き JSON として見る（例: `CallbackDeliveryAdapter`、`OutboxDeliveryAdapter`）。i18n 起床ナッジは通常不要。 |
| `SYSTEM` | OS が送信者識別を保証する OS レベルシステムメッセージチャネル。稀。 |

### 2. `wake(target_id)` — A1 型レベル R2（必須）

**ちょうどこの**シグネチャで実装します:

```python
def wake(self, target_id: str) -> bool: ...
```

**text 引数なし**。`content` なし。`payload` なし。送信者制御のバイトを wake 呼び出し地点に漏らしうる `**kwargs` もなし。アダプタがロケール／モード／バリアントを必要とする場合は、**アダプタローカルの状態**（コンストラクタ引数、環境変数、ローカルカタログ）から解決すること——`wake()` の関数引数からは決して解決しない。

**根拠.** v0.2 では R2 は call-graph 不変条件で、将来のリファクタがメッセージ由来テキストを `_inject()` に渡してしまえば静かに回帰しうるものでした。v0.3 ではこれを型レベルで不可能にします: そもそもシグネチャに送信者制御テキストを置ける場所がありません。アダプタはこの性質を保たなければなりません。

### 3. `consumes_marker_on_wake` — R-M1 ライフサイクル（必須）

どちらかを選びます——*中間はありません*:

```python
@property
def consumes_marker_on_wake(self) -> bool:
    return True   # または False
```

| 値 | 条件 | ウォッチャ挙動 |
|:---|:---|:---|
| `True` | `wake()` がウォッチャスレッド内で**同期的に**配送完了する（例: ユーザ提供コールバックを呼んで戻る／JSON バイトを outbox に書いて fsync する）。 | `wake()` が `True` を返した直後にウォッチャが `undelivered/<id>` を unlink。コールバックの return 自身が ACK だった。 |
| `False` | `wake()` は外部プロセスに**シグナルを送るだけ**（例: `agy.exe` への `CONIN$` 注入）であり、後から外部プロセスが自分のタイミングで `undelivered/<id>` を dequeue する。 | ウォッチャはマーカーを unlink **しない**。ACK はターゲットの仕事。In-Memory cooldown（`rewake_cooldown_sec`、既定 60 秒）がマーカー残存中の二重起床を防ぐ。 |

**根拠.** 非同期 wake 後の早すぎる `unlink()` が「1.5 秒マーカー刈り取り」バグを生み、メッセージを失わせました。R-M1 は明確な線を引きます: 実際に配送を完了する側が ACK を所有します。

### 4. `LivenessProbe` 互換性（必ず宣言、再利用可）

アダプタが動作することが確認されている `LivenessProbe` 実装を文書化します。既存の probe が覆わない liveness セマンティクスがターゲットに必要な場合、アダプタと併せて `LivenessProbe` 実装を同梱します（例: agy アダプタは `presence/<cid>.lock` から PID を逆引きする `AgyLockLivenessProbe` を同梱）。

一般的な組み合わせ:

| アダプタ種別 | 推奨 LivenessProbe |
|:---|:---|
| インプロセスコールバック | `AlwaysAliveProbe` |
| 協調プロセス間のファイル outbox | `FileLockLivenessProbe` |
| OS レベル liveness のネイティブデスクトップエージェント | カスタム probe（`AgyLockLivenessProbe` 参照） |
| HTTP / RPC ターゲット | ターゲットへの ping を行うカスタム probe |

**根拠.** R1 の 3 条件フィルタ `(registry well-formed ∧ ttl-未失効 ∧ LivenessProbe.is_alive(target_id))` は probe の精度に比例します。常に `True` を返す liveness 実装は、インプロセスアダプタ（あなた自身がターゲット）でのみ妥当です。

### 5. `SCOPE_<ADAPTER>.md` の公開（必須、B4）

アダプタモジュールの隣に、`SCOPE_AGY.md` の形式に従った scope ドキュメントを公開します。必須記載:

- **インストール形態** — optional extra か？依存は？
- **Capability 宣言** — §1 の 3 フィールドを逐語的に。
- **サポート対象** — scope 内の条件群を**連言**として明示。
- **対象外（設計上、動作しない）** — シナリオ ＋ 失敗理由 ＋ 代替手段の表。
- **R-M1 契約** — `consumes_marker_on_wake` の値と、それがウォッチャに与える帰結。
- **i18n** — アダプタに起床ナッジカタログがある場合は解決順と env 変数を、ない場合は "none (attributable channel)" を明記。
- **R2 テスト** — テスト名と forbidden set。
- **このアダプタ固有のセキュリティ注意** — agentknock 一般ではないがレビュー担当者が知るべきこと。

**根拠.** scope contract は「予測可能性」でアダプタがユーザの信頼を得る方法です。"なぜか不安定" は scope ドキュメントの失敗であり、バグではありません。

### 6. R2 構造テスト（必須、B1）

標準ヘルパを使ったテストを `tests/test_r2_<adapter>.py` に同梱します:

```python
from pathlib import Path
from agentknock.testing import assert_send_cannot_reach

def test_r2_send_cannot_reach_<adapter>():
    assert_send_cannot_reach(
        Path("agentknock/core/mailbox.py"),
        entry_func_name="send",
        forbidden_symbols=frozenset({
            # 1. アダプタのトップレベル公開名:
            "MyAdapter",
            # 2. 起床チャネルに触れる import 対象モジュール:
            "my_native_ext",
            # 3. 起床チャネルへの経路を開くシンボル:
            "open_my_wake_handle",
            "write_to_my_wake",
            # 4. 把握している Win32 / FFI / RPC シンボル:
            "AttachConsole",
            # （アダプタの実際の forbidden set に合わせて調整）
        }),
    )
```

ヘルパは `send()` の AST を推移的に追跡し、動的 import に対して fail-closed します。失敗は **stop-ship** 扱い: `send()` からアダプタの起床機構への新経路は構造的に R2 違反であり、根本で修正しなければなりません——forbidden set を緩めて逃げるのは不可。

### 7. `audit.jsonl` イベント参加（必須）

アダプタの挙動は 4 イベントの audit ライフサイクルを支えなければなりません:

```
CREATED → WOKEN → CONSUMED            # happy path
CREATED → WOKEN → RETRY → … → CONSUMED
CREATED → WOKEN → RETRY → … → DEAD_LETTER
CREATED → … → REAPED                  # 孤児清掃
```

`wake()` が `True` を返すと `WOKEN` が記録されます。リトライ上限枯渇後に `wake()` が `False` を返し続けると `DEAD_LETTER`。アダプタが `consumes_marker_on_wake=True` なら `CONSUMED` は unlink 直後に記録されます。そうでなければ、ターゲットがマーカーを消費した時、または Reaper がタイムアウトで記録します。

アダプタ自身が audit イベントを記録しては**いけません**——それはウォッチャの仕事です。義務は `wake()` から `True`/`False` を返して状態機械を駆動することのみ。

### 8. チェックリスト（PR レビュー用サマリ）

PR を開く前に、以下の各項目を確認:

- [ ] **§1** `capabilities` が 3 フィールドすべて宣言されている。
- [ ] **§2** `wake(self, target_id: str) -> bool` シグネチャ — `text` 引数なし、送信者バイトを漏らしうる kwargs なし。
- [ ] **§3** `consumes_marker_on_wake` プロパティが `True` または `False` を返す。
- [ ] **§4** `LivenessProbe` 互換性を文書化（再利用または併置）。
- [ ] **§5** `SCOPE_<ADAPTER>.md` をアダプタの隣に公開。
- [ ] **§6** `assert_send_cannot_reach` を使った R2 テストを `tests/test_r2_<adapter>.py` に、ローカルで PASS。
- [ ] **§7** `audit.jsonl` に `CREATED → WOKEN → CONSUMED`（およびリトライ枯渇時の `… → DEAD_LETTER`）が流れることを確認。
- [ ] README の § "同梱リファレンスアダプタ" 表にあなたのアダプタの 3 フィールド capability 行を追記。
