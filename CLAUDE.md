# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastMCP HTTP server with exactly four tools (`read`, `write`, `edit`, `execute`) and one prompt (`task`). A remote Claude session writes Python to a Windows machine with ArcGIS Pro, runs it there and gets the output back. The server is generic; the file passed to `--prompt` gives the client its task. Adding a fifth tool breaks a stated requirement and `test_exactly_four_tools_and_one_prompt`.

## Commands

```
uv run pytest                                  # all tests
uv run pytest -k test_name                     # one test
uv run server.py --prompt prompts/arcpy.md     # needs MCP_RWEE_TOKEN (32+ chars); --workdir --host --port --ssl-certfile --ssl-keyfile --artifact-dir --artifact-ttl
./remote.sh [-q] start|stop|status [ssh-host]  # from the client: remote server, SSH tunnel and local registration
./remote.sh [-q] fetch <download_path> <file> [ssh-host]  # download a large artifact through the tunnel
```

There is no lint or build step. `uv run` does not provide arcpy; on Windows, use the setup in `docs/index.html` section 6 and start the server with `.venv\Scripts\python.exe`.

Tests are async through the anyio plugin (`anyio_backend` fixture). The `http_server` fixture starts a real server process. The server's default port is 8000, but the manual and the Windows test runs use 8765.

## Architecture

Two processes, and the boundary between them is the main design decision:

- `server.py` holds the network connection, the token check and the file tools. `ScriptRunner` owns the worker: it starts it on demand, sends one job at a time and stops it on timeout or client disconnect.
- `worker.py` runs `.py` files on its main thread with `runpy`, and stays alive between runs so arcpy stays imported. A crash here must not reach the server. It is standard library only, so FastMCP's dependencies do not load beside arcpy.

`execute` routes on `target`: an existing `.py` file goes to the worker; anything else is a shell command in a new process, so `"python.exe" x.py` is shell mode. Shell mode is how a script gets its own process, for example with `multiprocessing`.

Jobs and results are JSON lines on private duplicates of the worker's stdin and stdout. Script output goes to per-run temp files through `dup2` on fds 1 and 2, so native and child-process output is captured and output cannot corrupt the channel. The server reads only the end of each file, so a flood cannot exhaust its memory.

Each `execute` gets a run folder under `--artifact-dir`, in `RWEE_ARTIFACT_DIR`. `Artifacts` owns these folders, `ArtifactProvider` lists and reads them as MCP resources, and two token-checked routes download a file and delete expired folders. A lifespan task also deletes expired folders.

The prompt file reaches the client in two ways. `instructions` is read once in `build()`, so a change needs a server restart. The `task` prompt reads the file on each request and adds the question.

## Constraints that are not obvious from the code

- faulthandler must keep its own file. On Windows it reports exceptions that native code handles, and the first `import arcpy` writes about 680 KB of these on a run that succeeds. The server reads that file only when `crashed` is true.
- The worker's pipe-reading thread is what stops a running script and its child processes when the server dies, including a hard kill where exit handlers do not run. Do not replace it with `atexit`.
- `-B` and the `sys.modules` cleanup in `run_script` keep edit-and-rerun correct. Without them a helper edited within the same second can run old code. The cleanup must skip `sys.prefix` and `sys.base_prefix`, because C extensions in a `.venv` under the workdir cannot load twice.
- Capture files need `O_BINARY`, or Windows gives `\r\r\n`.
- `ctypes.string_at(0)` does not crash on Windows; ctypes raises `OSError`. Crash tests use `faulthandler._sigsegv()`. `os.kill(pid, 0)` on Windows sends Ctrl+C, so use `tasklist`.
- Children get `CHILD_ENV`, not `os.environ`, so the token is not in their environment.
- A non-local `--host` without a certificate must stay refused: plain HTTP would expose the token. `--allowed-host` was removed on purpose, because FastMCP's Host check is off by default (`http_host_origin_protection`), so it had no effect.
- `run_script` joins non-daemon threads, as Python does at exit. Tested with arcpy on `Pro37`; without it, thread output is lost or lands in a later run.
- On Windows `_run_shell` runs `chcp 65001` in one `cmd` and the command in a second, because `_tail` decodes UTF-8 and `echo` keeps the code page its `cmd` started with. The command travels in `RWEE_COMMAND` so that only the last `cmd` parses it. `chcp` stays set in a shared console, so a test of this must set `chcp 437` first.
- FastMCP finds the user on a `custom_route` but does not refuse a request with no token. A test route returned a file with no token. Register each route with `token_route`. `requires("authenticated")` does not work here, because the token has no scopes.
- A blob resource over 16 MB after base64 is rejected by Claude Code, so `MAX_ARTIFACT_READ` is 12,000,000 bytes and is checked before the read. A 300 MB read used about 2 GB of server memory.
- The `sweep` rule that a folder name must be a run id protects other files in a shared `--artifact-dir`. Keep it.
- On protocol 2026-07-28 a list change reaches a client only on `subscriptions/listen`. FastMCP 4.1 does not serve it, so `build` adds `ListenHandler` through `_mcp_server`. Claude Code 2.1.319 opens the stream, but its `ListMcpResourcesTool` in `claude -p` still shows the list from connect. Tested with `--debug-file`.
- `ctx.send_notification` is still needed: clients on the older protocol do not use the listen stream.
- `describe` must not remove a run from `active`. A sweep could then delete a long run before `finish` sets its time.
- `prompts/northsea-duckdb.md` runs on the Mac with `uv run --with duckdb`, because the Windows machines do not have the database, and ArcGIS Pro has DuckDB 1.3.2. Manual section 8.2 has the distance and `ParquetLayer` results.
- Claude Code cuts server instructions after 4,096 characters, and the client sees only the text before the cut. `test_prompt_fits_the_client_limit_with_machine_details_first` enforces this for the prompt files it names, so add a new prompt file to its list. Detail goes in a file that the prompt tells the client to `read`, for example `prompts/northsea-duckdb-rules.md`, which has no limit. A test session on a machine that has the repo can hide this, because the client can find the file on disk, so read its transcript.
- A `ParquetLayer` test must check pixels, not counts, because the layer can report all features and draw nothing. A screenshot just after `drawn` can still be blank, so wait for the view.
- `.venv\conda-meta` must exist on Windows, or arcpy warns on import. `uv venv --clear` removes it.
- The `task` prompt declares `question` and `w2` to `w100` because Claude Code splits the command text on whitespace, gives one word to each declared argument and drops the rest, so quotes do not help. Tested with 2.1.319. To see what a real command sends, run `claude -p '/mcp__rwee__task ...'` and read its session transcript.

