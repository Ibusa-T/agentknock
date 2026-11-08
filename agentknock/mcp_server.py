"""
agentknock MCP Server (stdio) & Strict Tool Schemas (DESIGN-0001 D1)

特徴:
- `_contract.py` の `ALIAS_PATTERN` と `UUID_REGEX` を直接参照し、
  「ALIAS_PATTERN 合致 かつ UUID_REGEX 非合致」を JSON Schema とディスパッチャの両方で強制。
- `additionalProperties: False` と厳格キー検証により、スキーマ外のフィールドは一切渡せない。
"""

import json
from pathlib import Path
import sys
from types import MappingProxyType
from typing import Any, Final, Mapping, Optional

_MCP_DIR = Path(__file__).resolve().parent
_TOOLS_ROOT = _MCP_DIR.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _contract import ALIAS_PATTERN, UUID_REGEX
from agentknock import list_recipients, send

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
                    "serverInfo": {"name": "agentknock", "version": "0.2.0"},
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
