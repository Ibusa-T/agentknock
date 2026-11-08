"""
Antigravity 別窓エージェント バックグラウンド入力注入モジュール (console_injector.py)

特徴:
1. Win32 カーネル API (AttachConsole + WriteConsoleInputW) および Restart Manager API
   (rstrtmgr.dll) を扱う低レイヤ処理はすべて Cython ネイティブ拡張 (`_win_console.pyx`) に分離。
   `<windows.h>` / `<restartmanager.h>` を直接インクルードし、GIL 完全解放 (`with nogil:`) で動作します。
2. このモジュール (`console_injector.py`) は Python 標準ライブラリが高速に処理できる
   UUID バリデーション、JSON メッセージ生成・検証 (BOM 自動除去)、および
   自作ホットリロードフレームワーク「orochi」連携用エントリポイントを提供します。
3. i18n 対応 (OSS 既定=英語) と セキュリティ不変条件 (R2: Wake, Not Command) を実装。
   CONIN$ へ注入する起床ナッジは必ずローカル定数カタログ由来で、外部メッセージ
   (msg_data) のいかなるフィールドからも導出しない。
"""

from datetime import datetime, timezone
import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Optional, Dict, Any, Final, Mapping

# Windows コンソール文字化け対策
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 同ディレクトリの Cython 拡張 (_win_console.pyd / _win_console.pyx) をロード
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

try:
    import _win_console
except ImportError:
    import pyximport

    pyximport.install(language_level=3, inplace=True)
    import _win_console  # type: ignore[no-redef]


# UUID (会話ID / Message-ID) バリデーション用正規表現
UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Antigravity CLI のベースディレクトリ
AGY_BASE_DIR = Path.home() / ".gemini" / "antigravity-cli"
BRAIN_DIR = AGY_BASE_DIR / "brain"
PRESENCE_DIR = AGY_BASE_DIR / "presence"


# ============================================================================
# 0. i18n 起床ナッジ定数カタログ & ロケール解決コア (R2: Wake, Not Command) [T1]
# ============================================================================
# セキュリティ不変条件 (R2):
# CONIN$ へ注入する文字列は必ず以下のローカル定数カタログ由来でなければならず、
# 外部メッセージ (msg_data) のいかなるフィールドからも文字列を導出してはならない。

DEFAULT_LOCALE: Final[str] = "en"
ENV_CONINJECT_LOCALE: Final[str] = "CONINJECT_LOCALE"
ENV_CONINJECT_WAKE_LOCALE: Final[str] = "CONINJECT_WAKE_LOCALE"
ENV_CONINJECT_OUTPUT_LOCALE: Final[str] = "CONINJECT_OUTPUT_LOCALE"

SUPPORTED_LOCALES: Final[tuple[str, ...]] = (
    "en",
    "ja",
    "zh",
    "ko",
    "es",
    "fr",
    "de",
)

WAKE_BODY_CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "en": "A new message arrived. Review your inbox and begin.",
        "ja": "受信メッセージを確認して作業に着手してください。",
        "zh": "收到新消息，请查看收件箱并开始处理。",
        "ko": "새 메시지가 도착했습니다. 수신함을 확인하고 작업을 시작하세요.",
        "es": "Llegó un nuevo mensaje. Revisa tu bandeja de entrada y comienza.",
        "fr": "Un nouveau message est arrivé. Consultez votre boîte de réception et commencez.",
        "de": "Eine neue Nachricht ist eingegangen. Prüfen Sie Ihren Posteingang und beginnen Sie.",
    }
)

OUTPUT_DIRECTIVE_CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "en": "Respond in English.",
        "ja": "日本語で応答してください。",
        "zh": "请用中文回复。",
        "ko": "한국어로 응답해 주세요.",
        "es": "Responde en español.",
        "fr": "Répondez en français.",
        "de": "Antworten Sie auf Deutsch.",
    }
)

# 英語デフォルト。キー=locale。ナッジ本文自体をその言語で記述＋明示的出力言語指示を併記
# (language mirroring を味方につけつつ、明示指示で決定論的に出力言語を固定する)
WAKE_NUDGE_CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {
        loc: f"{WAKE_BODY_CATALOG[loc]} {OUTPUT_DIRECTIVE_CATALOG[loc]}"
        for loc in SUPPORTED_LOCALES
    }
)


def _normalize_supported_locale(candidate: Optional[str]) -> Optional[str]:
    """候補文字列を正規化し、SUPPORTED_LOCALES に含まれる場合のみそのキーを返す。"""
    if not isinstance(candidate, str):
        return None
    normalized = candidate.strip().lower()
    if normalized in WAKE_NUDGE_CATALOG:
        return normalized
    return None


