#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixed CLI runner for agentknock operations (status / send / ack).

Implements Phase 1-A (single fixed entry point driven by payload.json) and
Phase 1-C (explicit locale opt-in; defaults to AgyDeliveryAdapter's "en"
when unspecified).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional, Sequence
import uuid

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentknock import AgentKnock  # noqa: E402
from agentknock._contract import AGY_BASE_SUBDIR, REGISTRY_DIRNAME  # noqa: E402
from agentknock.adapters import _win_console_ctypes  # noqa: E402
from agentknock.adapters.agy import AgyDeliveryAdapter  # noqa: E402
from agentknock.core.probe import AlwaysAliveProbe, FileLockLivenessProbe  # noqa: E402
from agentknock.core.registry import FileSystemAliasRegistry  # noqa: E402

# Verified against agent_docs/TEAM.md (Claude Code sessions do not hold presence/*.lock)
CLAUDE_IDS: frozenset[str] = frozenset(
    {
        "44d8d80f-0394-4a07-ad6a-19acc09a3ec4",  # my-prompt-d4 (Leader)
        "36e46c8a-59ad-4e5b-9df3-9eec25ad352b",  # my-prompt-6d (Coder-C / Test)
        "8861d2dc-7669-4129-8d45-901a7b024f74",  # my-prompt-b4 (Coder-D)
        "35a5e2e2-5e49-4c8d-9361-58496c4f2c4f",  # my-prompt-98 (Back-office)
    }
)


