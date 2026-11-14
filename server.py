"""MCP server with four tools (read, write, edit, execute) and one user-defined prompt."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from functools import wraps
from itertools import islice
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.prompts import Prompt, PromptArgument
from fastmcp.resources import FileResource
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.lifespan import lifespan
from fastmcp.server.providers import Provider
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.subscriptions import ResourcesListChanged
from mcp.types import ResourceListChangedNotification, SubscriptionsListenRequestParams
from starlette.responses import FileResponse, JSONResponse

MAX_OUTPUT = 100_000
# Claude Code rejects a response over 16 MB, and base64 adds a third. 12.5 MB passed and 50 MB failed.
MAX_ARTIFACT_READ = 12_000_000
ARTIFACT_SWEEP_SECONDS = 300
RUN_ID = re.compile(r"[0-9a-f]{32}")
TTL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
log = logging.getLogger("mcp_rwee")
mimetypes.add_type("application/vnd.apache.parquet", ".parquet")
MAX_CRASH_REPORT = 8_000
TOKEN_ENV = "MCP_RWEE_TOKEN"
MIN_TOKEN_LENGTH = 32
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
# Claude Code gives a prompt argument one word each and drops words with no argument.
# shortcut: a longer question is cut off with a note, raise this if that happens in real use.
QUESTION_SLOTS = 100
WORKER = Path(__file__).with_name("worker.py")
# Keeps the token out of the environment of generated code. This is not a security boundary:
# code that runs as the same user can still read the server process.
CHILD_ENV = {**{k: v for k, v in os.environ.items() if k != TOKEN_ENV}, "PYTHONUTF8": "1"}
NEW_SESSION = os.name != "nt"  # lets _kill_tree reach every child on POSIX


def _tail(path, limit=MAX_OUTPUT):
    """The end of an output file. Reads a bounded amount, so a flood cannot exhaust server memory."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - limit))
        text = f.read(limit).decode("utf-8", errors="replace").replace("\r\n", "\n")
    return text if size <= limit else f"[{size - limit} bytes cut]\n{text}"


def _result(exit_code, out, err, note="", timed_out=False, crashed=False):
    stderr = _tail(err) + note
    return {"exit_code": exit_code, "stdout": _tail(out), "stderr": stderr, "timed_out": timed_out, "crashed": crashed}


def _kill_tree(proc):
    # macOS raises PermissionError for a process that already exited but is not yet reaped.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)


def _kill_and_wait(proc):
    _kill_tree(proc)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)


class ScriptRunner:
    """Owns the worker process that runs .py files. A worker failure never reaches the server."""

    def __init__(self, workdir):
        self.workdir = workdir
        self._proc = None
        self._lock = asyncio.Lock()

    def stop(self):
        proc, self._proc = self._proc, None  # first, so a second stop in another thread does nothing
        if proc is not None:
            _kill_and_wait(proc)

    def _running_worker(self):
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                [sys.executable, "-u", "-B", "-P", str(WORKER)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                cwd=self.workdir,
                env=CHILD_ENV,
                encoding="utf-8",
                start_new_session=NEW_SESSION,
            )
        return self._proc

    async def run(self, script, args, timeout, artifact_dir):
        started = time.monotonic()
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout)
        except TimeoutError:
            raise ToolError(f"busy: an earlier script is still running after {timeout}s, so {script.name} did not start")
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                out, err, fault = Path(tmp, "stdout"), Path(tmp, "stderr"), Path(tmp, "fault")
                for path in (out, err, fault):
                    path.touch()
                proc = self._running_worker()
                job = {"script": str(script), "args": args, "workdir": str(self.workdir), "stdout": str(out), "stderr": str(err), "fault": str(fault), "artifact_dir": str(artifact_dir)}
                proc.stdin.write(json.dumps(job) + "\n")
                proc.stdin.flush()

                line = None
                try:
                    # The time spent waiting for the lock counts, so a queued call keeps its timeout.
                    remaining = timeout - (time.monotonic() - started)
                    line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), remaining)
                except TimeoutError:
                    await asyncio.to_thread(self.stop)  # taskkill takes up to a second on Windows
                finally:
                    # The client went away: nothing supervises this run. Not awaited, because a
                    # cancelled task can be cancelled again at an await, and the worker must stop.
                    if line is None:
                        self.stop()
                if line is None:
                    return _result(None, out, err, f"timed out after {timeout}s. Worker killed; imports reload on the next run.\n", timed_out=True)
                if not line:
                    code = proc.wait()
                    self._proc = None
                    note = f"{_tail(fault, MAX_CRASH_REPORT)}\nworker process crashed (exit code {code}). The server is still running; imports reload on the next run.\n"
                    return _result(code, out, err, note, crashed=True)
                return _result(json.loads(line)["exit_code"], out, err)
        finally:
            self._lock.release()


