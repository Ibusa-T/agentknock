# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock MCP Server (stdio) & Strict Tool Schemas (v0.3.0 / BR1-SPEC).

特徴:
- `agentknock._contract` の `ALIAS_PATTERN` と `UUID_REGEX` を直接参照。
- 公開ツール: `send`, `list_recipients`, `register_self`, `unregister_self`
- `additionalProperties: False` と厳格キー検証により、スキーマ外のフィールドは一切渡せない。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from types import MappingProxyType
from typing import Any, Final, Mapping, Optional

_MCP_DIR = Path(__file__).resolve().parent
_TOOLS_ROOT = _MCP_DIR.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from agentknock._contract import ALIAS_PATTERN, UUID_REGEX
from agentknock import list_recipients, register_self, send, unregister_self

MCP_TOOLS_SCHEMA: Final[tuple[Mapping[str, Any], ...]] = (
    MappingProxyType(
        {
            "name": "send",
            "description": (
                "Send a signed asynchronous message to an opt-in registered CLI agent by alias. "
                "Writes only to the recipient's mailbox; wakeup is handled by the receiver-side watcher."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "pattern": ALIAS_PATTERN,
                        "not": {"pattern": UUID_REGEX},
                        "description": (
                            "Target recipient alias from list_recipients "
                            "(must match ALIAS_PATTERN and must NOT match UUID_REGEX)."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Message body to deliver.",
                    },
                    "sender": {
                        "type": "string",
                        "pattern": ALIAS_PATTERN,
                        "description": "Sender agent identifier.",
                    },
                },
                "required": ["recipient", "content", "sender"],
                "additionalProperties": False,
            },
        }
    ),
    MappingProxyType(
        {
            "name": "list_recipients",
            "description": (
                "List opt-in registered and currently active CLI agent recipients "
                "(alias, role, capabilities). Raw conversation UUIDs are never exposed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }
    ),
    MappingProxyType(
        {
            "name": "register_self",
            "description": "Register self into the opt-in discovery registry so others can send messages by alias.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string", "pattern": ALIAS_PATTERN},
                    "target_id": {"type": "string", "pattern": UUID_REGEX},
                    "role": {"type": "string"},
                    "ttl_sec": {"type": "number", "minimum": 1},
                },
                "required": ["alias", "target_id", "role"],
                "additionalProperties": False,
            },
        }
    ),
    MappingProxyType(
        {
            "name": "unregister_self",
            "description": "Kill switch: immediately remove self from discovery registry to halt inbound messages.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "alias": {"type": "string", "pattern": ALIAS_PATTERN},
                },
                "required": ["alias"],
                "additionalProperties": False,
            },
        }
    ),
)

_SEND_ALLOWED_KEYS: Final[frozenset[str]] = frozenset({"recipient", "content", "sender"})


def dispatch_mcp_tool(name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
    """
    MCP `tools/call` ディスパッチャ。
    スキーマ外のフィールドが1つでも含まれていれば ValueError で即座に拒絶する。
    """
    args = arguments if arguments is not None else {}
    if not isinstance(args, dict):
        raise ValueError("MCP tool arguments must be a JSON object.")

    if name == "send":
        extra_keys = set(args.keys()) - _SEND_ALLOWED_KEYS
        missing_keys = _SEND_ALLOWED_KEYS - set(args.keys())
        if extra_keys:
            raise ValueError(f"Unsupported fields for 'send': {sorted(extra_keys)}")
        if missing_keys:
            raise ValueError(f"Missing required fields for 'send': {sorted(missing_keys)}")
        message_id = send(
            recipient=args["recipient"],
            content=args["content"],
            sender=args["sender"],
        )
        return {"message_id": message_id}

    if name == "list_recipients":
        if args:
            raise ValueError(
                f"Unsupported fields for 'list_recipients': {sorted(args.keys())}"
            )
        return {"recipients": list_recipients()}

    if name == "register_self":
        register_self(
            alias=args["alias"],
            target_id=args["target_id"],
            role=args.get("role", ""),
            ttl_sec=float(args.get("ttl_sec", 86400.0)),
        )
        return {"status": "ok", "alias": args["alias"]}

    if name == "unregister_self":
        ok = unregister_self(alias=args["alias"])
        return {"status": "ok", "unregistered": ok}

    raise ValueError(f"Unknown MCP tool: {name!r}")


def run_stdio_server() -> None:
    """JSON-RPC 2.0 ベースの最小 MCP stdio サーバーループ。"""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}

            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agentknock", "version": "0.3.0"},
                }
            elif method == "tools/list":
                result = {"tools": [dict(t) for t in MCP_TOOLS_SCHEMA]}
            elif method == "tools/call":
                tool_out = dispatch_mcp_tool(
                    params.get("name", ""),
                    params.get("arguments"),
                )
                result = {
                    "content": [
                        {"type": "text", "text": json.dumps(tool_out, ensure_ascii=False)}
                    ],
                    "isError": False,
                }
            elif method == "notifications/initialized":
                continue
            else:
                raise ValueError(f"Unsupported method: {method}")

            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": str(exc)},
            }

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
