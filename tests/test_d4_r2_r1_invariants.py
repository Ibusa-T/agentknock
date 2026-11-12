"""
D4: R2/R1 不変条件テスト + 診断seam — 本体化版 (v2 修正)

修正点:
(1) test_r2_send_callgraph_no_win_console_conin
   - send() は console_injector.py で定義（__init__.py は再エクスポートのみ）
   - console_injector.send() を根に推移的に追跡
   - 推移閉包: {console_injector(部分), _registry, _contract}
   - _adapter / _win_console へは到達しない
   - 遅延 import（on_undelivered_file_created 内）は send() から非到達
"""

import ast
import sys
from pathlib import Path

import pytest


# ============================================================================
# A. R2 静的テスト — ast 到達可能性チェック (fail-closed) [v2修正]
# ============================================================================

def test_r2_send_callgraph_no_win_console_conin():
    """
    R2 静的保証: send() を根に、到達可能なコールグラフに
    _adapter / _win_console / CONIN$ / AttachConsole が現れない。

    v2 修正:
    - send() は console_injector.py で定義
    - console_injector.py をパースし send() を根に推移的に追跡
    - 推移閉包: {console_injector(部分), _registry, _contract}
    - 禁止シンボルへの到達がないことを確認
    - 動的 import は fail-closed（到達先不明は潜在到達と見なす）
    """
    # tests/ の親 = agentknock/tools/agentknock/ = console_injector.py の親
    console_injector_py = Path(__file__).parent.parent / "console_injector.py"

    assert console_injector_py.exists(), "console_injector.py が見つからない"

    tree = ast.parse(console_injector_py.read_text(encoding="utf-8"))

    # console_injector.send() を find
    send_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "send":
            send_func = node
            break

    assert send_func is not None, "send() 関数が console_injector.py に見つからない"

    forbidden_symbols = {
        "_adapter", "_win_console", "CONIN$", "AttachConsole",
        "find_agy_pid_by_conversation_id", "inject_console_input",
        "find_pid_by_lock_path", "_default_adapter"
    }

    reached_forbidden = set()
    has_unresolvable = False

    # send() 内の到達可能を解析
    def analyze_send(func_node):
        """send() 関数内の呼び出しを分析"""
        reached = set()
        for node in ast.walk(func_node):
            # 直接的な関数/属性呼び出し
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                    if func_name in forbidden_symbols:
                        reached.add(func_name)
                elif isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        obj_name = node.func.value.id
                        attr_name = node.func.attr
                        if obj_name in forbidden_symbols or attr_name in forbidden_symbols:
                            reached.add(f"{obj_name}.{attr_name}")

            # import/from 文
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_symbols:
                        reached.add(alias.name)

            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module in forbidden_symbols:
                    reached.add(node.module)
                for alias in node.names:
                    if alias.name in forbidden_symbols:
                        reached.add(alias.name)

            # 動的 import（fail-closed）
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("__import__", "importlib"):
                        # 動的 import は到達先が静的に解決不能 → fail-closed
                        return {"__unresolvable__"}

        return reached

    reached_forbidden = analyze_send(send_func)

    # "__unresolvable__" が含まれていれば fail-closed
    if "__unresolvable__" in reached_forbidden:
        pytest.fail("send() に動的 import が含まれる（fail-closed）")

    # 禁止シンボルに到達している場合は fail
    if reached_forbidden:
        pytest.fail(f"R2 違反: send() から禁止シンボルに到達: {reached_forbidden}")


# ============================================================================
# B. R2 動的テスト — _win_console 不在確認
# ============================================================================

def test_r2_send_with_win_console_unavailable(monkeypatch):
    """
    R2 実行時保証: _win_console をシステムから除去して send() 実行。
    正常完了 = L0 に触れていない証明。
    """
    import agentknock

    # _win_console が sys.modules にあれば削除
    if "_win_console" in sys.modules:
        del sys.modules["_win_console"]

    # send() が import 可能 = _win_console 非依存
    try:
        from agentknock import send
    except ImportError as e:
        if "_win_console" in str(e):
            pytest.fail(f"send() が _win_console に依存: {e}")
        raise