async def _run_shell(command, timeout, workdir, artifact_dir):
    env = {**CHILD_ENV, "RWEE_ARTIFACT_DIR": str(artifact_dir)}
    if os.name == "nt":
        # _tail decodes UTF-8, but `echo` uses the code page its cmd started with, so chcp must run
        # in an earlier cmd. !..! expands after parsing, so only the last cmd parses the command.
        env["RWEE_COMMAND"] = command
        command = '"%ComSpec%" /d /v:on /s /c "chcp 65001 >nul & "%ComSpec%" /d /v:off /s /c "!RWEE_COMMAND!""'
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        out, err = Path(tmp, "stdout"), Path(tmp, "stderr")
        with open(out, "wb") as stdout, open(err, "wb") as stderr:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=env,
                start_new_session=NEW_SESSION,
            )
        code = None
        try:
            code = await asyncio.to_thread(proc.wait, timeout=timeout)
        except subprocess.TimeoutExpired:
            await asyncio.to_thread(_kill_and_wait, proc)  # taskkill takes up to a second on Windows
        finally:
            if proc.poll() is None:  # the client disconnected; not awaited, as in ScriptRunner.run
                _kill_and_wait(proc)
        if code is None:
            return _result(None, out, err, f"timed out after {timeout}s\n", timed_out=True)
        return _result(code, out, err)


def _read_text(path, offset=0, limit=None):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            stop = None if limit is None else offset + limit
            if offset < 0 or (limit is not None and limit < 0):
                return "".join(f.readlines()[offset:stop])  # negative slices need the end of the file
            return "".join(islice(f, offset, stop))
    except UnicodeDecodeError as e:
        raise ToolError(f"{path} is not UTF-8 text") from e


def _write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8", newline="") as f:
            f.write(content)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)  # a failed write leaves the original file intact
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def default_artifact_dir(port):
    # One folder per port: a server lists every finished run in its folder, so two servers must not share one.
    return Path(tempfile.gettempdir()) / f"mcp-rwee-artifacts-{port}"


def parse_ttl(text):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text)
    if not match or float(match[1]) <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a time above 0 with a unit, for example 90m or 24h")
    return float(match[1]) * TTL_UNITS[match[2]]


