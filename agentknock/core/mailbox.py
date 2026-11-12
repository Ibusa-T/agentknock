"""
agentknock core.mailbox — Message Validation, Atomic Storage, & Dead-Letter Quarantine.

Implements Mailbox layer (core, zero external dependencies):
- Sender validation and attributed title sanitization.
- Atomic temp -> rename (os.replace) writing of messages to prevent partial reads.
- Atomic marker creation under `undelivered/<Message-ID>`.
- 2-tier DeadLetterStore (local vs central) with atomic quarantine.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Final, Optional, Union
import uuid

from agentknock._contract import (
    AGY_BASE_SUBDIR,
    DEAD_LETTER_DIRNAME,
    UUID_REGEX,
    DeadLetterStore,
    MessagePayload,
)

SENDER_FORMAT_REGEX: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_UUID_RE: Final[re.Pattern[str]] = re.compile(UUID_REGEX, re.IGNORECASE)
_TITLE_MAX_LEN: Final[int] = 120
_ENV_DEFAULT: Final[object] = object()


def _load_allowed_senders() -> Optional[frozenset[str]]:
    raw = os.environ.get("CONINJECT_ALLOWED_SENDERS", "").strip()
    if not raw:
        return None
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def validate_sender(
    sender_id: object,
    allowed_senders: object = _ENV_DEFAULT,
) -> bool:
    """
    Validate that sender_id matches format (^[a-zA-Z0-9_-]{1,64}$) and is not 'user'.
    If allowed_senders is set (or via CONINJECT_ALLOWED_SENDERS env), enforces membership.
    """
    if not isinstance(sender_id, str):
        return False
    if not SENDER_FORMAT_REGEX.match(sender_id):
        return False
    if sender_id.lower() == "user":
        return False

    effective_allowed = (
        _load_allowed_senders() if allowed_senders is _ENV_DEFAULT else allowed_senders
    )
    if effective_allowed is not None:
        if sender_id not in effective_allowed:  # type: ignore[operator]
            return False
    return True


def sanitize_title(title: object, max_len: int = _TITLE_MAX_LEN) -> str:
    """Strip control characters, newlines, and truncate to max length."""
    if not isinstance(title, str):
        title = str(title) if title is not None else ""
    cleaned = "".join(ch for ch in title if ch.isprintable())
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_len]


def attributed_title(title: object) -> str:
    """Return sanitized title prefixed with '[sender-supplied] '."""
    return f"[sender-supplied] {sanitize_title(title)}"


class FileSystemDeadLetterStore(DeadLetterStore):
    """
    Default 2-tier DeadLetterStore implementation:
    - Tier 'local': brain/<conversation_id>/.system_generated/messages/dead_letter/<id>.json
    - Tier 'central': ~/.gemini/antigravity-cli/dead_letter/<id>.json
    """

    def __init__(
        self,
        *,
        agy_base_dir: Optional[Path] = None,
        brain_dir: Optional[Path] = None,
    ) -> None:
        self.agy_base_dir = (
            agy_base_dir or (Path.home() / AGY_BASE_SUBDIR)
        )
        self.brain_dir = brain_dir or (self.agy_base_dir / "brain")

    def quarantine(
        self,
        marker_path: Path,
        *,
        message_id: str,
        recipient_id: str,
        sender_id: str,
        attempts: int,
        reason: str,
    ) -> tuple[Path, str]:
        marker = Path(marker_path)
        safe_name = message_id if _UUID_RE.match(message_id) else marker.name

        if _UUID_RE.match(recipient_id):
            tier = "local"
            dl_dir = (
                self.brain_dir
                / recipient_id
                / ".system_generated"
                / "messages"
                / DEAD_LETTER_DIRNAME
            )
        else:
            tier = "central"
            dl_dir = self.agy_base_dir / DEAD_LETTER_DIRNAME

        dl_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dl_dir / f"{safe_name}.json"

        # Resolve message JSON file
        if marker.parent.name == "undelivered":
            src_json = marker.parent.parent / f"{message_id}.json"
        else:
            src_json = marker.with_suffix(".json")

        try:
            if src_json.is_file():
                os.replace(src_json, dest_path)
            else:
                stub = {
                    "id": message_id,
                    "recipient": recipient_id,
                    "sender": sender_id,
                    "quarantined_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "reason": reason,
                    "attempts": attempts,
                }
                tmp_stub = dl_dir / f".{safe_name}.{uuid.uuid4().hex}.tmp"
                tmp_stub.write_text(json.dumps(stub, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp_stub, dest_path)
        finally:
            try:
                if marker.exists():
                    marker.unlink()
            except OSError:
                pass

        return dest_path, tier


class Mailbox:
    """
    Mailbox manager for posting and loading messages.
    Supports standard directory layout:
    `<root_dir>/<recipient_id>/.system_generated/messages/`
    or direct `<root_dir>/messages/` if direct_mode=True.
    """

    def __init__(
        self,
        root_dir: Optional[Path] = None,
        *,
        dead_letter_store: Optional[DeadLetterStore] = None,
        direct_mode: bool = False,
    ) -> None:
        self.root_dir = root_dir or (Path.home() / AGY_BASE_SUBDIR / "brain")
        self.dead_letter_store = dead_letter_store or FileSystemDeadLetterStore(
            brain_dir=self.root_dir
        )
        self.direct_mode = direct_mode

    def get_messages_dir(self, recipient_id: str) -> Path:
        if self.direct_mode:
            return self.root_dir / "messages"
        return self.root_dir / recipient_id / ".system_generated" / "messages"

    def get_undelivered_dir(self, recipient_id: str) -> Path:
        return self.get_messages_dir(recipient_id) / "undelivered"

    def post(
        self,
        recipient_id: str,
        content: str,
        *,
        sender: str = "agent",
        title: str = "Message",
        priority: str = "MESSAGE_PRIORITY_HIGH",
    ) -> tuple[str, Path]:
        """
        Atomically write `<message_id>.json` via temp -> rename, then create
        `undelivered/<message_id>` marker. Returns (message_id, marker_path).
        """
        if not validate_sender(sender):
            raise ValueError(f"Invalid sender ID: {sender!r}")

        messages_dir = self.get_messages_dir(recipient_id)
        undelivered_dir = self.get_undelivered_dir(recipient_id)
        messages_dir.mkdir(parents=True, exist_ok=True)
        undelivered_dir.mkdir(parents=True, exist_ok=True)

        message_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        payload: MessagePayload = {
            "id": message_id,
            "recipient": recipient_id,
            "sender": sender,
            "priority": priority,  # type: ignore[typeddict-item]
            "timestamp": timestamp,
            "renderDetails": {
                "messageTitle": attributed_title(title),
            },
            "content": content,
        }

        json_path = messages_dir / f"{message_id}.json"
        tmp_json = messages_dir / f".{message_id}.{uuid.uuid4().hex}.tmp"

        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_json, json_path)

        marker_path = undelivered_dir / message_id
        tmp_marker = undelivered_dir / f".{message_id}.{uuid.uuid4().hex}.tmp"
        tmp_marker.touch()
        os.replace(tmp_marker, marker_path)

        return message_id, marker_path

    def read_message(
        self,
        recipient_id: str,
        message_id: str,
    ) -> Optional[MessagePayload]:
        json_path = self.get_messages_dir(recipient_id) / f"{message_id}.json"
        if not json_path.is_file():
            return None
        return self._load_json(json_path)

    def validate_marker(self, marker_path: Union[str, Path]) -> Optional[MessagePayload]:
        """
        Validate marker and load corresponding JSON message.
        Strips UTF-8 BOM, validates sender, and checks required schema fields.
        """
        path = Path(marker_path)
        message_id = path.name.removesuffix(".json")

        if path.parent.name == "undelivered":
            json_path = path.parent.parent / f"{message_id}.json"
        else:
            json_path = path.with_suffix(".json")

        if not json_path.is_file():
            return None

        return self._load_json(json_path)

    def _load_json(self, json_path: Path) -> Optional[MessagePayload]:
        try:
            raw_bytes = json_path.read_bytes()
            if raw_bytes.startswith(b"\xef\xbb\xbf"):
                raw_bytes = raw_bytes[3:]
                json_path.write_bytes(raw_bytes)
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        sender = data.get("sender")
        if not validate_sender(sender):
            return None
        if "renderDetails" not in data or not isinstance(data["renderDetails"], dict):
            return None
        if "content" not in data:
            return None

        return data  # type: ignore[return-value]
