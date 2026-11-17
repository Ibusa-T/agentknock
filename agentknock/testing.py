# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock.testing — R2 Verification Testing Utilities (B1 仕様).

アダプタ作者およびコア開発者が、send() の AST コールグラフが
禁止シンボル (CONIN$, AttachConsole, _adapter, _win_console 等) に
一切到達しないことを機械的に検証するためのテストヘルパ。
"""
from __future__ import annotations

import ast
from pathlib import Path

DEFAULT_FORBIDDEN_SYMBOLS: frozenset[str] = frozenset(
    {
        "_adapter",
        "_win_console",
        "_win_console_ctypes",
        "CONIN$",
        "AttachConsole",
        "WriteConsoleInputW",
        "OpenProcess",
        "GetProcessTimes",
        "wake",
        "_inject",
        "_find_pid",
        "_find_process_identity",
        "find_agy_pid_by_conversation_id",
        "_find_agy_process_by_conversation_id",
        "inject_console_input",
        "inject_console_input_verified",
        "find_pid_by_lock_path",
        "find_process_by_lock_path",
        "_default_adapter",
    }
)


def _matches_forbidden(name: str, forbidden_symbols: frozenset[str]) -> bool:
    """モジュール名または識別子が禁止シンボルと一致するか判定（部分一致の誤判定を防止）。"""
    if name in forbidden_symbols:
        return True
    parts = name.split(".")
    return any(p in forbidden_symbols for p in parts)


def assert_send_cannot_reach(
    module_path: str | Path,
    entry_func_name: str = "send",
    forbidden_symbols: frozenset[str] = DEFAULT_FORBIDDEN_SYMBOLS,
) -> None:
    """
    指定ファイルの entry_func_name の AST を静的解析し、
    1. forbidden_symbols への関数呼び出し・属性アクセス・import が存在しないこと
    2. 動的 import (__import__, importlib) が存在しないこと (fail-closed)
    を検証する。違反時は AssertionError を送出する。
    entry_func_name が関数定義ではなく再エクスポート (import文) の場合は、
    import 元が forbidden_symbols に含まれていないかを検証する。
    """
    path = Path(module_path)
    assert path.is_file(), f"Target file does not exist: {path}"

    tree = ast.parse(path.read_text(encoding="utf-8"))
    entry_node: ast.FunctionDef | None = None
    imported_from: str | None = None

    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == entry_func_name
        ):
            entry_node = node  # type: ignore[assignment]
            break
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == entry_func_name:
                    imported_from = node.module
                    break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == entry_func_name:
                    imported_from = alias.name
                    break

    # 再エクスポートされている場合
    if entry_node is None:
        if imported_from is not None:
            if _matches_forbidden(imported_from, forbidden_symbols):
                raise AssertionError(
                    f"R2 Violation: {entry_func_name}() re-exported from forbidden module: {imported_from}"
                )
            return
        raise AssertionError(
            f"Function or re-export of {entry_func_name!r} not found in {path}"
        )

    reached_forbidden: set[str] = set()

    for node in ast.walk(entry_node):
        # 1. 呼び出し先シンボル名検査
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
                if func_name in ("__import__", "importlib"):
                    raise AssertionError(
                        f"R2 Violation: dynamic import detected in {entry_func_name}() (fail-closed)"
                    )
                if _matches_forbidden(func_name, forbidden_symbols):
                    reached_forbidden.add(func_name)
            elif isinstance(node.func, ast.Attribute):
                attr_name = node.func.attr
                if _matches_forbidden(attr_name, forbidden_symbols):
                    reached_forbidden.add(attr_name)
                if isinstance(node.func.value, ast.Name):
                    val_name = node.func.value.id
                    if _matches_forbidden(val_name, forbidden_symbols):
                        reached_forbidden.add(f"{val_name}.{attr_name}")

        # 2. 静的 import 文検査
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_forbidden(alias.name, forbidden_symbols):
                    reached_forbidden.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and _matches_forbidden(node.module, forbidden_symbols):
                reached_forbidden.add(node.module)
            for alias in node.names:
                if _matches_forbidden(alias.name, forbidden_symbols):
                    reached_forbidden.add(alias.name)

    if reached_forbidden:
        raise AssertionError(
            f"R2 Violation: {entry_func_name}() can reach forbidden symbols: {sorted(reached_forbidden)}"
        )
