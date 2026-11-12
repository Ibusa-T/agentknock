"""
agentknock core.watcher — Resident Watcher with In-Memory Cooldown & Safe Marker Lifecycle.

Implements R-M1〜R-M4 Lifecycle Invariants:
- R-M1: If `adapter.consumes_marker_on_wake == False`, Watcher NEVER unlinks
  `undelivered/<id>` after wake(); the receiving agent process owns dequeue/unlink.
  Only if `adapter.consumes_marker_on_wake == True`, Watcher unlinks after wake().
- In-Memory Cooldown: `_woken_at[message_id]` suppresses duplicate wake() calls
  for `rewake_cooldown_sec` (default 60s) without deleting the disk marker.
- Exponential Backoff: Retries transient wake failures with capped exponential delay,
  then quarantines exhausted or malformed messages via `mailbox.dead_letter_store`.
- R-M3/R-M4: Structured audit events (`WOKEN`, `CONSUMED`, `RETRY`, `DEAD_LETTER`, `REAPED`)
  written to `audit.jsonl`, plus a stale marker reaper.
- Non-blocking Dispatch Queue: `enqueue(marker_path)` decouples Notifier event loops
  from adapter execution.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional, Set

from agentknock._contract import (
    AGY_BASE_SUBDIR,
    DeliveryAdapter,
    Notifier,
)
from agentknock.core.mailbox import Mailbox


class ResidentWatcher:
    """
    Receiver-side resident watcher managing the notification-to-wake pipeline.
    """

    def __init__(
        self,
        mailbox: Mailbox,
        adapter: DeliveryAdapter,
        notifier: Optional[Notifier] = None,
        *,
        audit_log_path: Optional[Path] = None,
        rewake_cooldown_sec: float = 60.0,
        base_backoff_sec: float = 0.5,
        backoff_factor: float = 2.0,
        max_backoff_sec: float = 8.0,
        max_attempts: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.mailbox = mailbox
        self.adapter = adapter
        self.notifier = notifier

        self.audit_log_path = audit_log_path or (
            Path.home() / AGY_BASE_SUBDIR / "audit.jsonl"
        )
        self.rewake_cooldown_sec = max(0.0, rewake_cooldown_sec)
        self.base_backoff_sec = max(0.0, base_backoff_sec)
        self.backoff_factor = max(1.0, backoff_factor)
        self.max_backoff_sec = max(0.0, max_backoff_sec)
        self.max_attempts = max(1, max_attempts)
        self._clock = clock
        self._sleep = sleep_fn

        self._lock = threading.Lock()
        self._in_flight: Set[str] = set()
        self._woken_at: Dict[str, float] = {}  # message_id -> monotonic timestamp

        # Asynchronous dispatch queue
        self._queue: queue.Queue[Path] = queue.Queue()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the background dispatch worker thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="agentknock-watcher-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the background dispatch worker thread."""
        with self._lock:
            if not self._running:
                return
            self._running = False

        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

    def enqueue(self, marker_path: Path | str) -> None:
        """
        Non-blocking enqueue for Notifier callbacks.
        If the worker thread is running, the marker is pushed to the queue;
        otherwise, it is processed synchronously on_undelivered().
        """
        p = Path(marker_path)
        with self._lock:
            running = self._running

        if running:
            self._queue.put(p)
        else:
            self.on_undelivered(p)

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running and self._queue.empty():
                    break
            try:
                marker = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self.on_undelivered(marker)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def _append_audit(
        self,
        *,
        event: str,
        message_id: str,
        sender: str = "",
        recipient: str = "",
        attempts: int = 0,
        reason: str = "",
        tier: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "event": event,
            "message_id": message_id,
            "sender": sender,
            "recipient": recipient,
            "attempts": attempts,
            "reason": reason,
        }
        if tier is not None:
            record["tier"] = tier
        if extra:
            record.update(extra)

        try:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _compute_backoff(self, attempt: int) -> float:
        delay = self.base_backoff_sec * (self.backoff_factor ** max(0, attempt - 1))
        return min(delay, self.max_backoff_sec)

    def on_undelivered(self, marker_path: Path | str) -> bool:
        """
        Main pipeline entry point triggered when an `undelivered/<Message-ID>` marker is created.
        """
        marker = Path(marker_path)
        message_id = marker.name.removesuffix(".json")

        with self._lock:
            # 1. At-most-once cooldown: check if this message was recently woken
            now = self._clock()
            last_woken = self._woken_at.get(message_id)
            if last_woken is not None and (now - last_woken) < self.rewake_cooldown_sec:
                return True  # Suppressed by in-memory cooldown (no duplicate wake)

            # 2. Concurrency lock: do not process the same message in parallel
            if message_id in self._in_flight:
                return False
            self._in_flight.add(message_id)

        try:
            if not marker.exists():
                return False

            # Validate message payload
            msg_data = self.mailbox.validate_marker(marker)
            if not msg_data:
                # Quarantined as malformed
                dl_path, tier = self.mailbox.dead_letter_store.quarantine(
                    marker,
                    message_id=message_id,
                    recipient_id="",
                    sender_id="",
                    attempts=1,
                    reason="validation_failed",
                )
                self._append_audit(
                    event="DEAD_LETTER",
                    message_id=message_id,
                    attempts=1,
                    reason="validation_failed",
                    tier=tier,
                    extra={"dead_letter_path": str(dl_path)},
                )
                return False

            recipient_id = str(msg_data["recipient"])
            sender_id = str(msg_data["sender"])

            # Retry loop with exponential backoff
            for attempt in range(1, self.max_attempts + 1):
                if not marker.exists():
                    # Consumed by receiver process while waiting for retry
                    with self._lock:
                        self._woken_at[message_id] = self._clock()
                    self._append_audit(
                        event="CONSUMED",
                        message_id=message_id,
                        sender=sender_id,
                        recipient=recipient_id,
                        attempts=attempt,
                        reason="consumed_externally",
                    )
                    return True

                # Dispatch wake() — Note: wake takes ONLY target_id (A1 / R2)
                woken = self.adapter.wake(recipient_id)
                if woken:
                    with self._lock:
                        self._woken_at[message_id] = self._clock()

                    self._append_audit(
                        event="WOKEN",
                        message_id=message_id,
                        sender=sender_id,
                        recipient=recipient_id,
                        attempts=attempt,
                        reason="ok",
                    )

                    # R-M1: Marker consumption contract
                    if self.adapter.consumes_marker_on_wake:
                        try:
                            if marker.exists():
                                marker.unlink()
                        except OSError:
                            pass
                        self._append_audit(
                            event="CONSUMED",
                            message_id=message_id,
                            sender=sender_id,
                            recipient=recipient_id,
                            attempts=attempt,
                            reason="consumed_by_adapter",
                        )
                    else:
                        # R-M1: DO NOT UNLINK! The receiving process owns dequeue.
                        pass

                    return True

                # Wake failed; prepare for retry
                if attempt < self.max_attempts:
                    backoff = self._compute_backoff(attempt)
                    self._append_audit(
                        event="RETRY",
                        message_id=message_id,
                        sender=sender_id,
                        recipient=recipient_id,
                        attempts=attempt,
                        reason=f"wake_failed_backoff_{backoff:.2f}s",
                    )
                    if backoff > 0.0:
                        self._sleep(backoff)

            # Max attempts exhausted -> quarantine to dead letter
            dl_path, tier = self.mailbox.dead_letter_store.quarantine(
                marker,
                message_id=message_id,
                recipient_id=recipient_id,
                sender_id=sender_id,
                attempts=self.max_attempts,
                reason="max_attempts_exhausted",
            )
            self._append_audit(
                event="DEAD_LETTER",
                message_id=message_id,
                sender=sender_id,
                recipient=recipient_id,
                attempts=self.max_attempts,
                reason="max_attempts_exhausted",
                tier=tier,
                extra={"dead_letter_path": str(dl_path)},
            )
            return False

        finally:
            with self._lock:
                self._in_flight.discard(message_id)

    def reap_stale_markers(
        self,
        undelivered_dir: Path,
        *,
        stale_marker_ttl_sec: float = 300.0,
    ) -> list[str]:
        """
        R-M3/R-M4: Clean up orphan markers that have exceeded stale_marker_ttl_sec.
        Returns list of reaped message IDs.
        """
        if not undelivered_dir.is_dir():
            return []

        reaped: list[str] = []
        now = time.time()

        for marker in undelivered_dir.iterdir():
            if not marker.is_file() or marker.name.startswith("."):
                continue
            try:
                mtime = marker.stat().st_mtime
                if (now - mtime) > stale_marker_ttl_sec:
                    mid = marker.name
                    marker.unlink()
                    reaped.append(mid)
                    self._append_audit(
                        event="REAPED",
                        message_id=mid,
                        reason="stale_marker_ttl_exceeded",
                    )
            except OSError:
                pass

        return reaped