def resolve_locale(
    locale: Optional[str] = None,
    *,
    env_var: str = ENV_CONINJECT_LOCALE,
) -> str:
    """
    ロケール解決純粋関数:
    優先順位: env CONINJECT_LOCALE -> ライブラリ引数 locale -> DEFAULT_LOCALE ('en')
    未指定・空文字の場合は次段へフォールスルーし、不正/未対応コードが指定された場合は
    即座に DEFAULT_LOCALE ('en') へ fail-closed する。
    """
    env_raw = os.environ.get(env_var)
    if env_raw is not None and env_raw.strip() != "":
        resolved_env = _normalize_supported_locale(env_raw)
        return resolved_env if resolved_env is not None else DEFAULT_LOCALE

    if locale is not None and (not isinstance(locale, str) or locale.strip() != ""):
        resolved_arg = _normalize_supported_locale(locale)
        return resolved_arg if resolved_arg is not None else DEFAULT_LOCALE

    return DEFAULT_LOCALE


def resolve_wake_locale(wake_locale: Optional[str] = None) -> str:
    """
    [軸A: R2境界内・ローカル専権]
    起床ナッジ本文のロケール (wake_locale) を解決する純粋関数。
    ※ R2不変条件: 送信者ペイロード (msg_data) は引数に取らず一切参照しない。
    優先順位: env CONINJECT_WAKE_LOCALE -> env CONINJECT_LOCALE -> 引数 wake_locale -> 'en'
    """
    env_wake = os.environ.get(ENV_CONINJECT_WAKE_LOCALE)
    if env_wake is not None and env_wake.strip() != "":
        resolved = _normalize_supported_locale(env_wake)
        return resolved if resolved is not None else DEFAULT_LOCALE

    return resolve_locale(wake_locale, env_var=ENV_CONINJECT_LOCALE)


def resolve_output_locale(
    output_locale: Optional[str] = None,
    *,
    fallback_locale: Optional[str] = None,
) -> str:
    """
    [軸B: 出力言語解決・ローカル主権]
    被注入側AIが応答すべき出力言語ロケール (output_locale) を解決する純粋関数。
    ※ agy.exe (surface_custom_json=No, honor_sender_locale=No) ではローカル設定のみを参照。
    優先順位: env CONINJECT_OUTPUT_LOCALE -> env CONINJECT_LOCALE -> 引数 output_locale
              -> fallback_locale (通常は解決済み wake_locale) -> 'en'
    """
    env_out = os.environ.get(ENV_CONINJECT_OUTPUT_LOCALE)
    if env_out is not None and env_out.strip() != "":
        resolved = _normalize_supported_locale(env_out)
        return resolved if resolved is not None else DEFAULT_LOCALE

    env_common = os.environ.get(ENV_CONINJECT_LOCALE)
    if env_common is not None and env_common.strip() != "":
        resolved = _normalize_supported_locale(env_common)
        return resolved if resolved is not None else DEFAULT_LOCALE

    if output_locale is not None and (
        not isinstance(output_locale, str) or output_locale.strip() != ""
    ):
        resolved = _normalize_supported_locale(output_locale)
        return resolved if resolved is not None else DEFAULT_LOCALE

    if fallback_locale is not None:
        resolved_fb = _normalize_supported_locale(fallback_locale)
        if resolved_fb is not None:
            return resolved_fb

    return DEFAULT_LOCALE


def resolve_wake_prompt(
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
) -> str:
    """
    [R2保証境界]
    CONIN$ に注入する起床ナッジ定数文字列を決定論的に解決する純粋関数。
    戻り値は必ずローカル定数カタログ (WAKE_NUDGE_CATALOG 等) 由来の文字列となる。
    - wake_locale と output_locale が同一（既定）の場合は WAKE_NUDGE_CATALOG[locale] を返す。
    - 独立に異なるロケールが設定された場合は WAKE_BODY_CATALOG[wake_locale] と
      OUTPUT_DIRECTIVE_CATALOG[output_locale] の定数同士を結合して返す。
    """
    w_loc = resolve_wake_locale(wake_locale)
    o_loc = resolve_output_locale(output_locale, fallback_locale=w_loc)
    if w_loc == o_loc:
        return WAKE_NUDGE_CATALOG[w_loc]
    return f"{WAKE_BODY_CATALOG[w_loc]} {OUTPUT_DIRECTIVE_CATALOG[o_loc]}"


# ============================================================================
# 0b. セキュリティ硬化ヘルパ (sender 検証 / title 帰属サニタイズ) [T3]
# ============================================================================

