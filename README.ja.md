# agentknock

> **あなたの端末エージェントたちが、あなたの目の前で互いを叩き起こす——
> フォーカスを奪わずに。呼び鈴を鳴らすだけ。命令は決して出さない。**

[English](./README.md) | **日本語**

**agentknock** は、**デスクトップ・マルチエージェント連携のためのトランスポート層**です。送信側エージェントは `send(recipient, content, sender=...)` を呼び、受信側ローカルの常駐ウォッチャが対象の対話型 CLI/TUI の AI エージェント（例: Antigravity の `agy.exe`、およびアダプタ経由で他の端末エージェント）を起床させます——Windows カーネル API（`AttachConsole` + `WriteConsoleInputW`）で対象プロセスのコンソール入力バッファ（`CONIN$`）へキー入力を注入する形で。対象ウィンドウはフォーカスを一切得ないため、人間のオーナーは作業を妨げられません。宛先プロセスは **PID** で特定し、それは対象自身が公開した presence ロックから逆引きします——ウィンドウタイトルでもプロセス列挙でもありません。

これは**キー入力自動化ではありません**。(a) 改変できず、(b) `stdin` しか読まず、(c) アイドル／バックグラウンドにあり、(d) 人間の前面セッションを妨げずに到達しなければならない——そういうエージェントのための、**欠けていた配送チャネル**です。メッセージキューは*協調的な購読者*を前提とし、RPA/`pyautogui` は*前面ウィンドウ*を前提とします。本ツールはそのどちらも前提にしません。

---

## この README は誰のためか

本ドキュメントは**二つの異なる読者**を対象にしており、各読者向けのセクションを1つずつ用意しています。自分に合う方を読んでください:

