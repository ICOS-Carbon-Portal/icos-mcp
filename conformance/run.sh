#!/bin/sh
# The MCP conformance suite, pinned (package-lock.json), against a LOCAL
# icos-mcp it starts and stops itself -- docs/plan.md, Quality 4.  Exits
# non-zero on a failure not in baseline.yml, or on a baseline entry that
# now passes.
#
#     conformance/run.sh                 # port 8097, results in conformance/results/
#     PORT=9000 conformance/run.sh
#
# Never point it at CT122's production server from inside CT122: run it
# against a server of its own.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
port=${PORT:-8097}
url=http://127.0.0.1:$port

cd "$here"
[ -d node_modules ] || npm ci --silent

(cd "$here/.." && exec uv run icos-mcp --transport streamable-http --port "$port") \
    > "$here/server.log" 2>&1 &
server=$!
trap 'kill $server 2>/dev/null' EXIT INT TERM

i=0
until curl -sf "$url/health" > /dev/null; do
    i=$((i + 1))
    [ $i -lt 60 ] || { echo "icos-mcp did not start; see $here/server.log" >&2; exit 2; }
    sleep 0.5
done

npx conformance server --url "$url/mcp" --suite active \
    --spec-version 2025-11-25 --expected-failures baseline.yml \
    -o results
