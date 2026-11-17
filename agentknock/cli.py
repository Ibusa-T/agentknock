# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock CLI & Resident Watch Daemon Runtime (RT1 Implementation).

Provides:
- `python -m agentknock watch`: Resident background daemon for receiving messages.
- Subscribes `create_notifier()` to target mailbox `undelivered/` directory.
- Dispatches via `ResidentWatcher.enqueue` -> `DeliveryAdapter.wake`.
- R-M1: Supports external process non-destructive marker lifecycle.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Callable, Literal, Optional

from agentknock._contract import (
    AGY_BASE_SUBDIR,
    UUID_REGEX,
    DeliveryAdapter,
    Notifier,
)
from agentknock.adapters.agy import AgyDeliveryAdapter
from agentknock.adapters.callback import CallbackDeliveryAdapter
from agentknock.adapters.outbox import OutboxDeliveryAdapter
from agentknock.core.mailbox import Mailbox
from agentknock.core.notifier import create_notifier
from agentknock.core.registry import FileSystemAliasRegistry, is_valid_alias
from agentknock.core.watcher import ResidentWatcher


class WatchDaemon:
    """
    Resident daemon wiring Notifier -> ResidentWatcher -> DeliveryAdapter.
    """

    def __init__(
        self,
        *,
        target: Optional[str] = None,
        adapter: Optional[DeliveryAdapter] = None,
        adapter_type: Literal["agy", "callback", "outbox"] = "agy",
        mailbox: Optional[Mailbox] = None,
        mailbox_dir: Optional[Path] = None,
        notifier: Optional[Notifier] = None,
        notifier_mode: Literal["auto", "watchdog", "polling"] = "auto",
        poll_interval_sec: float = 0.5,
        wake_locale: Optional[str] = None,
        output_locale: Optional[str] = None,
        callback: Optional[Callable[[str], bool]] = None,
        audit_log_path: Optional[Path] = None,
        rewake_cooldown_sec: float = 60.0,
        stop_after_first: bool = False,
    ) -> None:
        self.mailbox = mailbox or Mailbox(root_dir=mailbox_dir)
        self.target = target
        self.stop_after_first = stop_after_first

        # Resolve Adapter
        if adapter is not None:
            self.adapter = adapter
        elif adapter_type == "agy":
            self.adapter = AgyDeliveryAdapter(
                wake_locale=wake_locale,
                output_locale=output_locale,
            )
        elif adapter_type == "callback":
            self.adapter = CallbackDeliveryAdapter(callback=callback)
        elif adapter_type == "outbox":
            self.adapter = OutboxDeliveryAdapter()
        else:
            raise ValueError(f"Unknown adapter_type: {adapter_type!r}")

        # Resolve Notifier
        self.notifier = notifier or create_notifier(
            mode=notifier_mode,
            poll_interval_sec=poll_interval_sec,
        )

        # Build Watcher
        self.watcher = ResidentWatcher(
            mailbox=self.mailbox,
            adapter=self.adapter,
            notifier=self.notifier,
            audit_log_path=audit_log_path,
            rewake_cooldown_sec=rewake_cooldown_sec,
        )

        self._running = False
        self._stop_event = threading.Event()
        self._processed_count = 0
        self._lock = threading.Lock()

    def _resolve_target_id(self, target_spec: str) -> str:
        """Resolve alias or UUID string to canonical target_id."""
        import re

        if re.match(UUID_REGEX, target_spec):
            return target_spec

        if is_valid_alias(target_spec):
            reg = FileSystemAliasRegistry()
            resolved = reg.resolve(target_spec)
            if resolved:
                return resolved
            raise LookupError(f"Cannot resolve alias {target_spec!r} in registry.")

        return target_spec

    def _on_marker_created(self, marker_path: Path) -> None:
        # Check if filename is a hidden/temp file
        if marker_path.name.startswith("."):
            return

        self.watcher.enqueue(marker_path)
        with self._lock:
            self._processed_count += 1
            if self.stop_after_first:
                self._stop_event.set()

    def start(self) -> None:
        """Start the daemon in background threads."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()

        # Start watcher queue worker
        self.watcher.start()

        # Determine watch directory
        if self.target:
            resolved_target = self._resolve_target_id(self.target)
            watch_dir = self.mailbox.get_undelivered_dir(resolved_target)
            watch_dir.mkdir(parents=True, exist_ok=True)
            self.notifier.subscribe(watch_dir, self._on_marker_created)
        else:
            # Watch root mailbox
            root = self.mailbox.root_dir
            root.mkdir(parents=True, exist_ok=True)
            self.notifier.subscribe(root, self._on_marker_created)

        self.notifier.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the daemon gracefully."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()

        try:
            self.notifier.stop(timeout=timeout)
        except Exception:
            pass
        try:
            self.watcher.stop(timeout=timeout)
        except Exception:
            pass

    def run_forever(self, poll_interval: float = 0.5) -> None:
        """Run blocking event loop until interrupted or stopped."""
        self.start()
        try:
            while self._running and not self._stop_event.is_set():
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def run_watch_daemon(
    *,
    target: Optional[str] = None,
    adapter_type: Literal["agy", "callback", "outbox"] = "agy",
    mailbox_dir: Optional[Path] = None,
    notifier_mode: Literal["auto", "watchdog", "polling"] = "auto",
    poll_interval_sec: float = 0.5,
    wake_locale: Optional[str] = None,
    output_locale: Optional[str] = None,
    audit_log_path: Optional[Path] = None,
    stop_after_first: bool = False,
) -> None:
    daemon = WatchDaemon(
        target=target,
        adapter_type=adapter_type,
        mailbox_dir=mailbox_dir,
        notifier_mode=notifier_mode,
        poll_interval_sec=poll_interval_sec,
        wake_locale=wake_locale,
        output_locale=output_locale,
        audit_log_path=audit_log_path,
        stop_after_first=stop_after_first,
    )
    daemon.run_forever()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agentknock",
        description="agentknock — Focus-free CLI Agent Transport & Resident Watch Daemon",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # `watch` subcommand
    watch_parser = subparsers.add_parser("watch", help="Run resident message receiving daemon")
    watch_parser.add_argument(
        "--target",
        "-t",
        default=None,
        help="Target conversation_id (UUID) or registered alias to watch",
    )
    watch_parser.add_argument(
        "--adapter",
        "-a",
        choices=["agy", "callback", "outbox"],
        default="agy",
        help="Delivery adapter to wake the recipient runtime (default: agy)",
    )
    watch_parser.add_argument(
        "--mailbox",
        "-m",
        type=Path,
        default=None,
        help="Custom root mailbox directory",
    )
    watch_parser.add_argument(
        "--notifier",
        choices=["auto", "watchdog", "polling"],
        default="auto",
        help="Filesystem notification backend (default: auto)",
    )
    watch_parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds if using PollingNotifier (default: 0.5)",
    )
    watch_parser.add_argument(
        "--wake-locale",
        default=None,
        help="Wake nudge locale for AgyDeliveryAdapter (e.g. en, ja)",
    )
    watch_parser.add_argument(
        "--output-locale",
        default=None,
        help="Output directive locale for AgyDeliveryAdapter (e.g. en, ja)",
    )
    watch_parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Custom audit.jsonl log path",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="Exit after waking the first detected message (for testing/CI)",
    )

    args = parser.parse_args(argv)

    if args.subcommand == "watch":
        print(f"🚀 Starting agentknock resident watch daemon (adapter={args.adapter}, target={args.target})...")
        run_watch_daemon(
            target=args.target,
            adapter_type=args.adapter,
            mailbox_dir=args.mailbox,
            notifier_mode=args.notifier,
            poll_interval_sec=args.poll_interval,
            wake_locale=args.wake_locale,
            output_locale=args.output_locale,
            audit_log_path=args.audit_log,
            stop_after_first=args.once,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