class Artifacts:
    """The run folders under --artifact-dir. Only folders with a run id are ever deleted."""

    def __init__(self, root, ttl):
        self.root = Path(root).resolve()
        self.ttl = ttl
        self.active = set()  # a run in progress has partial files, so it is not served or deleted
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def start(self):
        run = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)  # a temp folder cleaner can remove it
        (self.root / run).mkdir(mode=0o700)
        self.active.add(run)
        return run

    def finish(self, run):
        folder = self.root / run
        with contextlib.suppress(OSError):  # the folder has files, or Windows holds it open
            folder.rmdir()
        with contextlib.suppress(OSError):
            os.utime(folder)  # the time limit starts when the run ends
        self.active.discard(run)

    def locate(self, run, name):
        """The file for a client-supplied run and name, or None if clients cannot have it."""
        return None if run in self.active else self._valid_path(run, name)

    def _valid_path(self, run, name):
        if not RUN_ID.fullmatch(run):
            return None
        folder = self.root / run
        path = folder / name
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(folder) or not path.is_file():
                return None
        except (OSError, ValueError):  # for example a null character or a name too long
            return None
        return path

    def _names(self, run):
        folder = self.root / run
        for parent, _, files in os.walk(folder):
            for file in files:
                name = (Path(parent) / file).relative_to(folder).as_posix()
                if self._valid_path(run, name):
                    yield name

    def _finished_folders(self):
        if not self.root.is_dir():
            return
        for folder in self.root.iterdir():
            if RUN_ID.fullmatch(folder.name) and folder.name not in self.active and not folder.is_symlink() and folder.is_dir():
                yield folder

    def all(self):
        # shortcut: resources/list has no pagination here, add it if a time limit keeps thousands of files.
        return [(folder.name, name) for folder in self._finished_folders() for name in self._names(folder.name)]

    def describe(self, run):
        """What execute returns for each file."""
        items = []
        for name in sorted(self._names(run)):
            path = self.root / run / name
            digest = None
            with contextlib.suppress(OSError), open(path, "rb") as f:  # a running child can hold it open on Windows
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            with contextlib.suppress(FileNotFoundError):  # a child that outlives the run can delete it
                items.append(
                    {
                        "uri": artifact_uri(run, name),
                        "name": name,
                        "size": path.stat().st_size,
                        "sha256": digest,
                        "mime_type": artifact_mime(name),
                        "download_path": download_path(run, name),
                    }
                )
        return items

    def sweep(self):
        deleted = 0
        cutoff = time.time() - self.ttl
        for folder in self._finished_folders():
            with contextlib.suppress(FileNotFoundError):  # the timer and the cleanup route can run together
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)  # a locked file on Windows waits for the next sweep
                    deleted += not folder.exists()
        return deleted


def artifact_uri(run, name):
    return f"artifact://{run}/{quote(name)}"


def download_path(run, name):
    return f"/artifacts/{run}/{quote(name)}"


