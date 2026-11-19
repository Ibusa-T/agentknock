#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Disposable live-verification harness for User-Typing Collision Guard (Phase 1-B).

Usage:
    python live_typing_guard_demo.py --target-pid <PID> [--send-enter] [--rounds N]
"""
from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentknock.adapters import _win_console_ctypes as wc


def resolve_process_start_time(target_pid: int) -> tuple[int, str | None]:
    """Verify `target_pid` is alive and return `(creation_time_u64, image_basename_lower)`."""
    if sys.platform != "win32" or wc._kernel32 is None:
        raise RuntimeError("Windows console backend is unavailable on this platform.")

    h_proc = wc._kernel32.OpenProcess(
        wc.PROCESS_QUERY_LIMITED_INFORMATION | wc.SYNCHRONIZE,
        False,
        target_pid,
    )
    if not wc._is_valid_handle(h_proc):
        err = ctypes.get_last_error()
        raise RuntimeError(f"OpenProcess(pid={target_pid}) failed (win32_error={err}).")

    try:
        if wc._kernel32.WaitForSingleObject(h_proc, 0) != wc.WAIT_TIMEOUT:
            raise RuntimeError(f"Target process pid={target_pid} has already exited.")

        c_ft = wc.FILETIME()
        e_ft = wc.FILETIME()
        k_ft = wc.FILETIME()
        u_ft = wc.FILETIME()
        if not wc._kernel32.GetProcessTimes(
            h_proc,
            ctypes.byref(c_ft),
            ctypes.byref(e_ft),
            ctypes.byref(k_ft),
            ctypes.byref(u_ft),
        ):
            err = ctypes.get_last_error()
            raise RuntimeError(f"GetProcessTimes(pid={target_pid}) failed (win32_error={err}).")

        start_time = wc._filetime_to_u64(c_ft)
        if start_time <= 0:
            raise RuntimeError(f"Invalid creation time ({start_time}) for pid={target_pid}.")

        image_name, _ = wc._query_image_basename_lower(h_proc)
        return start_time, image_name
    finally:
        wc._kernel32.CloseHandle(h_proc)


def format_verdict(ok: bool, reason: str, err: int) -> str:
    if reason == "deferred_user_typing":
        return "DEFERRED（ユーザー入力検知→注入せず・入力保全）"
    if ok and reason == "ok":
        return "INJECTED（ナッジ注入実行）"
    return f"OTHER（未注入/異常: reason={reason!r}, win32_error={err}）"


def run_countdown(seconds: int = 3) -> None:
    for count in range(seconds, 0, -1):
        print(f"  {count}...", flush=True)
        time.sleep(1.0)
    print("  NOW!", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Live verification demo for User-Typing Collision Guard."
    )
    parser.add_argument(
        "--target-pid",
        type=int,
        required=True,
        help="Target agy.exe PID to probe and wake.",
    )
    parser.add_argument(
        "--send-enter",
        action="store_true",
        default=False,
        help="Send Enter key after text injection (default: False for safety).",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=2,
        help="Number of demo rounds to run (default: 2).",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=3,
        help="Countdown seconds before each shot (default: 3).",
    )
    args = parser.parse_args()

    if args.target_pid <= 0:
        print("Error: --target-pid must be a positive integer.", file=sys.stderr)
        return 2
    if args.rounds <= 0:
        print("Error: --rounds must be a positive integer.", file=sys.stderr)
        return 2

    try:
        expected_start_time, image_name = resolve_process_start_time(args.target_pid)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("=== User-Typing Collision Guard Live Demo ===")
    print(f"Target PID        : {args.target_pid}")
    print(f"Target Image      : {image_name or '(unknown)'}")
    print(f"ProcessStartTime  : {expected_start_time}")
    print(f"Send Enter        : {args.send_enter}")
    print(f"Rounds            : {args.rounds}")
    print("=============================================", flush=True)

    for round_idx in range(1, args.rounds + 1):
        print(f"\n[Round {round_idx}/{args.rounds}] カウントダウン開始（打鍵中/アイドル状態を作ってください）", flush=True)
        run_countdown(args.countdown)

        ok, reason, err = wc.inject_console_input_verified(
            args.target_pid,
            expected_start_time,
            text="【LIVEテスト】typing-guard demo",
            send_enter=args.send_enter,
            respect_user_typing=True,
            allowed_images=("agy.exe",),
        )
        verdict = format_verdict(ok, reason, err)
        print(f"  Result : ok={ok}, reason={reason!r}, err={err}", flush=True)
        print(f"  Verdict: {verdict}", flush=True)

        if round_idx < args.rounds:
            try:
                input("\n次のラウンドへ進むには Enter を押してください（打鍵中/アイドル切替用）...")
            except EOFError:
                break

    print("\nデモ終了。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
