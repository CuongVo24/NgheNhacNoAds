"""Spike S5: Windows process and system behaviour the daemon depends on.

    job                  is this process in a job object, and may children break away?
    detach start|status|stop
                         start a detached heartbeat process, check it is alive, stop it
    sleep                SetThreadExecutionState: can we keep the PC awake while playing?
    lan                  LAN IPv4 addresses and the Windows network profile (Private/Public)

Manual test for "the daemon survives closing the terminal":
  1. In Windows Terminal: uv run --group spikes python spikes/s5_windows.py detach start
  2. Close that terminal tab.
  3. In a new tab:       uv run --group spikes python spikes/s5_windows.py detach status
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from _common import out_path, setup_console

HEARTBEAT = out_path("s5_heartbeat.json")
STOP_FILE = out_path("s5_heartbeat.stop")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040
SYSTEM_EXECUTION_STATE = 16  # POWER_INFORMATION_LEVEL for CallNtPowerInformation

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class IO_COUNTERS(ctypes.Structure):  # noqa: N801 - Win32 names
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def kernel32() -> Any:
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.GetCurrentProcess.restype = wintypes.HANDLE
    lib.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    lib.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    lib.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    lib.SetThreadExecutionState.restype = wintypes.DWORD
    return lib


def job_info() -> dict[str, Any]:
    k32 = kernel32()
    in_job = wintypes.BOOL()
    if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(in_job)):
        raise ctypes.WinError(ctypes.get_last_error())
    info: dict[str, Any] = {"in_job": bool(in_job.value)}
    if in_job.value:
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ok = k32.QueryInformationJobObject(
            None,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            None,
        )
        if ok:
            flags = limits.BasicLimitInformation.LimitFlags
            info.update(
                limit_flags=hex(flags),
                kill_on_job_close=bool(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
                breakaway_ok=bool(flags & JOB_OBJECT_LIMIT_BREAKAWAY_OK),
                silent_breakaway_ok=bool(flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK),
            )
        else:
            info["query_error"] = ctypes.get_last_error()
    return info


# ------------------------------------------------------------------ detaching


def pythonw() -> str:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


def detach_start() -> dict[str, Any]:
    STOP_FILE.unlink(missing_ok=True)
    HEARTBEAT.unlink(missing_ok=True)
    command = [pythonw(), str(Path(__file__).resolve()), "heartbeat"]
    base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    common: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(Path(__file__).resolve().parent),
    }
    try:
        proc = subprocess.Popen(
            command, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common
        )
        breakaway = True
    except OSError:  # the job this process is in does not allow breakaway
        proc = subprocess.Popen(command, creationflags=base, **common)
        breakaway = False
    deadline = time.monotonic() + 10
    while not HEARTBEAT.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    return {"launcher_pid": proc.pid, "breakaway_from_job": breakaway, **detach_status()}


def detach_status() -> dict[str, Any]:
    if not HEARTBEAT.exists():
        return {"alive": False, "reason": "no heartbeat file"}
    beat = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    age = time.time() - beat["now"]
    return {"alive": age < 3, "heartbeat_age_s": round(age, 1), **beat}


def detach_stop() -> dict[str, Any]:
    status = detach_status()
    STOP_FILE.touch()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and detach_status().get("alive"):
        time.sleep(0.3)
    if detach_status().get("alive") and status.get("pid"):
        with contextlib.suppress(OSError):
            os.kill(status["pid"], signal.SIGTERM)
    return {"stopped": not detach_status().get("alive")}


def heartbeat() -> None:
    started = time.time()
    info = job_info()
    while not STOP_FILE.exists() and time.time() - started < 3600:
        payload = {"pid": os.getpid(), "started": started, "now": time.time(), "job": info}
        HEARTBEAT.write_text(json.dumps(payload), encoding="utf-8")
        time.sleep(1)
    HEARTBEAT.unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)


# --------------------------------------------------------------------- sleep


def system_execution_state() -> int:
    powrprof = ctypes.WinDLL("powrprof")
    state = wintypes.ULONG()
    status = powrprof.CallNtPowerInformation(
        SYSTEM_EXECUTION_STATE, None, 0, ctypes.byref(state), ctypes.sizeof(state)
    )
    if status != 0:
        raise OSError(f"CallNtPowerInformation failed: 0x{status & 0xFFFFFFFF:08x}")
    return int(state.value)


def describe(flags: int) -> list[str]:
    names = {
        ES_SYSTEM_REQUIRED: "SYSTEM_REQUIRED",
        ES_DISPLAY_REQUIRED: "DISPLAY_REQUIRED",
        ES_AWAYMODE_REQUIRED: "AWAYMODE_REQUIRED",
    }
    return [name for bit, name in names.items() if flags & bit]


def sleep_test() -> dict[str, Any]:
    k32 = kernel32()
    before = system_execution_state()
    previous = k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    during = system_execution_state()
    k32.SetThreadExecutionState(ES_CONTINUOUS)
    after = system_execution_state()
    return {
        "call_succeeded": previous != 0,
        "before": describe(before),
        "while_playing": describe(during),
        "after_release": describe(after),
    }


# ----------------------------------------------------------------------- LAN


def lan() -> dict[str, Any]:
    addresses = {
        info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    }
    default_route = None
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
        default_route = s.getsockname()[0]
    profiles: Any = None
    with contextlib.suppress(Exception):
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetConnectionProfile | Select-Object Name,InterfaceAlias,NetworkCategory"
                " | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        profiles = json.loads(result.stdout) if result.stdout.strip() else None
    categories = {0: "Public", 1: "Private", 2: "DomainAuthenticated"}
    if isinstance(profiles, dict):
        profiles = [profiles]
    for profile in profiles or []:
        category = profile.get("NetworkCategory")
        profile["NetworkCategory"] = categories.get(category, category)
    return {
        "hostname": socket.gethostname(),
        "ipv4": sorted(addresses),
        "default_route_ip": default_route,
        "profiles": profiles,
    }


def main() -> None:
    setup_console()
    if sys.platform != "win32":
        raise SystemExit("S5 only applies to Windows.")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("job")
    detach = sub.add_parser("detach")
    detach.add_argument("action", choices=["start", "status", "stop"])
    sub.add_parser("sleep")
    sub.add_parser("lan")
    sub.add_parser("heartbeat", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.command == "heartbeat":
        heartbeat()
        return
    if args.command == "job":
        result = job_info()
    elif args.command == "detach":
        result = {"start": detach_start, "status": detach_status, "stop": detach_stop}[
            args.action
        ]()
    elif args.command == "sleep":
        result = sleep_test()
    else:
        result = lan()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
