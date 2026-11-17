# agentknock

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](./LICENSE)

> **あなたの端末エージェントたちが、あなたの目の前で互いを叩き起こす——
> フォーカスを奪わずに。呼び鈴を鳴らすだけ。命令は決して出さない。**

[English](./README.md) | **日本語**

**agentknock** は、**orochi 非依存の単体 OSS として動く、デスクトップ・マルチエージェント連携のためのトランスポート層**です: *通知付き mailbox*（`send()` は署名付き JSON をディレクトリに書き込み、受信側のローカルウォッチャが到着時にコールバックまたは差し替え可能なアダプタを発火する）＋ 4 つの小さな DI 拡張点（`Notifier`, `DeliveryAdapter`, `AliasRegistry`, `LivenessProbe`）で環境差分を吸収する設計です。

core は**純 Python・外部依存なし**で動作します。`pip install agentknock` ＋ 共有ディレクトリだけで、同一マシン上の 2 つのエージェントが署名付きメッセージと起床コールバックのやり取りをできます——`watchdog` 不要、`agy.exe` 不要、`orochi` 不要。Windows 固有の `CONIN$` 注入と FS ネイティブ通知は**オプショナル extra** です。

これは**キー入力自動化ではありません**。(a) 改変できず、(b) `stdin` しか読まず、(c) アイドル／バックグラウンドにあり、(d) 人間の前面セッションを妨げずに到達しなければならない——そういうエージェントのための、**欠けていた配送チャネル**です。メッセージキューは*協調的な購読者*を前提とし、RPA/`pyautogui` は*前面ウィンドウ*を前提とします。本ツールはそのどちらも前提にしません。

---

## この README は誰のためか

