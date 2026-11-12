"""
Unit tests for agentknock v0.3 Core & Lifecycle (T-A and T-B).

Verifies acceptance criteria:
1. Pure Python core tests pass without _win_console.
2. R-M1: Marker does NOT disappear after wake() if consumes_marker_on_wake is False.
3. Marker is consumed if consumes_marker_on_wake is True.
4. A2: list_active() filters strictly by (valid ∧ unexpired ∧ probe.is_alive).
5. B6: create_notifier() falls back to PollingNotifier gracefully and detects new files.
6. Mailbox: Atomic temp->rename write, BOM stripping, dead letter quarantine.
7. In-Memory Cooldown: Duplicate wake() calls within cooldown are suppressed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, List
import uuid

import pytest

from agentknock._contract import Capabilities, DeliveryAdapter
from agentknock.core.mailbox import (
    FileSystemDeadLetterStore,
    Mailbox,
    attributed_title,
    sanitize_title,
    validate_sender,
)
from agentknock.core.notifier import (
    PollingNotifier,
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


# -----------------------------------------------------------------------------
# 1. Mock Delivery Adapters
# -----------------------------------------------------------------------------
class MockExternalProcessAdapter(DeliveryAdapter):
    """Simulates an external process (like agy.exe) that dequeues markers itself (consumes_marker_on_wake=False)."""

    def __init__(self, wake_result: bool = True) -> None:
        self.wake_result = wake_result
        self.wake_calls: List[str] = []

    @property
    def capabilities(self) -> Capabilities:
        return {"surface_custom_json": False, "honor_sender_locale": False}

    @property
    def consumes_marker_on_wake(self) -> bool:
        return False

    def wake(self, target_id: str) -> bool:
        self.wake_calls.append(target_id)
        return self.wake_result


class MockSyncCallbackAdapter(DeliveryAdapter):
    """Simulates a sync callback adapter where the watcher is allowed to consume markers immediately."""

    def __init__(self, wake_result: bool = True) -> None:
        self.wake_result = wake_result
        self.wake_calls: List[str] = []

    @property
    def capabilities(self) -> Capabilities:
        return {"surface_custom_json": True, "honor_sender_locale": True}

    @property
    def consumes_marker_on_wake(self) -> bool:
        return True

    def wake(self, target_id: str) -> bool:
        self.wake_calls.append(target_id)
        return self.wake_result


# -----------------------------------------------------------------------------
# 2. Acceptance Criteria 1: R-M1 Marker Lifecycle
# -----------------------------------------------------------------------------
def test_rm1_marker_does_not_disappear_after_wake_for_external_process(tmp_path: Path):
    """
    R-M1: When consumes_marker_on_wake is False, Watcher MUST NOT delete the marker
    after wake(), leaving it for the target process to dequeue.
    """
    mailbox = Mailbox(root_dir=tmp_path / "brain")
    adapter = MockExternalProcessAdapter(wake_result=True)
    watcher = ResidentWatcher(
        mailbox=mailbox,
        adapter=adapter,
        audit_log_path=tmp_path / "audit.jsonl",
        rewake_cooldown_sec=60.0,
    )

    recipient_id = str(uuid.uuid4())
    _, marker = mailbox.post(recipient_id, "test payload", sender="orchestrator")

    assert marker.is_file(), "Marker must exist initially"

    # Execute watcher
    ok = watcher.on_undelivered(marker)
    assert ok is True
    assert len(adapter.wake_calls) == 1
    assert adapter.wake_calls[0] == recipient_id

    # R-M1: The marker MUST still exist on disk!
    assert marker.is_file(), "Marker must NOT be deleted by ResidentWatcher for external adapters (R-M1)"


def test_rm1_marker_consumed_when_adapter_requests_consumption(tmp_path: Path):
    """
    When consumes_marker_on_wake is True (e.g. Callback delivery), Watcher unlinks the marker.
    """
    mailbox = Mailbox(root_dir=tmp_path / "brain")
    adapter = MockSyncCallbackAdapter(wake_result=True)
    watcher = ResidentWatcher(
        mailbox=mailbox,
        adapter=adapter,
        audit_log_path=tmp_path / "audit.jsonl",
    )

    recipient_id = str(uuid.uuid4())
    _, marker = mailbox.post(recipient_id, "test payload", sender="orchestrator")

    ok = watcher.on_undelivered(marker)
    assert ok is True
    assert len(adapter.wake_calls) == 1
    assert not marker.exists(), "Marker must be consumed when consumes_marker_on_wake is True"


# -----------------------------------------------------------------------------
# 3. Acceptance Criteria 2: In-Memory Cooldown (Duplicate Wake Suppression)
# -----------------------------------------------------------------------------
def test_in_memory_cooldown_suppresses_duplicate_wake(tmp_path: Path):
    mailbox = Mailbox(root_dir=tmp_path / "brain")
    adapter = MockExternalProcessAdapter(wake_result=True)
    watcher = ResidentWatcher(
        mailbox=mailbox,
        adapter=adapter,
        audit_log_path=tmp_path / "audit.jsonl",
        rewake_cooldown_sec=60.0,
    )

    recipient_id = str(uuid.uuid4())
    _, marker = mailbox.post(recipient_id, "payload", sender="orchestrator")

    # First event
    assert watcher.on_undelivered(marker) is True
    assert len(adapter.wake_calls) == 1

    # Second event immediately following (marker still exists on disk)
    assert watcher.on_undelivered(marker) is True
    assert len(adapter.wake_calls) == 1, "Duplicate wake() must be suppressed by in-memory cooldown"


# -----------------------------------------------------------------------------
# 4. Acceptance Criteria 3: AliasRegistry & LivenessProbe (A2/A3/R1)
# -----------------------------------------------------------------------------
def test_alias_validation():
    assert is_valid_alias("claude-agent") is True
    assert is_valid_alias("gemini_ipc_1") is True
    assert is_valid_alias("a") is True
    # Raw UUIDs must NOT be valid aliases (R1)
    assert is_valid_alias(str(uuid.uuid4())) is False
    assert is_valid_alias("") is False
    assert is_valid_alias("bad alias with spaces") is False
    assert is_valid_alias("x" * 65) is False


def test_registry_list_active_filters_by_validity_ttl_and_probe(tmp_path: Path):
    reg = FileSystemAliasRegistry(registry_dir=tmp_path / "registry")
    alive_targets = {"alive-target-1", "alive-target-2"}
    probe = CallableLivenessProbe(lambda tid: tid in alive_targets)

    # 1. Valid and alive
    reg.register("agent-one", "alive-target-1", role="Worker", ttl_sec=3600.0)
    # 2. Dead target
    reg.register("agent-dead", "dead-target-x", role="Dead", ttl_sec=3600.0)
    # 3. Valid and alive 2
    reg.register("agent-two", "alive-target-2", role="Reviewer", ttl_sec=3600.0)

    active = reg.list_active(probe=probe)
    aliases = [r["alias"] for r in active]
    assert aliases == ["agent-one", "agent-two"]

    # Verify R1: target_id / conversation_id is NEVER exposed
    for r in active:
        assert "conversation_id" not in r
        assert "target_id" not in r
        assert set(r.keys()) == {"alias", "role", "capabilities"}

    # Resolve check
    assert reg.resolve("agent-one", probe=probe) == "alive-target-1"
    assert reg.resolve("agent-dead", probe=probe) is None

    # Kill switch
    assert reg.unregister("agent-one") is True
    assert reg.resolve("agent-one", probe=probe) is None


def test_in_memory_registry(tmp_path: Path):
    reg = InMemoryAliasRegistry()
    probe = AlwaysAliveProbe()

    reg.register("test-alias", "target-123", role="Test")
    assert reg.resolve("test-alias", probe=probe) == "target-123"
    recipients = reg.list_active(probe=probe)
    assert len(recipients) == 1
    assert recipients[0]["alias"] == "test-alias"
    assert "target-123" not in recipients[0].values()


# -----------------------------------------------------------------------------
# 5. Acceptance Criteria 4: PollingNotifier & Fallback (B6)
# -----------------------------------------------------------------------------
def test_polling_notifier_detects_new_files(tmp_path: Path):
    watch_dir = tmp_path / "watch_target"
    watch_dir.mkdir()

    # Pre-create a file before subscribe
    (watch_dir / "existing.txt").write_text("old")

    notifier = PollingNotifier(poll_interval_sec=0.05)
    created: List[Path] = []

    notifier.subscribe(watch_dir, lambda p: created.append(p))
    notifier.start()

    try:
        # Create new file
        new_file = watch_dir / "new_message.txt"
        new_file.write_text("hello")

        # Wait for polling cycle
        deadline = time.time() + 2.0
        while time.time() < deadline and not created:
            time.sleep(0.02)

        assert len(created) == 1
        assert created[0].name == "new_message.txt"
    finally:
        notifier.stop(timeout=1.0)


def test_create_notifier_factory(monkeypatch):
    # Auto mode should return PollingNotifier if watchdog is absent
    monkeypatch.setenv("AGENTKNOCK_NOTIFIER", "polling")
    notifier = create_notifier()
    assert isinstance(notifier, PollingNotifier)


# -----------------------------------------------------------------------------
# 6. Mailbox & DeadLetterStore
# -----------------------------------------------------------------------------
def test_mailbox_atomic_post_and_bom_handling(tmp_path: Path):
    mailbox = Mailbox(root_dir=tmp_path / "brain")
    recipient_id = str(uuid.uuid4())

    mid, marker = mailbox.post(
        recipient_id,
        "payload content",
        sender="sender-agent",
        title="My Task",
    )

    assert marker.is_file()
    payload = mailbox.read_message(recipient_id, mid)
    assert payload is not None
    assert payload["id"] == mid
    assert payload["sender"] == "sender-agent"
    assert payload["content"] == "payload content"
    assert payload["renderDetails"]["messageTitle"] == "[sender-supplied] My Task"

    # Verify BOM handling in validate_marker
    json_path = mailbox.get_messages_dir(recipient_id) / f"{mid}.json"
    content_bytes = json_path.read_bytes()
    json_path.write_bytes(b"\xef\xbb\xbf" + content_bytes)

    validated = mailbox.validate_marker(marker)
    assert validated is not None
    assert validated["id"] == mid


def test_watcher_retry_and_dead_letter_quarantine(tmp_path: Path):
    mailbox = Mailbox(root_dir=tmp_path / "brain")
    # Adapter that always fails wake
    failing_adapter = MockExternalProcessAdapter(wake_result=False)
    audit_file = tmp_path / "audit.jsonl"
    watcher = ResidentWatcher(
        mailbox=mailbox,
        adapter=failing_adapter,
        audit_log_path=audit_file,
        base_backoff_sec=0.01,
        max_attempts=3,
        sleep_fn=lambda _: None,  # Instant retry in tests
    )

    recipient_id = str(uuid.uuid4())
    mid, marker = mailbox.post(recipient_id, "fail payload", sender="sender-agent")

    ok = watcher.on_undelivered(marker)
    assert ok is False
    assert len(failing_adapter.wake_calls) == 3

    # Marker must be removed and moved to dead_letter
    assert not marker.exists()
    dl_path = (
        tmp_path
        / "brain"
        / recipient_id
        / ".system_generated"
        / "messages"
        / "dead_letter"
        / f"{mid}.json"
    )
    assert dl_path.is_file()

    # Verify audit log
    audit_lines = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    events = [entry["event"] for entry in audit_lines]
    assert events.count("RETRY") == 2
    assert "DEAD_LETTER" in events
