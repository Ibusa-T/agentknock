"""
v0.3.1 Hardening Tests: PID Reuse / TOCTOU Lockdown & 3 Adjacent Fixes.

Covers:
1. (3) Kernel HANDLE pinning + exact ProcessStartTime (64-bit FILETIME) check +
   fail-closed `expected_start_time` requirement + `audit.jsonl` structured reason propagation.
2. (3-Adjacent #1) `AgyLockLivenessProbe.is_alive()` fail-closed when `_win_console is None`.
3. (3-Adjacent #2) `agentknock.adapters._win_console_ctypes` genuine ctypes backend (no fake `return True` stub).
4. (3-Adjacent #3) `RmGetList` multi-holder verified selection (`QueryFullProcessImageNameW` allowlist +
   `TSSessionId` check + fail-closed on ambiguity + `ERROR_MORE_DATA` bounded retry).
5. Live Windows subprocess verification of `RM_UNIQUE_PROCESS.ProcessStartTime == GetProcessTimes.lpCreationTime`.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from agentknock.adapters import _win_console_ctypes as wc
import agentknock.adapters.agy as agy_mod
from agentknock.adapters.agy import (
    OUTPUT_DIRECTIVE_CATALOG,
    WAKE_BODY_CATALOG,
    WAKE_NUDGE_CATALOG,
    AgyDeliveryAdapter,
    AgyLockLivenessProbe,
)
import agentknock._adapter as legacy_adp
from agentknock.core.mailbox import Mailbox
from agentknock.core.watcher import ResidentWatcher
from agentknock.testing import DEFAULT_FORBIDDEN_SYMBOLS, assert_send_cannot_reach


# ============================================================================
# 1. Fail-Closed Defaults: AgyLockLivenessProbe & Unverified Injection Reject
# ============================================================================

def test_agy_liveness_probe_fail_closed_when_win_console_none(tmp_path: Path, monkeypatch):
    """(3-Adjacent #1): _win_console is None must return False even if .lock exists."""
    import agentknock.adapters.agy as agy_mod

    cid = str(uuid.uuid4())
    lock_file = tmp_path / f"{cid}.lock"
    lock_file.write_text("locked", encoding="utf-8")

    monkeypatch.setattr(agy_mod, "_win_console", None)
    probe = AgyLockLivenessProbe(presence_dir=tmp_path)
    assert probe.is_alive(cid) is False


def test_inject_console_input_fail_closed_without_expected_start_time():
    """(3) & (3-Adjacent #2): Calling inject_console_input without expected_start_time returns False."""
    assert wc.inject_console_input(os.getpid(), "hello") is False
    assert wc.inject_console_input(os.getpid(), "hello", expected_start_time=None) is False
    assert wc.inject_console_input(os.getpid(), "hello", expected_start_time=0) is False
    assert wc.inject_console_input(os.getpid(), "hello", expected_start_time=-5) is False

    adapter = AgyDeliveryAdapter()
    assert adapter._inject(os.getpid(), "hello") is False
    assert adapter._inject(os.getpid(), "hello", expected_start_time=0) is False
    assert adapter.last_wake_detail is not None
    assert adapter.last_wake_detail["reason"] == "invalid_pid_or_start_time"

    legacy = legacy_adp._AgyAdapter()
    assert legacy._inject(os.getpid(), "hello") is False
    assert legacy._inject(os.getpid(), "hello", expected_start_time=0) is False


# ============================================================================
# 2. Kernel HANDLE Pinning Sequence & Step-by-Step Verification
# ============================================================================

def test_inject_verified_pinning_order_and_start_time_mismatch(monkeypatch):
    """
    Verify that inject_console_input_verified:
    1) Opens HANDLE via OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE) first,
    2) Checks WaitForSingleObject(h, 0) == WAIT_TIMEOUT,
    3) Checks GetProcessTimes creation time == expected_start_time,
    4) Rejects immediately on start_time mismatch BEFORE FreeConsole/AttachConsole,
    5) Closes HANDLE in outermost finally.
    """
    events: list[str] = []

    class FakeKernel32:
        def OpenProcess(self, access, inherit, pid):
            assert access == (wc.PROCESS_QUERY_LIMITED_INFORMATION | wc.SYNCHRONIZE)
            events.append(f"OpenProcess:{pid}")
            return 9001

        def WaitForSingleObject(self, h, timeout):
            assert h == 9001 and timeout == 0
            events.append("WaitForSingleObject")
            return wc.WAIT_TIMEOUT

        def GetProcessTimes(self, h, c_ptr, e_ptr, k_ptr, u_ptr):
            assert h == 9001
            events.append("GetProcessTimes")
            c_ft = ctypes.cast(c_ptr, ctypes.POINTER(wc.FILETIME)).contents
            c_ft.dwHighDateTime = 0x01DC0000
            c_ft.dwLowDateTime = 0x12345678
            return 1

        def FreeConsole(self):
            events.append("FreeConsole")
            return 1

        def AttachConsole(self, pid):
            events.append(f"AttachConsole:{pid}")
            return 1

        def CloseHandle(self, h):
            events.append(f"CloseHandle:{h}")
            return 1

    monkeypatch.setattr(wc, "_kernel32", FakeKernel32())

    wrong_start_time = (0x01DC0000 << 32) | 0x99999999
    ok, reason, err = wc.inject_console_input_verified(4242, wrong_start_time, "wake")
    assert ok is False
    assert reason == "pid_reused_start_time_mismatch"
    assert err == 0
    # Must NEVER call FreeConsole or AttachConsole on mismatch, and MUST close h_proc!
    assert events == [
        "OpenProcess:4242",
        "WaitForSingleObject",
        "GetProcessTimes",
        "CloseHandle:9001",
    ]


def test_inject_verified_holds_handle_until_after_console_restore(monkeypatch):
    """
    Verify that on successful injection, CloseHandle(h_proc) happens AFTER
    FreeConsole/AttachConsole(ATTACH_PARENT_PROCESS), proving Kernel HANDLE pinning
    across the entire AttachConsole + WriteConsoleInputW window.
    """
    events: list[str] = []
    expected_ft = (0x01DC0000 << 32) | 0x12345678

    class FakeKernel32:
        def OpenProcess(self, access, inherit, pid):
            events.append("OpenProcess")
            return 7777

        def WaitForSingleObject(self, h, timeout):
            events.append("WaitForSingleObject")
            return wc.WAIT_TIMEOUT

        def GetProcessTimes(self, h, c_ptr, e_ptr, k_ptr, u_ptr):
            events.append("GetProcessTimes")
            c_ft = ctypes.cast(c_ptr, ctypes.POINTER(wc.FILETIME)).contents
            c_ft.dwHighDateTime = 0x01DC0000
            c_ft.dwLowDateTime = 0x12345678
            return 1

        def QueryFullProcessImageNameW(self, h, flags, buf, size_ptr):
            events.append("QueryFullProcessImageNameW")
            val = r"C:\Program Files\Antigravity\agy.exe"
            for idx, ch in enumerate(val):
                buf[idx] = ch
            buf[len(val)] = "\0"
            ctypes.cast(size_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = len(val)
            return 1

        def FreeConsole(self):
            events.append("FreeConsole")
            return 1

        def AttachConsole(self, pid):
            if pid == wc.ATTACH_PARENT_PROCESS:
                events.append("AttachConsole:PARENT")
            else:
                events.append(f"AttachConsole:{pid}")
            return 1

        def CreateFileW(self, name, access, share, sec, disp, flags, tmpl):
            events.append(f"CreateFileW:{name}")
            return 8888

        def WriteConsoleInputW(self, h, recs, length, written_ptr):
            events.append(f"WriteConsoleInputW:{length}")
            return 1

        def CloseHandle(self, h):
            events.append(f"CloseHandle:{h}")
            return 1

    monkeypatch.setattr(wc, "_kernel32", FakeKernel32())

    ok, reason, err = wc.inject_console_input_verified(
        5555,
        expected_ft,
        "hi",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert ok is True
    assert reason == "ok"
    assert err == 0
    assert events == [
        "OpenProcess",
        "WaitForSingleObject",
        "GetProcessTimes",
        "QueryFullProcessImageNameW",
        "FreeConsole",
        "AttachConsole:5555",
        "CreateFileW:CONIN$",
        "WaitForSingleObject",
        "WriteConsoleInputW:4",
        "WriteConsoleInputW:2",
        "CloseHandle:8888",
        "FreeConsole",
        "AttachConsole:PARENT",
        "CloseHandle:7777",
    ]


# ============================================================================
# 3. RmGetList Multi-Holder Filtering, Ambiguity Reject & ERROR_MORE_DATA Retry
# ============================================================================

def test_rm_get_list_filters_by_image_and_session_and_rejects_ambiguity(monkeypatch):
    """
    (3-Adjacent #3):
    - Filters out candidates from other TS sessions or non-matching executable names.
    - Returns the single matching candidate `(pid, start_time)`.
    - Returns None (fail-closed) when 2 distinct eligible PIDs match.
    """
    ft_good_1 = (0x01DC0001 << 32) | 0x00000011
    ft_good_2 = (0x01DC0001 << 32) | 0x00000022
    ft_other = (0x01DC0001 << 32) | 0x00000033

    candidates_data = [
        # Candidate 1: wrong image (explorer.exe), same session
        (1001, ft_other, 1, r"C:\Windows\explorer.exe"),
        # Candidate 2: right image (agy.exe), wrong session (session 2 != 1)
        (1002, ft_other, 2, r"C:\App\agy.exe"),
        # Candidate 3: right image (agy.exe), right session (1)
        (1003, ft_good_1, 1, r"C:\App\agy.exe"),
    ]

    proc_map = {pid: (ft, img) for pid, ft, _, img in candidates_data}

    class FakeRstrtMgr:
        def __init__(self):
            self.list_calls = 0

        def RmStartSession(self, handle_ptr, flags, key):
            ctypes.cast(handle_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 1
            return 0

        def RmRegisterResources(self, session, nfiles, files, napps, apps, nsvcs, svcs):
            return 0

        def RmGetList(self, session, needed_ptr, count_ptr, info_ptr, reason_ptr):
            self.list_calls += 1
            needed = ctypes.cast(needed_ptr, ctypes.POINTER(ctypes.wintypes.UINT)).contents
            count = ctypes.cast(count_ptr, ctypes.POINTER(ctypes.wintypes.UINT)).contents
            needed.value = len(candidates_data)
            if info_ptr is None:
                return wc.ERROR_MORE_DATA
            # Simulate ERROR_MORE_DATA on first buffer pass to test bounded retry
            if self.list_calls == 2:
                return wc.ERROR_MORE_DATA
            count.value = len(candidates_data)
            for idx, (pid, ft, sess, _) in enumerate(candidates_data):
                info_ptr[idx].Process.dwProcessId = pid
                info_ptr[idx].Process.ProcessStartTime.dwHighDateTime = (ft >> 32) & 0xFFFFFFFF
                info_ptr[idx].Process.ProcessStartTime.dwLowDateTime = ft & 0xFFFFFFFF
                info_ptr[idx].TSSessionId = sess
            return 0

        def RmEndSession(self, session):
            return 0

    class FakeKernel32:
        def GetCurrentProcessId(self):
            return 999

        def ProcessIdToSessionId(self, pid, sess_ptr):
            ctypes.cast(sess_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 1
            return 1

        def OpenProcess(self, access, inherit, pid):
            return pid if pid in proc_map else 0

        def WaitForSingleObject(self, h, timeout):
            return wc.WAIT_TIMEOUT

        def GetProcessTimes(self, h, c_ptr, e_ptr, k_ptr, u_ptr):
            ft, _ = proc_map[h]
            c_ft = ctypes.cast(c_ptr, ctypes.POINTER(wc.FILETIME)).contents
            c_ft.dwHighDateTime = (ft >> 32) & 0xFFFFFFFF
            c_ft.dwLowDateTime = ft & 0xFFFFFFFF
            return 1

        def QueryFullProcessImageNameW(self, h, flags, buf, size_ptr):
            _, img = proc_map[h]
            for idx, ch in enumerate(img):
                buf[idx] = ch
            buf[len(img)] = "\0"
            ctypes.cast(size_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = len(img)
            return 1

        def CloseHandle(self, h):
            return 1

    monkeypatch.setattr(wc, "_rstrtmgr", FakeRstrtMgr())
    monkeypatch.setattr(wc, "_kernel32", FakeKernel32())

    # Exactly 1 valid agy.exe in session 1 -> returns (1003, ft_good_1)
    res = wc.find_process_by_lock_path("C:\\dummy.lock", allowed_images=("agy.exe",))
    assert res == (1003, ft_good_1)

    # Add a second distinct agy.exe PID in session 1 -> must fail closed (None) due to ambiguity
    candidates_data.append((1004, ft_good_2, 1, r"C:\App\agy.exe"))
    proc_map[1004] = (ft_good_2, r"C:\App\agy.exe")
    monkeypatch.setattr(wc, "_rstrtmgr", FakeRstrtMgr())
    assert wc.find_process_by_lock_path("C:\\dummy.lock", allowed_images=("agy.exe",)) is None


# ============================================================================
# 4. Live Windows Integration: Exact 64-bit FILETIME Match & TOCTOU Rejection
# ============================================================================

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only live Win32 API test")
def test_live_win32_lock_holder_start_time_exact_match_and_toctou_reject(tmp_path: Path):
    """
    Spawns a real child process holding a lock file on Windows and verifies:
    1) `find_process_by_lock_path` discovers `(child.pid, start_time_u64)` where
       `RM_UNIQUE_PROCESS.ProcessStartTime` == `GetProcessTimes.lpCreationTime` (exact 64-bit match).
    2) `AgyLockLivenessProbe` returns True for matching image and False for non-matching image.
    3) `inject_console_input_verified` rejects `start_time_u64 + 1` with `"pid_reused_start_time_mismatch"`.
    4) After child exits, `inject_console_input_verified` rejects with `"process_already_exited"` or `"open_process_failed"`.
    """
    cid = str(uuid.uuid4())
    lock_file = tmp_path / f"{cid}.lock"
    ready_file = tmp_path / "ready.txt"

    child_code = (
        "import os, pathlib, time\n"
        f"lf = open({str(lock_file)!r}, 'w')\n"
        "lf.write('hold')\n"
        "lf.flush()\n"
        f"pathlib.Path({str(ready_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(10)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", child_code])
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and not ready_file.is_file():
            time.sleep(0.05)
        assert ready_file.is_file(), "Child process did not create ready marker"
        real_child_pid = int(ready_file.read_text(encoding="utf-8").strip())

        # Query real child's creation time and image basename via live Win32 kernel32 APIs
        h_child = wc._kernel32.OpenProcess(
            wc.PROCESS_QUERY_LIMITED_INFORMATION | wc.SYNCHRONIZE,
            False,
            real_child_pid,
        )
        assert wc._is_valid_handle(h_child), "OpenProcess must succeed on live child"
        try:
            assert wc._kernel32.WaitForSingleObject(h_child, 0) == wc.WAIT_TIMEOUT
            c_ft = wc.FILETIME()
            e_ft = wc.FILETIME()
            k_ft = wc.FILETIME()
            u_ft = wc.FILETIME()
            assert wc._kernel32.GetProcessTimes(
                h_child,
                ctypes.byref(c_ft),
                ctypes.byref(e_ft),
                ctypes.byref(k_ft),
                ctypes.byref(u_ft),
            )
            real_start_time = wc._filetime_to_u64(c_ft)
            assert real_start_time > 0
            py_exe, _ = wc._query_image_basename_lower(h_child)
            assert py_exe is not None
        finally:
            wc._kernel32.CloseHandle(h_child)

        # Check if Restart Manager RPC session creation is permitted in current environment
        # (AppContainer sandboxes return ERROR_WRITE_FAULT=29 on RmStartSession)
        s_test = ctypes.wintypes.DWORD(0)
        k_test = (ctypes.wintypes.WCHAR * (wc.CCH_RM_SESSION_KEY + 1))()
        rm_res = wc._rstrtmgr.RmStartSession(ctypes.byref(s_test), 0, k_test)
        if rm_res == 0:
            wc._rstrtmgr.RmEndSession(s_test)
            identity = wc.find_process_by_lock_path(str(lock_file), allowed_images=(py_exe,))
            assert identity == (real_child_pid, real_start_time)
            assert wc.find_process_by_lock_path(str(lock_file), allowed_images=("agy.exe",)) is None

            probe_py = AgyLockLivenessProbe(presence_dir=tmp_path, allowed_images=(py_exe,))
            probe_agy = AgyLockLivenessProbe(presence_dir=tmp_path)
            assert probe_py.is_alive(cid) is True
            assert probe_agy.is_alive(cid) is False

        # Start time mismatch (+1 tick = 100ns difference) must fail-closed with pid_reused_start_time_mismatch
        ok, reason, _ = wc.inject_console_input_verified(
            real_child_pid,
            real_start_time + 1,
            "wake",
            allowed_images=(py_exe,),
        )
        assert ok is False
        assert reason == "pid_reused_start_time_mismatch"

        # Image mismatch on live process with exact start_time must fail-closed with image_name_mismatch
        ok_img, reason_img, _ = wc.inject_console_input_verified(
            real_child_pid,
            real_start_time,
            "wake",
            allowed_images=("agy.exe",),
        )
        assert ok_img is False
        assert reason_img == "image_name_mismatch"
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # Wait briefly for inner child (real_child_pid) to finish exiting if spawned via venv launcher
    for _ in range(20):
        h_chk = wc._kernel32.OpenProcess(wc.SYNCHRONIZE, False, real_child_pid)
        if not wc._is_valid_handle(h_chk):
            break
        wait_st = wc._kernel32.WaitForSingleObject(h_chk, 50)
        wc._kernel32.CloseHandle(h_chk)
        if wait_st != wc.WAIT_TIMEOUT:
            break

    # Once child has exited, even with its stale PID/start_time, injection must fail-closed
    ok_dead, reason_dead, _ = wc.inject_console_input_verified(
        real_child_pid,
        real_start_time,
        "wake",
        allowed_images=(py_exe,),
    )
    assert ok_dead is False
    assert reason_dead in ("open_process_failed", "process_already_exited", "pid_reused_start_time_mismatch")


# ============================================================================
# 5. ResidentWatcher Audit Log Structured Wake Reason Propagation
# ============================================================================

def test_watcher_audit_records_pid_reuse_wake_reason(tmp_path: Path, monkeypatch):
    """Verify ResidentWatcher records wake_reason='pid_reused_start_time_mismatch' in audit.jsonl."""
    mailbox = Mailbox(root_dir=tmp_path / "brain")
    target_cid = str(uuid.uuid4())
    _, marker_path = mailbox.post(
        recipient_id=target_cid,
        content="hello",
        sender="orchestrator",
    )

    adapter = AgyDeliveryAdapter(presence_dir=tmp_path / "presence")
    monkeypatch.setattr(adapter, "_find_process_identity", lambda cid: (4242, 100000))

    def fake_verified(pid, exp_st, text, send_enter, delay, *, allowed_images=None):
        return (False, "pid_reused_start_time_mismatch", 0)

    import agentknock.adapters.agy as agy_mod
    monkeypatch.setattr(agy_mod._win_console, "inject_console_input_verified", fake_verified)

    audit_path = tmp_path / "audit.jsonl"
    watcher = ResidentWatcher(
        adapter=adapter,
        mailbox=mailbox,
        audit_log_path=audit_path,
        max_attempts=2,
        base_backoff_sec=0.0,
    )

    assert watcher.on_undelivered(marker_path) is False
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    retry_rec = next(r for r in records if r["event"] == "RETRY")
    dl_rec = next(r for r in records if r["event"] == "DEAD_LETTER")
    assert retry_rec["wake_reason"] == "pid_reused_start_time_mismatch"
    assert dl_rec["wake_reason"] == "pid_reused_start_time_mismatch"


# ============================================================================
# 6. Shared Local Test Doubles for TS-1 .. TS-4 Edge & Failure Coverage
# ============================================================================

class _ConfigurableKernel32:
    """Configurable Win32 kernel32 mock recording API call sequence & error codes."""

    def __init__(
        self,
        *,
        open_proc_ret: int = 7001,
        open_proc_err: int = 0,
        wait_returns: list[int] | None = None,
        get_times_ok: int = 1,
        get_times_err: int = 0,
        creation_ft: int = (0x01DC0000 << 32) | 0x12345678,
        query_image_ok: int = 1,
        query_image_err: int = 0,
        image_path: str = r"C:\Program Files\Antigravity\agy.exe",
        attach_fail_on_target: bool = False,
        attach_err: int = 0,
        conin_ret: int = 8001,
        conin_err: int = 0,
        write_fail_on_call: int | None = None,
        write_err: int = 0,
    ) -> None:
        self.events: list[str] = []
        self.open_proc_ret = open_proc_ret
        self.open_proc_err = open_proc_err
        self.wait_returns = list(wait_returns) if wait_returns is not None else [wc.WAIT_TIMEOUT]
        self.get_times_ok = get_times_ok
        self.get_times_err = get_times_err
        self.creation_ft = creation_ft
        self.query_image_ok = query_image_ok
        self.query_image_err = query_image_err
        self.image_path = image_path
        self.attach_fail_on_target = attach_fail_on_target
        self.attach_err = attach_err
        self.conin_ret = conin_ret
        self.conin_err = conin_err
        self.write_fail_on_call = write_fail_on_call
        self.write_err = write_err
        self.write_calls = 0

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        self.events.append(f"OpenProcess:{pid}")
        if not wc._is_valid_handle(self.open_proc_ret):
            ctypes.set_last_error(self.open_proc_err)
        return self.open_proc_ret

    def WaitForSingleObject(self, h: int, timeout: int) -> int:
        self.events.append(f"WaitForSingleObject:{h}")
        if len(self.wait_returns) > 1:
            return self.wait_returns.pop(0)
        return self.wait_returns[0]

    def GetProcessTimes(self, h: int, c_ptr, e_ptr, k_ptr, u_ptr) -> int:
        self.events.append(f"GetProcessTimes:{h}")
        if not self.get_times_ok:
            ctypes.set_last_error(self.get_times_err)
            return 0
        c_ft = ctypes.cast(c_ptr, ctypes.POINTER(wc.FILETIME)).contents
        c_ft.dwHighDateTime = (self.creation_ft >> 32) & 0xFFFFFFFF
        c_ft.dwLowDateTime = self.creation_ft & 0xFFFFFFFF
        return 1

    def QueryFullProcessImageNameW(self, h: int, flags: int, buf, size_ptr) -> int:
        self.events.append(f"QueryFullProcessImageNameW:{h}")
        if not self.query_image_ok:
            ctypes.set_last_error(self.query_image_err)
            return 0
        for idx, ch in enumerate(self.image_path):
            buf[idx] = ch
        buf[len(self.image_path)] = "\0"
        ctypes.cast(size_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = len(
            self.image_path
        )
        return 1

    def FreeConsole(self) -> int:
        self.events.append("FreeConsole")
        return 1

    def AttachConsole(self, pid: int) -> int:
        if pid == wc.ATTACH_PARENT_PROCESS:
            self.events.append("AttachConsole:PARENT")
            return 1
        self.events.append(f"AttachConsole:{pid}")
        if self.attach_fail_on_target:
            ctypes.set_last_error(self.attach_err)
            return 0
        return 1

    def CreateFileW(self, name: str, access: int, share: int, sec, disp: int, flags: int, tmpl) -> int:
        self.events.append(f"CreateFileW:{name}")
        if not wc._is_valid_handle(self.conin_ret):
            ctypes.set_last_error(self.conin_err)
        return self.conin_ret

    def WriteConsoleInputW(self, h: int, recs, length: int, written_ptr) -> int:
        self.write_calls += 1
        self.events.append(f"WriteConsoleInputW:{length}")
        if self.write_fail_on_call is not None and self.write_calls == self.write_fail_on_call:
            ctypes.set_last_error(self.write_err)
            return 0
        return 1

    def CloseHandle(self, h: int) -> int:
        self.events.append(f"CloseHandle:{h}")
        return 1


class _FakeUser32:
    def MapVirtualKeyW(self, vk_code: int, map_type: int) -> int:
        return 0x1C


# ============================================================================
# 7. TS-1 Complete Coverage (TC-1-01, TC-1-03 .. TC-1-07)
# ============================================================================

def test_inject_fail_closed_rejects_bool_and_invalid_pid_without_win32_calls(monkeypatch):
    """TC-1-01: Invalid/bool target_pid or expected_start_time must fail-closed with zero Win32 calls."""
    fake_k32 = _ConfigurableKernel32()
    fake_u32 = _FakeUser32()
    monkeypatch.setattr(wc, "_kernel32", fake_k32)
    monkeypatch.setattr(wc, "_user32", fake_u32)

    valid_ft = (0x01DC0000 << 32) | 0x12345678
    bad_start_times = [None, 0, -1, True, False, "123"]
    bad_pids = [0, -1, True, False]

    adapter = AgyDeliveryAdapter()
    legacy = legacy_adp._AgyAdapter()

    for bad_st in bad_start_times:
        assert wc.inject_console_input_verified(4242, bad_st, "wake") == (  # type: ignore[arg-type]
            False,
            "invalid_pid_or_start_time",
            0,
        )
        assert wc.inject_console_input(4242, "wake", expected_start_time=bad_st) is False  # type: ignore[arg-type]
        assert adapter._inject(4242, "wake", expected_start_time=bad_st) is False  # type: ignore[arg-type]
        assert adapter.last_wake_detail is not None
        assert adapter.last_wake_detail["reason"] == "invalid_pid_or_start_time"
        assert legacy._inject(4242, "wake", expected_start_time=bad_st) is False  # type: ignore[arg-type]

    for bad_pid in bad_pids:
        assert wc.inject_console_input_verified(bad_pid, valid_ft, "wake") == (  # type: ignore[arg-type]
            False,
            "invalid_pid_or_start_time",
            0,
        )
        assert wc.inject_console_input(bad_pid, "wake", expected_start_time=valid_ft) is False  # type: ignore[arg-type]
        assert adapter._inject(bad_pid, "wake", expected_start_time=valid_ft) is False  # type: ignore[arg-type]
        assert adapter.last_wake_detail is not None
        assert adapter.last_wake_detail["reason"] == "invalid_pid_or_start_time"
        assert legacy._inject(bad_pid, "wake", expected_start_time=valid_ft) is False  # type: ignore[arg-type]

    assert fake_k32.events == []


@pytest.mark.parametrize(
    ("bad_handle", "err_code"),
    [
        (0, 5),
        (wc.INVALID_HANDLE_VALUE, 87),
    ],
)
def test_inject_verified_open_process_failed_stops_immediately(monkeypatch, bad_handle, err_code):
    """TC-1-03: Failed OpenProcess returns open_process_failed and stops before Wait/Console APIs."""
    fake_k32 = _ConfigurableKernel32(open_proc_ret=bad_handle, open_proc_err=err_code)
    monkeypatch.setattr(wc, "_kernel32", fake_k32)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    try:
        ok, reason, err = wc.inject_console_input_verified(
            4242,
            (0x01DC0000 << 32) | 0x12345678,
            "wake",
            submit_delay_sec=0.0,
        )
        assert (ok, reason, err) == (False, "open_process_failed", err_code)
        assert fake_k32.events == ["OpenProcess:4242"]
    finally:
        ctypes.set_last_error(0)


def test_inject_verified_detects_process_already_exited_at_step2_and_step5(monkeypatch):
    """TC-1-04: Detect exited process at Step 2 and Step 5 (right after CONIN$ open) with full cleanup."""
    valid_ft = (0x01DC0000 << 32) | 0x12345678

    # Step 2: initial WaitForSingleObject != WAIT_TIMEOUT
    k32_step2 = _ConfigurableKernel32(wait_returns=[wc.WAIT_OBJECT_0], creation_ft=valid_ft)
    monkeypatch.setattr(wc, "_kernel32", k32_step2)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok2, reason2, err2 = wc.inject_console_input_verified(4242, valid_ft, "wake", submit_delay_sec=0.0)
    assert (ok2, reason2, err2) == (False, "process_already_exited", 0)
    assert k32_step2.events == [
        "OpenProcess:4242",
        "WaitForSingleObject:7001",
        "CloseHandle:7001",
    ]

    # Step 5: Step 2 passes (WAIT_TIMEOUT), process exits just before WriteConsoleInputW
    k32_step5 = _ConfigurableKernel32(
        wait_returns=[wc.WAIT_TIMEOUT, wc.WAIT_OBJECT_0],
        creation_ft=valid_ft,
    )
    monkeypatch.setattr(wc, "_kernel32", k32_step5)

    ok5, reason5, err5 = wc.inject_console_input_verified(
        4242,
        valid_ft,
        "wake",
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok5, reason5, err5) == (False, "process_already_exited", 0)
    assert k32_step5.events == [
        "OpenProcess:4242",
        "WaitForSingleObject:7001",
        "GetProcessTimes:7001",
        "QueryFullProcessImageNameW:7001",
        "FreeConsole",
        "AttachConsole:4242",
        "CreateFileW:CONIN$",
        "WaitForSingleObject:7001",
        "CloseHandle:8001",
        "FreeConsole",
        "AttachConsole:PARENT",
        "CloseHandle:7001",
    ]


def test_inject_verified_get_process_times_failed_and_zero_start_time(monkeypatch):
    """TC-1-05: GetProcessTimes failure or zero creation FILETIME fails closed before console attach."""
    valid_ft = (0x01DC0000 << 32) | 0x12345678

    k32_fail = _ConfigurableKernel32(get_times_ok=0, get_times_err=6)
    monkeypatch.setattr(wc, "_kernel32", k32_fail)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())
    try:
        assert wc.inject_console_input_verified(4242, valid_ft, "wake") == (
            False,
            "get_process_times_failed",
            6,
        )
        assert k32_fail.events == [
            "OpenProcess:4242",
            "WaitForSingleObject:7001",
            "GetProcessTimes:7001",
            "CloseHandle:7001",
        ]
    finally:
        ctypes.set_last_error(0)

    k32_zero = _ConfigurableKernel32(creation_ft=0)
    monkeypatch.setattr(wc, "_kernel32", k32_zero)
    assert wc.inject_console_input_verified(4242, valid_ft, "wake") == (
        False,
        "pid_reused_start_time_mismatch",
        0,
    )
    assert k32_zero.events == [
        "OpenProcess:4242",
        "WaitForSingleObject:7001",
        "GetProcessTimes:7001",
        "CloseHandle:7001",
    ]


def test_inject_verified_image_allowlist_validation(monkeypatch):
    """TC-1-06: Verify QueryFullProcessImageNameW failures, image mismatch, and empty allowlist."""
    valid_ft = (0x01DC0000 << 32) | 0x12345678
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    # 1) QueryFullProcessImageNameW fails -> image_query_failed
    k32_qfail = _ConfigurableKernel32(creation_ft=valid_ft, query_image_ok=0, query_image_err=122)
    monkeypatch.setattr(wc, "_kernel32", k32_qfail)
    try:
        assert wc.inject_console_input_verified(
            4242,
            valid_ft,
            "wake",
            allowed_images=("agy.exe",),
        ) == (False, "image_query_failed", 122)
        assert k32_qfail.events == [
            "OpenProcess:4242",
            "WaitForSingleObject:7001",
            "GetProcessTimes:7001",
            "QueryFullProcessImageNameW:7001",
            "CloseHandle:7001",
        ]
    finally:
        ctypes.set_last_error(0)

    # 2) Image mismatch (cmd.exe vs agy.exe) -> image_name_mismatch
    k32_mismatch = _ConfigurableKernel32(
        creation_ft=valid_ft,
        image_path=r"C:\Windows\System32\cmd.exe",
    )
    monkeypatch.setattr(wc, "_kernel32", k32_mismatch)
    assert wc.inject_console_input_verified(
        4242,
        valid_ft,
        "wake",
        allowed_images=("agy.exe",),
    ) == (False, "image_name_mismatch", 0)
    assert k32_mismatch.events == [
        "OpenProcess:4242",
        "WaitForSingleObject:7001",
        "GetProcessTimes:7001",
        "QueryFullProcessImageNameW:7001",
        "CloseHandle:7001",
    ]

    # 3) Empty allowlist allowed_images=() -> immediate reject before OpenProcess
    k32_empty = _ConfigurableKernel32(creation_ft=valid_ft)
    monkeypatch.setattr(wc, "_kernel32", k32_empty)
    assert wc.inject_console_input_verified(
        4242,
        valid_ft,
        "wake",
        allowed_images=(),
    ) == (False, "image_name_mismatch", 0)
    assert k32_empty.events == []


def test_inject_verified_console_failures_restore_parent_and_close_handles(monkeypatch):
    """TC-1-07: AttachConsole / CreateFileW(CONIN$) / WriteConsoleInputW (text & Enter) restore parent & close handles."""
    valid_ft = (0x01DC0000 << 32) | 0x12345678
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    # 1) AttachConsole(target_pid) fails
    k32_attach = _ConfigurableKernel32(
        creation_ft=valid_ft,
        attach_fail_on_target=True,
        attach_err=31,
    )
    monkeypatch.setattr(wc, "_kernel32", k32_attach)
    try:
        assert wc.inject_console_input_verified(4242, valid_ft, "hi", submit_delay_sec=0.0) == (
            False,
            "attach_console_failed",
            31,
        )
        assert k32_attach.events == [
            "OpenProcess:4242",
            "WaitForSingleObject:7001",
            "GetProcessTimes:7001",
            "FreeConsole",
            "AttachConsole:4242",
            "AttachConsole:PARENT",
            "CloseHandle:7001",
        ]
    finally:
        ctypes.set_last_error(0)

    # 2) CreateFileW("CONIN$") returns INVALID_HANDLE_VALUE
    k32_conin = _ConfigurableKernel32(
        creation_ft=valid_ft,
        conin_ret=wc.INVALID_HANDLE_VALUE,
        conin_err=2,
    )
    monkeypatch.setattr(wc, "_kernel32", k32_conin)
    try:
        assert wc.inject_console_input_verified(4242, valid_ft, "hi", submit_delay_sec=0.0) == (
            False,
            "conin_open_failed",
            2,
        )
        assert k32_conin.events == [
            "OpenProcess:4242",
            "WaitForSingleObject:7001",
            "GetProcessTimes:7001",
            "FreeConsole",
            "AttachConsole:4242",
            "CreateFileW:CONIN$",
            "FreeConsole",
            "AttachConsole:PARENT",
            "CloseHandle:7001",
        ]
    finally:
        ctypes.set_last_error(0)

    # 3) WriteConsoleInputW fails on text batch (call #1)
    k32_w1 = _ConfigurableKernel32(creation_ft=valid_ft, write_fail_on_call=1, write_err=995)
    monkeypatch.setattr(wc, "_kernel32", k32_w1)
    try:
        assert wc.inject_console_input_verified(
            4242,
            valid_ft,
            "hi",
            send_enter=True,
            submit_delay_sec=0.0,
        ) == (False, "write_console_input_failed", 995)
        assert k32_w1.events == [
            "OpenProcess:4242",
            "WaitForSingleObject:7001",
            "GetProcessTimes:7001",
            "FreeConsole",
            "AttachConsole:4242",
            "CreateFileW:CONIN$",
            "WaitForSingleObject:7001",
            "WriteConsoleInputW:4",
            "CloseHandle:8001",
            "FreeConsole",
            "AttachConsole:PARENT",
            "CloseHandle:7001",
        ]
    finally:
        ctypes.set_last_error(0)

    # 4) WriteConsoleInputW fails on Enter key batch (call #2)
    k32_w2 = _ConfigurableKernel32(creation_ft=valid_ft, write_fail_on_call=2, write_err=996)
    monkeypatch.setattr(wc, "_kernel32", k32_w2)
    try:
        assert wc.inject_console_input_verified(
            4242,
            valid_ft,
            "hi",
            send_enter=True,
            submit_delay_sec=0.0,
        ) == (False, "write_console_input_failed", 996)
        assert k32_w2.events == [
            "OpenProcess:4242",
            "WaitForSingleObject:7001",
            "GetProcessTimes:7001",
            "FreeConsole",
            "AttachConsole:4242",
            "CreateFileW:CONIN$",
            "WaitForSingleObject:7001",
            "WriteConsoleInputW:4",
            "WriteConsoleInputW:2",
            "CloseHandle:8001",
            "FreeConsole",
            "AttachConsole:PARENT",
            "CloseHandle:7001",
        ]
    finally:
        ctypes.set_last_error(0)


# ============================================================================
# 8. TS-2 Complete Coverage (TC-2-01 .. TC-2-04)
# ============================================================================

def test_find_process_rm_session_and_register_failures(monkeypatch):
    """TC-2-01: RmStartSession or RmRegisterResources failure returns None and always closes started session."""

    class RmStartFail:
        def __init__(self):
            self.end_calls = 0

        def RmStartSession(self, handle_ptr, flags, key):
            return 5

        def RmEndSession(self, session):
            self.end_calls += 1
            return 0

    class RmRegFail:
        def __init__(self):
            self.ended_sessions: list[int] = []

        def RmStartSession(self, handle_ptr, flags, key):
            ctypes.cast(handle_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 55
            return 0

        def RmRegisterResources(self, session, nfiles, files, napps, apps, nsvcs, svcs):
            return 87

        def RmEndSession(self, session):
            self.ended_sessions.append(int(getattr(session, "value", session)))
            return 0

    k32 = _ConfigurableKernel32()
    k32.GetCurrentProcessId = lambda: 999  # type: ignore[method-assign]
    k32.ProcessIdToSessionId = lambda pid, ptr: (  # type: ignore[method-assign]
        setattr(ctypes.cast(ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents, "value", 1) or 1
    )
    monkeypatch.setattr(wc, "_kernel32", k32)

    rm1 = RmStartFail()
    monkeypatch.setattr(wc, "_rstrtmgr", rm1)
    assert wc.find_process_by_lock_path("C:\\dummy.lock") is None
    assert rm1.end_calls == 0

    rm2 = RmRegFail()
    monkeypatch.setattr(wc, "_rstrtmgr", rm2)
    assert wc.find_process_by_lock_path("C:\\dummy.lock") is None
    assert rm2.ended_sessions == [55]


def test_find_process_rm_error_more_data_retry_limit_and_max_procs_guard(monkeypatch):
    """TC-2-02: Fail-closed when ERROR_MORE_DATA persists past MAX_RM_RETRIES or needed > MAX_RM_PROCS."""

    class RmBoundedFail:
        def __init__(self, needed_seq: list[int]):
            self.needed_seq = needed_seq
            self.list_calls = 0
            self.ended_sessions: list[int] = []

        def RmStartSession(self, handle_ptr, flags, key):
            ctypes.cast(handle_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 77
            return 0

        def RmRegisterResources(self, session, nfiles, files, napps, apps, nsvcs, svcs):
            return 0

        def RmGetList(self, session, needed_ptr, count_ptr, info_ptr, reason_ptr):
            idx = min(self.list_calls, len(self.needed_seq) - 1)
            self.list_calls += 1
            ctypes.cast(needed_ptr, ctypes.POINTER(ctypes.wintypes.UINT)).contents.value = (
                self.needed_seq[idx]
            )
            return wc.ERROR_MORE_DATA

        def RmEndSession(self, session):
            self.ended_sessions.append(int(getattr(session, "value", session)))
            return 0

    k32 = _ConfigurableKernel32()
    k32.GetCurrentProcessId = lambda: 999  # type: ignore[method-assign]
    k32.ProcessIdToSessionId = lambda pid, ptr: (  # type: ignore[method-assign]
        setattr(ctypes.cast(ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents, "value", 1) or 1
    )
    monkeypatch.setattr(wc, "_kernel32", k32)

    # 1) 3 retries all return ERROR_MORE_DATA -> stops after 1 initial + 3 retries and ends session
    rm_retry_exhaust = RmBoundedFail([1, 2, 3, 4])
    monkeypatch.setattr(wc, "_rstrtmgr", rm_retry_exhaust)
    assert wc.find_process_by_lock_path("C:\\dummy.lock") is None
    assert rm_retry_exhaust.list_calls == 1 + wc.MAX_RM_RETRIES
    assert rm_retry_exhaust.ended_sessions == [77]

    # 2) needed > MAX_RM_PROCS (256) -> refuses buffer allocation immediately and ends session
    rm_overflow = RmBoundedFail([wc.MAX_RM_PROCS + 1])
    monkeypatch.setattr(wc, "_rstrtmgr", rm_overflow)
    assert wc.find_process_by_lock_path("C:\\dummy.lock") is None
    assert rm_overflow.list_calls == 1
    assert rm_overflow.ended_sessions == [77]


def test_find_process_skips_dead_and_mismatched_and_deduplicates_same_pid(monkeypatch):
    """TC-2-03 & TC-2-04: Skip dead / mismatched-time / wrong-session / wrong-image, deduplicate identical PID."""
    ft_dead = (0x01DC0002 << 32) | 0x00000001
    ft_reused_rm = (0x01DC0002 << 32) | 0x00000002
    ft_reused_actual = (0x01DC0002 << 32) | 0x00000099
    ft_wrong_sess = (0x01DC0002 << 32) | 0x00000003
    ft_wrong_img = (0x01DC0002 << 32) | 0x00000004
    ft_valid = (0x01DC0002 << 32) | 0x00000005

    rm_entries = [
        (2001, ft_wrong_sess, 2),   # different TS session
        (2002, ft_dead, 1),         # dead / exited process
        (2003, ft_reused_rm, 1),    # start time mismatch
        (2004, ft_wrong_img, 1),    # disallowed image
        (2005, ft_valid, 1),        # eligible agy.exe #1
        (2005, ft_valid, 1),        # duplicate entry for same PID 2005
    ]

    class FakeRmDup:
        def __init__(self):
            self.ended_sessions: list[int] = []

        def RmStartSession(self, handle_ptr, flags, key):
            ctypes.cast(handle_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 88
            return 0

        def RmRegisterResources(self, session, nfiles, files, napps, apps, nsvcs, svcs):
            return 0

        def RmGetList(self, session, needed_ptr, count_ptr, info_ptr, reason_ptr):
            ctypes.cast(needed_ptr, ctypes.POINTER(ctypes.wintypes.UINT)).contents.value = len(rm_entries)
            if info_ptr is None:
                return wc.ERROR_MORE_DATA
            ctypes.cast(count_ptr, ctypes.POINTER(ctypes.wintypes.UINT)).contents.value = len(rm_entries)
            for idx, (pid, ft, sess) in enumerate(rm_entries):
                info_ptr[idx].Process.dwProcessId = pid
                info_ptr[idx].Process.ProcessStartTime.dwHighDateTime = (ft >> 32) & 0xFFFFFFFF
                info_ptr[idx].Process.ProcessStartTime.dwLowDateTime = ft & 0xFFFFFFFF
                info_ptr[idx].TSSessionId = sess
            return 0

        def RmEndSession(self, session):
            self.ended_sessions.append(int(getattr(session, "value", session)))
            return 0

    proc_details = {
        2002: (wc.WAIT_OBJECT_0, ft_dead, r"C:\App\agy.exe"),
        2003: (wc.WAIT_TIMEOUT, ft_reused_actual, r"C:\App\agy.exe"),
        2004: (wc.WAIT_TIMEOUT, ft_wrong_img, r"C:\Windows\explorer.exe"),
        2005: (wc.WAIT_TIMEOUT, ft_valid, r"C:\App\agy.exe"),
    }

    class FakeK32Multi:
        def __init__(self):
            self.open_handles: set[int] = set()

        def GetCurrentProcessId(self):
            return 999

        def ProcessIdToSessionId(self, pid, sess_ptr):
            ctypes.cast(sess_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 1
            return 1

        def OpenProcess(self, access, inherit, pid):
            if pid in proc_details:
                self.open_handles.add(pid)
                return pid
            return 0

        def WaitForSingleObject(self, h, timeout):
            return proc_details[h][0]

        def GetProcessTimes(self, h, c_ptr, e_ptr, k_ptr, u_ptr):
            ft = proc_details[h][1]
            c_ft = ctypes.cast(c_ptr, ctypes.POINTER(wc.FILETIME)).contents
            c_ft.dwHighDateTime = (ft >> 32) & 0xFFFFFFFF
            c_ft.dwLowDateTime = ft & 0xFFFFFFFF
            return 1

        def QueryFullProcessImageNameW(self, h, flags, buf, size_ptr):
            img = proc_details[h][2]
            for idx, ch in enumerate(img):
                buf[idx] = ch
            buf[len(img)] = "\0"
            ctypes.cast(size_ptr, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = len(img)
            return 1

        def CloseHandle(self, h):
            self.open_handles.discard(h)
            return 1

    fake_rm = FakeRmDup()
    fake_k32 = FakeK32Multi()
    monkeypatch.setattr(wc, "_rstrtmgr", fake_rm)
    monkeypatch.setattr(wc, "_kernel32", fake_k32)

    assert wc.find_process_by_lock_path("C:\\dummy.lock", allowed_images=("agy.exe",)) == (
        2005,
        ft_valid,
    )
    assert fake_k32.open_handles == set()
    assert fake_rm.ended_sessions == [88]


# ============================================================================
# 9. TS-3 Complete Coverage (TC-3-01 .. TC-3-04) & TS-4 (TC-4-01)
# ============================================================================

def test_agy_liveness_probe_fail_closed_on_invalid_inputs_and_bad_identities(tmp_path: Path, monkeypatch):
    """TC-3-01: Malformed UUID, missing lock file, None, or bad tuples must all fail-closed as False."""
    probe = AgyLockLivenessProbe(presence_dir=tmp_path)

    assert probe.is_alive("not-a-uuid") is False
    assert probe.is_alive("") is False

    cid = str(uuid.uuid4())
    assert probe.is_alive(cid) is False  # .lock missing

    lock_file = tmp_path / f"{cid}.lock"
    lock_file.write_text("locked", encoding="utf-8")

    class StubWinConsole:
        def __init__(self, ret_val):
            self.ret_val = ret_val

        def find_process_by_lock_path(self, path, allowed_images=None):
            return self.ret_val

    bad_identities = [
        None,
        (0, 100),
        (100, 0),
        (-1, 100),
        (True, 100),
        (100, True),
        ("100", 200),
        (100,),
        (100, 200, 300),
    ]
    for bad in bad_identities:
        monkeypatch.setattr(agy_mod, "_win_console", StubWinConsole(bad))
        assert probe.is_alive(cid) is False, f"Expected False for bad identity {bad!r}"


def test_win_console_ctypes_packaging_and_no_root_stub():
    """TC-3-02: Verify root _win_console.py is gone, pyximport is unused, and fallback resolves to ctypes."""
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / "_win_console.py").exists()

    for rel in ("agentknock/adapters/agy.py", "agentknock/_adapter.py"):
        src = (repo_root / rel).read_text(encoding="utf-8")
        assert "pyximport" not in src
        assert "_win_console_ctypes" in src

    assert agy_mod._win_console is not None
    assert getattr(agy_mod._win_console, "_BACKEND", None) in ("ctypes", "cython")
    assert legacy_adp._win_console is not None
    assert getattr(legacy_adp._win_console, "_BACKEND", None) in ("ctypes", "cython")

    # Isolated subprocess test simulating missing _win_console Cython extension -> pure ctypes fallback
    probe_code = (
        "import sys\n"
        "sys.modules['_win_console'] = None\n"
        "import agentknock.adapters.agy as agy_mod\n"
        "import agentknock._adapter as legacy_adp\n"
        "assert agy_mod._win_console._BACKEND == 'ctypes'\n"
        "assert legacy_adp._win_console._BACKEND == 'ctypes'\n"
        "assert 'pyximport' not in sys.modules\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Subprocess fallback check failed: {proc.stderr}"


def test_legacy_run_doctor_reports_backend_and_toctou_hardened(tmp_path: Path):
    """TC-3-03: _run_doctor() outputs win_console_backend and toctou_hardened=True."""
    adapter = legacy_adp._AgyAdapter(
        base_dir=tmp_path,
        brain_dir=tmp_path / "brain",
        presence_dir=tmp_path / "presence",
        registry_dir=tmp_path / "registry",
    )
    report = legacy_adp._run_doctor(adapter=adapter)
    assert report["status"] == "ok"
    assert report["native_extension_loaded"] is True
    assert report["win_console_backend"] in ("ctypes", "cython")
    assert report["toctou_hardened"] is True


def test_watcher_audit_and_last_wake_detail_never_leak_content_or_nudge(tmp_path: Path, monkeypatch):
    """TC-3-04: audit.jsonl and last_wake_detail record structured wake_reason/win32_error without leaking secrets."""
    secret_body = "__SECRET_CONTENT_SHOULD_NEVER_LEAK_991__"
    secret_title = "__SECRET_TITLE_SHOULD_NEVER_LEAK_882__"

    mailbox = Mailbox(root_dir=tmp_path / "brain")
    target_cid = str(uuid.uuid4())
    _, marker_path = mailbox.post(
        recipient_id=target_cid,
        content=secret_body,
        sender="orchestrator",
        title=secret_title,
    )

    presence_dir = tmp_path / "presence"
    presence_dir.mkdir(parents=True, exist_ok=True)
    (presence_dir / f"{target_cid}.lock").write_text("lock", encoding="utf-8")

    adapter = AgyDeliveryAdapter(presence_dir=presence_dir, wake_locale="en", output_locale="ja")
    monkeypatch.setattr(adapter, "_find_process_identity", lambda cid: (4242, 100000))

    def fake_verified(pid, exp_st, text, send_enter, delay, *, allowed_images=None):
        return (False, "open_process_failed", 5)

    monkeypatch.setattr(agy_mod._win_console, "inject_console_input_verified", fake_verified)

    audit_path = tmp_path / "audit.jsonl"
    watcher = ResidentWatcher(
        adapter=adapter,
        mailbox=mailbox,
        audit_log_path=audit_path,
        max_attempts=2,
        base_backoff_sec=0.0,
    )

    assert watcher.on_undelivered(marker_path) is False

    audit_raw = audit_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in audit_raw.splitlines()]
    retry_rec = next(r for r in records if r["event"] == "RETRY")
    dl_rec = next(r for r in records if r["event"] == "DEAD_LETTER")
    assert retry_rec["wake_reason"] == "open_process_failed"
    assert retry_rec["win32_error"] == 5
    assert dl_rec["wake_reason"] == "open_process_failed"
    assert dl_rec["win32_error"] == 5

    assert adapter.last_wake_detail is not None
    assert adapter.last_wake_detail["reason"] == "open_process_failed"
    assert adapter.last_wake_detail["win32_error"] == 5

    detail_serialized = json.dumps(adapter.last_wake_detail, ensure_ascii=False)
    forbidden_strings = (
        [secret_body, secret_title]
        + list(WAKE_NUDGE_CATALOG.values())
        + list(WAKE_BODY_CATALOG.values())
        + list(OUTPUT_DIRECTIVE_CATALOG.values())
    )
    for forbidden in forbidden_strings:
        assert forbidden not in audit_raw
        assert forbidden not in detail_serialized


def test_r2_forbidden_symbols_include_v031_toctou_symbols():
    """TC-4-01: testing.DEFAULT_FORBIDDEN_SYMBOLS includes all v0.3.1 TOCTOU symbols and send() passes."""
    required = {
        "_win_console_ctypes",
        "find_process_by_lock_path",
        "inject_console_input_verified",
        "_find_process_identity",
        "_find_agy_process_by_conversation_id",
        "OpenProcess",
        "GetProcessTimes",
    }
    missing = required - set(DEFAULT_FORBIDDEN_SYMBOLS)
    assert not missing, f"Missing R2 forbidden symbols: {sorted(missing)}"

    repo_root = Path(__file__).resolve().parent.parent
    assert_send_cannot_reach(repo_root / "console_injector.py", entry_func_name="send")
    assert_send_cannot_reach(repo_root / "agentknock" / "__init__.py", entry_func_name="send")