- **OSS 利用者** — LLM の function call または MCP ツールから agentknock で*送信する*人またはエージェント。**[利用者向け: 公開 API](#利用者向け-公開-api)** を読んでください。できることはそのツールスキーマ1つに集約されています——そこに無いものは送れません。
- **アダプタ作者** — `agy.exe` 以外の新しいターゲットエージェントへ agentknock を拡張する人。**[アダプタ作者向け: capability contract](#アダプタ作者向け-capability-contract)** を読んでください。あなたのアダプタが宣言する3つの load-bearing フィールドが、「プラットフォームが OSS 利用者にあなたのターゲットに対して何を安全に許すか」を決めます。

両読者は以下のセクション——セキュリティ不変条件、scope contract、i18n、運用ガイド、脅威モデル——を共通して読んでください。

---

## ランチャー型オーケストレーターとの違い

デスクトップ・マルチエージェント分野は活発です（例: [hcom]、CLI Agent Orchestrator、agentpipe）。それらは**ランチャー・ラッパー型**です。エージェントを*それら経由で*起動し（`hcom agy`）、ツールが作成した PTY を所有して最初からフックを仕込みます。それらが起こせるのは**自分自身が起動したエージェントだけ**で、Windows では*自前の* ConPTY に書き込む方式であり、自分が作っていないコンソールへ attach することはありません。

agentknock はその**補完的な半分**、すなわち **attach-to-existing（既存プロセスへの後付け attach）**です。エージェントは**普通に**起動されており——ラッパー無し・フック無し・再起動無し——agentknock はその既に動いている改変不能なプロセスを presence ロックから見つけ、PID を解決し、その*既存の*コンソール入力バッファへ注入します。すべてのエージェントをラッパー経由で再起動できるなら、ランチャーを使ってください。**既に動いていて、改変も再起動もできない**エージェントを起こす必要があるなら、そこが agentknock の埋めるギャップです。両者は競合ではなく補完です。

[hcom]: https://github.com/aannoo/hcom

エンドツーエンドのシーケンスと関数別リファレンスは [`WORK_FLOW.md`](./WORK_FLOW.md) を、**注入される側**のエージェントが従うべき規約は [`AGENTS.ja.md`](./AGENTS.ja.md) を、現アーキテクチャを決定した ADR は [`../orochi/DESIGN-0001-agentknock-positioning.md`](../orochi/DESIGN-0001-agentknock-positioning.md) を参照してください。

---

## 中核原則: `wake, not command`（起こすが命令しない）

トランスポートは意図的に **1 ビットの呼び鈴**に絞られています。この**自己限定こそが製品**であり、正当な連携トランスポートと、同じ機構を持つ注入攻撃ツールとを分かつものです。

理由は、受信側ハーネスが入力に付ける信頼ラベルの**非対称性**にあります（注入される側のエージェントの当事者証言に基づく）:

| 到達経路 | 受信側エージェントが見る出所タグ | 信頼度 |
| :--- | :--- | :--- |
| JSON メッセージファイル（`<Message-ID>.json`） | `<SYSTEM_MESSAGE>` — `not sent by the user`、`sender` を持つ | **署名付き／帰属可能** |
| `CONIN$` 注入（本ツール） | `source: USER_EXPLICIT` / `<USER_REQUEST>` — 人間オーナー自身のキー入力と区別不能 | **ゼロ** |

`CONIN$` へ注入された文字列は**人間オーナーが打ったかのように**届くため、リモート送信者にその文字列を制御させることは**オーナーへのなりすまし＝事実上の権限昇格**にあたります。したがって:

- 呼び鈴（`CONIN$`）が運ぶのは**無害でローカル定義された起床ナッジのみ**。
- 手紙（実際の指示）は**署名付き JSON チャネル**を通り、エージェントが*能動的に・帰属付きで*読みます。

---

## 利用者向け: 公開 API

> これが OSS 利用者——人・エージェント・MCP クライアント——が agentknock でできることのすべてです。**関数 2 つ、ルール 1 つ: スキーマに無いものは送れない。**

### 公開面

`agentknock` から公開されるのはこの 2 名のみです。両者は LLM ハーネス連携のため MCP ツール（`send` / `list_recipients`）としても露出されます。正本のスタブは `agentknock/_contract.py` にあります。

```python
# agentknock/__init__.py

def send(recipient: str, content: str, *, sender: str) -> str:
    """signed message を `recipient`（list_recipients が返す alias）へ配送し、
    message_id を返す。内部では、`send()` は alias を **L2 の純関数**
    `_resolve_alias_from_registry`（素のファイル読取のみ——Win32 もアダプタ
    import も無し）で解決し、mailbox に書き込む。この関数の静的コールグラフ
    は `_adapter` / `_win_console` / CONIN$ / PID / inject に到達しない
    ——セキュリティ不変条件 § R2 を参照。"""

def list_recipients() -> list[Recipient]:
    """opt-in discovery のみ（R1 参照）。registry エントリが未失効かつ
    presence ロックが生存中の、alias で addressable な相手のみ返す。
    conversation_id は決して返さない。"""

class Recipient(TypedDict):
    alias: str            # send() の `recipient` にはこれを渡す
    role: str
    capabilities: dict    # アダプタの capability contract をそのまま反映
```

### 送信者が制御できるもの、できないもの

| フィールド | 送信者制御？ | 注記 |
| :--- | :--- | :--- |
| `recipient` | はい | `list_recipients` が返した alias のみ: `ALIAS_PATTERN` に合致し、かつ `UUID_REGEX` に**合致しない**こと（生の conversation ID は `is_valid_alias()` および MCP スキーマ両方で拒否されます）。 |
| `content` | はい | 自由記述。メッセージ本体。署名付き JSON チャネルを通ります。 |
| `sender` | はい | 自己申告の識別子（書式: `^[a-zA-Z0-9_-]{1,64}$`）。帰属の**主張**であって認証ではありません。 |
| 起床ナッジ文字列 | **いいえ** | 受信側ローカルの `WAKE_NUDGE_CATALOG` から解決されます。`CONIN$` に書き込まれるバイトを送信者は左右できません。 |
| 対象 PID | **いいえ** | アダプタ内で、受信側の自己公開 presence ロックから解決。送信者は PID を一切見ません。 |
| 対象 `conversation_id` | **いいえ** | アダプタ内部のみ。公開 API から完全に隠蔽。 |
| 出力言語 | **いいえ** | 受信側ローカルのナッジで固定。[i18n § Layer B 削除](#layer-b送信者提供ロケール-v1-では意図的に不在) 参照。 |

右半分の「できない」側こそ**構造的な R2** の意味するところです: 公開 API には、送信者が `CONIN$` に影響を与えうるフィールド・キーワード引数・環境変数が**存在しません**。能力そのものが呼び出し可能な形で存在しないのです。

### MCP ツールスキーマ

公開関数は MCP ツールとしても露出されます。**スキーマに現れるものが送信者が送れるものであり、それ以外はアダプタに届きません。** ここで capability contract が機械的に強制されます——アダプタが honor 不能と宣言したフィールド（例: `surface_custom_json: No` の時のロケールヒント）は、そもそもツールスキーマに入れません。

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
{ "name": "list_recipients", "input_schema": { "type": "object", "additionalProperties": false } }
```

`additionalProperties: false` は load-bearing です: アダプタが対応表明していないフィールドを送信者が紛れ込ませられないことを、MCP 層で強制します。

### UUID 隠蔽

conversation ID はアダプタ内部でのみ使う**内部**識別子です。送信者は決して目にせず、`list_recipients` は alias のみを返します。alias は**プロセス再起動をまたいで安定**（registry ファイルに存在）であるのに対し、conversation ID は安定ではありません。これにより送信者は受信側のライフサイクルから切り離され、R1 の「自己公開ランデブー」原則に**安定した公開アドレス**が与えられます。

---

## Discovery（opt-in 台帳）— 不変条件 R1

### R1 — ランデブー印の逆引き限定

**対象は、対象自身が公開した成果物からのみ addressable である。** agentknock はそのうち 2 つを組み合わせて受信者を解決します: **opt-in registry エントリ**（alias → capabilities）と、**presence ロック**（生存性）。agentknock は候補探索のために**決して**プロセス列挙を行いません。`Process32First` も、`tasklist` の走査も、「presence ロックを握っているウィンドウならどれでも起こす」もありません。

これが R1 であり、2 箇所で強制されます:

- 公開 API 側: `list_recipients` は registry ディレクトリを読みます——プロセステーブルではありません。登録していないプロセスは addressable ではありません。
- アダプタ内部: PID 解決は対象自身の presence ロック（パス指定の逆引き）を使い、走査しません。

### Registry スキーマ

到達可能になりたいエージェントは次を公開します:

```
~/.gemini/antigravity-cli/registry/<alias>.json
{
  "alias": "gemini-ipc",                      // ^[a-zA-Z0-9_-]{1,64}$
  "conversation_id": "<uuid>",                // アダプタ内部用; agentknock 外へは決して出ない
  "role": "IPC core",
  "capabilities": {                           // アダプタの capability contract を反映
    "surface_custom_json": false,
    "honor_sender_locale": false,
    "injected_input_trust_label": "USER_EXPLICIT"
  },
  "ttl": "2026-11-09T00:00:00Z"               // ISO-8601 の失効時刻（UTC）
}
```

登録は**自己広告のみ**です: エージェントは自分自身を登録し、あるエージェントが他を登録することはできません。失効したエントリは自動的に外れます。

### `list_recipients` のフィルタ

エントリが返されるのは、**次の 3 条件すべて**が満たされる時だけです:

1. registry ファイルが存在し、整形式である、**かつ**
2. `ttl` が未失効、**かつ**
3. `conversation_id` が指す presence ロックを、生存中のプロセスが保持している。

registry エントリだけでは addressable になりません。未登録の presence ロックだけでも addressable になりません。ランデブーは両方必要です。

### alias の書式

alias は次の両方を満たさなければなりません:

1. `ALIAS_PATTERN`（`^[a-zA-Z0-9_-]{1,64}$`）に合致すること。`ALLOWED_SENDERS` の書式と整合させ、送信者帰属と受信者アドレッシングで同一の識別子正規表現を共有します。**かつ**
2. `UUID_REGEX`（`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`）に**合致しない**こと。生の conversation ID は alias として**無効**です——アダプタ内部の値であり、recipient として使うことは決して許されません。拒否は `is_valid_alias()` および MCP ツールスキーマ両方で強制されます。

---

## アダプタ作者向け: capability contract

> これはアダプタ作者が宣言しなければならないものです。この 3 フィールドに基づいて、プラットフォームは「OSS 利用者があなたのターゲットに対して何を安全にできるか」を決めます。

起床機構は `agy.exe` を越えて一般化しますが、その*安全性*は無料では転用できません——各ターゲットのハーネスが入力をどうタグ付け・surface するかに依存します。よって各ターゲットアダプタは以下の第一級フィールドを**必ず**宣言しなければなりません。これらはドキュメントではなく load-bearing で、プラットフォームが送信者に何を試させるかを決定します。

| フィールド | 意味 |
| :--- | :--- |
| `surface_custom_json` | このエージェントはカスタム JSON フィールドを作業文脈に surface するか。**No** なら、送信者提供のメタデータ（例: ロケールヒント）はエージェントに届かず honor 不能——送信者が何を送っても。 |
| `honor_sender_locale` | 送信者提供の出力ロケール選好を honor するか。`surface_custom_json: Yes` の時のみ意味を持つ。**既定: No。** |
| `injected_input_trust_label` | ターゲットが `CONIN$` 注入入力に付ける信頼ラベル（`USER_EXPLICIT`／低信頼 `SYSTEM` タグ等）。エージェント側規約（`AGENTS.ja.md`）の厳格度を決める。 |

### リファレンスアダプタ — `agy.exe`（Antigravity CLI, Go 製）

```yaml
target: agy.exe
surface_custom_json: No          # カスタム JSON フィールドは <SYSTEM_MESSAGE> にコピーされない
honor_sender_locale: No          # surface_custom_json: No から演繹される
injected_input_trust_label: USER_EXPLICIT   # 注入テキスト == 人間オーナーのキー入力
```

**帰結（好みでなく演繹）:** `agy.exe` はカスタム JSON フィールドを surface せず、*かつ* language mirroring が帯域外ヒントを打ち消すため、`agy.exe` の出力言語は実質**100 % ローカル専権**です——ローカルのナッジ定数のみで決まります。したがって `force-local` が唯一正しい既定であり、これは実装事実から導かれます。

### 契約が OSS 利用者にどう現れるか

`list_recipients` が返す各 `Recipient` の `capabilities` dict は、アダプタが宣言した契約をそのままコピーしたものです。MCP クライアントが「この相手にロケールヒントを送れるか？」を問うときは、`honor_sender_locale` を読んでローカルで判断できます。`send` の MCP ツールスキーマ**自体**も、アダプタ契約の和集合から導出されます: **どの**アダプタも `honor_sender_locale: Yes` を宣言していなければ、`send` ツールには `locale` フィールドが存在しない——OSS 利用者は、誰も honor できないものを要求することすらできません。

---

## セキュリティ不変条件

これらは全タスク・全アダプタで成立します（実装とテストで強制。本節はそれが実装する契約です）。

### R1 — ランデブー印の逆引き限定

上の [Discovery（opt-in 台帳）](#discoveryopt-in-台帳-不変条件-r1) を参照。要約: PID／alias 解決は、対象自身が公開した成果物のみを起点に進めます。プロセス列挙は永遠に行いません。

### R2 — `CONIN$` に送信者制御のバイトを乗せない

R2 は**公開 API 形状の構造的特性**です。`send()` の静的コールグラフは `_adapter` にも `_win_console` にも到達しません——送信者は `CONIN$` 注入を、偶然も含めて、いかなるフィールド・キーワード引数・環境変数からも呼び出せません。これが L2 分割の目的です: alias 解決は純粋なファイル読取関数 `_resolve_alias_from_registry` に置かれ、これは `_adapter` も Win32 コードも import しません。ゆえに `send()` の AST／import closure には mailbox 書き込みとこの registry 読取しか含まれません。`CONIN$` 注入は**受信側**の orochi ウォッチャが、署名付き JSON を検証したのちに*のみ*実行するもので、その時書き込まれるナッジは受信側ローカルの `WAKE_NUDGE_CATALOG` のみから解決され、`msg_data` から導出されることは決してありません。

これは v0.1 の保証（「規約＋レビュー」）より強力です。v0.1 では `inject_console_input` が公開面から呼び出し可能で、R2 は `on_undelivered_file_created` がメッセージ由来のテキストを渡さないことに依存していました。v0.2 では inject のコールグラフ全体が、*構造的に*送信者から到達不能です。これを固定する専用テストが用意されます: `send` の AST／import closure に `_adapter` や `_win_console` のシンボルが現れてはならない、というものです。

### 起床シグナルとペイロードの分離

`CONIN$` が配送するのは起床ナッジのみ。送信者制御のコンテンツ（タスク・タイトル・メタデータ）は JSON ファイルに置かれ、帰属可能な `<SYSTEM_MESSAGE>` チャネル経由でエージェントが読みます。

### 2 つの層、1 つではない問題

- **ガバナンス層 — 分離統治。** 起床ナッジの i18n と出力言語の i18n は信頼境界の反対側にあります。R2 は*起床ナッジにのみ*発火し、出力言語の選好は注入バイトを変えないため R2 に抵触しません。
- **機構層 — language mirroring による結合。** LLM は直前の `<USER_REQUEST>`（＝注入ナッジ）の言語に強く同調します。ゆえにナッジの言語が*機構的に*出力言語を決めます。だからこそ出力言語指示は**ローカルのナッジ定数に束ねられ**、運任せや送信者任せにしません。

---

## scope contract（適用範囲）

機構は `AttachConsole` + `WriteConsoleInputW` に依存します。その境界は**「ConPTY で壊れる」ではありません**——ConPTY ホストのアプリも実コンソール入力バッファを持ち、到達*可能*です。真の境界を第一級仕様として明示しないと、ツールは「なぜか不安定」なものに成り下がります。

### サポート対象
- **実コンソールを持つ**対話型 CLI/TUI プロセス（従来の conhost、Windows Terminal / ConPTY、`cmd`、`pwsh` 等）で、`stdin` を**コンソール入力**として読むもの。
- 注入側（受信側ウォッチャ）と**同一ユーザーセッション**・**同一インテグリティレベル**で動作しているもの。

### 対象外（設計上、動作しない）
- `stdin` が**パイプ／リダイレクト**のプロセス（バイトストリームを読むためコンソール入力レコードが届かない——`WriteConsoleInput` は到達しない）。*これが唯一最大の実制約。*
- **コンソールを持たない**プロセス（GUI アプリ、`DETACHED_PROCESS` / `CREATE_NO_WINDOW`）。
- **昇格**ターゲットや**別セッション**（クロスインテグリティ・クロスセッション注入は不可——これは制限でなくセキュリティ特性）。
- **WSL**（Linux 側——Win32 コンソールではない）、および**リモート**ターゲット。

これはツールを恒久的に**デスクトップローカル・同一セッション・コンソール接続**のトランスポートに固定します。クロスマシン／ヘッドレス／CI のトランスポートには*決してなりません*——それでよいのです。その領分は従来のメッセージキューのもの。適用範囲は市場（人間が自分のデスクトップのウィンドウで走らせる端末 AI エージェント）と一致し、それはまさに契約を満たす集合です。

---

## 国際化（i18n）

OSS は世界中で使われるため、既定は**英語**で、言語選択はすべて**ローカル専権**です——送信者は一切影響しません。

```python
# 英語デフォルト。キー = output_locale。
# ナッジ本文はその言語で記述し、かつ明示的指示を併記するため、
# language mirroring を敵にせず味方につける。
WAKE_NUDGE_CATALOG = {
  "en": "A new message arrived. Review your inbox and begin. Respond in English.",
  "ja": "受信メッセージを確認して作業に着手してください。日本語で応答してください。",
  # zh / ko / es / fr / de ...
}
DEFAULT_LOCALE = "en"

# 解決順（送信者入力は決して参照しない）:
#   1. ローカル上書き:  env CONINJECT_LOCALE  /  ライブラリ引数 locale="ja"
#   2. フォールバック:   "en"（不正/未対応 -> 無条件に en、fail-closed）
#
# wake_locale と output_locale は独立に解決、各々ローカルのみ:
#   env CONINJECT_WAKE_LOCALE   -> resolve_wake_locale()    （人間が読む言語）
#   env CONINJECT_OUTPUT_LOCALE -> resolve_output_locale()  （エージェントが応答する言語）
#   いずれも CONINJECT_LOCALE、次いで "en" にフォールバック。
```

- **出力言語はローカル指示で決定論的に固定**し、mirroring 任せにしません。mirroring は追い風で、不一致時は明示指示が勝ちます。
- `wake_locale`（人間オーナーが読む）と `output_locale`（エージェントが応答する）は**独立したローカル設定**です（両方ローカル → R2 は不変）。既定では同一値。
- 人間可読性 ≠ 出力言語の場合、ナッジを**バイリンガルにしない**（mirroring を濁す）。`CONIN$` は単一言語に保ち、人間向けテキストは**可視インジケータ**（注入経路の*外*のローカル UI）で示します。

### Layer B（送信者提供ロケール）— v1 では意図的に不在

送信者提供の出力ロケール（JSON の `requestedOutputLocale`）は **v1 で削除**されており、この不在は**意図的で、見落としではありません**:

1. `agy.exe` はカスタム JSON フィールドをエージェント文脈に surface しない（`surface_custom_json: No`）——フィールドは届かない。
2. 仮に届いても、ローカルナッジの **language mirroring** が上書きする。

ゆえにこの機能は二重に不活性でありながら攻撃面を増やします。将来**再導入**されるとしても、**advisory-only**・**ローカルカタログ従属**・**`surface_custom_json: Yes` かつ `honor_sender_locale: Yes` を契約で宣言するアダプタ限定**で、しかもローカルポリシーが上書き可能な検証済み enum *ヒント*として、決してコマンドとしてではなく、`en` に fail-closed します。その日が来たとき、それは**オプショナルな** MCP ツールフィールドとして現れます——コアには入りません。

---

## 運用ガイド

フォーカスを奪わない特性は、機能であると同時にリスクです——注入は設計上、人間に不可視です。以下の 3 つの統制は人間の監督を取り戻すもので、**運用上の必須要件**であり任意オプションではありません。

### 1. 監査ログ（追記専用）
各起床は `who → whom · when · message-id · result` を**追記専用**・改ざん検知可能なログに記録します。これは対象ウィンドウに可視の痕跡を残さない行為の、否認防止記録です。ログは受信側ウォッチャが書き出します（起床を発火させうる唯一のコンポーネント）。

```
2026-11-08T12:14:07Z  wake  sender=claude-code  recipient=gemini-ipc  msg=4b1e…  pid=13820  result=OK
```

### 2. 可視インジケータ
ローカル UI 要素（ステータスライン／トレイ／バンド——注入経路の**外**）が、人間オーナー自身の `wake_locale` で、起床が起きていることと誰がトリガーしたかを表示します。これは *UI ブラインド*攻撃（オーナーが読めない言語へ出力を強制し、不審挙動の監査を遅らせる）への対策です（出力言語の権限をローカルに保つ——i18n 参照——がもう半分の防御）。

### 3. キルスイッチ
全起床注入を即座に停止する単一のローカル無効化。既存のループガード（無限ループ防止の `sender == "user"` スキップ＋レート制限／ループ検出）と組み合わせ、起床ストームを抑え込みます。リトライ上限を超えた失敗メッセージは無限に再試行せず、`.system_generated/messages/dead_letter/<id>.json` に隔離されます。

---

## 脅威モデル（一行）

> 正当性は*何を注入できるか*では証明されません——機構はキーロガー型マルウェアと同一です。正当性は**制約**で証明されます: 対象自身が公開した成果物からのみ受信者を解決（R1 — 決して列挙しない）、ローカルで無害な1ビットの起床ナッジのみ注入（R2 — 決して送信者バイトを乗せない）、あらゆる指示は署名付きチャネルを通す、そして人間が見て止められる状態を保つ。

---

## 関連資料
- [`WORK_FLOW.md`](./WORK_FLOW.md) — ディレクトリのライフサイクル、シーケンス図、関数別仕様、単体テスト仕様。
- [`AGENTS.ja.md`](./AGENTS.ja.md) — **注入される側**のエージェントが従う規約。
- [`../orochi/DESIGN-0001-agentknock-positioning.md`](../orochi/DESIGN-0001-agentknock-positioning.md) — 現在の二読者 API 形状を決定した ADR。