SENDER_FORMAT_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_TITLE_MAX_LEN = 120
_ENV_DEFAULT = object()


def _load_allowed_senders() -> Optional[frozenset]:
    """
    env CONINJECT_ALLOWED_SENDERS (カンマ区切り) が設定されていればその集合を返す。
    未設定なら None (= フォーマット検証のみ・OSS 既定)。ハードコード固定リストは持たない。
    """
    raw = os.environ.get("CONINJECT_ALLOWED_SENDERS", "").strip()
    if not raw:
        return None
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def validate_sender(sender_id, allowed_senders=_ENV_DEFAULT) -> bool:
    """
    sender_id がフォーマット (^[a-zA-Z0-9_-]{1,64}$) を満たすか検証する。
    allowed_senders が設定されている (env 経由含む) 場合は所属も強制する。
    既定 (None) ではフォーマット検証のみ。
    """
    if not isinstance(sender_id, str) or not SENDER_FORMAT_REGEX.match(sender_id):
        return False
    if allowed_senders is _ENV_DEFAULT:
        allowed_senders = _load_allowed_senders()
    if allowed_senders is not None and sender_id not in allowed_senders:
        return False
    return True


def _sanitize_title(title) -> str:
    """messageTitle から制御文字・改行を除去し、長さ上限でクリップする。"""
    if not isinstance(title, str):
        title = str(title)
    cleaned = "".join(ch for ch in title if ch.isprintable())
    cleaned = " ".join(cleaned.split())
    return cleaned[:_TITLE_MAX_LEN]


def _attributed_title(title) -> str:
    """送信者制御の自由文字列である title を帰属明示＋サニタイズして返す。"""
    return f"[sender-supplied] {_sanitize_title(title)}"


# ============================================================================
# 1. 会話ID (UUID) から起動中の agy.exe の PID を逆引きする関数 (Cython 委譲)
# ============================================================================

def find_agy_pid_by_conversation_id(conversation_id: str) -> Optional[int]:
    """
    presence/<conversation_id>.lock を掴んでいる agy.exe のプロセスID (PID) を
    Cython 拡張 (`_win_console.find_pid_by_lock_path`) 経由で取得する。
    """
    if not UUID_REGEX.match(conversation_id):
        raise ValueError(f"不正な会話ID形式です: {conversation_id}")

    lock_path = PRESENCE_DIR / f"{conversation_id}.lock"
    if not lock_path.exists():
        return None

    return _win_console.find_pid_by_lock_path(str(lock_path))


# ============================================================================
# 2. バックグラウンドで対象 PID のコンソール入力 (CONIN$) に文字＋Enterを注入 (Cython 委譲)
# ============================================================================

def inject_console_input(
    target_pid: int,
    text: Optional[str] = None,
    send_enter: bool = True,
    submit_delay_sec: float = 0.08,
    *,
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
) -> bool:
    """
    指定した PID (agy.exe) のコンソール入力バッファ (CONIN$) に
    Cython 拡張 (`_win_console.inject_console_input`) を用いて GIL 解放 (`nogil`) 状態で
    バックグラウンドで文字列と Enter キーを直接書き込む（フォーカス移動なし）。

    ※ R2準拠: `text` 未指定時はローカル設定 (`wake_locale` / `output_locale`) に基づき
       `WAKE_NUDGE_CATALOG` の定数へ解決される。呼び出し側は msg_data 由来の文字列を
       `text` に渡してはならない（起床ナッジは常にローカル定数カタログ由来）。
    """
    resolved_text = (
        text
        if text is not None
        else resolve_wake_prompt(wake_locale=wake_locale, output_locale=output_locale)
    )
    return bool(
        _win_console.inject_console_input(
            target_pid,
            resolved_text,
            send_enter,
            submit_delay_sec,
        )
    )


# ============================================================================
# 3. 外部エージェント (Claude等) からのメッセージファイル生成 & Orochi 連携ハンドラ
# ============================================================================

