"""
End-to-End integration tests for agentknock resident watch daemon (RT1).

Acceptance criteria:
1. `agentknock.send()` -> daemon detects via Notifier -> ResidentWatcher -> adapter.wake()
   runs with ZERO manual intervention.
2. CallbackDeliveryAdapter executes callback and consumes marker.
3. AgyDeliveryAdapter runs wake() while preserving marker for recipient dequeue (R-M1).
4. `python -m agentknock watch --help` exits cleanly.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import List
import uuid

import pytest

import agentknock
from agentknock import AgentKnock
from agentknock.adapters.agy import AgyDeliveryAdapter
from agentknock.adapters.callback import CallbackDeliveryAdapter
from agentknock.cli import WatchDaemon
from agentknock.core.mailbox import Mailbox
from agentknock.core.probe import AlwaysAliveProbe
from agentknock.core.registry import InMemoryAliasRegistry


def test_daemon_watch_end_to_end_zero_touch(tmp_path: Path):
    """
    Core RT1 acceptance test:
    Sender calls agentknock.send() -> background WatchDaemon detects and wakes
    recipient runtime with ZERO manual intervention.
    """
    mailbox_dir = tmp_path / "brain"
    mailbox = Mailbox(root_dir=mailbox_dir)
    registry = InMemoryAliasRegistry()
    probe = AlwaysAliveProbe()

    client = AgentKnock(mailbox=mailbox, registry=registry, probe=probe)

    # 1. Register recipient
    target_uuid = str(uuid.uuid4())
    client.register_self("worker-agent", target_uuid, role="Worker")

    # 2. Setup CallbackDeliveryAdapter
    received: List[str] = []

    def on_wake(tid: str) -> bool:
        received.append(tid)
        return True

    adapter = CallbackDeliveryAdapter(callback=on_wake)

    # 3. Start resident WatchDaemon
    daemon = WatchDaemon(
        target="worker-agent",
        adapter=adapter,
        mailbox=mailbox,
        notifier_mode="polling",
        poll_interval_sec=0.05,
        audit_log_path=tmp_path / "audit.jsonl",
    )
    # Wire daemon's target resolution with our in-memory registry
    daemon._resolve_target_id = lambda t: target_uuid

    daemon.start()

    try:
        # 4. Sender transmits message
        msg_id = client.send(
            recipient="worker-agent",
            content="Automated transmission test",
            sender="orchestrator",
        )
        assert msg_id, "Message ID must be returned"

        # 5. Wait for daemon to dispatch with zero touch
        deadline = time.time() + 3.0
        while time.time() < deadline and not received:
            time.sleep(0.05)

        assert len(received) == 1, "Daemon must automatically wake the target runtime"
        assert received[0] == target_uuid

        # 6. Verify marker consumed for Callback adapter
        marker_path = mailbox.get_undelivered_dir(target_uuid) / msg_id
        # Wait a short moment for async task completion
        time.sleep(0.1)
        assert not marker_path.exists(), "CallbackDeliveryAdapter must consume the marker on wake"

    finally:
        daemon.stop(timeout=1.0)


def test_daemon_watch_agy_adapter_r_m1_retention(tmp_path: Path, monkeypatch):
    """
    R-M1 verification in resident daemon:
    When AgyDeliveryAdapter is used, wake() runs but the marker is NOT deleted,
    allowing the receiving agy.exe process to dequeue it independently.
    """
    mailbox_dir = tmp_path / "brain"
    mailbox = Mailbox(root_dir=mailbox_dir)
    registry = InMemoryAliasRegistry()
    probe = AlwaysAliveProbe()
    client = AgentKnock(mailbox=mailbox, registry=registry, probe=probe)

    target_uuid = str(uuid.uuid4())
    client.register_self("agy-worker", target_uuid, role="AgyWorker")

    # Mock AgyDeliveryAdapter injection (v0.3.1 verified seam: (pid, start_time))
    injected_targets: List[str] = []
    agy_adapter = AgyDeliveryAdapter()
    monkeypatch.setattr(
        agy_adapter,
        "_find_process_identity",
        lambda cid: (9999, 134391280290210460),
    )
    monkeypatch.setattr(
        agy_adapter,
        "_inject",
        lambda pid, text, *, expected_start_time=None, **kw: (
            injected_targets.append(f"{target_uuid}:{expected_start_time}") or True
        )
        if expected_start_time == 134391280290210460
        else False,
    )

    daemon = WatchDaemon(
        target="agy-worker",
        adapter=agy_adapter,
        mailbox=mailbox,
        notifier_mode="polling",
        poll_interval_sec=0.05,
        audit_log_path=tmp_path / "audit.jsonl",
    )
    daemon._resolve_target_id = lambda t: target_uuid
    daemon.start()

    try:
        msg_id = client.send(
            recipient="agy-worker",
            content="Task for agy",
            sender="orchestrator",
        )

        deadline = time.time() + 3.0
        while time.time() < deadline and not injected_targets:
            time.sleep(0.05)

        assert len(injected_targets) == 1, "AgyDeliveryAdapter must receive wake signal"

        # R-M1: Marker MUST still exist on disk!
        marker_path = mailbox.get_undelivered_dir(target_uuid) / msg_id
        time.sleep(0.1)
        assert marker_path.is_file(), "R-M1: Marker must NOT be deleted by Watcher for AgyDeliveryAdapter"

    finally:
        daemon.stop(timeout=1.0)


def test_cli_watch_help_subprocess():
    """Verify that `python -m agentknock watch --help` runs and exits with 0."""
    tools_dir = Path(__file__).parent.parent
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{tools_dir}{os.pathsep}{existing_pp}" if existing_pp else str(tools_dir)

    result = subprocess.run(
        [sys.executable, "-m", "agentknock", "watch", "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "agentknock watch" in result.stdout or "--target" in result.stdout
