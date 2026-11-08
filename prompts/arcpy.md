# ArcPy execution environment

This server is based on ArcPy, on a remote Windows machine with ArcGIS Pro. Your job is to generate Python code with ArcPy, execute it on that machine, and answer from the results. Your code can also use NumPy, Pandas and the other libraries in this Python environment. Files and code exist on the remote machine, not on yours.

## Machine details

Edit this section for your machine.

- Interpreter: `C:\path\to\mcp-rwee\.venv\Scripts\python.exe`
- Data folders: `C:\path\to\data`

## How to answer a question

1. Generate a Python script that gets what the question needs.
2. Save it with `write`, and run it with `execute`.
3. If execution fails, use the traceback in `stderr`. Correct the script with `edit`, and run it again. Change the code on each retry.
4. If the output does not answer the question, change the script and run it again.
5. Answer from the output. Do not state a result that the output does not show.

## Rules for scripts

- Only printed output comes back. Output over 100,000 bytes is cut from the start, so print summaries of large results.
- Call `arcpy.ResetEnvironments()` and then set `arcpy.env.workspace` and other `arcpy.env` values at the top of each script. Values from an earlier run persist.
- Use `with` for cursors, or delete them. A cursor that stays open keeps its lock on the data.
- The worker keeps a `.lock` file on a workspace between runs. If another process must change that data, call `arcpy.management.ClearWorkspaceCache()` at the end of the script.
- `in_memory` data stays between runs and uses memory. Call `arcpy.management.Delete("in_memory")` when you no longer need it.
- `read` returns UTF-8 text only. Write a plot or other binary file to the artifact folder.
- Each run starts with new script globals.

## What `execute` does

An existing `.py` file runs in a long-lived Python worker. Installed modules such as `arcpy` stay imported between runs, so `arcpy` is fast after the first run. Helper modules in the script folder or workdir load again on each run, so do not keep state in them. Only one script runs at a time. If `execute` returns a "busy" error, wait and try again. To get a clean worker, run a script that contains `import os; os._exit(0)`. The result then shows `crashed` as true, and this is normal.

- `crashed` is true when the script stopped the worker. The server restarts it for the next run. `stderr` can hold the Python stack. A script exception does not set `crashed`: `exit_code` is 1, and `stderr` holds the traceback.
- `timed_out` is true when the script ran longer than `timeout` seconds (default 300). The worker restarts for the next run. Pass a larger `timeout` for long geoprocessing.

Any other `target` runs as a shell command in a new process. For `robocopy`, an exit code below 8 is not a failure. Use a shell command for a script that uses `multiprocessing`, with the interpreter above.

## Artifacts

Each run gets an empty folder in the environment variable `RWEE_ARTIFACT_DIR`, for any language. Run TypeScript as a shell command, for example `node my_script.mts`. `artifacts` in the result has one item for each file.

- For a file of 12,000,000 bytes or less, read the resource at `uri` with your MCP resource tool. Claude Code saves it with a `.bin` name, so copy it to the correct name.
- For a larger file, in a Claude Code session started with `remote.sh`, run `./remote.sh fetch <download_path> <local file>`. Otherwise, send `Authorization: Bearer <token>` with an HTTP GET to the server address and `download_path`.
- Compare the `sha256` value. After a timeout or crash, a file can be incomplete.

Artifacts are read-only. The server deletes them after a time, so get them soon.
