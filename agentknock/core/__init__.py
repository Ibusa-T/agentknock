# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock.core — Zero-dependency core library (Mailbox, Registry, Probe, Notifier, Watcher).
"""
from agentknock.core.mailbox import (
    FileSystemDeadLetterStore,
    Mailbox,
    attributed_title,
    sanitize_title,
    validate_sender,
)
from agentknock.core.notifier import (
    PollingNotifier,
    WatchdogNotifier,
    create_notifier,
)
from agentknock.core.probe import (
    AlwaysAliveProbe,
    CallableLivenessProbe,
    FileLockLivenessProbe,
)
from agentknock.core.registry import (
    FileSystemAliasRegistry,
    InMemoryAliasRegistry,
    is_valid_alias,
)
from agentknock.core.watcher import ResidentWatcher

__all__ = [
    "Mailbox",
    "FileSystemDeadLetterStore",
    "validate_sender",
    "sanitize_title",
    "attributed_title",
    "FileSystemAliasRegistry",
    "InMemoryAliasRegistry",
    "is_valid_alias",
    "AlwaysAliveProbe",
    "FileLockLivenessProbe",
    "CallableLivenessProbe",
    "PollingNotifier",
    "WatchdogNotifier",
    "create_notifier",
    "ResidentWatcher",
]
