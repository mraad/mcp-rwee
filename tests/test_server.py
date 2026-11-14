import base64
import datetime
import hashlib
import http.client
import ipaddress
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import tracemalloc
import uuid
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import anyio
import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.client.client import Client as SdkClient
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.subscriptions import ResourcesListChanged

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import server  # noqa: E402
import worker  # noqa: E402

pytestmark = pytest.mark.anyio
TOKEN = "test-token-0123456789-abcdefghijklmnop"


# Writes its own pid to a file, then stays alive, so a test can check that it gets stopped.
PID_THEN_SLEEP = "import os, pathlib, time\npathlib.Path('{file}').write_text(str(os.getpid()))\ntime.sleep(120)\n"
FLOOD = "print('x' * 1_000_000); print('END')"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def prompt(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text("Use arcpy when needed.", encoding="utf-8")
    return path


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def artifact_root(tmp_path):
    return tmp_path / "artifacts"


@pytest.fixture
def mcp(prompt, work, artifact_root):
    return server.build(prompt, work, TOKEN, server.Artifacts(artifact_root, 86400))


async def call(mcp, tool, **kwargs):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, kwargs)
    return result.data


async def test_exactly_four_tools_and_one_prompt(mcp):
    async with Client(mcp) as client:
        assert {t.name for t in await client.list_tools()} == {"read", "write", "edit", "execute"}
        assert [p.name for p in await client.list_prompts()] == ["task"]
        assert client.instructions == "Use arcpy when needed."


@pytest.mark.parametrize("name", ["arcpy.md", "northsea-duckdb.md"])
def test_prompt_fits_the_client_limit_with_machine_details_first(name):
    # Claude Code cuts server instructions after 4,096 characters, and the client sees only the text before.
    text = (ROOT / "prompts" / name).read_text(encoding="utf-8")
    assert len(text) <= 4096
    assert "## Machine details" in text and text.index("## Machine details") < text.index("## How to answer a question")


async def test_prompt_has_file_text_and_question_and_rereads_file(mcp, prompt):
    async with Client(mcp) as client:
        first = (await client.get_prompt("task", {"question": "How many wells?"})).messages[0].content.text
        prompt.write_text("Changed prompt.", encoding="utf-8")
        second = (await client.get_prompt("task", {"question": "Why?"})).messages[0].content.text
    assert "Use arcpy when needed." in first and "How many wells?" in first
    assert "Changed prompt." in second and "Why?" in second


async def test_prompt_rebuilds_a_question_split_into_one_word_per_argument(mcp):
    async def as_claude_code(client, text):
        # What Claude Code does with the command text: split on whitespace, zip with argument names.
        names = [a.name for a in (await client.list_prompts())[0].arguments]
        result = await client.get_prompt("task", dict(zip(names, text.split())))
        return result.messages[0].content.text

    question = "Calculate statistics on well water depth in the North Sea?"
    async with Client(mcp) as client:
        assert (await as_claude_code(client, question)).endswith(f"## Question\n\n{question}")

        exact = " ".join(f"word{i}" for i in range(100))
        assert (await as_claude_code(client, " ".join(f"word{i}" for i in range(99)))).endswith("word98")
        over = await as_claude_code(client, exact + " word100 word101")
        assert exact in over and "word100" not in over and "may be cut off" in over

        whole = (await client.get_prompt("task", {"question": "Line one\nline  two"})).messages[0].content.text
        assert whole.endswith("Line one\nline  two")

        with pytest.raises(Exception, match="question"):
            await client.get_prompt("task", {})


async def test_write_read_edit_round_trip(mcp, work):
    await call(mcp, "write", path="sub/dir/a.txt", content="one\ntwo\nthree\n")
    assert await call(mcp, "read", path="sub/dir/a.txt") == "one\ntwo\nthree\n"
    assert await call(mcp, "read", path=str(work / "sub/dir/a.txt"), offset=1, limit=1) == "two\n"
    await call(mcp, "edit", path="sub/dir/a.txt", old_string="two", new_string="2")
    assert (work / "sub/dir/a.txt").read_text() == "one\n2\nthree\n"


async def test_crlf_survives_write_and_edit(mcp, work):
    await call(mcp, "write", path="w.txt", content="a\r\nb\r\n")
    await call(mcp, "edit", path="w.txt", old_string="a", new_string="z")
    assert (work / "w.txt").read_bytes() == b"z\r\nb\r\n"
    assert await call(mcp, "read", path="w.txt", offset=1) == "b\r\n"


async def test_edit_rejects_missing_empty_and_ambiguous(mcp, work):
    (work / "e.txt").write_text("x x")
    for old in ("nope", "", "x"):
        with pytest.raises(ToolError):
            await call(mcp, "edit", path="e.txt", old_string=old, new_string="y")
    assert (work / "e.txt").read_text() == "x x"
    await call(mcp, "edit", path="e.txt", old_string="x", new_string="y", replace_all=True)
    assert (work / "e.txt").read_text() == "y y"


async def test_read_lines_break_only_at_line_ends(mcp, work):
    (work / "f.txt").write_bytes("a\x0cb\u2028c\nd\r\ne\n".encode())
    assert await call(mcp, "read", path="f.txt", limit=1) == "a\x0cb\u2028c\n"
    assert await call(mcp, "read", path="f.txt", offset=1, limit=1) == "d\r\n"