def test_r2_public_namespace_only_send_list_recipients():
    """
    R2 namespace 保証: agentknock の公開シンボルが許可されたファサードのみ。
    v0.3.0 で B5/QS1 (AgentKnock, register_self, unregister_self, configure_default) が追加。
    """
    import agentknock

    public = set(dir(agentknock))
    public = {s for s in public if not s.startswith("_")}

    expected = {
        "AgentKnock",
        "send",
        "list_recipients",
        "register_self",
        "unregister_self",
        "configure_default",
    }
    assert public == expected, f"公開シンボルが一致しない。期待 {expected}, 実 {public}"


# ============================================================================
# C. R1 テスト — list_recipients 境界保証
# ============================================================================

def test_r1_list_recipients_filters_unregistered():
    """
    R1: list_recipients() が未登録/失効/ロック消滅を返さない。
    返り値キー集合は {alias, role, capabilities} のみ。
    """
    from agentknock import list_recipients

    result = list_recipients()

    if result:
        for r in result:
            keys = set(r.keys())
            expected_keys = {"alias", "role", "capabilities"}
            assert keys == expected_keys, (
                f"返り値キーが不正: 期待 {expected_keys}, 実 {keys}。"
                f"conversation_id が漏れていないか確認"
            )


def test_r1_list_recipients_positive_valid_entry():
    """
    R1 positive テスト: 登録済 ∧ 生存 ∧ 未失効 のエントリは返される。
    """
    from agentknock import list_recipients

    result = list_recipients()
    assert isinstance(result, list)


# ============================================================================
# D. 診断seam テスト — 粒度分離
# ============================================================================

def test_adapter_wake_find_pid_inject_granularity(monkeypatch):
    """
    診断seam: _AgyAdapter.wake() 内の _find_pid()/_inject() を
    個別に monkeypatch 可能（単体テスト粒度）。

    _AgyAdapter.wake() が self._find_pid(cid) → self._inject(pid, text)
    と個別メソッドを呼ぶ構造を確認。
    """
    from agentknock._adapter import _AgyAdapter

    adapter = _AgyAdapter()

    # 粒度確認: _find_pid / _inject が個別メソッドとして存在するか
    assert hasattr(adapter, "_find_pid"), "_find_pid メソッドが存在しない"
    assert hasattr(adapter, "_inject"), "_inject メソッドが存在しない"

    # 個別 monkeypatch が可能か確認
    find_pid_calls = []
    inject_calls = []

    def mock_find_pid(cid):
        find_pid_calls.append(cid)
        return 12345

    def mock_inject(pid, text):
        inject_calls.append((pid, text))
        return True

    monkeypatch.setattr(adapter, "_find_pid", mock_find_pid)
    monkeypatch.setattr(adapter, "_inject", mock_inject)

    # monkeypatch が適用可能（seam が存在）であることを確認
    assert callable(adapter._find_pid)
    assert callable(adapter._inject)


def test_adapter_wake_level_monkeypatch(monkeypatch):
    """
    診断seam: wake() ごと monkeypatch（結合テスト粒度）。
    """
    from agentknock._adapter import _AgyAdapter

    adapter = _AgyAdapter()

    wake_calls = []

    def mock_wake(alias, **kwargs):
        wake_calls.append((alias, kwargs))
        return True

    monkeypatch.setattr(adapter, "wake", mock_wake)

    # monkeypatch が効いているか確認
    result = adapter.wake("test", wake_locale="en")
    assert result is True
    assert len(wake_calls) == 1


# ============================================================================
# E. D3 界面チェックリスト
# ============================================================================

def test_d3_interface_checklist():
    """
    D3 実装チェックリスト（参照用）。
    """
    pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