def _is_safe_path_token(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if value in (".", "..") or "/" in value or "\\" in value:
        return False
    return Path(value).name == value


def _normalize_optional_locale(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _is_valid_identity(identity: object) -> bool:
    return bool(
        isinstance(identity, tuple)
        and len(identity) == 2
        and isinstance(identity[0], int)
        and not isinstance(identity[0], bool)
        and isinstance(identity[1], int)
        and not isinstance(identity[1], bool)
        and identity[0] > 0
        and identity[1] > 0
    )


def check_target_status(
    target_id: str,
    *,
    presence_dir: Optional[Path] = None,
) -> dict[str, Any]:
    if not _is_safe_path_token(target_id):
        return {
            "alive": False,
            "reason": "invalid_target_id",
            "pid": None,
            "start_time": None,
        }

    p_dir = (
        presence_dir
        if presence_dir is not None
        else (Path.home() / AGY_BASE_SUBDIR / "presence")
    )
    lock_file = p_dir / f"{target_id}.lock"
    try:
        exists = lock_file.is_file()
    except PermissionError:
        return {
            "alive": False,
            "reason": "permission_denied",
            "pid": None,
            "start_time": None,
        }
    if not exists:
        return {
            "alive": False,
            "reason": "lock_missing",
            "pid": None,
            "start_time": None,
        }

    # status 照合は AttachConsole を伴わないため same_session_only=False で判定する
    identity = _win_console_ctypes.find_process_by_lock_path(
        str(lock_file),
        allowed_images=("agy.exe",),
        same_session_only=False,
    )
    if not _is_valid_identity(identity):
        return {
            "alive": False,
            "reason": "process_not_found_or_ambiguous",
            "pid": None,
            "start_time": None,
        }

    pid, start_time = identity  # type: ignore[misc]
    return {
        "alive": True,
        "reason": "ok",
        "pid": int(pid),
        "start_time": int(start_time),
    }


def run_status(
    payload: dict[str, Any],
    *,
    presence_dir: Optional[Path] = None,
) -> dict[str, Any]:
    targets = payload.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("payload 'targets' must be a list")

    results: list[dict[str, Any]] = []
    for t in targets:
        if not isinstance(t, dict):
            raise ValueError("each target entry must be a JSON object")
        tid = t["target_id"]
        st = check_target_status(tid, presence_dir=presence_dir)
        results.append(
            {
                "alias": t.get("alias", ""),
                "target_id": tid,
                "role": t.get("role", ""),
                **st,
            }
        )
    return {"ok": True, "action": "status", "results": results}


def run_send(
    payload: dict[str, Any],
    *,
    cli_wake_locale: Optional[str] = None,
    cli_output_locale: Optional[str] = None,
) -> dict[str, Any]:
    sender = payload.get("sender")
    if not isinstance(sender, str) or not sender.strip():
        raise ValueError("payload 'sender' must be a non-empty string")

    targets = payload.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("payload 'targets' must be a list")

    # 明示指定がある場合のみ渡し、未指定 (None) 時は AgyDeliveryAdapter 既定 ("en" / 環境変数) に委譲する
    wake_locale = _normalize_optional_locale(cli_wake_locale) or _normalize_optional_locale(
        payload.get("wake_locale")
    )
    output_locale = _normalize_optional_locale(cli_output_locale) or _normalize_optional_locale(
        payload.get("output_locale")
    )

    adapter_kwargs: dict[str, Any] = {
        "wake_locale": wake_locale,
        "output_locale": output_locale,
    }
    if "respect_user_typing" in payload:
        adapter_kwargs["respect_user_typing"] = bool(payload["respect_user_typing"])

    adapter = AgyDeliveryAdapter(**adapter_kwargs)
    registry = FileSystemAliasRegistry(
        registry_dir=Path.home() / AGY_BASE_SUBDIR / REGISTRY_DIRNAME
    )
    agy_client = AgentKnock(registry=registry, probe=FileLockLivenessProbe())
    claude_client = AgentKnock(registry=registry, probe=AlwaysAliveProbe())

    results: list[dict[str, Any]] = []
    for t in targets:
        if not isinstance(t, dict):
            raise ValueError("each target entry must be a JSON object")
        alias = t["alias"]
        tid = t["target_id"]
        role = t.get("role", "Agent")
        content = t["content"]
        is_claude = (isinstance(tid, str) and tid.lower() in CLAUDE_IDS) or bool(
            t.get("is_claude", False)
        )
        should_wake = bool(t.get("wake", True)) and (not is_claude)
        client = claude_client if is_claude else agy_client

        client.register_self(
            alias=alias,
            target_id=tid,
            role=role,
            ttl_sec=float(t.get("ttl_sec", 86400.0)),
        )
        msg_id = client.send(
            recipient=alias,
            content=content,
            sender=sender,
        )

        woke = False
        wake_detail: Any = "skipped_claude_mailbox_only" if is_claude else "skipped"
        if should_wake:
            woke = bool(adapter.wake(tid))
            wake_detail = getattr(adapter, "last_wake_detail", None)

        results.append(
            {
                "alias": alias,
                "target_id": tid,
                "msg_id": msg_id,
                "woke": woke,
                "wake_detail": wake_detail,
            }
        )
    return {"ok": True, "action": "send", "results": results}


def run_ack(
    payload: dict[str, Any],
    *,
    brain_dir: Optional[Path] = None,
) -> dict[str, Any]:
    self_id = payload.get("self_id")
    if not _is_safe_path_token(self_id):
        raise ValueError(f"Invalid self_id: {self_id!r}")

    message_ids = payload.get("message_ids", [])
    if not isinstance(message_ids, list):
        raise ValueError("payload 'message_ids' must be a list")

    b_dir = (
        brain_dir
        if brain_dir is not None
        else (Path.home() / AGY_BASE_SUBDIR / "brain")
    )
    msg_dir = b_dir / str(self_id) / ".system_generated" / "messages"
    undelivered_dir = msg_dir / "undelivered"
    read_file = msg_dir / "read.json"

    msg_dir.mkdir(parents=True, exist_ok=True)

    read_map: dict[str, Any] = {}
    if read_file.is_file():
        try:
            loaded = json.loads(read_file.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                read_map = loaded
        except Exception:
            read_map = {}

    acked: list[str] = []
    for mid in message_ids:
        if not _is_safe_path_token(mid):
            raise ValueError(f"Invalid message_id: {mid!r}")
        marker = undelivered_dir / mid
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        read_map[mid] = True
        acked.append(mid)

    tmp_path = msg_dir / f".read.{uuid.uuid4().hex}.tmp"
    try:
        tmp_path.write_text(
            json.dumps(read_map, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, read_file)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return {"ok": True, "action": "ack", "acked": acked}


def dispatch(
    payload: dict[str, Any],
    *,
    cli_wake_locale: Optional[str] = None,
    cli_output_locale: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload root must be a JSON object")
    action = payload.get("action")
    if action == "status":
        return run_status(payload)
    if action == "send":
        return run_send(
            payload,
            cli_wake_locale=cli_wake_locale,
            cli_output_locale=cli_output_locale,
        )
    if action == "ack":
        return run_ack(payload)
    raise ValueError(f"Unsupported or missing action: {action!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_op.py",
        description="Fixed CLI runner for agentknock operations (status / send / ack).",
    )
    parser.add_argument(
        "--payload",
        required=True,
        type=Path,
        help="Path to the input JSON payload file.",
    )
    parser.add_argument(
        "--wake-locale",
        default=None,
        help="Optional wake nudge locale override (default: AgyDeliveryAdapter default 'en').",
    )
    parser.add_argument(
        "--output-locale",
        default=None,
        help="Optional output directive locale override (default: AgyDeliveryAdapter default 'en').",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)

    action: Any = None
    try:
        raw = args.payload.read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
        if isinstance(payload, dict):
            action = payload.get("action")
        result = dispatch(
            payload,
            cli_wake_locale=args.wake_locale,
            cli_output_locale=args.output_locale,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        err_out = {
            "ok": False,
            "action": action,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print(json.dumps(err_out, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