@pytest.mark.parametrize("offset, limit", [(0, None), (1, 2), (2, 0), (20, 2), (-2, None), (1, -1), (-3, 2)])
async def test_read_ranges_keep_slice_semantics(mcp, work, offset, limit):
    lines = ["a\r\n", "b\r", "c\n", "d"]
    (work / "lines.txt").write_bytes("".join(lines).encode())
    expected = "".join(lines[offset : None if limit is None else offset + limit])
    assert await call(mcp, "read", path="lines.txt", offset=offset, limit=limit) == expected


async def test_small_read_does_not_load_the_whole_file(mcp, work):
    (work / "large.txt").write_bytes(b"first\n" + b"line\n" * 1_000_000)
    tracemalloc.start()
    try:
        assert await call(mcp, "read", path="large.txt", limit=1) == "first\n"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2_000_000


async def test_read_rejects_binary(mcp, work):
    (work / "b.bin").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ToolError, match="not UTF-8"):
        await call(mcp, "read", path="b.bin")


async def test_read_refuses_a_large_file_without_a_limit(mcp, work):
    (work / "big.txt").write_bytes(b"x\n" * (server.MAX_ARTIFACT_READ // 2 + 1))
    for kwargs in ({}, {"offset": -1}, {"offset": 0, "limit": -1}):
        with pytest.raises(ToolError, match="in parts"):
            await call(mcp, "read", path="big.txt", **kwargs)
    assert await call(mcp, "read", path="big.txt", offset=5, limit=2) == "x\nx\n"
    assert "replaced 1" in await call(mcp, "edit", path="big.txt", old_string="x\nx\nx\nx\n" * (server.MAX_ARTIFACT_READ // 8), new_string="y\n")


async def test_failed_write_keeps_original_and_leaves_no_temp(work):
    target = work / "keep.txt"
    target.write_text("original")
    with pytest.raises(UnicodeEncodeError):
        server._write_text(target, "bad \udc80")
    assert target.read_text() == "original"
    assert [p.name for p in work.iterdir()] == ["keep.txt"]


SCRIPT = """
import json, os, sys, threading
print(json.dumps({"pid": os.getpid(), "main_thread": threading.current_thread() is threading.main_thread(),
                  "name": __name__, "argv": sys.argv[1:], "cwd": os.getcwd()}))
"""


async def test_python_runs_in_one_long_lived_worker_not_a_fork_per_run(mcp, work):
    (work / "s.py").write_text(SCRIPT)
    first = await call(mcp, "execute", target="s.py", args=["a", "b"])
    second = await call(mcp, "execute", target="s.py")
    one, two = json.loads(first["stdout"]), json.loads(second["stdout"])
    assert first["exit_code"] == 0 and first["crashed"] is False
    assert one["pid"] == two["pid"] != os.getpid()
    assert one["main_thread"] is True
    assert one["name"] == "__main__"
    assert one["argv"] == ["a", "b"] and two["argv"] == []
    assert Path(one["cwd"]).resolve() == work.resolve()


async def test_worker_state_is_restored_after_a_script(mcp, work):
    (work / "dirty.py").write_text(
        "import os, sys, io\nsys.argv.append('x'); sys.path.append('junk'); os.chdir('..')\n"
        "print('hi', file=sys.stderr); sys.stdout = io.StringIO(); sys.stderr = io.StringIO()\n"
    )
    (work / "check.py").write_text("import os, sys\nprint(sys.argv[1:], 'junk' in sys.path, os.getcwd(), sep='|')")
    assert (await call(mcp, "execute", target="dirty.py"))["stderr"] == "hi\n"
    assert (await call(mcp, "execute", target="check.py"))["stdout"].strip() == f"[]|False|{work.resolve()}"


async def test_script_thread_finishes_and_its_output_comes_back(mcp, work):
    (work / "t.py").write_text("import threading, time\nthreading.Thread(target=lambda: (time.sleep(0.3), print('late'))).start()\nprint('main')")
    (work / "ok.py").write_text("print('ok')")
    assert (await call(mcp, "execute", target="t.py"))["stdout"].split() == ["main", "late"]
    assert (await call(mcp, "execute", target="ok.py"))["stdout"].strip() == "ok"


async def test_thread_started_by_a_script_thread_also_finishes(mcp, work):
    (work / "t.py").write_text(
        "import threading, time\n"
        "def inner():\n    time.sleep(0.3)\n    print('inner')\n"
        "def outer():\n    time.sleep(0.2)\n    threading.Thread(target=inner).start()\n"
        "threading.Thread(target=outer).start()\n"
    )
    assert (await call(mcp, "execute", target="t.py"))["stdout"].strip() == "inner"


async def test_timeout_of_zero_is_a_clear_error_and_not_busy(mcp, work):
    (work / "ok.py").write_text("print('ok')")
    for target in ("ok.py", "echo hi"):
        with pytest.raises(ToolError, match="greater than 0"):
            await call(mcp, "execute", target=target, timeout=0)


async def test_native_and_child_process_output_is_captured(mcp, work):
    (work / "n.py").write_text("import os, sys\nos.write(1, b'native\\n'); os.write(2, b'nerr\\n'); os.system('echo child')")
    result = await call(mcp, "execute", target="n.py")
    assert result["stdout"].split() == ["native", "child"]
    assert result["stderr"] == "nerr\n"


async def test_script_output_cannot_corrupt_the_job_channel(mcp, work):
    (work / "j.py").write_text("print('{\"exit_code\": 99}')")
    result = await call(mcp, "execute", target="j.py")
    assert result["exit_code"] == 0 and "99" in result["stdout"]


@pytest.mark.parametrize(
    "code, exit_code, in_stderr",
    [
        ("raise ValueError('boom')", 1, "ValueError: boom"),
        ("import sys; sys.exit(7)", 7, ""),
        ("import sys; sys.exit()", 0, ""),
        ("import sys; sys.exit('failed')", 1, "failed"),
        ("input()", 1, "EOFError"),
        ("import sys; sys.exit(True)", 1, ""),
    ],
)
async def test_python_errors_leave_the_worker_alive(mcp, work, code, exit_code, in_stderr):
    (work / "pid.py").write_text("import os; print(os.getpid())")
    pid = (await call(mcp, "execute", target="pid.py"))["stdout"]
    (work / "f.py").write_text(code)
    result = await call(mcp, "execute", target="f.py")
    assert result["exit_code"] == exit_code and type(result["exit_code"]) is int and result["crashed"] is False
    assert in_stderr in result["stderr"]
    assert (await call(mcp, "execute", target="pid.py"))["stdout"] == pid


@pytest.mark.parametrize(
    "crash",
    [
        "import faulthandler; faulthandler._sigsegv()",  # real access violation, as a native arcpy failure
        "import os; os.abort()",
        "import os; os._exit(9)",
    ],
)
async def test_hard_crash_does_not_stop_server_and_worker_restarts(mcp, work, crash):
    (work / "crash.py").write_text(f"print('before crash')\n{crash}\n")
    (work / "pid.py").write_text("import os; print(os.getpid())")
    first_pid = (await call(mcp, "execute", target="pid.py"))["stdout"]
    result = await call(mcp, "execute", target="crash.py")
    assert result["crashed"] is True
    assert result["exit_code"] != 0
    assert "before crash" in result["stdout"]
    assert "server is still running" in result["stderr"]
    # The same in-memory server keeps working, with a new worker process.
    assert (await call(mcp, "execute", target="pid.py"))["stdout"] != first_pid
    assert await call(mcp, "read", path="pid.py")


async def test_segfault_stack_is_reported(mcp, work):
    (work / "crash.py").write_text("import faulthandler\ndef bad():\n    faulthandler._sigsegv()\nbad()\n")
    result = await call(mcp, "execute", target="crash.py")
    assert "fatal" in result["stderr"].lower() and "in bad" in result["stderr"]


@pytest.mark.skipif(os.name != "nt", reason="Windows reports exceptions that native code handles")
async def test_handled_native_exception_is_not_reported_as_fatal(mcp, work):
    # arcpy does this about 240 times on import. ctypes handles this one and raises OSError.
    (work / "h.py").write_text("import ctypes\ntry:\n    ctypes.string_at(0)\nexcept OSError:\n    print('handled')\n")
    result = await call(mcp, "execute", target="h.py")
    assert result["stdout"].strip() == "handled" and result["stderr"] == "" and result["crashed"] is False


def test_tail_limit_keeps_the_end(tmp_path):
    path = tmp_path / "report"
    path.write_bytes(b"x" * 100 + b"END")
    assert server._tail(path, 5) == "[98 bytes cut]\nxxEND"


def test_tail_stays_bounded_if_output_grows(tmp_path, monkeypatch):
    path = tmp_path / "report"
    path.write_bytes(b"x" * 100)
    stat = Path.stat

    def grow_after_stat(self, *args, **kwargs):
        info = stat(self, *args, **kwargs)
        if self == path:
            with self.open("ab") as output:
                output.write(b"y" * 1_000_000)
        return info

    monkeypatch.setattr(Path, "stat", grow_after_stat)
    assert server._tail(path, 5) == "[95 bytes cut]\nxxxxx"


async def test_edited_helper_module_is_reloaded(mcp, work):
    # Same size within the same second is the case a .pyc check would miss.
    (work / "helper.py").write_text("VALUE = 'old'\n")
    (work / "main.py").write_text("import helper; print(helper.VALUE)")
    assert (await call(mcp, "execute", target="main.py"))["stdout"].strip() == "old"
    await call(mcp, "edit", path="helper.py", old_string="old", new_string="new")
    assert (await call(mcp, "execute", target="main.py"))["stdout"].strip() == "new"
    assert not (work / "__pycache__").exists()


async def test_helper_written_just_before_the_run_is_importable(mcp, work):
    (work / "main.py").write_text("print(1)")
    await call(mcp, "execute", target="main.py")
    await call(mcp, "write", path="fresh.py", content="VALUE = 42\n")
    await call(mcp, "write", path="main.py", content="import fresh; print(fresh.VALUE)")
    assert (await call(mcp, "execute", target="main.py"))["stdout"].strip() == "42"


def test_installed_packages_are_never_unloaded():
    root = Path(sys.prefix).parent.resolve()
    assert not worker.is_user_module(Path(sys.prefix) / "lib" / "numpy" / "__init__.py", [root])
    assert worker.is_user_module(root / "my.py", [root])


async def test_library_module_stays_imported_between_runs(mcp, work):
    (work / "a.py").write_text("import wave; print(id(wave))")
    first = await call(mcp, "execute", target="a.py")
    second = await call(mcp, "execute", target="a.py")
    assert first["stdout"] == second["stdout"]


@pytest.mark.parametrize(
    "code",
    [
        "print('started', flush=True)\nwhile True:\n    pass\n",  # Python loop
        "print('started', flush=True)\nimport time\ntime.sleep(60)\n",  # native call, as arcpy would be
        "print('started', flush=True)\nimport signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:\n    pass\n",
    ],
)
async def test_python_timeout_kills_worker_and_next_run_works(mcp, work, code):
    (work / "spin.py").write_text(code)
    (work / "ok.py").write_text("print('ok')")
    started = time.monotonic()
    result = await call(mcp, "execute", target="spin.py", timeout=0.7)
    assert time.monotonic() - started < 10
    assert result["timed_out"] is True and result["exit_code"] is None
    assert "started" in result["stdout"]
    assert (await call(mcp, "execute", target="ok.py", timeout=10))["stdout"].strip() == "ok"


async def test_second_call_is_busy_while_a_script_runs(mcp, work):
    (work / "slow.py").write_text("import time; time.sleep(1.5); print('slow done')")
    (work / "ok.py").write_text("print('ok')")
    async with Client(mcp) as client:
        slow = {}

        async def run_slow():
            slow["result"] = await client.call_tool("execute", {"target": "slow.py", "timeout": 30})

        async with anyio.create_task_group() as group:
            group.start_soon(run_slow)
            await anyio.sleep(0.3)
            with pytest.raises(ToolError, match="busy"):
                await client.call_tool("execute", {"target": "ok.py", "timeout": 0.2})
            # The call waited, did not start, and so did not disturb the running script.
    assert slow["result"].data["stdout"].strip() == "slow done"


async def test_time_waiting_for_the_lock_counts_in_the_timeout(mcp, work):
    (work / "slow.py").write_text("import time; time.sleep(1.5)")
    (work / "next.py").write_text("import time; time.sleep(2.5); print('ran too long')")
    async with Client(mcp) as client, anyio.create_task_group() as group:
        group.start_soon(client.call_tool, "execute", {"target": "slow.py", "timeout": 30})
        await anyio.sleep(0.3)
        # About 1.2 s waiting plus 2.5 s running is over the 3 s timeout.
        result = (await client.call_tool("execute", {"target": "next.py", "timeout": 3})).data
    assert result["timed_out"] and "ran too long" not in result["stdout"]


async def test_event_loop_stays_free_during_a_script(mcp, work):
    (work / "slow.py").write_text("import time; time.sleep(1)")
    started = time.monotonic()
    async with Client(mcp) as client, anyio.create_task_group() as group:
        group.start_soon(client.call_tool, "execute", {"target": "slow.py"})
        await anyio.sleep(0.1)
        await client.call_tool("write", {"path": "x.txt", "content": "x"})
        assert time.monotonic() - started < 0.9


async def test_token_is_hidden_from_generated_code(prompt, work, monkeypatch, tmp_path):
    monkeypatch.setitem(server.CHILD_ENV, "PROBE", "visible")
    mcp = server.build(prompt, work, TOKEN, server.Artifacts(tmp_path / "artifacts", 86400))
    code = "import os; print(os.environ.get('PROBE'), os.environ.get('MCP_RWEE_TOKEN'))"
    (work / "env.py").write_text(code)
    assert (await call(mcp, "execute", target="env.py"))["stdout"].strip() == "visible None"
    assert server.TOKEN_ENV not in server.CHILD_ENV


async def test_shell_command_and_exit_code(mcp):
    result = await call(mcp, "execute", target="echo hello && exit 3")
    assert result["stdout"].strip() == "hello" and result["exit_code"] == 3


async def test_shell_output_keeps_non_ascii_file_names(mcp, work):
    (work / "Mülleré").mkdir()
    result = await call(mcp, "execute", target="dir /b" if os.name == "nt" else "ls")
    assert unicodedata.normalize("NFC", result["stdout"].strip()) == "Mülleré"


async def test_shell_echo_keeps_non_ascii(mcp):
    if os.name == "nt":  # an earlier chcp stays set in this console, and would hide the fault
        await call(mcp, "execute", target="chcp 437")
    result = await call(mcp, "execute", target="echo café 日本語")
    assert result["stdout"].strip() == "café 日本語"


@pytest.mark.skipif(os.name != "nt", reason="cmd parsing rules")
@pytest.mark.parametrize(
    "command, expected",
    [
        ('echo a& echo "b & c"', 'a\n"b & c"'),
        ("echo wow!", "wow!"),
        ("echo ^& %OS%", "& Windows_NT"),
        ('"%ComSpec%" /c "echo nested"', "nested"),
    ],
)
async def test_shell_command_is_parsed_once(mcp, command, expected):
    result = await call(mcp, "execute", target=command)
    assert result["stdout"].strip() == expected and result["stderr"] == ""


async def test_interpreter_command_runs_in_a_separate_process(mcp, work):
    (work / "s.py").write_text(SCRIPT)
    result = await call(mcp, "execute", target=f'"{sys.executable}" s.py')
    assert json.loads(result["stdout"])["pid"] != os.getpid()


async def test_args_are_rejected_for_shell_commands(mcp):
    with pytest.raises(ToolError, match="args apply only"):
        await call(mcp, "execute", target="echo hi", args=["x"])


def is_alive(pid):
    if os.name == "nt":  # os.kill(pid, 0) on Windows sends Ctrl+C
        found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in found
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_dead(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not is_alive(pid)


async def wait_for_file(path, seconds=30):
    deadline = time.monotonic() + seconds
    while not path.exists() or not path.read_text():
        if time.monotonic() > deadline:
            pytest.fail(f"{path.name} was not written within {seconds} seconds")
        await anyio.sleep(0.05)


async def test_wait_for_file_fails_at_its_deadline(tmp_path):
    with pytest.raises(pytest.fail.Exception, match="missing was not written"):
        await wait_for_file(tmp_path / "missing", seconds=0.1)


async def test_shell_timeout_kills_child_and_does_not_hang(mcp, work):
    # `&&` stops the shell from exec-ing the command, so a real child of the shell exists.
    (work / "child.py").write_text(PID_THEN_SLEEP.format(file="child"))
    started = time.monotonic()
    result = await call(mcp, "execute", target=f'echo start && "{sys.executable}" child.py', timeout=4)
    assert result["timed_out"] is True and "start" in result["stdout"]
    assert time.monotonic() - started < 20
    assert wait_until_dead(int((work / "child").read_text()))


async def test_output_flood_is_bounded_and_keeps_the_end(mcp, work):
    (work / "flood.py").write_text(FLOOD)
    result = await call(mcp, "execute", target=f'"{sys.executable}" flood.py', timeout=30)
    assert len(result["stdout"]) < server.MAX_OUTPUT + 100
    assert result["stdout"].endswith("END\n") and "bytes cut" in result["stdout"]


async def test_python_output_flood_is_bounded(mcp, work):
    (work / "flood.py").write_text(FLOOD)
    result = await call(mcp, "execute", target="flood.py")
    assert len(result["stdout"]) < server.MAX_OUTPUT + 100 and result["stdout"].endswith("END\n")


def wait_for_port(port):
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def status_of(url, token=None, context=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, body, headers), context=context).status
    except urllib.error.HTTPError as e:
        return e.code


def test_server_refuses_to_start_without_token_or_prompt(prompt, work):
    no_token_env = {k: v for k, v in os.environ.items() if k != server.TOKEN_ENV}
    no_token = subprocess.run(
        [sys.executable, "server.py", "--prompt", str(prompt)], cwd=ROOT, env=no_token_env, capture_output=True, text=True
    )
    assert no_token.returncode != 0 and server.TOKEN_ENV in no_token.stderr
    no_prompt = subprocess.run(
        [sys.executable, "server.py", "--prompt", str(work / "missing.md")],
        cwd=ROOT,
        env={**os.environ, server.TOKEN_ENV: TOKEN},
        capture_output=True,
        text=True,
    )
    assert no_prompt.returncode != 0 and "prompt file not found" in no_prompt.stderr


@pytest.mark.parametrize(
    "token, options, message",
    [
        ("short", [], "at least 32 characters"),
        (TOKEN, ["--host", "0.0.0.0"], "not local"),
        (TOKEN, ["--ssl-certfile", "cert.pem"], "set both"),
        (TOKEN, ["--ssl-keyfile", "key.pem"], "set both"),
        (TOKEN, ["--artifact-ttl", "0s"], "not a time"),
        (TOKEN, ["--artifact-ttl=-1h"], "not a time"),
        (TOKEN, ["--artifact-ttl", "24"], "not a time"),
        (TOKEN, ["--workdir", ".", "--artifact-dir", "."], "must not contain the workdir"),
    ],
)
def test_server_refuses_unsafe_settings(prompt, token, options, message):
    result = subprocess.run(
        [sys.executable, "server.py", "--prompt", str(prompt), *options],
        cwd=ROOT,
        env={**os.environ, server.TOKEN_ENV: token},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and message in result.stderr


def self_signed_cert(folder):
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "rwee")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


async def test_tls_serves_https_and_still_requires_the_token(prompt, work, tmp_path):
    cert, key = self_signed_cert(tmp_path)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--prompt", str(prompt), "--workdir", str(work), "--port", str(port),
         "--ssl-certfile", str(cert), "--ssl-keyfile", str(key)],
        cwd=ROOT,
        env={**os.environ, server.TOKEN_ENV: TOKEN},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(port)
        url = f"https://127.0.0.1:{port}/mcp"
        context = ssl.create_default_context(cafile=str(cert))
        assert status_of(url, context=context) == 401
        transport = StreamableHttpTransport(url, auth=BearerAuth(TOKEN), verify=context)
        async with Client(transport) as client:
            await client.call_tool("write", {"path": "s.py", "content": "print(6 * 7)"})
            assert (await client.call_tool("execute", {"target": "s.py"})).data["stdout"].strip() == "42"
        with pytest.raises(OSError):
            status_of(f"http://127.0.0.1:{port}/mcp")  # the port does not speak plain HTTP
    finally:
        proc.kill()
        proc.wait()


def start_server(prompt, work, artifact_root, *options):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--prompt", str(prompt), "--workdir", str(work), "--port", str(port), "--artifact-dir", str(artifact_root), *options],
        cwd=ROOT,
        env={**os.environ, server.TOKEN_ENV: TOKEN},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_for_port(port)
    return proc, port


@pytest.fixture
def http_server(prompt, work, artifact_root):
    proc, port = start_server(prompt, work, artifact_root)
    yield proc, f"http://127.0.0.1:{port}/mcp"
    proc.kill()
    proc.wait()


async def test_http_end_to_end_requires_token_and_survives_a_crash(http_server):
    proc, url = http_server
    assert status_of(url) == 401
    assert status_of(url, "wrong") == 401

    # Clients on the older initialize handshake get the prompt on connect too.
    async with Client(url, auth=BearerAuth(TOKEN), mode="legacy") as client:
        assert client.initialize_result.instructions == "Use arcpy when needed."
        assert len(await client.list_tools()) == 4

    async with Client(url, auth=BearerAuth(TOKEN)) as client:
        assert client.instructions == "Use arcpy when needed."
        await client.call_tool("write", {"path": "answer.py", "content": "print(6 * 7)"})
        result = await client.call_tool("execute", {"target": "answer.py"})
        assert result.data["stdout"].strip() == "42"
        prompt_result = await client.get_prompt("task", {"question": "What is 6 * 7?"})
        assert "What is 6 * 7?" in prompt_result.messages[0].content.text

        await client.call_tool("write", {"path": "crash.py", "content": "import faulthandler; faulthandler._sigsegv()"})
        crashed = await client.call_tool("execute", {"target": "crash.py"})
        assert crashed.data["crashed"] is True
        assert proc.poll() is None
        assert (await client.call_tool("execute", {"target": "answer.py"})).data["stdout"].strip() == "42"


@pytest.mark.parametrize("shell", [False, True])
async def test_client_disconnect_stops_execution_and_next_call_works(http_server, work, shell):
    _, url = http_server
    auth = BearerAuth(TOKEN)
    async with Client(url, auth=auth) as client:
        await client.call_tool("write", {"path": "long.py", "content": PID_THEN_SLEEP.format(file="pid")})
        await client.call_tool("write", {"path": "ok.py", "content": "print('ok')"})

    async def run_then_drop():
        async with Client(url, auth=auth) as client:
            target = f'"{sys.executable}" long.py' if shell else "long.py"
            await client.call_tool("execute", {"target": target})

    async with anyio.create_task_group() as group:
        group.start_soon(run_then_drop)
        await wait_for_file(work / "pid")
        await anyio.sleep(0.3)
        group.cancel_scope.cancel()

    pid = int((work / "pid").read_text())
    try:
        assert wait_until_dead(pid)
    finally:
        if is_alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, signal.SIGKILL)
    async with Client(url, auth=auth) as client:
        result = await client.call_tool("execute", {"target": "ok.py", "timeout": 10})
    assert result.data["stdout"].strip() == "ok"


@pytest.mark.parametrize("stop", [subprocess.Popen.terminate, subprocess.Popen.kill])
async def test_stopping_the_server_also_stops_a_running_worker_and_its_child(http_server, work, stop):
    proc, url = http_server
    (work / "child.py").write_text(PID_THEN_SLEEP.format(file="child"))
    code = "import subprocess, sys\nsubprocess.Popen([sys.executable, 'child.py'])\n" + PID_THEN_SLEEP.format(file="pid")
    with pytest.raises(BaseException):
        async with Client(url, auth=BearerAuth(TOKEN)) as client:
            await client.call_tool("write", {"path": "long.py", "content": code})
            async with anyio.create_task_group() as group:
                group.start_soon(client.call_tool, "execute", {"target": "long.py"})
                await wait_for_file(work / "pid")
                await wait_for_file(work / "child")
                stop(proc)
                await anyio.sleep(120)
    assert proc.wait(timeout=20) is not None
    assert wait_until_dead(int((work / "pid").read_text()))
    assert wait_until_dead(int((work / "child").read_text()))


WRITE_ARTIFACT = """
import os, pathlib
folder = pathlib.Path(os.environ["RWEE_ARTIFACT_DIR"])
(folder / "sub dir").mkdir()
(folder / "sub dir" / "out.parquet").write_bytes(bytes(range(256)))
print(folder)
"""
ARTIFACT_BYTES = bytes(range(256))


def finished_run(root, *files):
    root.mkdir(exist_ok=True)
    folder = root / uuid.uuid4().hex
    folder.mkdir()
    for name in files:
        (folder / name).write_bytes(b"x")
    return folder


def old_run(root, *files):
    """A finished run folder that is past any time limit."""
    folder = finished_run(root, *files)
    os.utime(folder, (1, 1))
    return folder


@pytest.mark.parametrize("shell", [False, True])
async def test_each_run_gets_its_own_artifact_folder(mcp, work, artifact_root, shell):
    (work / "make.py").write_text(WRITE_ARTIFACT)
    target = f'"{sys.executable}" make.py' if shell else "make.py"
    first = await call(mcp, "execute", target=target, timeout=60)
    second = await call(mcp, "execute", target=target, timeout=60)
    assert first["exit_code"] == 0, first["stderr"]
    folders = {Path(r["stdout"].strip()) for r in (first, second)}
    assert len(folders) == 2 and all(f.parent == artifact_root.resolve() and f.is_dir() for f in folders)
    item = first["artifacts"][0]
    assert item["sha256"] == hashlib.sha256(ARTIFACT_BYTES).hexdigest()
    assert (item["name"], item["size"], item["mime_type"]) == ("sub dir/out.parquet", 256, "application/vnd.apache.parquet")
    assert item["uri"].startswith("artifact://") and item["uri"].endswith("/sub%20dir/out.parquet")
    assert item["download_path"].startswith("/artifacts/") and item["download_path"].endswith("/sub%20dir/out.parquet")


def test_worker_clears_the_artifact_folder_after_a_run(work, monkeypatch):
    monkeypatch.delenv("RWEE_ARTIFACT_DIR", raising=False)
    script = work / "seen.py"
    script.write_text("import os, pathlib; pathlib.Path('seen.txt').write_text(os.environ['RWEE_ARTIFACT_DIR'])")
    assert worker.run_script(script, [], work, "the-folder") == 0
    assert (work / "seen.txt").read_text() == "the-folder" and "RWEE_ARTIFACT_DIR" not in os.environ


async def test_a_run_with_no_artifacts_leaves_no_folder(mcp, work, artifact_root):
    (work / "ok.py").write_text("print('ok')")
    assert (await call(mcp, "execute", target="ok.py"))["artifacts"] == []
    assert list(artifact_root.iterdir()) == []


async def test_a_removed_artifact_folder_comes_back(mcp, work, artifact_root):
    (work / "make.py").write_text(WRITE_ARTIFACT)
    shutil.rmtree(artifact_root)
    async with Client(mcp) as client:
        assert await client.list_resources() == []
        result = (await client.call_tool("execute", {"target": "make.py"})).data
        assert result["exit_code"] == 0 and [str(r.uri) for r in await client.list_resources()] == [result["artifacts"][0]["uri"]]
    artifacts = server.Artifacts(artifact_root, 1)
    shutil.rmtree(artifact_root)
    assert artifacts.sweep() == 0


async def test_timed_out_run_still_reports_its_artifacts(mcp, work):
    (work / "slow.py").write_text(
        "import os, pathlib, time\npathlib.Path(os.environ['RWEE_ARTIFACT_DIR'], 'part.bin').write_bytes(b'x')\ntime.sleep(120)\n"
    )
    result = await call(mcp, "execute", target="slow.py", timeout=3)
    assert result["timed_out"] and [a["name"] for a in result["artifacts"]] == ["part.bin"]


async def test_resources_list_and_read_artifacts(mcp, work):
    (work / "make.py").write_text(WRITE_ARTIFACT)
    item = (await call(mcp, "execute", target="make.py"))["artifacts"][0]
    async with Client(mcp) as client:
        listed = await client.list_resources()
        assert [str(r.uri) for r in listed] == [item["uri"]]
        assert listed[0].mime_type == "application/vnd.apache.parquet" and item["download_path"] in listed[0].description
        content = (await client.read_resource(item["uri"]))[0]
    assert base64.b64decode(content.blob) == ARTIFACT_BYTES


@pytest.mark.parametrize("script, notified", [(WRITE_ARTIFACT, True), ("print('none')", False)])
async def test_run_with_artifacts_notifies_that_the_resource_list_changed(http_server, work, script, notified):
    _, url = http_server
    (work / "make.py").write_text(script)
    changes = []

    class Handler(MessageHandler):
        async def on_resource_list_changed(self, message):
            changes.append(message)

    async with Client(url, auth=BearerAuth(TOKEN), message_handler=Handler()) as client:
        await client.call_tool("execute", {"target": "make.py"})
        await anyio.sleep(0.5)
    assert bool(changes) == notified


async def test_listen_stream_reports_new_and_deleted_artifacts(http_server, work, artifact_root):
    # Protocol 2026-07-28 delivers a list change only on this stream.
    _, url = http_server
    (work / "make.py").write_text(WRITE_ARTIFACT)
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    async with http, SdkClient(streamable_http_client(url, http_client=http)) as client:
        async with client.listen(resources_list_changed=True) as stream:
            with anyio.fail_after(30):
                assert (await client.call_tool("execute", {"target": "make.py"})).structured_content["artifacts"]
                assert isinstance(await anext(stream), ResourcesListChanged)
                assert [r.name for r in (await client.list_resources()).resources] == ["sub dir/out.parquet"]

                old_run(artifact_root, "old.bin")
                assert (await anyio.to_thread.run_sync(request_artifact, url, "/artifacts/cleanup", "POST"))[0] == 200
                assert isinstance(await anext(stream), ResourcesListChanged)


async def test_large_artifact_is_refused_before_it_is_read(mcp, artifact_root, monkeypatch):
    folder = finished_run(artifact_root)
    with open(folder / "big.parquet", "wb") as f:
        f.truncate(server.MAX_ARTIFACT_READ + 1)

    async def must_not_read(self):
        pytest.fail("the file was read")

    monkeypatch.setattr(server.FileResource, "read", must_not_read)
    async with Client(mcp) as client:
        with pytest.raises(Exception, match=f"/artifacts/{folder.name}/big.parquet"):
            await client.read_resource(f"artifact://{folder.name}/big.parquet")


def test_a_file_deleted_while_artifacts_are_listed_is_left_out(artifact_root, monkeypatch):
    artifacts = server.Artifacts(artifact_root, 86400)
    folder = finished_run(artifact_root, "kept.txt")
    monkeypatch.setattr(artifacts, "_names", lambda run: iter(["gone.txt", "kept.txt"]))
    assert [item["name"] for item in artifacts.describe(folder.name)] == ["kept.txt"]


async def test_bad_artifact_uris_are_refused(mcp, artifact_root, tmp_path):
    folder, other = finished_run(artifact_root, "a.bin"), finished_run(artifact_root, "b.bin")
    (tmp_path / "secret.txt").write_text("secret")
    uris = [
        "artifact://notahexrunid/a.bin",
        f"artifact://{folder.name}/missing.bin",
        f"artifact://{folder.name}/../{other.name}/b.bin",
        f"artifact://{folder.name}/%2e%2e/%2e%2e/secret.txt",
        f"artifact://{folder.name.upper()}x/a.bin",
    ]
    try:
        (folder / "link.txt").symlink_to(tmp_path / "secret.txt")
        uris.append(f"artifact://{folder.name}/link.txt")
    except OSError:
        pass  # Windows without the symlink privilege
    async with Client(mcp) as client:
        for uri in uris:
            with pytest.raises(Exception):
                contents = await client.read_resource(uri)
                pytest.fail(f"{uri} returned {contents}")
        assert "link.txt" not in {r.name for r in await client.list_resources()}


async def test_write_and_edit_cannot_change_an_artifact(mcp, artifact_root):
    folder = finished_run(artifact_root, "a.txt")
    with pytest.raises(ToolError, match="cannot be written or edited"):
        await call(mcp, "write", path=str(folder / "a.txt"), content="changed")
    with pytest.raises(ToolError, match="cannot be written or edited"):
        await call(mcp, "edit", path=str(folder / "a.txt"), old_string="x", new_string="y")
    assert (folder / "a.txt").read_bytes() == b"x"


def test_sweep_deletes_only_expired_finished_runs(tmp_path):
    root = tmp_path / "artifacts"
    artifacts = server.Artifacts(root, ttl=100)
    expired = old_run(root, "a.bin")
    fresh = finished_run(root)
    active = old_run(root, "a.bin")
    artifacts.active.add(active.name)
    not_a_run = root / "keep-me"
    not_a_run.mkdir()
    os.utime(not_a_run, (1, 1))
    assert artifacts.sweep() == 1
    assert not expired.exists() and fresh.exists() and active.exists() and not_a_run.exists()


def test_a_run_in_progress_is_not_served(tmp_path):
    artifacts = server.Artifacts(tmp_path / "artifacts", ttl=100)
    run = artifacts.start()
    (artifacts.root / run / "part.bin").write_bytes(b"x")
    assert artifacts.locate(run, "part.bin") is None and artifacts.all() == []
    artifacts.finish(run)
    assert artifacts.locate(run, "part.bin") and artifacts.all() == [(run, "part.bin")]


def request_artifact(base, path, method="GET", token=TOKEN, headers=None):
    # http.client, because urllib and other clients remove ".." before sending a path.
    host = base.removeprefix("http://").removesuffix("/mcp")
    conn = http.client.HTTPConnection(host, timeout=30)
    sent = dict(headers or {})
    if token:
        sent["Authorization"] = f"Bearer {token}"
    try:
        conn.request(method, path, headers=sent)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


async def test_http_download_and_cleanup_require_the_token(http_server, work, artifact_root):
    _, url = http_server
    (work / "make.py").write_text(WRITE_ARTIFACT)
    async with Client(url, auth=BearerAuth(TOKEN)) as client:
        item = (await client.call_tool("execute", {"target": "make.py"})).data["artifacts"][0]
    path = item["download_path"]
    run = path.split("/")[2]
    other = old_run(artifact_root, "other.bin")

    for token in (None, "wrong"):
        assert request_artifact(url, path, token=token)[0] == 401
        assert request_artifact(url, "/artifacts/cleanup", "POST", token=token)[0] == 401
    assert other.exists()

    assert request_artifact(url, path) == (200, ARTIFACT_BYTES)
    assert request_artifact(url, path, headers={"Range": "bytes=10-19"}) == (206, ARTIFACT_BYTES[10:20])
    for bad in (f"/artifacts/{run}/../{other.name}/other.bin", f"/artifacts/{run}/%2e%2e/{other.name}/other.bin", "/artifacts/notahexrunid/a.bin", f"/artifacts/{run}/missing.bin"):
        assert request_artifact(url, bad)[0] == 404, bad

    status, body = request_artifact(url, "/artifacts/cleanup", "POST")
    assert (status, json.loads(body)) == (200, {"deleted": 1})
    assert not other.exists() and request_artifact(url, path)[0] == 200


def wait_until_gone(path, seconds=10):
    deadline = time.time() + seconds
    while path.exists() and time.time() < deadline:
        time.sleep(0.1)
    return not path.exists()


def test_server_timer_deletes_expired_artifacts(prompt, work, artifact_root):
    expired = old_run(artifact_root, "a.bin")
    proc, _ = start_server(prompt, work, artifact_root, "--artifact-ttl", "1s")
    try:
        assert wait_until_gone(expired)
        # Created after the first sweep, so only a later sweep can delete it.
        assert wait_until_gone(old_run(artifact_root, "b.bin"))
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("timeout", ["inf", "-inf", "nan"])
async def test_execute_rejects_nonfinite_timeout(mcp, work, timeout):
    with pytest.raises(ToolError, match="finite"):
        await call(mcp, "execute", target="echo must-not-run", timeout=timeout)
