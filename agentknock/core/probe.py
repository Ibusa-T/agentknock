# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock core.probe — LivenessProbe Default Implementations (A2 / R1 Invariant).

Implements LivenessProbe:
- AlwaysAliveProbe: Unconditional alive status (for CI, test environments, and standalone outbox).
- FileLockLivenessProbe: Checks presence of `<presence_dir>/<target_id>.lock`.
- CallableLivenessProbe: Wraps an arbitrary predicate callable `(target_id) -> bool`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from agentknock._contract import AGY_BASE_SUBDIR, LivenessProbe


class AlwaysAliveProbe(LivenessProbe):
    """Liveness probe that always reports alive. Ideal for CI, testing, and decoupled workflows."""

    def is_alive(self, target_id: str) -> bool:
        return True


class FileLockLivenessProbe(LivenessProbe):
    """
    Checks if `<presence_dir>/<target_id><lock_extension>` exists on disk.
    Default presence directory: `~/.gemini/antigravity-cli/presence/`.
    """

    def __init__(
        self,
        presence_dir: Optional[Path] = None,
        *,
        lock_extension: str = ".lock",
        extra_checker: Optional[Callable[[str, Path], bool]] = None,
    ) -> None:
        self.presence_dir = presence_dir or (
            Path.home() / AGY_BASE_SUBDIR / "presence"
        )
        self.lock_extension = lock_extension
        self.extra_checker = extra_checker

    def is_alive(self, target_id: str) -> bool:
        if not isinstance(target_id, str) or not target_id.strip():
            return False
        lock_path = self.presence_dir / f"{target_id}{self.lock_extension}"
        if not lock_path.is_file():
            return False
        if self.extra_checker is not None:
            return bool(self.extra_checker(target_id, lock_path))
        return True


class CallableLivenessProbe(LivenessProbe):
    """Wraps an arbitrary predicate callable `is_alive_fn(target_id) -> bool`."""

    def __init__(self, is_alive_fn: Callable[[str], bool]) -> None:
        self.is_alive_fn = is_alive_fn

    def is_alive(self, target_id: str) -> bool:
        try:
            return bool(self.is_alive_fn(target_id))
        except Exception:
            return False