## Testing on Windows with real arcpy

macOS tests pass without finding Windows faults, so test any change to `server.py` or `worker.py` on the Parallels VM `Pro37` (ArcGIS Pro 3.7.3). The install is at `%USERPROFILE%\mcp-rwee`, and `\\Mac\Home` is the Mac home folder.

- Use `prlctl exec Pro37 --current-user cmd /c ...`. Without `--current-user` the command runs as `SYSTEM`, and arcpy fails with "The Product License has not been initialized."
- Copy the project with `robocopy`, excluding `.venv` and `.git`, then run `.venv\Scripts\python.exe -m pytest -q`. Do not put a venv on the `\\Mac` share.
- Other projects run `python.exe` on this VM. Stop the server by the PID on its port, never by image name.

The second test machine is a physical x64 machine with ArcGIS Pro 3.7.3, reached with `ssh -o BatchMode=yes $RWEE_HOST` (key login, elevated session). Its host name is in the local Claude memory, not in this repo. The install is at `%USERPROFILE%\mcp-rwee`. Copy with `git archive HEAD | ssh $RWEE_HOST "tar -xf - -C %USERPROFILE%\mcp-rwee"`.

- Closing the SSH session does not stop a server started in it. Use `taskkill /F /T` on the venv launcher's PID, then confirm that port 8765 is free and remove the token.
- For a network test, bind to `127.0.0.1` and use `ssh -o ExitOnForwardFailure=yes -N -L`. A separate tunnel session lets a test drop the tunnel without stopping the server. Auto mode denied a server start on `0.0.0.0`.
- `disabledMcpServers` in the project's entry in `~/.claude.json` survives `claude mcp remove`. A new registration then shows `Disabled for this project`, and only `/mcp` re-enables it from the CLI side.
- A Claude Code session does not load a server registered after it started. Use a new session, or a scripted FastMCP client.
- `$` in a PowerShell command over SSH is expanded by the local shell. Use `-EncodedCommand`.
- `net session` reported this elevated session as not elevated. Use `whoami /groups` to check.
- FastMCP `client.ping()` returns `Method not found` against this server, so wait for readiness with `list_tools()`.

## Documentation

- `docs/index.html` is the manual, written in the spirit of ASD STE100: short direct sentences, one action per procedural step, one word for each meaning (the terms are in its section 1). Every number comes from a recorded test run.
- It is self-contained: the map is embedded as base64 and section 8.1 holds a copy of `prompts/arcpy.md`. Update that copy when the prompt changes. Section 8.2 has a copy of the `ParquetLayer` code in `prompts/northsea-duckdb.md`.
- `README.md` is a short summary on purpose. Put details in the manual, not in both. `AGENTS.md` is a symlink to it, so edit `README.md` only.
- It is also published at https://claude.ai/artifact/KLgzwHegyiftJxNy84Vots. Build the copy from the `<style>` block and the `<body>` content, without the doctype, `html`, `head` and `body` tags. Keep the title, viewport meta and font link from the previous copy. Replace `�` with `&#xFFFD;`, or the publish is refused.
- Base64 images fill one line in section 9 (about 138,000 tokens) and two lines in section 15.8. A `Read` range that includes one fails, and a plain `grep` can print them. Read around them, or cap the output: `awk 'length($0)<3000 && /pattern/ {print FNR": "$0}' docs/index.html`.
- Test counts appear in the status table near the top and in section 9.1. Procedures refer to each other by section and step number, for example "steps 1 to 5 in section 7.2", so check those references after you renumber.