- **OSS 利用者** — コード・LLM function call・MCP ツールから agentknock で*送信する*人またはエージェント。まず **[Install & Quickstart](#install--quickstart-qs1)** を、次に **[公開 API](#公開-api利用者向け)** を読んでください。
- **アダプタ作者** — `agy.exe` 以外の新しいターゲットエージェントへ agentknock を拡張する人。**[capability contract](#アダプタ作者向け-capability-contract)** と **[Adapter Authoring Checklist](./docs/adapters/ADAPTER_AUTHORING.md)** を読んでください。

両読者は以下のセクション——セキュリティ不変条件、マーカー・ライフサイクル、運用ガイド、脅威モデル——を共通して読んでください。

---

## Install & Quickstart (QS1)

### インストール

| インストール形態 | 外部依存 | 同梱されるもの |
|:---|:---|:---|
| `pip install agentknock` | **なし**（標準ライブラリのみ） | `Mailbox`, `PollingNotifier`, `CallbackDeliveryAdapter`, `OutboxDeliveryAdapter`, `FileSystemAliasRegistry`, `AlwaysAliveProbe`, `FileLockLivenessProbe`。同一マシン上の 2 つの Python エージェントが対話するのに必要十分。 |
| `pip install "agentknock[watch]"` | `watchdog>=4.0` | ...＋ `WatchdogNotifier`（Windows: `ReadDirectoryChangesW`、Linux: `inotify`、macOS: `FSEvents`）。`watchdog` が import できない環境では自動で `PollingNotifier` にフォールバック。 |
| `pip install "agentknock[win32]"` | ネイティブ Cython 拡張 | ...＋ `AgyDeliveryAdapter`（Windows `AttachConsole` + `WriteConsoleInputW` → `CONIN$` 注入）＋ `AgyLockLivenessProbe`（Restart Manager API）。Antigravity の `agy.exe` 系ターゲットを起こす場合にのみ必要。 |

3 つの形態は積み上げ可能: `[watch]` + `[win32]` で全部入り。**core 機能は extra なしで全て動きます**——extra はオプションです。

### Quickstart: 外部依存ゼロの往復

同一マシン上の 2 つの Python プロセスだけで動きます。`agy.exe` も `orochi` も Windows カーネル呼び出しも不要。

**受信側** (`recv.py`):

```python
from pathlib import Path
import agentknock
from agentknock.adapters.callback import CallbackDeliveryAdapter
from agentknock.cli import WatchDaemon
from agentknock.core.mailbox import Mailbox

# 1. 自分自身を alias + 安定 target id で自己広告（opt-in R1）
agentknock.register_self("hello-recv", target_id="recv-1", role="greeter")

# 2. 起床コールバックを定義
# 【A1 型レベル R2 保証】: wake は target_id のみを受け取ります（1ビットの呼び鈴原則）。
# 実際の指示本文は、署名付き mailbox チャネルから能動的に安全に取得します。
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

# 3. 常駐バックグラウンド・ウォッチャデーモンを起動
daemon = WatchDaemon(target="hello-recv", adapter=adapter, mailbox=mailbox)
daemon.start()
try:
    input("Press Enter to stop...\n")
finally:
    daemon.stop()
```

**送信側** (`send.py`、別ターミナルで):

```python
import agentknock

# 1. 到達可能な相手を発見する（R1: opt-in 台帳 ＋ 生存性）
for r in agentknock.list_recipients():
    print(r["alias"], r["role"], r["capabilities"])

# 2. その alias に署名付き JSON メッセージを送る
msg_id = agentknock.send(
    recipient="hello-recv",
    content="hello from the OSS Quickstart",
    sender="quickstart-sender",
)
print("sent:", msg_id)
```

**実行:**

```bash
# ターミナル 1
python recv.py
# → 何も出力されず入力待ち

# ターミナル 2（recv.py が走っている間に）
python send.py
# → hello-recv greeter {'surface_custom_json': True, 'honor_sender_locale': True, 'injected_input_trust_label': 'CALLBACK'}
# → sent: <message-id>

# ターミナル 1 に次が出力される:
# → [recv] Woken up for target: recv-1
# → [recv] from=quickstart-sender content='hello from the OSS Quickstart'
```

これだけで**往復完了です: 登録 → 一覧 → 送信 → 受信コールバック**。この後に続く CLI エージェント向けアダプタ・`CONIN$` 注入・Antigravity 連携は、すべて*この上に opt-in で乗る*ものです。

### 常駐ウォッチャデーモン（RT1 CLI ランタイム）

単体プロセスや自動化環境で常駐受信を行う場合、CLI から直接デーモンを起動できます:

```bash
# Antigravity (agy.exe) ターゲットを監視し、フォーカスを奪わずに CONIN$ 起床:
python -m agentknock watch --target <alias-or-uuid> --adapter agy

# カスタム配送アダプタとポーリング通知バックエンドで監視:
python -m agentknock watch --target my-agent --adapter callback --notifier polling --poll-interval 0.5
```

| オプション | 説明 | 既定値 |
| :--- | :--- | :--- |
| `--target`, `-t` | 監視対象の conversation UUID または登録済み alias | None（ルート監視） |
| `--adapter`, `-a` | 起床配送アダプタ（`agy`, `callback`, `outbox`） | `agy` |
| `--mailbox`, `-m` | カスタム mailbox ルートディレクトリ | `~/.gemini/antigravity-cli/brain` |
| `--notifier` | ファイルシステム通知バックエンド（`auto`, `watchdog`, `polling`） | `auto` |
| `--poll-interval` | `polling` 使用時のポーリング間隔（秒） | `0.5` |
| `--wake-locale` | `AgyDeliveryAdapter` の起床ナッジ言語（`en`, `ja`） | ローカル既定 |
| `--output-locale`| `AgyDeliveryAdapter` の出力指示言語（`en`, `ja`） | ローカル既定 |
| `--audit-log` | 追記専用監査ログ `audit.jsonl` のカスタムパス | `audit.jsonl` |
| `--once` | 最初のメッセージを起床処理した後に終了（テスト/CI用） | False |

### Agent Skill による自律マルチエージェント連携サイクル

本ツールを **Agent Skill**（Antigravity や Claude Code 向けの `SKILL.md` など）としてエージェントが利用する場合、3 つのステップで自律連携します:

1. **発見と送信**: 送信側エージェントは `agentknock.list_recipients()` で到達可能な協調相手を確認し、`agentknock.send(recipient=..., content=..., sender=...)` で署名付きタスクを投函します。
2. **フォーカス非強奪の起床**: 受信側マシンで動作する `WatchDaemon` が `undelivered/` へのマーカー着信を検知し、`adapter.wake(target_id)` により作業中の人間オーナーの前面ウィンドウを奪わずに `CONIN$` へ起床ノックを送ります。
3. **エージェント規約に基づく自律処理 ([`docs/AGENTS.ja.md`](./docs/AGENTS.ja.md))**: 起床したエージェントは呼び鈴を認識し、能動的に `<SYSTEM_MESSAGE>` ペイロードから指示をロードします。処理完了後に `undelivered/` マーカーを削除して `read.json` に記録します（R-M1 マーカー所有権）。

---

## ランチャー型オーケストレーターとの違い

デスクトップ・マルチエージェント分野は活発です（例: [hcom]、CLI Agent Orchestrator、agentpipe）。それらは**ランチャー・ラッパー型**です: エージェントを*それら経由で*起動し（`hcom agy`）、ツールが作成した PTY を所有して最初からフックを仕込みます。それらが起こせるのは**自分自身が起動したエージェントだけ**で、Windows では*自前の* ConPTY に書き込む方式であり、自分が作っていないコンソールへ attach することはありません。

agentknock はその**補完的な半分**、すなわち **attach-to-existing（既存プロセスへの後付け attach）**です。エージェントは**普通に**起動されており——ラッパー無し・フック無し・再起動無し——agentknock はその既に動いている改変不能なプロセスを opt-in `AliasRegistry` エントリから見つけ、内部 target id を解決し、差し替え可能な `DeliveryAdapter` を通じて到達します。すべてのエージェントをラッパー経由で再起動できるなら、ランチャーを使ってください。**既に動いていて、改変も再起動もできない**エージェントに到達する必要があるなら、そこが agentknock の埋めるギャップです。両者は競合ではなく補完です。

[hcom]: https://github.com/aannoo/hcom

---

## 中核原則: `wake, not command`（起こすが命令しない）

トランスポートは意図的に **1 ビットの呼び鈴**に絞られています。この**自己限定こそが製品**であり、正当な連携トランスポートと、同じ機構を持つ注入攻撃ツールとを分かつものです。

`CONIN$` 型の起床チャネル（`AgyDeliveryAdapter`）については、その理由は受信側ハーネスが入力に付ける信頼ラベルの**非対称性**にあります（注入される側のエージェントの当事者証言に基づく）:

| 到達経路 | 受信側エージェントが見る出所タグ | 信頼度 |
| :--- | :--- | :--- |
| JSON メッセージファイル（`<Message-ID>.json`） | `<SYSTEM_MESSAGE>` — `not sent by the user`、`sender` を持つ | **署名付き／帰属可能** |
| `CONIN$` 注入（帰属不能な起床チャネル） | `source: USER_EXPLICIT` / `<USER_REQUEST>` — 人間オーナー自身のキー入力と区別不能 | **ゼロ** |

`CONIN$` へ注入された文字列は**人間オーナーが打ったかのように**届くため、リモート送信者にその文字列を制御させることは**オーナーへのなりすまし＝事実上の権限昇格**にあたります。したがって:

- 呼び鈴（`CONIN$`）が運ぶのは**無害でローカル定義された起床ナッジのみ**。
- 手紙（実際の指示）は**署名付き JSON チャネル**を通り、エージェントが*能動的に・帰属付きで*読みます。

v0.3 ではこの原則は**型レベル**でも強制されるようになりました: `DeliveryAdapter.wake()` は text 引数をそもそも持ちません（下記 § R2 参照）。

---

## 4 つの拡張点 (DI)

環境差分はすべてプラグインです。core（Message + Mailbox + Watcher）は標準ライブラリのみ・orochi 非依存で、4 つの拡張点によって core をフォークせずにあらゆるターゲットに適合できます。

| 拡張点 | 役割 | 既定実装 | 差し替えるのは... |
|:---|:---|:---|:---|
| **`Notifier`** | mailbox への新着マーカー検知 | `watchdog` が import 可能なら `WatchdogNotifier`、そうでなければ `PollingNotifier`（既定 500 ms、env `AGENTKNOCK_POLL_INTERVAL_SEC`）に自動フォールバック（B6） | カスタム FS イベント／Kafka 風トリガー／HTTP webhook 等をトリガー源にしたい時 |
| **`DeliveryAdapter`** | 検証済みメッセージ到着時にターゲットを起こす。**`wake(target_id: str) -> bool`** — text 引数なし（A1、型レベル R2） | `CallbackDeliveryAdapter`（インプロセス、Quickstart）／`OutboxDeliveryAdapter`（ファイルブリッジ配送、BR1: Gemini → Claude）／`AgyDeliveryAdapter`（オプション、`[win32]` extra）`CONIN$` 注入 | 新ターゲットエージェント対応時。[Adapter Authoring Checklist](./docs/adapters/ADAPTER_AUTHORING.md) 参照 |
| **`AliasRegistry`** | opt-in 自己広告台帳、**Mailbox から独立（A3）**。`register` / `unregister` / `resolve` / `list_active` を公開 | `FileSystemAliasRegistry`（`~/.agentknock/registry/<alias>.json`）／`InMemoryAliasRegistry`（テスト用） | 複数マシン間で共有 SQLite やネットワーク台帳を使いたい時 |
| **`LivenessProbe`** | ターゲットの生存判定（A2）。`is_alive(target_id) -> bool` | `AlwaysAliveProbe`（常に true）／`FileLockLivenessProbe`（任意 Mailbox）／`AgyLockLivenessProbe`（`[win32]`、Restart Manager 由来） | HTTP ping・pidfile 式・クラスタ対応生存判定が必要な時 |

`AgentKnock` クラスはこの 4 つを配線する DI コンテナです（下記 API セクション参照）。

---

## 公開 API（利用者向け）

### 二層ファサード (B5): `AgentKnock` クラス＋トップレベル糖衣

明示配線したいときは DI クラスを、既定構成で一行で済ませたいときはトップレベル糖衣関数を使います。

```python
from pathlib import Path
from agentknock import AgentKnock
from agentknock.core.mailbox import Mailbox
from agentknock.core.registry import FileSystemAliasRegistry
from agentknock.core.probe import AlwaysAliveProbe

# DI コンテナ — 明示配線
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
# トップレベル糖衣 — プロセス全体の既定 AgentKnock に委譲
import agentknock
agentknock.configure_default(mailbox=..., registry=..., probe=...)  # 任意
agentknock.register_self("my-agent", target_id="agent-1", role="worker")
msg_id = agentknock.send("peer-agent", "hello", sender="my-agent")
agentknock.unregister_self("my-agent")  # キルスイッチ
```

公開トップレベル API（`agentknock/__init__.py`）:

```python
def send(recipient: str, content: str, *, sender: str) -> str: ...
def list_recipients() -> list[Recipient]: ...
def register_self(alias: str, target_id: str, *,
                  role: str = "",
                  capabilities: Capabilities | None = None,
                  ttl_sec: float = 86400.0) -> None: ...
def unregister_self(alias: str) -> bool: ...   # キルスイッチ
def configure_default(*, mailbox=None, registry=None, probe=None) -> None: ...

class Recipient(TypedDict):
    alias: str            # send() の recipient にはこれを渡す
    role: str
    capabilities: dict    # アダプタの capability contract を反映
```

### 送信者が制御できるもの、できないもの

| フィールド | 送信者制御？ | 注記 |
| :--- | :--- | :--- |
| `recipient` | はい | `list_recipients` が返した alias のみ: `ALIAS_PATTERN` に合致し、かつ `UUID_REGEX` に**合致しない**こと。生 target id は `is_valid_alias()` および MCP スキーマ両方で拒否されます。 |
| `content` | はい | 自由記述。メッセージ本体。署名付き JSON チャネルを通ります。 |
| `sender` | はい | 自己申告の識別子（書式: `^[a-zA-Z0-9_-]{1,64}$`）。帰属の**主張**であって認証ではありません。 |
| 起床ナッジのバイト列 | **いいえ** | 各 DeliveryAdapter 内部でローカル定数から解決。**型レベル: `wake()` は text 引数を持ちません**（A1）。受信側の起床チャネルに届くバイトを送信者は左右できません。 |
| target `target_id` | **いいえ** | `AliasRegistry` ／ `LivenessProbe` 内部で解決。公開 API からは完全に隠蔽。 |
| 出力言語 | **いいえ** | 帰属不能な起床チャネル（例: `AgyDeliveryAdapter`）では受信側ローカルで固定。帰属可能チャネルでは JSON メタデータに乗り、アダプタの `honor_sender_locale` capability に従う。 |

右半分の「できない」側こそ**構造的な R2** の意味するところです: 公開 API には、送信者が起床チャネルに影響を与えうるフィールド・キーワード引数・環境変数が**存在しません**。能力そのものが呼び出し可能な形で存在しないのです。

### MCP ツールスキーマ

公開関数は MCP ツール（`python -m agentknock.mcp_server --stdio` で起動）としても露出されます。**スキーマに現れるものが送信者が送れるものであり、それ以外はアダプタに届きません。** ここで capability contract が機械的に強制されます——アダプタが honor 不能と宣言したフィールド（例: `surface_custom_json: No` 時のロケールヒント）はそもそもツールスキーマに入れません。

```jsonc
// MCP tool: send
{
  "name": "send",
  "input_schema": {
    "type": "object",
    "required": ["recipient", "content", "sender"],
    "additionalProperties": false,            // ← MCP 層での R2
    "properties": {
      "recipient": { "type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$" },
      "content":   { "type": "string" },
      "sender":    { "type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$" }
    }
  }
}

// MCP tool: list_recipients — 入力なし
{ "name": "list_recipients",
  "input_schema": { "type": "object", "additionalProperties": false } }
```

`additionalProperties: false` は load-bearing: アダプタが対応表明していないフィールドを送信者が紛れ込ませられないことを、MCP 層で強制します。

### UUID 隠蔽

内部 target id（例: `agy.exe` の conversation UUID）はアダプタ内部でのみ使う**内部**識別子です。送信者は決して目にせず、`list_recipients` は alias のみを返します。alias は**プロセス再起動をまたいで安定**（registry に存在）であるのに対し、内部 target id は通常安定ではありません。

---

## Discovery（opt-in 台帳）— 不変条件 R1

### R1 — ランデブー印の逆引き限定

**対象は、対象自身が公開した成果物からのみ addressable である。** agentknock は **`AliasRegistry`**（opt-in 自己広告）と **`LivenessProbe`**（このターゲットはまだ生きているか？）の 2 つから受信者を解決します。agentknock は候補探索のために**決して**プロセス・ウィンドウ・ネットワークピアを列挙しません——`Process32First` も、`tasklist` の走査も、「lock を握っているプロセスならどれでも起こす」もありません。

2 箇所で強制されます:

- 公開 API 側: `list_recipients` は設定された `AliasRegistry` を読みます——プロセステーブルではありません。
- アダプタ内部: 対象解決は台帳の alias → `target_id` マッピングを使い、必要なら `LivenessProbe` に照会してから起床します。

### 3 条件フィルタ

エントリが `list_recipients` から返されるのは、**次の 3 条件すべて**が満たされる時だけです:

1. 台帳エントリが存在し、整形式である、**かつ**
2. `ttl` が未失効、**かつ**
3. 設定された `LivenessProbe.is_alive(target_id)` が `True` を返す。

既定構成:

| 構成 | AliasRegistry | LivenessProbe |
|:---|:---|:---|
| `pip install agentknock` Quickstart | `FileSystemAliasRegistry`（`~/.agentknock/registry/`） | `AlwaysAliveProbe`（常に真 — Quickstart 向け） |
| ファイルロック付きピア | 同上 | `FileLockLivenessProbe`（`.lock` ファイル ＋ OS レベル生存判定） |
| `pip install "agentknock[win32]"` ＋ `AgyDeliveryAdapter` | 同上 | `AgyLockLivenessProbe`（Restart Manager による `presence/<cid>.lock` の逆引き） |

### 台帳エントリのスキーマ

到達可能になりたいエージェントは次を公開します:

```
~/.agentknock/registry/<alias>.json
{
  "alias": "hello-recv",
  "target_id": "recv-1",                      // アダプタ内部用; agentknock 外へは出ない
  "role": "greeter",
  "capabilities": {                           // アダプタの capability contract を反映
    "surface_custom_json": true,
    "honor_sender_locale": true,
    "injected_input_trust_label": "ATTRIBUTED_METADATA"
  },
  "ttl": "2026-11-13T00:00:00Z"               // ISO-8601 の失効時刻（UTC）
}
```

登録は**自己広告のみ**です: エージェントは自分自身を登録し、あるエージェントが他を登録することはできません。`unregister_self(alias)` は**キルスイッチ**です——一度呼べばエントリは即座に外れ、以降の discovery と送信はどちらも止まります。失効エントリは自動的に外れます。

### alias の書式

alias は次の両方を満たさなければなりません:

1. `ALIAS_PATTERN`（`^[a-zA-Z0-9_-]{1,64}$`）に合致すること（`ALLOWED_SENDERS` と整合）、**かつ**
2. `UUID_REGEX`（`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`）に**合致しない**こと。生の conversation ID は alias として**無効**です——アダプタ内部の値であり、recipient として使うことは決して許されません。拒否は `is_valid_alias()` および MCP ツールスキーマ両方で強制されます。

---

## アダプタ作者向け: capability contract

> これはアダプタ作者が宣言しなければならないものです。この 3 フィールドに基づいて、プラットフォームは「OSS 利用者があなたのターゲットに対して何を安全にできるか」を決めます。

| フィールド | 意味 |
| :--- | :--- |
| `surface_custom_json` | このエージェントはカスタム JSON フィールドを作業文脈に surface するか。**No** なら、送信者提供のメタデータ（例: ロケールヒント）はエージェントに届かず honor 不能——送信者が何を送っても。 |
| `honor_sender_locale` | 送信者提供の出力ロケール選好を honor するか。`surface_custom_json: Yes` の時のみ意味を持つ。**既定: No。** |
| `injected_input_trust_label` | ターゲットが起床チャネル経由の入力に付ける信頼ラベル（`CONIN$` 相当のキーストローク配送なら `USER_EXPLICIT`、署名付きインプロセス callback や署名付き outbox JSON なら `ATTRIBUTED_METADATA`、OS レベルシステムメッセージなら `SYSTEM`、...）。このターゲット向けの `AGENTS.md` 規約の厳格度を決める。 |

### 同梱リファレンスアダプタ

| アダプタ | インストール形態 | `surface_custom_json` | `honor_sender_locale` | `injected_input_trust_label` |
|:---|:---|:---|:---|:---|
| `CallbackDeliveryAdapter`（`adapters/callback.py`） | base | Yes | Yes | `ATTRIBUTED_METADATA` |
| `OutboxDeliveryAdapter`（`adapters/outbox.py`、BR1） | base | Yes | Yes | `ATTRIBUTED_METADATA` |
| `AgyDeliveryAdapter`（`adapters/agy.py`） | `[win32]` | No | No | `USER_EXPLICIT` |

`AgyDeliveryAdapter` の完全な scope contract は **[`docs/adapters/SCOPE_AGY.md`](./docs/adapters/SCOPE_AGY.md)** を参照してください。

**帰結（好みでなく演繹）:** `agy.exe` はカスタム JSON フィールドを surface せず、*かつ* language mirroring が帯域外ヒントを打ち消すため、`agy.exe` の出力言語は実質 100 % ローカル専権です——ローカルのナッジ定数のみで決まります。したがって `force-local` が*このアダプタに限り*唯一正しい既定であり、これは実装事実から導かれます。`Yes/Yes` を宣言するアダプタは送信者提供ロケールを advisory として surface することがあります。

### 新アダプタの実装

**[`docs/adapters/ADAPTER_AUTHORING.md`](./docs/adapters/ADAPTER_AUTHORING.md)** のチェックリストを参照。義務事項:

1. 3 フィールドの capability を宣言する。
2. `wake(target_id) -> bool` を **text 引数なし**で実装する（A1）。
3. `consumes_marker_on_wake` を選ぶ（`wake()` が同期的に配送完了するか、受信側が自分でマーカーを消費する out-of-process 型か？ R-M1）。
4. アダプタの隣に `SCOPE_<ADAPTER>.md` を公開する（B4）。
5. `agentknock.testing.assert_send_cannot_reach(...)` を用いて自アダプタの forbidden set で R2 構造テストを同梱する（B1）。

---

## セキュリティ不変条件

### R1 — ランデブー印の逆引き限定

上の [Discovery](#discoveryopt-in-台帳-不変条件-r1) 参照。

### R2 — 起床チャネルに送信者制御のバイトを乗せない（型レベル）

v0.3 で R2 は**型レベル**にも持ち上げられた**構造的特性**です。`DeliveryAdapter` Protocol は次のように定義されます:

```python
class DeliveryAdapter(Protocol):
    def wake(self, target_id: str) -> bool: ...
```

**text 引数はそもそも存在しません。** アダプタ作者が wake 呼び出しに送信者由来文字列を渡す経路は、メソッドの形状として*不可能*です。各アダプタはローカル起床ナッジカタログを**自身の内部**に保持します（例: `AgyDeliveryAdapter` は i18n `WAKE_NUDGE_CATALOG` をコンストラクタ引数のロケール設定から解決）。

さらに、`send()` の静的コールグラフは`DeliveryAdapter` には一切到達しません。AST テスト
[`tests/test_d4_r2_r1_invariants.py::test_r2_send_callgraph_no_win_console_conin`](./tests/test_d4_r2_r1_invariants.py)
が `send()` を host するモジュールをパースし AST を推移的に追跡し、禁止シンボルまたは動的 import が現れた時点で fail-closed します。**新規アダプタ**向けには標準テストヘルパを用意しています:

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

アダプタに R2 テストを同梱することは Adapter Authoring Checklist の**義務**です（B1）。

core 側の実行時裏付け 2 本: `test_r2_send_with_win_console_unavailable`（`sys.modules` から `_win_console` を抜いても `send` が import 可能）と `test_r2_public_namespace_only_send_list_recipients`（トップレベルモジュールの公開名を強制）が併置されています。

### 起床シグナルとペイロードの分離

起床チャネルが配送するのは起床シグナルのみ。送信者制御のコンテンツ（タスク・タイトル・メタデータ）は署名付き JSON ファイルに置かれ、帰属可能な `<SYSTEM_MESSAGE>` チャネル経由でエージェントが読みます。

### ガバナンス／機構の二層モデル

- **ガバナンス層 — 分離統治。** 起床ナッジの i18n と出力言語の i18n は信頼境界の反対側にあります。R2 は起床ナッジにのみ発火し、出力言語の選好は注入バイトを変えないため R2 に抵触しません。
- **機構層 — language mirroring。** `AgyDeliveryAdapter` では LLM が直前の `<USER_REQUEST>` の言語に強く同調するため、出力言語指示をローカルナッジ定数に束ねて mirroring を味方につけます。これは*アダプタ内部*の関心事（B3）であり、帰属可能チャネルを持つアダプタにはそもそもこの結合がありません。

---

## マーカー・ライフサイクル（R-M1 … R-M4）

受信側ウォッチャは `wake()` の後に `undelivered/<id>` マーカーを無条件で削除**しません**。v0.3 は「1.5 秒で早すぎる刈り取り」バグを防ぐためのライフサイクル契約を追加しました。

### R-M1 — マーカー削除権は受信側専有

`DeliveryAdapter.consumes_marker_on_wake == False`（例: `AgyDeliveryAdapter`、*ターゲットが out-of-process* でマーカーを自分で読むアダプタ）の場合、ウォッチャは `wake()` の後に**決してマーカーを unlink しません**。ACK は受信側の仕事です。

`consumes_marker_on_wake == True`（例: `CallbackDeliveryAdapter`、ウォッチャスレッド内でコールバックを同期実行＝配送完了となるアダプタ）の場合のみ、ウォッチャは成功後にマーカーを unlink します——コールバックそのものが ACK だったからです。

### At-Most-Once（In-Memory Cooldown）

ウォッチャはメモリ上に `_woken_at: dict[message_id, monotonic_time]` を保持し、同一 message_id に対しては `rewake_cooldown_sec`（既定 **60 秒**）の間、再起床をスキップします。マーカーが残り続けても二重配送は発生しません。

### R-M2 — 送信側による Watcher 代行の禁止

`send()` はファイル投函のみです。ウォッチャは必ず受信プロセス内（または長命の受信側 companion）で動かします。送信側が受信側の代わりにウォッチャを走らせることは、本契約が防いでいるライフサイクルバグを再発させます。

### R-M3 / R-M4 — 孤児マーカー Reaper と `audit.jsonl` 観測性

バックグラウンド Reaper は、`stale_marker_ttl_sec`（既定 **300 秒**）経過後、または受信側が既に消費済みと記録した場合にのみ、孤児マーカーを清掃します。`audit.jsonl` のイベント遷移:

```
CREATED → WOKEN → CONSUMED            # happy path
CREATED → WOKEN → RETRY → … → CONSUMED
CREATED → WOKEN → RETRY → … → DEAD_LETTER
CREATED → … → REAPED                  # 孤児清掃
```

シーケンスレベルの詳細は [`docs/WORK_FLOW.md`](./docs/WORK_FLOW.md) を参照。

---

## Scope contract — アダプタ毎

agentknock の core は cross-platform（純 Python、Win32 非依存）です。各 delivery adapter が自分の scope contract を公開します:

- **[`docs/adapters/SCOPE_AGY.md`](./docs/adapters/SCOPE_AGY.md)** — `AgyDeliveryAdapter` の scope（Windows 限定・実コンソール同一セッション同一インテグリティ・昇格/WSL/リモート/パイプ stdin 非対応）。
- `CallbackDeliveryAdapter` — cross-platform、インプロセスのみ（他プロセスには届かない）。
- `OutboxDeliveryAdapter` — cross-platform、共有 FS 上で cross-process。scope は「両エージェントが mailbox ディレクトリを読み書きできる場所ならどこでも」。

アダプタ作者は自アダプタ用に `SCOPE_<ADAPTER>.md` を**必ず**同梱します（B4）——Adapter Authoring Checklist を参照。

---

## 国際化（i18n）— v0.3 ではアダプタスコープ（B3）

v0.3 では i18n 起床ナッジカタログは**アダプタ内部**の関心事であり、core レベルの設定ではありません。起床チャネルが*帰属不能*（ゆえに language mirroring 頼みで出力言語を誘導せざるを得ない）アダプタでのみ意味を持ちます——`AgyDeliveryAdapter` が典型例です。帰属可能アダプタ（callback、outbox、HTTP）にはカタログは不要です: 出力ロケールは帰属可能チャネルを通じて JSON メタデータに乗り、R2 に抵触せずにエージェントが読めます。

`AgyDeliveryAdapter` の場合、カタログと解決規則は `agentknock/adapters/agy.py` に存在します:

```python
# ローカル上書きのみ — 送信者入力は決して参照しない:
#   1. env CONINJECT_WAKE_LOCALE / CONINJECT_OUTPUT_LOCALE / CONINJECT_LOCALE
#   2. AgyDeliveryAdapter(wake_locale="ja", output_locale="ja")
#   3. "en"（fail-closed; 不正/未対応 → en）
```

### Layer B（送信者提供ロケール）— アダプタ毎

送信者提供の出力ロケールは意図的に **core には存在しません**。`surface_custom_json: Yes` **かつ** `honor_sender_locale: Yes` を契約で宣言するアダプタでは surface することがあります——ただし、ローカルポリシーが上書き可能な検証済み enum *advisory* としてのみ、コマンドとしてではなく、`en` に fail-closed します。

---

## 運用ガイド

フォーカスを奪わない特性は、機能であると同時にリスクです——帰属不能な起床チャネルへの注入は設計上、人間に不可視です。以下の 3 つの統制は人間の監督を取り戻すもので、**運用上の必須要件**であり任意オプションではありません。

### 1. 監査ログ（追記専用）

各起床は `who → whom · when · message-id · result` を**追記専用**・改ざん検知可能なログ（既定 `~/.agentknock/audit.jsonl`）に記録します。これは対象ウィンドウに可視の痕跡を残さない可能性のある行為の、否認防止記録です。ログは受信側ウォッチャが書き出します。

```
2026-11-12T12:14:07Z  event=WOKEN   sender=claude-code  recipient=gemini-ipc  msg=4b1e…  result=OK
2026-11-12T12:14:09Z  event=CONSUMED  msg=4b1e…
```

### 2. 可視インジケータ

ローカル UI 要素（ステータスライン／トレイ／バンド——注入経路の**外**）が、人間オーナー自身の `wake_locale` で、起床が起きていることと誰がトリガーしたかを表示します。これは *UI ブラインド*攻撃（オーナーが読めない言語へ出力を強制し、不審挙動の監査を遅らせる）への対策です。

### 3. キルスイッチ

`unregister_self(alias)` が discovery レイヤのキルスイッチです——一度呼べばエントリは外れ、以降の discovery と送信先選択はどちらも即座に止まります。既存のループガード（無限ループ防止の `sender == "user"` スキップ ＋ レート制限 / ループ検出）と `rewake_cooldown_sec` を組み合わせ、起床ストームを抑え込みます。リトライ上限超過の失敗メッセージは `dead_letter/<id>.json` に隔離され、無限再試行はしません。

---

## 脅威モデル（一行）

> 正当性は*何を注入できるか*では証明されません——機構はキーロガー型マルウェアと同一です。正当性は**制約**で証明されます: 対象自身が公開した成果物からのみ受信者を解決（R1 — 決して列挙しない）、起床チャネルはローカルで無害な 1 ビットの起床シグナルしか運べない（R2 — 型レベル: `wake()` に text 引数そのものが無い）、あらゆる指示は署名付きチャネルを通す、そして人間が見て止められる状態を保つ。

---

## 関連資料
- **[`docs/adapters/ADAPTER_AUTHORING.md`](./docs/adapters/ADAPTER_AUTHORING.md)** — 新アダプタ作者が満たすべきチェックリスト（R2 テスト、Scope doc、capability 宣言、マーカーライフサイクル選択）。
- **[`docs/adapters/SCOPE_AGY.md`](./docs/adapters/SCOPE_AGY.md)** — リファレンス `AgyDeliveryAdapter` の scope contract。
- [`docs/WORK_FLOW.md`](./docs/WORK_FLOW.md) — ディレクトリのライフサイクル、シーケンス図、関数別仕様、単体テスト仕様。
- [`docs/AGENTS.ja.md`](./docs/AGENTS.ja.md) — **注入される側**のエージェントが従う規約。

---

## ライセンス (License)

`agentknock` は **[GNU Affero General Public License v3.0 (AGPL-3.0-or-later)](./LICENSE)** の下で公開されています。

- **個人・ローカル環境での利用**: ご自身のローカル環境や個人の開発ワークフローにおける利用・検証・カスタマイズは完全に自由です。
- **改変物の再配布・クラウド／ネットワークサービスへの組み込み**: 本ソフトウェアを改変して配布する場合や、ネットワーク越しに提供するクラウドサービス・ホスティングサービスの一部として組み込んで提供する場合は、適切な著作権表示（Attribution）を行った上で、同じく **AGPL-3.0** の下でソースコードを公開する必要があります。

もし `agentknock` を試してみて役に立ったり面白いと感じていただけたら、ぜひ GitHub の ⭐ **Star** や **Issue / Discussion** で感想・フィードバックをいただけると開発の大きな励みになります！
