# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock core.notifier — Pluggable Notifier Implementations (B6 / Extension Point 4).

Implements Notifier:
- PollingNotifier: Zero-dependency fallback using standard library threading.
- WatchdogNotifier: Low-latency OS-native filesystem events via `watchdog`.
- `create_notifier()` factory: Auto-detects watchdog with graceful fallback to PollingNotifier.
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Callable, Dict, Final, Literal, Optional, Set

from agentknock._contract import Notifier

DEFAULT_POLL_INTERVAL_SEC: Final[float] = 0.5


class PollingNotifier(Notifier):
    """
    Standard-library polling notifier for environments without watchdog.
    Runs a lightweight background thread scanning subscribed directories for newly created files.
    """

    def __init__(
        self,
        *,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.poll_interval_sec = max(0.01, poll_interval_sec)
        self._clock = clock
        self._sleep = sleep_fn

        self._subscriptions: Dict[Path, Callable[[Path], None]] = {}
        self._seen_files: Dict[Path, Set[str]] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def subscribe(self, directory: Path, on_created: Callable[[Path], None]) -> None:
        resolved = Path(directory).resolve()
        with self._lock:
            self._subscriptions[resolved] = on_created
            # Initialize seen files with currently existing files so we only alert on newly created ones
            if resolved.is_dir():
                try:
                    self._seen_files[resolved] = {
                        p.name for p in resolved.iterdir() if not p.name.startswith(".")
                    }
                except OSError:
                    self._seen_files[resolved] = set()
            else:
                self._seen_files[resolved] = set()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._poll_loop,
                name="agentknock-polling-notifier",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _poll_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
                subs = list(self._subscriptions.items())

            for directory, callback in subs:
                if not directory.is_dir():
                    continue

                try:
                    current_names = {
                        p.name for p in directory.iterdir() if not p.name.startswith(".")
                    }
                except OSError:
                    continue

                with self._lock:
                    previous_names = self._seen_files.get(directory, set())
                    new_names = current_names - previous_names
                    self._seen_files[directory] = current_names

                for name in sorted(new_names):
                    file_path = directory / name
                    try:
                        callback(file_path)
                    except Exception:
                        pass

            self._sleep(self.poll_interval_sec)


class WatchdogNotifier(Notifier):
    """
    OS-native event notifier powered by `watchdog`.
    """

    def __init__(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as e:
            raise ImportError(
                "watchdog is not installed. Install via `pip install agentknock[watch]` "
                "or use PollingNotifier."
            ) from e

        self._observer = Observer()
        self._subscriptions: Dict[Path, Callable[[Path], None]] = {}
        self._lock = threading.Lock()
        self._running = False

        class _Handler(FileSystemEventHandler):
            def __init__(self, callback: Callable[[Path], None]) -> None:
                super().__init__()
                self.callback = callback

            def on_created(self, event: Any) -> None:
                if getattr(event, "is_directory", False):
                    return
                src_path = Path(event.src_path)
                if src_path.name.startswith("."):
                    return
                try:
                    self.callback(src_path)
                except Exception:
                    pass

        self._handler_cls = _Handler

    def subscribe(self, directory: Path, on_created: Callable[[Path], None]) -> None:
        resolved = Path(directory).resolve()
        with self._lock:
            resolved.mkdir(parents=True, exist_ok=True)
            self._subscriptions[resolved] = on_created
            handler = self._handler_cls(on_created)
            self._observer.schedule(handler, str(resolved), recursive=False)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._observer.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._observer.stop()
        self._observer.join(timeout=timeout)


def create_notifier(
    mode: Literal["auto", "watchdog", "polling"] = "auto",
    *,
    poll_interval_sec: Optional[float] = None,
) -> Notifier:
    """
    Factory creating a Notifier instance according to specified mode or env settings.
    - Resolves mode from AGENTKNOCK_NOTIFIER env var ('auto' | 'watchdog' | 'polling') if set.
    - Resolves interval from AGENTKNOCK_POLL_INTERVAL_SEC env var if set.
    - Falls back gracefully to PollingNotifier if watchdog is not available in 'auto' mode.
    """
    env_mode = os.environ.get("AGENTKNOCK_NOTIFIER", "").strip().lower()
    effective_mode: str = env_mode if env_mode in ("auto", "watchdog", "polling") else mode

    env_interval = os.environ.get("AGENTKNOCK_POLL_INTERVAL_SEC", "").strip()
    if poll_interval_sec is not None:
        interval = poll_interval_sec
    elif env_interval:
        try:
            interval = float(env_interval)
        except ValueError:
            interval = DEFAULT_POLL_INTERVAL_SEC
    else:
        interval = DEFAULT_POLL_INTERVAL_SEC

    if effective_mode == "watchdog":
        return WatchdogNotifier()

    if effective_mode == "polling":
        return PollingNotifier(poll_interval_sec=interval)

    # mode == "auto"
    try:
        return WatchdogNotifier()
    except (ImportError, Exception):
        return PollingNotifier(poll_interval_sec=interval)
