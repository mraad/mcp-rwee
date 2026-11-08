#!/usr/bin/env bash
# Start, stop or check the rwee server on a remote Windows machine from this client, or download an artifact.
# Needs key login over SSH, the install from docs/index.html section 6, and the claude CLI.
set -euo pipefail

usage() {
    echo "usage: $0 [-q] start|stop|status [ssh-host]" >&2
    echo "       $0 [-q] fetch <download_path> <local file> [ssh-host]" >&2
    echo "  -q: show only errors and warnings. status still shows its result." >&2
    echo "  host: ssh-host or \$RWEE_HOST; port: \$RWEE_PORT or 8765" >&2
    exit 2
}

QUIET=
for arg; do
    shift
    case $arg in
        -q) QUIET=1 ;;
        -*) usage ;;
        *) set -- "$@" "$arg" ;;
    esac
done

CMD=${1:-}
if [ "$CMD" = fetch ]; then
    [ $# -ge 3 ] || usage
    SOURCE=$2
    case $3 in
        /*) TARGET=$3 ;;
        *) TARGET=$PWD/$3 ;;  # before the cd below, so a relative file is where the caller expects
    esac
    shift 2
fi
HOST=${2:-${RWEE_HOST:-}}
[ -n "$HOST" ] || usage
PORT=${RWEE_PORT:-8765}
URL=http://127.0.0.1:$PORT/mcp
# Has the host because "ssh -O" does not check it: another host's tunnel must not count as this one's.
SOCK=$HOME/.ssh/rwee-tunnel-$HOST-$PORT

# --scope local ties the registration to the current folder.
cd "$(dirname "$0")"

say() { [ -n "$QUIET" ] || echo "$*"; }
die() { echo "error: $*" >&2; exit 1; }
remote() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }

# Prints the PID to kill, "other:<pid>" if something else holds the port, or nothing.
# The venv launcher is the parent of the real server, and only killing it stops both.
# -EncodedCommand because $ and quotes do not survive the trip through ssh and cmd.
server_pid() {
    local script="\$ProgressPreference='SilentlyContinue'
\$c = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not \$c) { exit 0 }
\$p = Get-CimInstance Win32_Process -Filter \"ProcessId=\$(\$c.OwningProcess)\"
if (\$p.CommandLine -notmatch 'server\.py.*--prompt') { \"other:\$(\$c.OwningProcess)\"; exit 0 }
\$q = Get-CimInstance Win32_Process -Filter \"ProcessId=\$(\$p.ParentProcessId)\"
if (\$q.CommandLine -match 'server\.py.*--prompt') { \$q.ProcessId } else { \$p.ProcessId }"
    remote "powershell -NoProfile -NonInteractive -EncodedCommand $(printf '%s' "$script" | iconv -f UTF-8 -t UTF-16LE | base64 | tr -d '\n')" | tr -d '\r'
}

tunnel_open() { ssh -S "$SOCK" -O check "$HOST" >/dev/null 2>&1; }
open_tunnel() {
    if tunnel_open; then
        say "==> SSH tunnel is already open"
        return
    fi
    say "==> Opening SSH tunnel: 127.0.0.1:$PORT here to 127.0.0.1:$PORT on $HOST"
    ssh -f -N -M -S "$SOCK" -o BatchMode=yes -o ConnectTimeout=10 \
        -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
        -L "127.0.0.1:$PORT:127.0.0.1:$PORT" "$HOST"
}
# Not grep -q: closing the pipe early would fail the pipeline under pipefail.
rwee_line() { claude mcp list 2>/dev/null | grep '^rwee:' || true; }
# "Disabled" is a per-project choice made in /mcp. It outlives "claude mcp remove", and the CLI cannot clear it.
DISABLED_HELP="warning: rwee is disabled for this project. In Claude Code, run /mcp and enable rwee."

stop() {
    local rc=0 pid
    say "==> Looking for the server on port $PORT of $HOST"
    if ! pid=$(server_pid); then
        echo "server: cannot reach $HOST, not stopped" >&2
        rc=1
    elif [[ $pid == other:* ]]; then
        echo "server: port $PORT is used by another process (PID ${pid#other:}), not stopped" >&2
        rc=1
    elif [ -n "$pid" ]; then
        say "==> Stopping server process tree, PID $pid"
        # The check below decides, because the process can exit before taskkill runs.
        remote "taskkill /F /T /PID $pid" >/dev/null || true
        say "==> Checking that port $PORT is free"
        if pid=$(server_pid) && [ -z "$pid" ]; then say "server: stopped"; else echo "server: still running or unknown" >&2; rc=1; fi
    else
        say "server: not running"
    fi
    say "==> Closing SSH tunnel"
    ssh -S "$SOCK" -O exit "$HOST" >/dev/null 2>&1 || true
    tunnel_open && { echo "tunnel: still open" >&2; rc=1; } || say "tunnel: closed"
    say "==> Removing rwee registration, which also removes the token from ~/.claude.json"
    # The exit code cannot decide: remove also fails when there is nothing to remove.
    claude mcp remove rwee -s local >/dev/null 2>&1 || true
    if [ -z "$(rwee_line)" ]; then say "registration: removed"; else echo "registration: still present in ~/.claude.json" >&2; rc=1; fi
    return $rc
}

start() {
    local pid token code client
    say "==> Checking port $PORT on $HOST"
    pid=$(server_pid)
    [[ $pid != other:* ]] || die "port $PORT on $HOST is used by another process (PID ${pid#other:})"
    # First, so that a busy local port fails before anything starts on the remote machine.
    open_tunnel

    if [ -n "$pid" ]; then
        say "==> Server is already running, PID $pid. Checking the registration"
        case $(rwee_line) in
            *Connected*) say "server: already running (PID $pid), tunnel open, rwee connected" ;;
            *Disabled*) say "server: already running (PID $pid), tunnel open"; echo "$DISABLED_HELP" >&2 ;;
            *) die "server PID $pid runs with a token this client does not have. Run: $0 stop $HOST" ;;
        esac
        return
    fi

    say "==> Checking workdir C:\\work and prompt file C:\\work\\arcpy-prompt.md on $HOST"
    remote 'if not exist C:\work mkdir C:\work'
    if [ -z "$(remote 'if exist C:\work\arcpy-prompt.md echo yes')" ]; then
        scp -q -o BatchMode=yes prompts/arcpy.md "$HOST:C:/work/arcpy-prompt.md"
        echo "warning: copied prompts/arcpy.md. Edit Machine details in C:\\work\\arcpy-prompt.md." >&2
    fi

    say "==> Making a new token (not shown)"
    # The token travels on stdin, so it is never in a remote file or a remote command line.
    token=$(openssl rand -hex 32)
    say "==> Starting server on $HOST, sending the token over SSH stdin"
    printf '%s\n' "$token" | remote "cd /d %USERPROFILE%\\mcp-rwee && set /p MCP_RWEE_TOKEN= && .venv\\Scripts\\python.exe server.py --prompt C:\\work\\arcpy-prompt.md --workdir C:\\work --port $PORT > C:\\work\\rwee-server.log 2>&1" >/dev/null 2>&1 &
    client=$!

    say "==> Waiting for the server: a request with no token must return 401"
    code=$(curl -s -o /dev/null -w '%{http_code}' --retry 30 --retry-delay 2 --retry-all-errors --max-time 10 -X POST "$URL" || true)
    # The server outlives its SSH session, so this client is not needed after startup.
    { kill "$client"; wait "$client"; } 2>/dev/null || true
    if [ "$code" != 401 ]; then
        echo "error: a request with no token returned $code, expected 401. Server log:" >&2
        remote 'type C:\work\rwee-server.log' >&2 || true
        stop || true
        exit 1
    fi
    say "==> Server answered 401"

    say "==> Registering rwee at local scope for $PWD"
    # shortcut: the token is in the local process list while this runs, the CLI has no other input.
    claude mcp remove rwee -s local >/dev/null 2>&1 || true
    if ! claude mcp add --transport http --scope local rwee "$URL" --header "Authorization: Bearer $token" >/dev/null 2>&1; then
        echo "error: claude mcp add failed" >&2
        stop || true
        exit 1
    fi
    unset token
    say "==> Setting ~/.claude.json to mode 600"
    chmod 600 "$HOME/.claude.json"

    say "==> Checking the connection with claude mcp list"
    case $(rwee_line) in
        *Connected*) ;;
        *Disabled*) echo "$DISABLED_HELP" >&2 ;;
        *)
            echo "error: rwee does not show Connected" >&2
            stop || true
            exit 1
            ;;
    esac
    if [ -z "$QUIET" ]; then
        echo "server: running on $HOST (PID $(server_pid)), log C:\\work\\rwee-server.log"
        echo "tunnel: $URL"
        echo "registration: rwee, local scope, $PWD"
        echo "The token is in plain text in ~/.claude.json (mode 600). Start a new Claude Code session here to get the tools."
    fi
}

status() {
    local pid
    say "==> Asking $HOST about port $PORT"
    pid=$(server_pid) || pid=unreachable
    case $pid in
        unreachable) echo "server: cannot reach $HOST" ;;
        other:*) echo "server: not running, port $PORT is used by another process (PID ${pid#other:})" ;;
        '') echo "server: not running on $HOST" ;;
        *) echo "server: running on $HOST (PID $pid)" ;;
    esac
    if tunnel_open; then echo "tunnel: open"; else echo "tunnel: closed"; fi
    say "==> Asking claude mcp list about rwee"
    echo "registration: $(rwee_line | grep . || echo none)"
}

fetch() {
    local auth part
    case $SOURCE in
        /artifacts/*) ;;
        *) die "$SOURCE is not a download_path. It must start with /artifacts/" ;;
    esac
    [ ! -e "$TARGET" ] || die "$TARGET already exists"
    tunnel_open || die "no tunnel to $HOST. Run: $0 start $HOST"
    say "==> Reading the token from the rwee registration for $PWD (not shown)"
    auth=$(python3 -c '
import json, os, sys
config = json.load(open(os.path.expanduser("~/.claude.json")))
print(config["projects"][sys.argv[1]]["mcpServers"]["rwee"]["headers"]["Authorization"])
' "$PWD" 2>/dev/null) || die "no rwee registration for $PWD. Run: $0 start $HOST"
    part="$TARGET.part"
    say "==> Downloading $SOURCE to $TARGET"
    # The token travels on stdin, so it is not in the local process list.
    if ! printf 'header = "Authorization: %s"\n' "$auth" | curl -fsS -K - -o "$part" "http://127.0.0.1:$PORT$SOURCE"; then
        rm -f "$part"
        die "download failed"
    fi
    mv "$part" "$TARGET"
    echo "saved: $TARGET"
    echo "sha256: $(shasum -a 256 "$TARGET" | cut -d' ' -f1)"
}

case $CMD in
    start | stop | status | fetch) "$CMD" ;;
    *) usage ;;
esac
