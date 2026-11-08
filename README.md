# mcp-rwee

An MCP server with four tools (`read`, `write`, `edit`, `execute`) and one prompt. A remote Claude session writes Python to a machine with ArcGIS Pro, runs it there, and gets the answer back.

Python files run in a long-lived worker process, so `arcpy` stays imported between runs, and a crash or timeout cannot stop the server.

**Full manual: open [`docs/index.html`](docs/index.html).** It has setup steps, diagrams, test results, security notes, troubleshooting and a complete worked example on a real project (section 15). Tested with ArcGIS Pro 3.7.3 and Python 3.13.13 on Windows 11 on Arm and on Windows on x64 hardware.

Related approach: [Code Mode](https://www.philschmid.de/code-mode), by Philipp Schmid. The manual compares the two in section 2.2.

## Quick start

On a Windows machine with ArcGIS Pro, in Command Prompt:

```bat
git clone https://github.com/mraad/mcp-rwee.git
cd mcp-rwee
uv venv --python "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" --system-site-packages
uv sync
mkdir .venv\conda-meta
mkdir C:\work
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
set MCP_RWEE_TOKEN=<the value printed above>
.venv\Scripts\python.exe server.py --prompt prompts\arcpy.md --workdir C:\work --port 8765
```

Before you start the server, edit the "Machine details" section of `prompts\arcpy.md`. The server listens on `127.0.0.1` only.

## Connect from another machine

The simplest way is an SSH tunnel. It needs SSH access to the Windows machine (manual section 7.6), but no certificate and no firewall rule for the server port.

1. On the client, open the tunnel and keep it open:

   ```
   ssh -o ExitOnForwardFailure=yes -N -L 127.0.0.1:8765:127.0.0.1:8765 <user>@<host>
   ```

   `<user>` is the Windows login name. Keep `ExitOnForwardFailure=yes`. Without it, SSH can stay open with no tunnel if a different process uses the port, and the client then sends the token to that process.

2. Make `mcp.json`:

   ```json
   {"mcpServers": {"rwee": {"type": "http",
     "url": "http://127.0.0.1:8765/mcp",
     "headers": {"Authorization": "Bearer ${MCP_RWEE_TOKEN}"}}}}
   ```

3. Start Claude Code:

   ```
   MCP_RWEE_TOKEN=<token> claude --mcp-config mcp.json
   ```

To start the server from the client, use manual section 7.10. To keep the server in Claude Code without `--mcp-config`, use section 7.11. That method writes the token as plain text in `~/.claude.json`, and a session that is already open does not get the new tools.

To do all of this with one command from a Mac or Linux client, use `remote.sh` (manual section 7.12). It starts the server, opens the tunnel and registers `rwee` for this folder. `stop` removes all three. It shows each step. With `-q`, it shows only errors and warnings, and `status` still shows its result.

```
./remote.sh [-q] start [ssh-host]     # host: argument or $RWEE_HOST; port: $RWEE_PORT or 8765
./remote.sh [-q] status [ssh-host]
./remote.sh [-q] stop [ssh-host]
./remote.sh [-q] fetch <download_path> <local file> [ssh-host]
```

For a direct connection, use a TLS certificate and a firewall rule (manual sections 7.2 and 7.8). The server refuses a non-local `--host` without a certificate. The direct TLS path passed on the Parallels network, but not between two physical machines, and the firewall steps are not tested.

## Artifacts

Code that `execute` runs can write files to the folder in `RWEE_ARTIFACT_DIR`, and the result lists them in `artifacts`. Clients list and read these files as read-only MCP resources, up to 12,000,000 bytes. A larger file downloads from `download_path` with the token. The server deletes run folders after `--artifact-ttl` (default `24h`). See manual section 3.6.

## Ask questions about a DuckDB database

`prompts/northsea-duckdb.md` answers questions about `NorthSea.ddb`, using the descriptions in `Layers.json`, and returns GeoJSON or GeoParquet for ArcGIS layers. It needs no arcpy, so it runs on a Mac:

```
uv run --with duckdb server.py --prompt prompts/northsea-duckdb.md --workdir <folder> --artifact-dir <folder> --port 8765
```

See manual section 8.2.

## Stop the server

Closing an SSH session does not stop a server that started in it, and its token keeps working. Find the process number, which is the last value on the line, and stop it:

```bat
netstat -ano | findstr LISTENING | findstr :8765
taskkill /F /T /PID <process number>
```

**Warning:** `execute` is remote code execution by design. Anyone with the token runs code as the server's Windows user. Read section 12 of the manual before you expose the port.

## Project structure

- `server.py` contains the FastMCP HTTP server, authentication, file tools, and `ScriptRunner` worker supervision.
- `worker.py` executes Python scripts in a persistent process. Keep it standard-library-only so FastMCP dependencies do not load beside arcpy.
- `tests/test_server.py` covers tools, prompts, worker behavior, authentication, and HTTP integration.
- `prompts/arcpy.md` supplies the task instructions. `docs/index.html` is the self-contained manual, including embedded images; `README.md` is the quick start.
- `remote.sh` manages the remote server, SSH tunnel, and local Claude Code registration.

## Development

Use Python 3.11 or newer and `uv`:

- `uv sync` installs dependencies, including the pytest development group.
- `uv run pytest -k test_name` selects matching tests.
- `uv run server.py --prompt prompts/arcpy.md --workdir /path/to/work` starts locally; set `MCP_RWEE_TOKEN` to at least 32 characters first.
- `./remote.sh status [ssh-host]` checks remote operation.

There is no build step or configured formatter/linter. For arcpy, follow the Windows environment setup in the manual; `uv run` alone does not supply it.

### Coding style

Follow existing Python style: four-space indentation, `snake_case` functions and variables, `UPPER_CASE` constants, and `PascalCase` classes. Reuse existing helpers and keep changes focused. Preserve exactly four tools (`read`, `write`, `edit`, `execute`) and one prompt (`task`).

### Tests

On any machine (no arcpy):

```
uv run pytest
```

On Windows, after the quick start:

```bat
.venv\Scripts\python.exe -m pytest -q
```

Use pytest with AnyIO's asyncio backend, `test_*` functions, and temporary-path fixtures. Add regression tests for behavior changes. No numeric coverage threshold is configured. Changes to `server.py` or `worker.py` also require Windows testing with real arcpy. Consult `CLAUDE.md` for platform constraints.

### Commits and pull requests

History uses short imperative subjects, such as "Fix non-ASCII echo output in Windows shell commands"; merged subjects include PR numbers. Keep commits focused. PR descriptions should explain the behavior change, relevant issues, tests run, and platform limitations. Update the manual for user-visible changes and its embedded prompt copy when the prompt changes.

### Security and configuration

Treat `execute` as code execution with the server user's privileges. Keep tokens out of commits and child environments. Retain loopback binding by default and TLS enforcement for non-local hosts; use SSH tunnels for remote access. The work directory is not a security sandbox.

## License

MIT. See `LICENSE`.