def create_external_message(
    recipient_id: str,
    content: str,
    sender_id: str = "claude-agent",
    message_title: str = "Message from External Agent",
    base_brain_dir: Optional[Path] = None,
) -> Path:
    """
    外部エージェント (Claude Code 等) から Antigravity の指定会話ID宛てに
    `<Message-ID>.json` と `undelivered/<Message-ID>` を生成する。

    Returns:
        Path: 生成された `undelivered/<Message-ID>` のファイルパス
    """
    if not UUID_REGEX.match(recipient_id):
        raise ValueError(f"不正な宛先会話ID形式です: {recipient_id}")

    # [T3] 生成時 sender 検証: フォーマット (+ env 設定時はアローリスト所属) を強制
    if not validate_sender(sender_id):
        raise ValueError(f"不正な sender 形式 または 非許可 sender です: {sender_id!r}")

    target_brain = base_brain_dir or BRAIN_DIR
    messages_dir = target_brain / recipient_id / ".system_generated" / "messages"
    undelivered_dir = messages_dir / "undelivered"
    undelivered_dir.mkdir(parents=True, exist_ok=True)

    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    payload = {
        "id": message_id,
        "recipient": recipient_id,
        "sender": sender_id,
        "priority": "MESSAGE_PRIORITY_HIGH",
        "timestamp": timestamp,
        # [T3] title は送信者制御の自由文字列。帰属明示＋サニタイズして注入面を硬化
        "renderDetails": {
            "messageTitle": _attributed_title(message_title),
        },
        "content": content,
    }

    # 1. 先に messages/<Message-ID>.json (本文) を書き出す
    json_path = messages_dir / f"{message_id}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # 2. 直後に undelivered/<Message-ID> (0バイトの未読目印ファイル) を生成する
    undelivered_path = undelivered_dir / message_id
    undelivered_path.touch()

    return undelivered_path


def validate_and_load_message(undelivered_file_path: str | Path) -> Optional[Dict[str, Any]]:
    """
    `messages/undelivered/<Message-ID>` のパスを受け取り、
    1. ファイル名が UUID (Message-ID) 形式か検証
    2. 親階層の `messages/<Message-ID>.json` を読み込み
    3. 外部エージェントからのメッセージ (`sender != "user"` かつ `renderDetails` あり) か判定
    4. [T3] sender がフォーマット (+ env 設定時はアローリスト) を満たすか検証
    条件を満たせば JSON 辞書を返し、対象外なら None を返す。
    """
    path = Path(undelivered_file_path)
    message_id = path.name.removesuffix(".json")

    # 1. UUID 形式のバリデーション
    if not UUID_REGEX.match(message_id):
        return None

    # 2. 親フォルダの <Message-ID>.json のパスを解決
    # undelivered 内のファイルが渡された場合は親の messages/ を参照
    if path.parent.name == "undelivered":
        json_path = path.parent.parent / f"{message_id}.json"
    else:
        json_path = path.with_suffix(".json")

    if not json_path.exists():
        return None

    try:
        raw_bytes = json_path.read_bytes()
        # PowerShell 5.1 等で UTF-8 BOM (\xef\xbb\xbf) 付きで生成された場合、
        # agy.exe (Go製パーサ) がエラーになるため自動で BOM を除去して保存し直す
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            raw_bytes = raw_bytes[3:]
            json_path.write_bytes(raw_bytes)
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None

    # 3. 人間 (user) の入力ではなく、エージェントからの着信であることを確認
    #    (早期 return 順序依存: BOM 除去・sender==user 除外の後段に sender 検証を置く)
    if data.get("sender") == "user":
        return None
    if "renderDetails" not in data:
        return None
    # 4. [T3] 受信時 sender 検証
    if not validate_sender(str(data.get("sender", ""))):
        return None

    return data


def on_undelivered_file_created(
    undelivered_file_path: str | Path,
    *,
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
    send_enter: bool = True,
    submit_delay_sec: float = 0.08,
) -> bool:
    """
    【Orochi のファイル監視イベントから直接呼ぶエントリ関数】
    `undelivered/<Message-ID>` の生成を検知した際に呼び出すと、
    バリデーション → PID特定 → バックグラウンド入力注入までを一括実行する。

    ===== R2 INVARIANT（弱体化禁止）=====
    wake_prompt は CONIN$ に書く唯一のテキストで source:USER_EXPLICIT として着信する。
    必ずローカル由来 (カタログ/env/ローカル引数 wake_locale/output_locale) のみで解決し、
    msg_data のいかなるフィールド (content, renderDetails.messageTitle, sender, 独自locale 等)
    からも導出・連結・補間してはならない。送信者ペイロードは JSON <SYSTEM_MESSAGE> 経由のみ。
    固定テスト: tests/test_t3_security.py::test_r2_wake_prompt_never_derives_from_msg_data
    """
    msg_data = validate_and_load_message(undelivered_file_path)
    if not msg_data:
        return False

    recipient_id = msg_data.get("recipient", "")
    sender_id = msg_data.get("sender", "")
    message_id = msg_data.get("id", "")

    pid = find_agy_pid_by_conversation_id(recipient_id)
    if not pid:
        print(f"⚠️ 宛先会話ID ({recipient_id}) の起動中 agy.exe プロセスが見つかりません。")
        return False

    # [T3] 空撃ち No-Op: 未読マーカーが既に消費済みなら注入しない
    marker = Path(undelivered_file_path)
    if not marker.exists():
        print(f"⚠️ 未読マーカーが既に消費済みのため注入を中止: {message_id}")
        return False

    # [R2] 起床ナッジはローカル (wake_locale / output_locale) のみから解決する
    resolved_wake = resolve_wake_locale(wake_locale)
    resolved_output = resolve_output_locale(output_locale, fallback_locale=resolved_wake)
    wake_text = resolve_wake_prompt(wake_locale=resolved_wake, output_locale=resolved_output)

    success = inject_console_input(
        target_pid=pid,
        text=wake_text,
        send_enter=send_enter,
        submit_delay_sec=submit_delay_sec,
    )
    if success:
        print(
            f"✅ [Wakeup成功] Message-ID: {message_id} | "
            f"Sender: {sender_id} -> Recipient: {recipient_id} (PID: {pid}, "
            f"wake_locale={resolved_wake}, output_locale={resolved_output})"
        )
    else:
        print(f"❌ [Wakeup失敗] PID: {pid} へのコンソール入力注入に失敗しました。")

    return success


