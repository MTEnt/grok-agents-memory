#!/usr/bin/env python3
"""Ensure the local SQLite memory database exists and is seeded.

This is safe to run before every Grok launch. SQLite is not a daemon, so there
is no database service to start; creating/opening the file is the start-up step.
Grok starts the MCP server process itself from `.grok/config.toml`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

from sqlite_memory_server import MemoryStore  # noqa: E402


DB_PATH = ROOT / ".grok" / "memory" / "agent_memory.sqlite"


def memory_exists(store: MemoryStore, title: str) -> bool:
    row = store.conn.execute("SELECT 1 FROM memories WHERE title = ? LIMIT 1", (title,)).fetchone()
    return row is not None


def ensure_installation_memory(store: MemoryStore) -> bool:
    title = "SQLite memory layer installed"
    if memory_exists(store, title):
        return False
    store.memory_write(
        scope="project",
        kind="system",
        title=title,
        content=(
            "This project uses the project-scoped Grok MCP server named "
            "agent_memory. Memories are persisted in .grok/memory/"
            "agent_memory.sqlite via Python sqlite3. Agents must not claim "
            "memory was saved unless the MCP memory_write call succeeds."
        ),
        tags=["sqlite", "mcp", "memory", "bootstrap"],
        importance=5,
        source="ensure_memory.py",
    )
    return True


def ensure_kickoff_memory(store: MemoryStore) -> bool:
    title = "Project kickoff skill installed"
    if memory_exists(store, title):
        return False
    store.memory_write(
        scope="project",
        kind="system",
        title=title,
        content=(
            "The project has a /project-kickoff skill. In empty projects, Grok "
            "must ask six short interview questions, save a project brief to "
            "SQLite, then coordinate the selected persistent agent team."
        ),
        tags=["kickoff", "interview", "agents", "sqlite"],
        importance=5,
        source="ensure_memory.py",
    )
    return True


def ensure_job_board_memory(store: MemoryStore) -> bool:
    title = "SQLite job board installed"
    if memory_exists(store, title):
        return False
    store.memory_write(
        scope="project",
        kind="system",
        title=title,
        content=(
            "The MCP server exposes a real SQLite job board. Agents must call "
            "context_pack_get at startup, claim jobs with job_claim, append "
            "progress with job_report or job_append_event, and finish with "
            "job_complete or job_block."
        ),
        tags=["job-board", "coordination", "agents", "sqlite"],
        importance=5,
        source="ensure_memory.py",
    )
    return True


def main() -> int:
    try:
        sqlite3.connect(":memory:").close()
    except Exception as exc:
        print(f"Python is available, but sqlite3 is not working: {exc}", file=sys.stderr)
        return 1

    existed_before = DB_PATH.exists()
    store = MemoryStore(DB_PATH)
    try:
        seeded = store.seed_defaults()
        install_created = ensure_installation_memory(store)
        kickoff_created = ensure_kickoff_memory(store)
        job_board_created = ensure_job_board_memory(store)
        stats = store.stats()
    finally:
        store.close()

    result = {
        "ok": True,
        "db_path": str(DB_PATH),
        "db_existed_before": existed_before,
        "agents_created": seeded["agents_created"],
        "default_project_created": seeded["default_project_created"],
        "installation_memory_created": install_created,
        "kickoff_memory_created": kickoff_created,
        "job_board_memory_created": job_board_created,
        "stats": stats,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
