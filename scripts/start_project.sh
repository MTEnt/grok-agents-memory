#!/usr/bin/env sh
set -eu

AGENT="project-founder"
SKIP_DOCTOR=0
CHECK_ONLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --agent)
      shift
      if [ "$#" -eq 0 ]; then
        echo "--agent requires a value" >&2
        exit 2
      fi
      AGENT="$1"
      ;;
    --skip-doctor)
      SKIP_DOCTOR=1
      ;;
    --check-only)
      CHECK_ONLY=1
      ;;
    -h|--help)
      echo "Usage: sh scripts/start_project.sh [--check-only] [--skip-doctor] [--agent NAME]"
      exit 0
      ;;
    *)
      AGENT="$1"
      ;;
  esac
  shift
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$ROOT_DIR"

PY_EXE=""
PY_ARGS=""

if [ -n "${PYTHON:-}" ] && "$PYTHON" -c "import sqlite3" >/dev/null 2>&1; then
  PY_EXE="$PYTHON"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import sqlite3" >/dev/null 2>&1; then
  PY_EXE="python3"
elif command -v python >/dev/null 2>&1 && python -c "import sqlite3" >/dev/null 2>&1; then
  PY_EXE="python"
elif command -v py >/dev/null 2>&1 && py -3 -c "import sqlite3" >/dev/null 2>&1; then
  PY_EXE="py"
  PY_ARGS="-3"
else
  echo "Python 3 is required so the SQLite memory layer can run." >&2
  exit 1
fi

if ! command -v grok >/dev/null 2>&1; then
  echo "Grok CLI is required. Install it and run 'grok login' first." >&2
  exit 1
fi

"$PY_EXE" $PY_ARGS scripts/ensure_memory.py

if [ "$SKIP_DOCTOR" -eq 0 ]; then
  grok mcp doctor agent_memory
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  printf '\nPreflight OK. SQLite memory and Grok MCP wiring are ready.\n'
  exit 0
fi

printf '\nStarting Grok with agent %s.\n' "$AGENT"
printf 'When the prompt opens, type: /project-kickoff\n\n'

exec grok --experimental-memory --agent "$AGENT"
