"""Runs scripts for server.py in a separate process, so a crash cannot stop the server.

Stays alive between scripts, so imports such as arcpy stay loaded. Standard library only, so
FastMCP's dependencies are not imported next to arcpy. Started with: python -u -B -P worker.py
  -u  unbuffered, so output printed before a crash still reaches the capture file
  -B  no .pyc, which is validated by mtime in seconds and size, so a quick edit could run old code
  -P  this folder is not on sys.path, so it cannot shadow a script's imports

Reads one JSON job per line on stdin and writes one JSON result per line on stdout.
"""

import faulthandler
import importlib
import json
import os
import queue
import runpy
import signal
import subprocess
import sys
import threading
import traceback
from pathlib import Path


def is_user_module(file, roots):
    """True for code under the script or work folders, and not for installed packages."""
    path = Path(file).resolve()
    if any(path.is_relative_to(Path(p).resolve()) for p in (sys.prefix, sys.base_prefix)):
        return False  # a .venv inside workdir holds C extensions that cannot load twice
    return any(path.is_relative_to(root) for root in roots)


def run_script(script, args, workdir, artifact_dir):
    script = Path(script)
    saved = sys.argv, sys.path[:], os.getcwd(), set(sys.modules)
    sys.argv = [str(script), *args]
    sys.path.insert(0, str(script.parent))
    os.chdir(workdir)
    os.environ["RWEE_ARTIFACT_DIR"] = artifact_dir
    importlib.invalidate_caches()  # a helper module written just now must be importable
    exit_code = 0
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            exit_code = int(e.code or 0)
        else:
            print(e.code, file=sys.__stderr__)
            exit_code = 1
    except BaseException:
        traceback.print_exc(file=sys.__stderr__)
        exit_code = 1
    finally:
        # As Python does at exit. Otherwise a thread's output is lost, or lands in a later run.
        # Enumerate again after each join, because a thread can start another thread.
        while others := [t for t in threading.enumerate() if t is not threading.current_thread() and not t.daemon]:
            others[0].join()
        os.environ.pop("RWEE_ARTIFACT_DIR", None)  # a later run must not write to this run's folder
        sys.argv, sys.path[:], cwd, modules = saved
        sys.stdout, sys.stderr, sys.stdin = sys.__stdout__, sys.__stderr__, sys.__stdin__
        os.chdir(cwd)
        roots = (script.parent, Path(workdir).resolve())
        for name in set(sys.modules) - modules:
            file = getattr(sys.modules[name], "__file__", None)
            if file and is_user_module(file, roots):
                del sys.modules[name]  # an edited helper must not keep its old code
    return exit_code


def read_jobs(jobs, pending):
    for line in jobs:
        pending.put(line)
    # The server is gone, however it died, so a running script and its children must not outlive it.
    # shortcut: a native call that holds the GIL delays this until it returns. Upgrade to a Windows
    # Job Object with KILL_ON_JOB_CLOSE if a worker is ever seen to outlive the server.
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())], capture_output=True)
    else:
        os.killpg(0, signal.SIGKILL)  # the server starts the worker as a session leader
    os._exit(1)


def main():
    # Private copies of the job channel, not inherited by children, so script output cannot corrupt it.
    jobs = os.fdopen(os.dup(0), encoding="utf-8")
    results = os.fdopen(os.dup(1), "w", encoding="utf-8")
    # O_BINARY: a Windows fd in text mode translates newlines a second time, and gives \r\r\n.
    binary = getattr(os, "O_BINARY", 0)
    devnull = os.open(os.devnull, os.O_RDWR | binary)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)

    # Scripts stay on the main thread. The job channel is read beside them, to see the server exit.
    pending = queue.Queue()
    threading.Thread(target=read_jobs, args=(jobs, pending), daemon=True).start()
    while True:
        job = json.loads(pending.get())
        # Capture at the OS level, so native libraries and child processes are captured too.
        # shortcut: C stdio output still buffered when a script ends can appear in a later run.
        # Upgrade to an fflush through the right C runtime if arcpy output is ever misplaced.
        for fd, path in ((1, job["stdout"]), (2, job["stderr"])):
            capture = os.open(path, os.O_WRONLY | os.O_APPEND | binary)
            os.dup2(capture, fd)
            os.close(capture)
        # Not in stderr: on Windows, faulthandler also reports exceptions that native code handles.
        # The first `import arcpy` writes about 680 KB of these on a run that succeeds.
        with open(job["fault"], "wb") as fault:
            faulthandler.enable(file=fault)
            exit_code = run_script(job["script"], job["args"], job["workdir"], job["artifact_dir"])
            faulthandler.disable()
        for fd in (1, 2):
            os.dup2(devnull, fd)  # releases the capture files, which Windows cannot delete while open
        results.write(json.dumps({"exit_code": exit_code}) + "\n")
        results.flush()


if __name__ == "__main__":
    main()
