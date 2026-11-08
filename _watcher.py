"""
agentknock D2 — Receiver-Side Resident Watcher (`_Watcher` implementation).

Responsibilities:
1. Debounce filesystem creation events for `messages/undelivered/<Message-ID>`.
2. Validate incoming JSON via `validate_and_load_message` (BOM strip, schema,
   `sender != "user"`, `validate_sender`).
3. Dispatch wakeup exclusively through `_AgyAdapter.wake(conversation_id, ...)`
   where wake text is resolved strictly from local catalog constants (R2).
4. Guarantee at-most-once wake per message via in-flight locking and atomic
   `undelivered/<id>` marker consumption after `ack_grace_sec` (default 1.5s).
5. Retry transient failures (e.g. PID not found / CONIN$ busy) with capped
   exponential backoff (`heapq` scheduler), then quarantine exhausted or invalid
   messages to 2-tier `dead_letter/<id>.json` (`local` vs `central`) via
   `os.replace` and append a structured record to `audit.jsonl`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import heapq
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Dict, Optional

try:
    from ._contract import (
        AGY_BASE_SUBDIR,
        DEAD_LETTER_DIRNAME,
        UUID_REGEX,
        _AgyAdapter,
        _Watcher,
    )
except ImportError:
    from _contract import (  # type: ignore[no-redef]
        AGY_BASE_SUBDIR,
        DEAD_LETTER_DIRNAME,
        UUID_REGEX,
        _AgyAdapter,
        _Watcher,
    )

_UUID_RE = re.compile(UUID_REGEX)
DEFAULT_AGY_BASE_DIR: Path = Path.home() / AGY_BASE_SUBDIR


@dataclass(order=True)
class _ScheduledTask:
    due_monotonic: float
    seq: int
    undelivered_path: Path = field(compare=False)
    attempt: int = field(default=1, compare=False)


class ResidentWatcher(_Watcher):
    """
    Receiver-side resident watcher conforming to `_contract._Watcher`.
    The sole production caller of `_AgyAdapter.wake()`.
    """

    def __init__(
        self,
        adapter: _AgyAdapter,
        validator: Callable[[str | Path], Optional[Dict[str, Any]]],
        *,
        agy_base_dir: Optional[Path] = None,
        brain_dir: Optional[Path] = None,
        audit_log_path: Optional[Path] = None,
        wake_locale: Optional[str] = None,
        output_locale: Optional[str] = None,
        debounce_sec: float = 0.08,
        ack_grace_sec: float = 1.5,
        base_backoff_sec: float = 0.5,
        backoff_factor: float = 2.0,
        max_backoff_sec: float = 8.0,
        max_attempts: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._adapter = adapter
        self._validator = validator
        self._agy_base_dir = agy_base_dir or DEFAULT_AGY_BASE_DIR
        self._brain_dir = brain_dir or (self._agy_base_dir / "brain")
        self._audit_log_path = audit_log_path or (self._agy_base_dir / "audit.jsonl")
        self._wake_locale = wake_locale
        self._output_locale = output_locale

        self._debounce_sec = max(0.0, debounce_sec)
        self._ack_grace_sec = max(0.0, ack_grace_sec)
        self._base_backoff_sec = max(0.0, base_backoff_sec)
        self._backoff_factor = max(1.0, backoff_factor)
        self._max_backoff_sec = max(0.0, max_backoff_sec)
        self._max_attempts = max(1, max_attempts)
        self._clock = clock
        self._sleep = sleep_fn

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._in_flight: set[str] = set()
        self._dispatched_ids: set[str] = set()
        self._heap: list[_ScheduledTask] = []
        self._seq: int = 0
        self._running: bool = False
        self._worker_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Audit logging & 2-tier dead-letter quarantine
    # ------------------------------------------------------------------
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
        dead_letter_path: Optional[Path] = None,
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
        if dead_letter_path is not None:
            record["dead_letter_path"] = str(dead_letter_path)

        try:
            self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def _resolve_json_path(marker_path: Path, message_id: str) -> Path:
        if marker_path.parent.name == "undelivered":
            return marker_path.parent.parent / f"{message_id}.json"
        return marker_path.with_suffix(".json")

    def _quarantine_to_dead_letter(
        self,
        marker_path: Path,
        *,
        message_id: str,
        recipient_id: str,
        sender_id: str,
        attempts: int,
        reason: str,
    ) -> Path:
        """
        Move failed/invalid `<Message-ID>.json` to 2-tier `dead_letter/<id>.json`
        via `os.replace` and unlink the `undelivered/<id>` marker.
        - Tier "local": `brain/<conversation_id>/.system_generated/messages/dead_letter/<id>.json`
        - Tier "central": `~/.gemini/antigravity-cli/dead_letter/<id>.json`
        """
        if _UUID_RE.match(recipient_id):
            tier = "local"
            dl_dir = (
                self._brain_dir
                / recipient_id
                / ".system_generated"
                / "messages"
                / DEAD_LETTER_DIRNAME
            )
        else:
            tier = "central"
            dl_dir = self._agy_base_dir / DEAD_LETTER_DIRNAME

        dl_dir.mkdir(parents=True, exist_ok=True)
        safe_name = message_id if _UUID_RE.match(message_id) else marker_path.name
        dest_path = dl_dir / f"{safe_name}.json"
        src_json = self._resolve_json_path(marker_path, message_id)

        try:
            if src_json.is_file():
                os.replace(src_json, dest_path)
            else:
                # No valid JSON payload on disk; write diagnostic stub so dead_letter has a record
                stub = {
                    "id": message_id,
                    "recipient": recipient_id,
                    "sender": sender_id,
                    "quarantined_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "reason": reason,
                    "attempts": attempts,
                }
                tmp_stub = dl_dir / f".{safe_name}.tmp"
                tmp_stub.write_text(json.dumps(stub, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp_stub, dest_path)
        finally:
            try:
                if marker_path.exists():
                    marker_path.unlink()
            except OSError:
                pass

        self._append_audit(
            event="DEAD_LETTER",
            message_id=message_id,
            sender=sender_id,
            recipient=recipient_id,
            attempts=attempts,
            reason=reason,
            tier=tier,
            dead_letter_path=dest_path,
        )
        return dest_path

    def _compute_backoff(self, attempt: int) -> float:
        delay = self._base_backoff_sec * (self._backoff_factor ** max(0, attempt - 1))
        return min(delay, self._max_backoff_sec)

    def _wait_or_consume_marker(self, marker_path: Path) -> None:
        """
        Wait up to `ack_grace_sec` for `agy.exe` to dequeue `undelivered/<id>`.
        If still present after the grace window, unlink atomically to guarantee
        at-most-once wake across restarts.
        """
        if self._ack_grace_sec > 0.0:
            deadline = self._clock() + self._ack_grace_sec
            step = min(0.05, self._ack_grace_sec)
            while self._clock() < deadline:
                if not marker_path.exists():
                    return
                self._sleep(step)
        try:
            if marker_path.exists():
                marker_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Synchronous `_Watcher.on_undelivered` entry point
    # ------------------------------------------------------------------
    def on_undelivered(self, undelivered_path: str | Path) -> bool:
        """
        Process an `undelivered/<Message-ID>` file with debounce, validation,
        exponential backoff retry, and 2-tier dead-letter quarantine.
        """
        marker = Path(undelivered_path)
        message_id = marker.name.removesuffix(".json")

        with self._lock:
            if message_id in self._dispatched_ids or message_id in self._in_flight:
                return False
            self._in_flight.add(message_id)

        try:
            if self._debounce_sec > 0.0:
                self._sleep(self._debounce_sec)

            if not marker.exists():
                return False

            msg_data = self._validator(marker)
            if not msg_data:
                # Try to extract recipient_id only for local vs central tier routing
                inferred_recipient = ""
                if (
                    marker.parent.name == "undelivered"
                    and marker.parent.parent.name == "messages"
                    and marker.parent.parent.parent.name == ".system_generated"
                ):
                    candidate_conv = marker.parent.parent.parent.parent.name
                    if _UUID_RE.match(candidate_conv):
                        inferred_recipient = candidate_conv

                self._quarantine_to_dead_letter(
                    marker,
                    message_id=message_id,
                    recipient_id=inferred_recipient,
                    sender_id="",
                    attempts=1,
                    reason="validation_failed",
                )
                return False

            recipient_id = str(msg_data.get("recipient", ""))
            sender_id = str(msg_data.get("sender", ""))

            if not _UUID_RE.match(recipient_id):
                self._quarantine_to_dead_letter(
                    marker,
                    message_id=message_id,
                    recipient_id="",
                    sender_id=sender_id,
                    attempts=1,
                    reason="invalid_recipient_uuid",
                )
                return False

            for attempt in range(1, self._max_attempts + 1):
                if not marker.exists():
                    with self._lock:
                        self._dispatched_ids.add(message_id)
                    self._append_audit(
                        event="DELIVERED",
                        message_id=message_id,
                        sender=sender_id,
                        recipient=recipient_id,
                        attempts=attempt,
                        reason="consumed_before_retry",
                    )
                    return True

                # R2 invariant: wake_locale / output_locale are strictly local;
                # msg_data is NEVER passed to wake().
                woken = self._adapter.wake(
                    recipient_id,
                    wake_locale=self._wake_locale,
                    output_locale=self._output_locale,
                )
                if woken:
                    with self._lock:
                        self._dispatched_ids.add(message_id)
                    self._wait_or_consume_marker(marker)
                    self._append_audit(
                        event="DELIVERED",
                        message_id=message_id,
                        sender=sender_id,
                        recipient=recipient_id,
                        attempts=attempt,
                        reason="ok",
                    )
                    return True

                if attempt < self._max_attempts:
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

            # Exhausted all attempts -> quarantine to dead_letter
            self._quarantine_to_dead_letter(
                marker,
                message_id=message_id,
                recipient_id=recipient_id,
                sender_id=sender_id,
                attempts=self._max_attempts,
                reason="max_attempts_exhausted",
            )
            return False
        finally:
            with self._lock:
                self._in_flight.discard(message_id)

    # ------------------------------------------------------------------
    # Non-blocking `heapq` background scheduler for Orochi FS watcher threads
    # ------------------------------------------------------------------
    def enqueue(self, undelivered_path: str | Path) -> None:
        """
        Non-blocking enqueue for filesystem watcher threads (zero stall).
        Schedules `undelivered_path` after `debounce_sec` on the worker heap.
        """
        marker = Path(undelivered_path)
        with self._cv:
            self._seq += 1
            task = _ScheduledTask(
                due_monotonic=self._clock() + self._debounce_sec,
                seq=self._seq,
                undelivered_path=marker,
            )
            heapq.heappush(self._heap, task)
            self._cv.notify()

    def start(self) -> None:
        """Start the background dispatch worker thread."""
        with self._cv:
            if self._running:
                return
            self._running = True
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="agentknock-resident-watcher",
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the background dispatch worker thread gracefully."""
        with self._cv:
            self._running = False
            self._cv.notify_all()
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)

    def _worker_loop(self) -> None:
        while True:
            with self._cv:
                while self._running and not self._heap:
                    self._cv.wait()
                if not self._running and not self._heap:
                    return
                now = self._clock()
                next_task = self._heap[0]
                wait_sec = next_task.due_monotonic - now
                if wait_sec > 0.0:
                    self._cv.wait(timeout=wait_sec)
                    continue
                heapq.heappop(self._heap)

            # Execute outside the lock; nogil injection never stalls enqueue()
            orig_debounce = self._debounce_sec
            try:
                self._debounce_sec = 0.0
                self.on_undelivered(next_task.undelivered_path)
            finally:
                self._debounce_sec = orig_debounce
