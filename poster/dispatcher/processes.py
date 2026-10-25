"""Owned process groups on POSIX and kill-on-close Job Objects on Windows."""

import os
import signal
import subprocess
import sys
import time

import psutil

from .models import DispatcherError
from .launcher import command, environment, process_options


def same_process(pid, birth):
    try:
        process = psutil.Process(pid)
        return process if process.create_time() == birth else None
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
        return None


def terminate_tree(pid, birth, *, group=False):
    process = same_process(pid, birth)
    if process:
        try:
            children = process.children(recursive=True)
        except psutil.Error:
            children = []
        targets = []
        for child in children:
            try:
                targets.append((child.pid, child.create_time()))
            except psutil.NoSuchProcess:
                pass
    else:
        targets = []
    signalled = []
    for child_pid, child_birth in reversed(targets):
        child = same_process(child_pid, child_birth)
        if child:
            try:
                child.kill()
                signalled.append(child)
            except psutil.NoSuchProcess:
                pass
    if group and os.name != "nt":
        # A surviving group keeps its original leader ID reserved. Do not signal
        # a new leader which has reused an old PID after that group disappeared.
        try:
            if process or not psutil.pid_exists(pid):
                os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process:
        try:
            process.kill()
            signalled.append(process)
        except psutil.NoSuchProcess:
            pass
    # Observe exit without waitpid: Popen owns and must reap its child's actual
    # exit code. A SIGKILL request alone is not proof that the process has exited.
    deadline = time.monotonic() + 3

    def alive(item):
        try:
            return item.is_running() and item.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    while signalled and time.monotonic() < deadline:
        signalled = [p for p in signalled if alive(p)]
        if signalled:
            time.sleep(0.025)
    if signalled:
        raise DispatcherError("PROCESS_CONTROL_FAILED")


def clean_descendants():
    children = psutil.Process().children(recursive=True)
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=1)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=1)
    return not [p for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]


def reap_detached(run_id, birth=0):
    """Linux Chromium creates its own session; killpg(worker) cannot own it.

    A launch-only run marker survives reparenting and is not graph-controlled.
    Restrict discovery to this UID, then recheck exact process birth before
    signalling. Never inspect/publish command lines or environment contents.
    Windows uses the existing kernel Job Object instead.
    """
    if sys.platform != "linux" or not run_id:
        return
    targets = []
    for process in psutil.process_iter():
        try:
            if process.pid == os.getpid() or process.uids().real != os.getuid():
                continue
            created = process.create_time()
            if created >= birth and process.environ().get("POSTER_DISPATCHER_RUN") == run_id:
                targets.append((process.pid, created))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for pid, created in reversed(targets):
        terminate_tree(pid, created)


class WindowsJob:
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", w.DWORD),
                ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t),
                ("active", w.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [("counter" + str(i), ctypes.c_uint64) for i in range(6)]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = self.kernel.OpenProcess(0x0101, False, pid)
        try:
            if (
                not self.handle
                or not process
                or not self.kernel.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
                )
                or not self.kernel.AssignProcessToJobObject(self.handle, process)
            ):
                self.close()
                raise DispatcherError("PROCESS_CONTROL_FAILED")
        finally:
            if process:
                self.kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WorkerProcess:
    def __init__(self, store, execution):
        self.execution = execution
        self.created = time.monotonic()
        self.process = self.job = None
        self.cancelled_at = self.reason = None
        self.started = False
        try:
            self.process = subprocess.Popen(
                command(
                    "worker",
                    "--db",
                    str(store.path),
                    "--run",
                    execution["run_id"],
                    "--owner",
                    execution["owner"],
                ),
                cwd=store.root,
                env={**environment(), "POSTER_DISPATCHER_RUN": execution["run_id"]},
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **process_options(),
            )
            self.birth = psutil.Process(self.process.pid).create_time()
            if os.name == "nt":
                self.job = WindowsJob(self.process.pid)
            if not store.record_process(
                execution["run_id"], execution["owner"], self.process.pid, self.birth
            ):
                raise DispatcherError("PROCESS_CONTROL_FAILED")
        except Exception:
            self.close(force=True)
            raise

    def permit(self):
        import json
        from ..auth import session_key

        # The key goes through an anonymous pipe, never argv or a plaintext file.
        self.process.stdin.write(json.dumps({"key": session_key().decode()}).encode() + b"\n")
        self.process.stdin.flush()
        self.started = True

    def request(self, reason):
        import time

        if self.cancelled_at is not None:
            return
        self.reason, self.cancelled_at = reason, time.monotonic()
        try:
            self.process.stdin.write(reason.encode() + b"\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            pass

    def close(self, *, force=False):
        if self.process:
            if force or self.process.poll() is None:
                terminate_tree(self.process.pid, getattr(self, "birth", None), group=True)
            if self.job:
                self.job.close()
            reap_detached(self.execution["run_id"], getattr(self, "birth", 0))
            if self.process.stdin:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise DispatcherError("PROCESS_CONTROL_FAILED") from None