def artifact_mime(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class ArtifactResource(FileResource):
    download_path: str

    async def read(self):
        size = self.path.stat().st_size
        if size > MAX_ARTIFACT_READ:  # before reading, because a large read uses several times its size
            raise ResourceError(f"{self.name} has {size} bytes, over the {MAX_ARTIFACT_READ} byte limit for a resource. Download it from {self.download_path}")
        return await super().read()


class ArtifactProvider(Provider):
    """Lists and reads artifacts. MCP resources have no write, edit or delete request."""

    def __init__(self, artifacts):
        super().__init__()
        self.artifacts = artifacts

    def _resource(self, run, name):
        path = self.artifacts.root / run / name
        return ArtifactResource(
            uri=artifact_uri(run, name),
            name=name,
            description=f"{path.stat().st_size} bytes. Download path: {download_path(run, name)}",
            mime_type=artifact_mime(name),
            path=path,
            is_binary=True,
            download_path=download_path(run, name),
        )

    def _all_resources(self):
        resources = []
        for run, name in self.artifacts.all():
            with contextlib.suppress(FileNotFoundError):  # a sweep can delete a file during this loop
                resources.append(self._resource(run, name))
        return resources

    async def _list_resources(self):
        return await asyncio.to_thread(self._all_resources)

    async def _get_resource(self, uri, version=None):
        parts = urlsplit(str(uri))
        name = unquote(parts.path.removeprefix("/"))
        if parts.scheme == "artifact" and self.artifacts.locate(parts.netloc, name):
            return self._resource(parts.netloc, name)
        return None


def build(prompt_path, workdir, token, artifacts):
    prompt_path, workdir = Path(prompt_path), Path(workdir).resolve()
    runner = ScriptRunner(workdir)
    listeners = InMemorySubscriptionBus()

    def resolve(path):
        return (workdir / path).resolve()

    def resolve_for_change(path):
        target = resolve(path)
        if target.is_relative_to(artifacts.root):
            raise ToolError(f"{target} is an artifact, and artifacts cannot be written or edited")
        return target

    async def sweep_and_notify():
        deleted = await asyncio.to_thread(artifacts.sweep)
        if deleted:
            await listeners.publish(ResourcesListChanged())
        return deleted

    @lifespan
    async def sweep_artifacts(server):
        async def repeat():
            while True:
                try:
                    await sweep_and_notify()
                except Exception:
                    log.exception("artifact cleanup failed")  # the next sweep tries again
                await asyncio.sleep(min(ARTIFACT_SWEEP_SECONDS, artifacts.ttl))

        task = asyncio.create_task(repeat())
        try:
            yield {}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    mcp = FastMCP(
        "rwee",
        instructions=prompt_path.read_text(encoding="utf-8"),
        auth=StaticTokenVerifier(tokens={token: {"client_id": "rwee", "scopes": []}}),
        lifespan=sweep_artifacts,
        providers=[ArtifactProvider(artifacts)],
    )
    # On protocol 2026-07-28 a list change reaches a client only on subscriptions/listen, or Claude Code
    # keeps the list it got on connect. shortcut: FastMCP 4.1 does not serve this method and gives no
    # public way to add it, so this uses _mcp_server. Remove it when FastMCP serves the method.
    mcp._mcp_server.add_request_handler("subscriptions/listen", SubscriptionsListenRequestParams, ListenHandler(listeners))

    def token_route(path, methods):
        """A custom route that needs the token. FastMCP finds the user on these routes but lets everyone in."""

        def register(handler):
            @wraps(handler)
            async def checked(request):
                if not request.user.is_authenticated:
                    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                return await handler(request)

            return mcp.custom_route(path, methods=methods, include_in_schema=False)(checked)

        return register

    @token_route("/artifacts/cleanup", ["POST"])
    async def cleanup_artifacts(request):
        return JSONResponse({"deleted": await sweep_and_notify()})

    @token_route("/artifacts/{run}/{name:path}", ["GET"])
    async def download_artifact(request):
        path = artifacts.locate(request.path_params["run"], request.path_params["name"])
        if path is None:
            return JSONResponse({"error": "artifact not found"}, status_code=404)
        return FileResponse(path, filename=path.name, media_type=artifact_mime(path.name))

    @mcp.tool
    def read(path: str, offset: int = 0, limit: int | None = None) -> str:
        """Read a UTF-8 text file. offset and limit select a range of lines (zero-based)."""
        target = resolve(path)
        # Without a limit, or with a negative slice, the whole file is in memory, and the client refuses it anyway.
        if (limit is None or offset < 0 or limit < 0) and target.stat().st_size > MAX_ARTIFACT_READ:
            raise ToolError(f"{target} has over {MAX_ARTIFACT_READ} bytes. Read it in parts with a positive offset and a limit, or use execute")
        return _read_text(target, offset, limit)

    @mcp.tool
    def write(path: str, content: str) -> str:
        """Create or replace a text file. Parent folders are created."""
        target = resolve_for_change(path)
        _write_text(target, content)
        return f"wrote {len(content)} characters to {target}"

    @mcp.tool
    def edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        """Replace an exact string in a file. It must match once unless replace_all is true."""
        target = resolve_for_change(path)
        text = _read_text(target)
        count = text.count(old_string) if old_string else 0
        if count == 0:
            raise ToolError(f"old_string not found in {target}")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string matches {count} times in {target}; add context or set replace_all")
        _write_text(target, text.replace(old_string, new_string))
        return f"replaced {count} occurrence(s) in {target}"

    @mcp.tool
    async def execute(ctx: Context, target: str, args: list[str] | None = None, timeout: float = 300) -> dict:
        """Run an existing .py file in the long-lived Python worker, or run a shell command.

        A target that is an existing .py file runs in the worker: imports stay loaded between
        runs, and only one script runs at a time. args apply to this mode only. Any other target
        runs as a shell command in a new subprocess. A crash or timeout does not affect the server.
        Each run gets its own folder in the RWEE_ARTIFACT_DIR environment variable.
        Returns exit_code, stdout, stderr, timed_out, crashed and artifacts, with an item for each
        file in that folder. Read an artifact with its uri, or download a large one from download_path.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ToolError(f"timeout must be finite and greater than 0, not {timeout}")
        script = resolve(target)
        is_script = script.suffix == ".py" and script.is_file()
        if args and not is_script:
            raise ToolError(f"args apply only to an existing .py file, and {script} is not one")
        run = artifacts.start()
        try:
            folder = artifacts.root / run
            if is_script:
                result = await runner.run(script, args or [], timeout, folder)
            else:
                result = await _run_shell(target, timeout, workdir, folder)
            result["artifacts"] = await asyncio.to_thread(artifacts.describe, run)
        finally:
            artifacts.finish(run)
        if result["artifacts"]:
            # The first reaches listen streams. The second reaches clients on the older protocol.
            # A notification is only a hint, so its failure must not change the result of the run.
            with contextlib.suppress(Exception):
                await listeners.publish(ResourcesListChanged())
                await ctx.send_notification(ResourceListChangedNotification())
        return result

    class TaskPrompt(Prompt):
        async def render(self, arguments=None):
            given = arguments or {}
            if not given.get("question"):
                raise ValueError("Missing required argument: question")
            question = " ".join(given[a.name] for a in self.arguments if given.get(a.name))
            if given.get(self.arguments[-1].name):
                question += f"\n\n(The client sends at most {QUESTION_SLOTS} words, so this question may be cut off.)"
            return f"{prompt_path.read_text(encoding='utf-8')}\n\n## Question\n\n{question}"

    mcp.add_prompt(
        TaskPrompt(
            name="task",
            description="The configured prompt, followed by the question.",
            arguments=[
                PromptArgument(name="question", description="The question", required=True),
                *(PromptArgument(name=f"w{i}") for i in range(2, QUESTION_SLOTS + 1)),
            ],
        )
    )

    return mcp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, type=Path, help="prompt file returned to clients")
    parser.add_argument("--workdir", default=".", type=Path, help="base for relative paths and commands")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--ssl-certfile", help="TLS certificate file (PEM); required for a non-local --host")
    parser.add_argument("--ssl-keyfile", help="TLS private key file (PEM)")
    parser.add_argument("--artifact-dir", type=Path, help="folder for generated files (default: mcp-rwee-artifacts-<port> in the temp folder)")
    parser.add_argument("--artifact-ttl", type=parse_ttl, default="24h", help="time to keep a run's artifacts, for example 90m or 24h")
    args = parser.parse_args()

    token = os.environ.get(TOKEN_ENV)
    # execute is remote code execution, so there is no unauthenticated mode and no guessable token.
    if not token or len(token) < MIN_TOKEN_LENGTH:
        parser.error(f"set {TOKEN_ENV} to a random value of at least {MIN_TOKEN_LENGTH} characters")
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("set both --ssl-certfile and --ssl-keyfile, or neither")
    if not args.ssl_certfile and args.host not in LOCAL_HOSTS:  # plain HTTP would expose the token
        parser.error(f"--host {args.host} is not local, so it needs --ssl-certfile and --ssl-keyfile")
    if not args.prompt.is_file():
        parser.error(f"prompt file not found: {args.prompt}")
    if not args.workdir.is_dir():
        parser.error(f"workdir not found: {args.workdir}")
    artifact_dir = (args.artifact_dir or default_artifact_dir(args.port)).resolve()
    if args.workdir.resolve().is_relative_to(artifact_dir):  # write and edit refuse all paths in --artifact-dir
        parser.error(f"--artifact-dir {artifact_dir} must not contain the workdir")
    try:
        artifacts = Artifacts(artifact_dir, args.artifact_ttl)
    except OSError as e:
        parser.error(f"cannot create --artifact-dir {artifact_dir}: {e}")

    tls = {"ssl_certfile": args.ssl_certfile, "ssl_keyfile": args.ssl_keyfile} if args.ssl_certfile else None
    build(args.prompt, args.workdir, token, artifacts).run(
        transport="http", host=args.host, port=args.port, uvicorn_config=tls, show_banner=False
    )


if __name__ == "__main__":
    main()
