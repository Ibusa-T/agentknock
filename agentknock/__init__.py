"""
agentknock Public API (L3 Agent-Facing Surface)

DESIGN-0001 D0/D1 契約:
- 公開関数は `send` と `list_recipients` の2つのみ。
- L0 (`_win_console`) および L1 (`_adapter`) は一切 import・公開しない。
"""

from console_injector import list_recipients, send

__all__ = ["send", "list_recipients"]


def __dir__() -> list[str]:
    """dir(agentknock) に inject / find_pid 等の内部シンボルを一切露出させない。"""
    return list(__all__)