# ============================================================================
# 4. 単体テスト・手動実行用 CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Antigravity 別窓バックグラウンド入力注入ツール"
    )
    parser.add_argument(
        "--conversation",
        "-c",
        help="起こしたい別窓の会話ID (UUID)",
    )
    parser.add_argument(
        "--undelivered-file",
        "-f",
        help="検知した undelivered/<Message-ID> ファイルのパス",
    )
    # [T2] 自由文字列 --text/-t は R2 のため完全廃止。ロケール選択のみ受け付ける
    parser.add_argument(
        "--locale",
        "-l",
        choices=list(SUPPORTED_LOCALES),
        default=DEFAULT_LOCALE,
        help="起床ナッジおよび出力言語のロケール (デフォルト: en)",
    )
    parser.add_argument(
        "--wake-locale",
        choices=list(SUPPORTED_LOCALES),
        default=None,
        help="起床ナッジ側のロケールを個別指定する場合に使用 (省略時は --locale に従う)",
    )
    parser.add_argument(
        "--output-locale",
        choices=list(SUPPORTED_LOCALES),
        default=None,
        help="AI応答出力側のロケールを個別指定する場合に使用 (省略時は --locale に従う)",
    )
    parser.add_argument(
        "--send-message",
        "-m",
        help="外部エージェント(Claude等)から指定会話ID(--conversation)宛てに送信するメッセージ本文",
    )
    parser.add_argument(
        "--sender",
        "-s",
        default="claude-agent",
        help="外部メッセージ送信時の sender 名 (デフォルト: claude-agent)",
    )
    args = parser.parse_args()

    cli_wake_locale = args.wake_locale if args.wake_locale is not None else args.locale
    cli_output_locale = args.output_locale if args.output_locale is not None else args.locale

    if args.send_message and args.conversation:
        undelivered = create_external_message(
            recipient_id=args.conversation,
            content=args.send_message,
            sender_id=args.sender,
        )
        print(f"📩 外部メッセージを生成しました: {undelivered}")
        on_undelivered_file_created(
            undelivered,
            wake_locale=cli_wake_locale,
            output_locale=cli_output_locale,
        )
    elif args.undelivered_file:
        on_undelivered_file_created(
            args.undelivered_file,
            wake_locale=cli_wake_locale,
            output_locale=cli_output_locale,
        )
    elif args.conversation:
        # [T3] 空撃ち No-Op 準拠: 未読メッセージ (undelivered/*) が実在する場合のみ起床を実行
        if not UUID_REGEX.match(args.conversation):
            print(f"❌ 不正な会話ID形式です: {args.conversation}")
            sys.exit(1)
        undelivered_dir = (
            BRAIN_DIR / args.conversation / ".system_generated" / "messages" / "undelivered"
        )
        candidates = sorted(undelivered_dir.iterdir()) if undelivered_dir.exists() else []
        woken = False
        for candidate in candidates:
            if on_undelivered_file_created(
                candidate,
                wake_locale=cli_wake_locale,
                output_locale=cli_output_locale,
            ):
                woken = True
                break
        if not woken:
            print(
                f"ℹ️ 会話ID {args.conversation} に有効な未読メッセージが存在しないため起床をスキップしました (No-Op)。"
            )
    else:
        parser.print_help()
