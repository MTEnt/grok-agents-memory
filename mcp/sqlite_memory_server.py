#!/usr/bin/env python3
"""SQLite-backed MCP memory server for Grok agent teams.

The server speaks JSON-RPC over stdio and exposes durable memory tools backed by
an on-disk SQLite database. It intentionally uses only the Python standard
library so it can run anywhere Grok can spawn `python`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVER_NAME = "sqlite-agent-memory"
SERVER_VERSION = "0.2.0"
DEFAULT_DB = Path(".grok") / "memory" / "agent_memory.sqlite"
DEFAULT_PROJECT_ID = "default"
JOB_STATUSES = {"pending", "claimed", "in_progress", "blocked", "review", "done", "cancelled"}


DEFAULT_AGENTS: list[dict[str, str]] = [
    {
        "name": "project-founder",
        "display_name": "Project Founder",
        "role": "Intake lead and multi-agent coordinator for new projects.",
        "personality": (
            "Warm, concrete, and product-minded. Asks enough questions to remove "
            "ambiguity, then turns answers into a practical project shape."
        ),
        "responsibilities": (
            "Question the user in empty projects, create the project brief, assign "
            "work to specialized agents, and keep the shared project memory clean."
        ),
    },
    {
        "name": "product-lead",
        "display_name": "Product Lead",
        "role": "User, workflow, and product-scope specialist.",
        "personality": (
            "Curious, plain-spoken, and allergic to vague value props. Cares about "
            "who uses the thing, what they need first, and what can wait."
        ),
        "responsibilities": (
            "Turn user goals into requirements, flows, acceptance criteria, and "
            "scope boundaries."
        ),
    },
    {
        "name": "architect",
        "display_name": "Architect",
        "role": "Technical architecture and risk specialist.",
        "personality": (
            "Calm, skeptical, and systems-oriented. Prefers boring durable choices "
            "unless the project truly benefits from novelty."
        ),
        "responsibilities": (
            "Choose stack shape, data boundaries, module boundaries, and migration "
            "paths. Identify risks before implementation starts."
        ),
    },
    {
        "name": "implementer",
        "display_name": "Implementer",
        "role": "Pragmatic builder.",
        "personality": (
            "Focused, careful, and hands-on. Keeps changes small, verifies behavior, "
            "and writes down what actually changed."
        ),
        "responsibilities": (
            "Make code and file changes, run checks, and update memory with real "
            "implementation outcomes."
        ),
    },
    {
        "name": "reviewer",
        "display_name": "Reviewer",
        "role": "Adversarial review and verification specialist.",
        "personality": (
            "Direct, evidence-hungry, and constructive. Looks for bugs, missing "
            "tests, unclear assumptions, and integration gaps."
        ),
        "responsibilities": (
            "Review plans, diffs, and test results. Store durable risks and resolved "
            "decisions in memory."
        ),
    },
    {
        "name": "memory-curator",
        "display_name": "Memory Curator",
        "role": "Long-term memory maintainer.",
        "personality": (
            "Precise and tidy. Separates durable facts from temporary noise and "
            "prefers short, searchable records."
        ),
        "responsibilities": (
            "Consolidate session notes, deduplicate stale memory, and keep agent "
            "histories searchable."
        ),
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_text(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [part.strip() for part in re.split(r"[,#]", tags)]
    if not isinstance(tags, list):
        raise ValueError("tags must be a string or an array of strings")
    normalized = []
    for tag in tags:
        if tag is None:
            continue
        value = str(tag).strip().lower()
        if value:
            normalized.append(value)
    return sorted(set(normalized))


def clean_name(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} is required")
    if not re.match(r"^[a-zA-Z0-9_.:-]+$", value):
        raise ValueError(f"{field} may only contain letters, numbers, dot, colon, dash, and underscore")
    return value


def clean_project_id(value: str | None = None) -> str:
    if not value:
        return DEFAULT_PROJECT_ID
    return clean_name(value, "project_id")


def normalize_json_list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a string or an array")
    return [item for item in value if item not in (None, "")]


def json_list(value: Any, field: str) -> str:
    return json.dumps(normalize_json_list(value, field))


def clean_status(status: str) -> str:
    status = status.strip().lower()
    if status not in JOB_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(JOB_STATUSES))}")
    return status


def fts_query(query: str) -> str | None:
    tokens = re.findall(r"[A-Za-z0-9_./:-]+", query)
    if not tokens:
        return None
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:12])


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.fts_enabled = True
        self._setup()

    def close(self) -> None:
        self.conn.close()

    def _setup(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                name TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                personality TEXT NOT NULL,
                responsibilities TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT,
                scope TEXT NOT NULL CHECK (scope IN ('agent', 'project', 'global')),
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                importance INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_agent_scope
                ON memories(agent_name, scope, updated_at);
            CREATE INDEX IF NOT EXISTS idx_memories_kind
                ON memories(kind, updated_at);

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                brief_memory_id INTEGER,
                current_phase TEXT NOT NULL DEFAULT 'kickoff',
                active_team_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (brief_memory_id) REFERENCES memories(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL DEFAULT 'default',
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'claimed', 'in_progress', 'blocked', 'review', 'done', 'cancelled')),
                owner_agent TEXT,
                created_by TEXT,
                priority INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
                depends_on_json TEXT NOT NULL DEFAULT '[]',
                context_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                acceptance_criteria TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                claimed_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_project_status
                ON jobs(project_id, status, priority, updated_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_owner_status
                ON jobs(owner_agent, status, priority, updated_at);

            CREATE TABLE IF NOT EXISTS job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                agent_name TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_job_events_job_created
                ON job_events(job_id, created_at);

            CREATE TABLE IF NOT EXISTS job_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                agent_name TEXT,
                path TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'file',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_job_artifacts_job
                ON job_artifacts(job_id, created_at);

            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL DEFAULT 'default',
                job_id INTEGER,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'started',
                summary TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_started
                ON agent_runs(agent_name, started_at);
            """
        )

        try:
            self.conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    title,
                    content,
                    tags,
                    agent_name,
                    kind,
                    scope,
                    content='memories',
                    content_rowid='id'
                );

                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, title, content, tags, agent_name, kind, scope)
                    VALUES (new.id, new.title, new.content, new.tags_json, COALESCE(new.agent_name, ''), new.kind, new.scope);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags, agent_name, kind, scope)
                    VALUES ('delete', old.id, old.title, old.content, old.tags_json, COALESCE(old.agent_name, ''), old.kind, old.scope);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags, agent_name, kind, scope)
                    VALUES ('delete', old.id, old.title, old.content, old.tags_json, COALESCE(old.agent_name, ''), old.kind, old.scope);
                    INSERT INTO memories_fts(rowid, title, content, tags, agent_name, kind, scope)
                    VALUES (new.id, new.title, new.content, new.tags_json, COALESCE(new.agent_name, ''), new.kind, new.scope);
                END;
                """
            )
        except sqlite3.OperationalError:
            self.fts_enabled = False

        self.conn.commit()

    def seed_defaults(self) -> dict[str, Any]:
        inserted = 0
        for agent in DEFAULT_AGENTS:
            result = self.agent_register(**agent)
            inserted += int(result["created"])
        project = self.project_upsert(
            project_id=DEFAULT_PROJECT_ID,
            name="Default Project",
            summary="Default workspace project used by the Grok agent job board.",
            current_phase="kickoff",
        )
        return {
            "agents_seen": len(DEFAULT_AGENTS),
            "agents_created": inserted,
            "default_project_created": project["created"],
            "db_path": str(self.db_path),
        }

    def agent_register(
        self,
        name: str,
        display_name: str | None = None,
        role: str = "",
        personality: str = "",
        responsibilities: str = "",
    ) -> dict[str, Any]:
        name = clean_name(name, "name")
        now = utc_now()
        existing = self.conn.execute("SELECT name FROM agents WHERE name = ?", (name,)).fetchone()
        self.conn.execute(
            """
            INSERT INTO agents(name, display_name, role, personality, responsibilities, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                display_name = excluded.display_name,
                role = excluded.role,
                personality = excluded.personality,
                responsibilities = excluded.responsibilities,
                updated_at = excluded.updated_at
            """,
            (
                name,
                display_name or name,
                role.strip(),
                personality.strip(),
                responsibilities.strip(),
                now,
                now,
            ),
        )
        self.conn.commit()
        return {"name": name, "created": existing is None}

    def agent_get(self, name: str) -> dict[str, Any]:
        name = clean_name(name, "name")
        row = self.conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"agent not found: {name}")
        return dict(row)

    def agent_list(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT * FROM agents ORDER BY name").fetchall()
        return {"agents": [dict(row) for row in rows]}

    def project_upsert(
        self,
        project_id: str | None = None,
        name: str = "Default Project",
        summary: str = "",
        brief_memory_id: int | None = None,
        current_phase: str = "kickoff",
        active_team: Any = None,
    ) -> dict[str, Any]:
        project_id = clean_project_id(project_id)
        now = utc_now()
        existing = self.conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        self.conn.execute(
            """
            INSERT INTO projects(id, name, summary, brief_memory_id, current_phase, active_team_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                summary = excluded.summary,
                brief_memory_id = COALESCE(excluded.brief_memory_id, projects.brief_memory_id),
                current_phase = excluded.current_phase,
                active_team_json = excluded.active_team_json,
                updated_at = excluded.updated_at
            """,
            (
                project_id,
                name.strip() or project_id,
                summary.strip(),
                brief_memory_id,
                current_phase.strip() or "kickoff",
                json_list(active_team, "active_team"),
                now,
                now,
            ),
        )
        self.conn.commit()
        return {"project_id": project_id, "created": existing is None}

    def project_get(self, project_id: str | None = None) -> dict[str, Any]:
        project_id = clean_project_id(project_id)
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            self.project_upsert(project_id=project_id)
            row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._project_row(row)

    def memory_write(
        self,
        title: str,
        content: str,
        agent_name: str | None = None,
        scope: str = "agent",
        kind: str = "note",
        tags: Any = None,
        importance: int = 3,
        source: str = "mcp",
    ) -> dict[str, Any]:
        scope = scope.strip().lower()
        if scope not in {"agent", "project", "global"}:
            raise ValueError("scope must be one of: agent, project, global")
        if scope == "agent":
            if not agent_name:
                raise ValueError("agent_name is required when scope is agent")
            agent_name = clean_name(agent_name, "agent_name")
        elif agent_name:
            agent_name = clean_name(agent_name, "agent_name")

        kind = clean_name(kind.strip().lower(), "kind")
        title = title.strip()
        content = content.strip()
        if not title:
            raise ValueError("title is required")
        if not content:
            raise ValueError("content is required")
        importance = max(1, min(5, int(importance)))
        tags_json = json.dumps(normalize_tags(tags))
        now = utc_now()

        cur = self.conn.execute(
            """
            INSERT INTO memories(agent_name, scope, kind, title, content, tags_json, importance, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (agent_name, scope, kind, title, content, tags_json, importance, source, now, now),
        )
        self.conn.commit()
        return {"id": cur.lastrowid, "scope": scope, "agent_name": agent_name, "kind": kind, "title": title}

    def memory_get(self, memory_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        if row is None:
            raise KeyError(f"memory not found: {memory_id}")
        self.conn.execute("UPDATE memories SET last_accessed_at = ? WHERE id = ?", (utc_now(), int(memory_id)))
        self.conn.commit()
        return self._memory_row(row)

    def memory_recent(self, agent_name: str | None = None, limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(50, int(limit)))
        args: list[Any] = []
        where = ""
        if agent_name:
            agent_name = clean_name(agent_name, "agent_name")
            where = "WHERE agent_name = ? OR scope IN ('project', 'global')"
            args.append(agent_name)
        rows = self.conn.execute(
            f"SELECT * FROM memories {where} ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        return {"memories": [self._memory_row(row) for row in rows]}

    def memory_search(
        self,
        query: str,
        agent_name: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        include_other_agents: bool = False,
        limit: int = 8,
    ) -> dict[str, Any]:
        limit = max(1, min(50, int(limit)))
        filters: list[str] = []
        args: list[Any] = []

        if agent_name and not include_other_agents:
            agent_name = clean_name(agent_name, "agent_name")
            filters.append("(m.agent_name = ? OR m.scope IN ('project', 'global'))")
            args.append(agent_name)
        elif agent_name:
            agent_name = clean_name(agent_name, "agent_name")

        if kind:
            filters.append("m.kind = ?")
            args.append(clean_name(kind.lower(), "kind"))
        if scope:
            scope = scope.lower()
            if scope not in {"agent", "project", "global"}:
                raise ValueError("scope must be one of: agent, project, global")
            filters.append("m.scope = ?")
            args.append(scope)

        where_tail = (" AND " + " AND ".join(filters)) if filters else ""
        query = query.strip()
        if self.fts_enabled and query:
            match = fts_query(query)
            if match:
                try:
                    rows = self.conn.execute(
                        f"""
                        SELECT m.*, bm25(memories_fts) AS rank
                        FROM memories_fts
                        JOIN memories m ON m.id = memories_fts.rowid
                        WHERE memories_fts MATCH ? {where_tail}
                        ORDER BY rank ASC, m.importance DESC, m.updated_at DESC
                        LIMIT ?
                        """,
                        (match, *args, limit),
                    ).fetchall()
                    return {"query": query, "memories": [self._memory_row(row) for row in rows]}
                except sqlite3.OperationalError:
                    pass

        like = f"%{query}%"
        rows = self.conn.execute(
            f"""
            SELECT m.*
            FROM memories m
            WHERE (m.title LIKE ? OR m.content LIKE ? OR m.tags_json LIKE ?) {where_tail}
            ORDER BY m.importance DESC, m.updated_at DESC
            LIMIT ?
            """,
            (like, like, like, *args, limit),
        ).fetchall()
        return {"query": query, "memories": [self._memory_row(row) for row in rows]}

    def memory_update(self, memory_id: int, **updates: Any) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        if row is None:
            raise KeyError(f"memory not found: {memory_id}")

        allowed = {"title", "content", "kind", "scope", "agent_name", "tags", "importance", "source"}
        set_parts: list[str] = []
        args: list[Any] = []
        for key, value in updates.items():
            if key not in allowed or value is None:
                continue
            column = "tags_json" if key == "tags" else key
            if key == "tags":
                value = json.dumps(normalize_tags(value))
            elif key in {"kind", "agent_name"} and value:
                value = clean_name(str(value).lower(), key)
            elif key == "scope":
                value = str(value).lower()
                if value not in {"agent", "project", "global"}:
                    raise ValueError("scope must be one of: agent, project, global")
            elif key == "importance":
                value = max(1, min(5, int(value)))
            else:
                value = str(value).strip()
            set_parts.append(f"{column} = ?")
            args.append(value)

        if not set_parts:
            return {"id": int(memory_id), "updated": False}

        set_parts.append("updated_at = ?")
        args.append(utc_now())
        args.append(int(memory_id))
        self.conn.execute(f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ?", args)
        self.conn.commit()
        return {"id": int(memory_id), "updated": True}

    def memory_delete(self, memory_id: int) -> dict[str, Any]:
        cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
        self.conn.commit()
        return {"id": int(memory_id), "deleted": cur.rowcount > 0}

    def job_create(
        self,
        title: str,
        description: str = "",
        project_id: str | None = None,
        owner_agent: str | None = None,
        created_by: str | None = None,
        priority: int = 3,
        depends_on: Any = None,
        context_memory_ids: Any = None,
        acceptance_criteria: str = "",
        status: str = "pending",
    ) -> dict[str, Any]:
        project_id = clean_project_id(project_id)
        self.project_get(project_id)
        title = title.strip()
        if not title:
            raise ValueError("title is required")
        status = clean_status(status)
        if owner_agent:
            owner_agent = clean_name(owner_agent, "owner_agent")
        if created_by:
            created_by = clean_name(created_by, "created_by")
        priority = max(1, min(5, int(priority)))
        now = utc_now()
        cur = self.conn.execute(
            """
            INSERT INTO jobs(
                project_id, title, description, status, owner_agent, created_by, priority,
                depends_on_json, context_memory_ids_json, acceptance_criteria, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                title,
                description.strip(),
                status,
                owner_agent,
                created_by,
                priority,
                json_list(depends_on, "depends_on"),
                json_list(context_memory_ids, "context_memory_ids"),
                acceptance_criteria.strip(),
                now,
                now,
            ),
        )
        job_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO job_events(job_id, event_type, agent_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, "created", created_by, f"Job created: {title}", now),
        )
        self.conn.commit()
        return self.job_get(job_id)

    def job_get(self, job_id: int, include_events: bool = True, include_artifacts: bool = True) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        job = self._job_row(row)
        if include_events:
            events = self.conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id",
                (int(job_id),),
            ).fetchall()
            job["events"] = [dict(event) for event in events]
        if include_artifacts:
            artifacts = self.conn.execute(
                "SELECT * FROM job_artifacts WHERE job_id = ? ORDER BY id",
                (int(job_id),),
            ).fetchall()
            job["artifacts"] = [dict(artifact) for artifact in artifacts]
        return job

    def job_list(
        self,
        project_id: str | None = None,
        status: str | None = None,
        owner_agent: str | None = None,
        include_done: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        project_id = clean_project_id(project_id)
        limit = max(1, min(100, int(limit)))
        filters = ["project_id = ?"]
        args: list[Any] = [project_id]
        if status:
            filters.append("status = ?")
            args.append(clean_status(status))
        elif not include_done:
            filters.append("status NOT IN ('done', 'cancelled')")
        if owner_agent:
            filters.append("owner_agent = ?")
            args.append(clean_name(owner_agent, "owner_agent"))
        rows = self.conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {' AND '.join(filters)}
            ORDER BY priority DESC, updated_at DESC, id DESC
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()
        return {"project_id": project_id, "jobs": [self._job_row(row) for row in rows]}

    def job_claim(self, job_id: int, agent_name: str, status: str = "in_progress") -> dict[str, Any]:
        agent_name = clean_name(agent_name, "agent_name")
        status = clean_status(status)
        if status not in {"claimed", "in_progress"}:
            raise ValueError("claim status must be claimed or in_progress")
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        now = utc_now()
        self.conn.execute(
            """
            UPDATE jobs
            SET owner_agent = ?, status = ?, claimed_at = COALESCE(claimed_at, ?), updated_at = ?
            WHERE id = ?
            """,
            (agent_name, status, now, now, int(job_id)),
        )
        self.conn.execute(
            "INSERT INTO job_events(job_id, event_type, agent_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(job_id), "claimed", agent_name, f"Job claimed by {agent_name}; status={status}.", now),
        )
        self.conn.commit()
        return self.job_get(job_id)

    def job_update(self, job_id: int, agent_name: str | None = None, **updates: Any) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        allowed = {
            "title",
            "description",
            "status",
            "owner_agent",
            "priority",
            "depends_on",
            "context_memory_ids",
            "acceptance_criteria",
            "result_summary",
        }
        columns: list[str] = []
        args: list[Any] = []
        for key, value in updates.items():
            if key not in allowed or value is None:
                continue
            column = key
            if key == "status":
                value = clean_status(str(value))
            elif key == "owner_agent" and value:
                value = clean_name(str(value), "owner_agent")
            elif key == "priority":
                value = max(1, min(5, int(value)))
            elif key == "depends_on":
                column = "depends_on_json"
                value = json_list(value, "depends_on")
            elif key == "context_memory_ids":
                column = "context_memory_ids_json"
                value = json_list(value, "context_memory_ids")
            else:
                value = str(value).strip()
            columns.append(f"{column} = ?")
            args.append(value)
        if not columns:
            return self.job_get(job_id)
        now = utc_now()
        columns.append("updated_at = ?")
        args.append(now)
        if any(part.startswith("status =") for part in columns) and updates.get("status") == "done":
            columns.append("completed_at = COALESCE(completed_at, ?)")
            args.append(now)
        args.append(int(job_id))
        self.conn.execute(f"UPDATE jobs SET {', '.join(columns)} WHERE id = ?", args)
        if agent_name:
            agent_name = clean_name(agent_name, "agent_name")
        self.conn.execute(
            "INSERT INTO job_events(job_id, event_type, agent_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(job_id), "updated", agent_name, f"Job updated: {', '.join(sorted(updates.keys()))}", now),
        )
        self.conn.commit()
        return self.job_get(job_id)

    def job_append_event(self, job_id: int, event_type: str, content: str, agent_name: str | None = None) -> dict[str, Any]:
        self.job_get(job_id, include_events=False, include_artifacts=False)
        event_type = clean_name(event_type.strip().lower(), "event_type")
        content = content.strip()
        if not content:
            raise ValueError("content is required")
        if agent_name:
            agent_name = clean_name(agent_name, "agent_name")
        now = utc_now()
        cur = self.conn.execute(
            "INSERT INTO job_events(job_id, event_type, agent_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(job_id), event_type, agent_name, content, now),
        )
        self.conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now, int(job_id)))
        self.conn.commit()
        return {"id": int(cur.lastrowid), "job_id": int(job_id), "event_type": event_type, "agent_name": agent_name}

    def job_complete(self, job_id: int, agent_name: str, result_summary: str) -> dict[str, Any]:
        agent_name = clean_name(agent_name, "agent_name")
        result_summary = result_summary.strip()
        if not result_summary:
            raise ValueError("result_summary is required")
        now = utc_now()
        cur = self.conn.execute(
            """
            UPDATE jobs
            SET status = 'done', owner_agent = COALESCE(owner_agent, ?), result_summary = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (agent_name, result_summary, now, now, int(job_id)),
        )
        if cur.rowcount == 0:
            raise KeyError(f"job not found: {job_id}")
        self.conn.execute(
            "INSERT INTO job_events(job_id, event_type, agent_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(job_id), "completed", agent_name, result_summary, now),
        )
        self.conn.commit()
        return self.job_get(job_id)

    def job_block(self, job_id: int, agent_name: str, reason: str) -> dict[str, Any]:
        agent_name = clean_name(agent_name, "agent_name")
        reason = reason.strip()
        if not reason:
            raise ValueError("reason is required")
        now = utc_now()
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'blocked', owner_agent = COALESCE(owner_agent, ?), updated_at = ? WHERE id = ?",
            (agent_name, now, int(job_id)),
        )
        if cur.rowcount == 0:
            raise KeyError(f"job not found: {job_id}")
        self.conn.execute(
            "INSERT INTO job_events(job_id, event_type, agent_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(job_id), "blocked", agent_name, reason, now),
        )
        self.conn.commit()
        return self.job_get(job_id)

    def job_add_artifact(
        self,
        job_id: int,
        path: str,
        agent_name: str | None = None,
        kind: str = "file",
        summary: str = "",
    ) -> dict[str, Any]:
        self.job_get(job_id, include_events=False, include_artifacts=False)
        path = path.strip()
        if not path:
            raise ValueError("path is required")
        if agent_name:
            agent_name = clean_name(agent_name, "agent_name")
        kind = clean_name(kind.strip().lower(), "kind")
        now = utc_now()
        cur = self.conn.execute(
            "INSERT INTO job_artifacts(job_id, agent_name, path, kind, summary, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (int(job_id), agent_name, path, kind, summary.strip(), now),
        )
        self.conn.execute(
            "INSERT INTO job_events(job_id, event_type, agent_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (int(job_id), "artifact", agent_name, f"{kind}: {path}", now),
        )
        self.conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now, int(job_id)))
        self.conn.commit()
        return {"id": int(cur.lastrowid), "job_id": int(job_id), "path": path, "kind": kind}

    def job_report(
        self,
        job_id: int,
        agent_name: str,
        report: str,
        status: str | None = None,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        self.job_append_event(job_id, "report", report, agent_name)
        updates: dict[str, Any] = {}
        if status:
            updates["status"] = status
        if result_summary:
            updates["result_summary"] = result_summary
        if updates:
            return self.job_update(job_id, agent_name=agent_name, **updates)
        return self.job_get(job_id)

    def context_pack_get(
        self,
        agent_name: str,
        project_id: str | None = None,
        job_id: int | None = None,
        memory_query: str | None = None,
        memory_limit: int = 6,
        job_limit: int = 10,
    ) -> dict[str, Any]:
        agent_name = clean_name(agent_name, "agent_name")
        project_id = clean_project_id(project_id)
        profile = self.agent_get(agent_name)
        project = self.project_get(project_id)
        if memory_query:
            memories = self.memory_search(memory_query, agent_name=agent_name, limit=memory_limit)["memories"]
        else:
            memories = self.memory_recent(agent_name=agent_name, limit=memory_limit)["memories"]
        assigned = self.job_list(project_id=project_id, owner_agent=agent_name, limit=job_limit)["jobs"]
        open_jobs = self.job_list(project_id=project_id, limit=job_limit)["jobs"]
        pack: dict[str, Any] = {
            "agent": profile,
            "project": project,
            "relevant_memories": memories,
            "assigned_jobs": assigned,
            "open_jobs": open_jobs,
            "instructions": [
                "Claim a job before doing substantial work.",
                "Append job events while working.",
                "Complete or block the job before final handoff.",
                "Write durable facts to memory only when they should survive future sessions.",
            ],
        }
        if job_id is not None:
            pack["job"] = self.job_get(int(job_id))
        return pack

    def stats(self) -> dict[str, Any]:
        agents = self.conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
        memories = self.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        projects = self.conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        jobs = self.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        open_jobs = self.conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ('done', 'cancelled')"
        ).fetchone()["n"]
        by_scope = {
            row["scope"]: row["n"]
            for row in self.conn.execute("SELECT scope, COUNT(*) AS n FROM memories GROUP BY scope")
        }
        by_status = {
            row["status"]: row["n"]
            for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        }
        return {
            "db_path": str(self.db_path),
            "agents": agents,
            "memories": memories,
            "projects": projects,
            "jobs": jobs,
            "open_jobs": open_jobs,
            "by_scope": by_scope,
            "by_status": by_status,
            "fts_enabled": self.fts_enabled,
        }

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = json.loads(data.pop("tags_json") or "[]")
        return data

    @staticmethod
    def _project_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["active_team"] = json.loads(data.pop("active_team_json") or "[]")
        return data

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["depends_on"] = json.loads(data.pop("depends_on_json") or "[]")
        data["context_memory_ids"] = json.loads(data.pop("context_memory_ids_json") or "[]")
        return data


def tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "agent_register",
            "description": "Create or update a persistent named agent profile in SQLite.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "display_name": {"type": "string"},
                    "role": {"type": "string"},
                    "personality": {"type": "string"},
                    "responsibilities": {"type": "string"},
                },
                "required": ["name", "role", "personality"],
            },
        },
        {
            "name": "agent_get",
            "description": "Read one persistent agent profile from SQLite.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "agent_list",
            "description": "List all persistent agent profiles stored in SQLite.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "memory_write",
            "description": "Persist a real memory row in SQLite for an agent, project, or global scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "scope": {"type": "string", "enum": ["agent", "project", "global"], "default": "agent"},
                    "kind": {"type": "string", "default": "note"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                    "source": {"type": "string", "default": "mcp"},
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "memory_search",
            "description": "Search SQLite memories with FTS5 when available, scoped to an agent by default.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "agent_name": {"type": "string"},
                    "kind": {"type": "string"},
                    "scope": {"type": "string", "enum": ["agent", "project", "global"]},
                    "include_other_agents": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_recent",
            "description": "Read the most important recent SQLite memories for an agent and shared project/global scope.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
            },
        },
        {
            "name": "memory_get",
            "description": "Read one memory by SQLite row id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        {
            "name": "memory_update",
            "description": "Update fields on an existing SQLite memory row.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "kind": {"type": "string"},
                    "scope": {"type": "string", "enum": ["agent", "project", "global"]},
                    "agent_name": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "source": {"type": "string"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "memory_delete",
            "description": "Delete one SQLite memory row by id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        {
            "name": "project_upsert",
            "description": "Create or update the current project record used by the SQLite job board.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "default": "default"},
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "brief_memory_id": {"type": "integer"},
                    "current_phase": {"type": "string"},
                    "active_team": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "project_get",
            "description": "Read the current project record from the SQLite job board.",
            "inputSchema": {
                "type": "object",
                "properties": {"project_id": {"type": "string", "default": "default"}},
            },
        },
        {
            "name": "context_pack_get",
            "description": "Read the required startup context pack for an agent: profile, project, memories, and jobs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "project_id": {"type": "string", "default": "default"},
                    "job_id": {"type": "integer"},
                    "memory_query": {"type": "string"},
                    "memory_limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 6},
                    "job_limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
                "required": ["agent_name"],
            },
        },
        {
            "name": "job_create",
            "description": "Create a real SQLite job-board item for agent coordination.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "default": "default"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "owner_agent": {"type": "string"},
                    "created_by": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    "context_memory_ids": {"type": "array", "items": {"type": "integer"}},
                    "acceptance_criteria": {"type": "string"},
                    "status": {"type": "string", "enum": sorted(JOB_STATUSES), "default": "pending"},
                },
                "required": ["title"],
            },
        },
        {
            "name": "job_list",
            "description": "List SQLite job-board items filtered by project, status, or owner.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "default": "default"},
                    "status": {"type": "string", "enum": sorted(JOB_STATUSES)},
                    "owner_agent": {"type": "string"},
                    "include_done": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
        },
        {
            "name": "job_get",
            "description": "Read one SQLite job-board item with events and artifacts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "include_events": {"type": "boolean", "default": True},
                    "include_artifacts": {"type": "boolean", "default": True},
                },
                "required": ["job_id"],
            },
        },
        {
            "name": "job_claim",
            "description": "Claim a job before doing substantial work.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "agent_name": {"type": "string"},
                    "status": {"type": "string", "enum": ["claimed", "in_progress"], "default": "in_progress"},
                },
                "required": ["job_id", "agent_name"],
            },
        },
        {
            "name": "job_update",
            "description": "Update job metadata, status, owner, context, or result summary.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "agent_name": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string", "enum": sorted(JOB_STATUSES)},
                    "owner_agent": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    "context_memory_ids": {"type": "array", "items": {"type": "integer"}},
                    "acceptance_criteria": {"type": "string"},
                    "result_summary": {"type": "string"},
                },
                "required": ["job_id"],
            },
        },
        {
            "name": "job_append_event",
            "description": "Append a chronological event to a job-board item.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "event_type": {"type": "string"},
                    "agent_name": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["job_id", "event_type", "content"],
            },
        },
        {
            "name": "job_complete",
            "description": "Mark a job done with a required result summary.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "agent_name": {"type": "string"},
                    "result_summary": {"type": "string"},
                },
                "required": ["job_id", "agent_name", "result_summary"],
            },
        },
        {
            "name": "job_block",
            "description": "Mark a job blocked with the concrete blocker.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "agent_name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["job_id", "agent_name", "reason"],
            },
        },
        {
            "name": "job_add_artifact",
            "description": "Attach a file/path/API artifact reference to a job-board item.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "agent_name": {"type": "string"},
                    "path": {"type": "string"},
                    "kind": {"type": "string", "default": "file"},
                    "summary": {"type": "string"},
                },
                "required": ["job_id", "path"],
            },
        },
        {
            "name": "job_report",
            "description": "Append a job report and optionally update status/result summary in one call.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "agent_name": {"type": "string"},
                    "report": {"type": "string"},
                    "status": {"type": "string", "enum": sorted(JOB_STATUSES)},
                    "result_summary": {"type": "string"},
                },
                "required": ["job_id", "agent_name", "report"],
            },
        },
        {
            "name": "memory_stats",
            "description": "Return SQLite memory and job-board database counts and storage details.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


class MCPServer:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.tools = {
            "agent_register": self.store.agent_register,
            "agent_get": self.store.agent_get,
            "agent_list": self.store.agent_list,
            "memory_write": self.store.memory_write,
            "memory_search": self.store.memory_search,
            "memory_recent": self.store.memory_recent,
            "memory_get": lambda id: self.store.memory_get(id),
            "memory_update": lambda id, **kwargs: self.store.memory_update(id, **kwargs),
            "memory_delete": lambda id: self.store.memory_delete(id),
            "project_upsert": self.store.project_upsert,
            "project_get": self.store.project_get,
            "context_pack_get": self.store.context_pack_get,
            "job_create": self.store.job_create,
            "job_list": self.store.job_list,
            "job_get": self.store.job_get,
            "job_claim": self.store.job_claim,
            "job_update": lambda job_id, **kwargs: self.store.job_update(job_id, **kwargs),
            "job_append_event": self.store.job_append_event,
            "job_complete": self.store.job_complete,
            "job_block": self.store.job_block,
            "job_add_artifact": self.store.job_add_artifact,
            "job_report": self.store.job_report,
            "memory_stats": self.store.stats,
        }

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                response = self.handle(request)
            except Exception as exc:  # keep protocol alive on malformed input
                response = self.error_response(None, -32700, f"parse error: {exc}")
            if response is not None:
                self.write(response)

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if request_id is None and method in {"notifications/initialized", "notifications/cancelled"}:
            return None

        try:
            if method == "initialize":
                return self.result(
                    request_id,
                    {
                        "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                )
            if method == "tools/list":
                return self.result(request_id, {"tools": tool_schema()})
            if method == "tools/call":
                return self.result(request_id, self.call_tool(params))
            if method == "ping":
                return self.result(request_id, {})
            if method == "resources/list":
                return self.result(request_id, {"resources": []})
            if method == "prompts/list":
                return self.result(request_id, {"prompts": []})
            return self.error_response(request_id, -32601, f"method not found: {method}")
        except KeyError as exc:
            return self.tool_error(request_id, str(exc))
        except Exception as exc:
            return self.tool_error(request_id, str(exc))

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in self.tools:
            raise KeyError(f"unknown tool: {name}")
        result = self.tools[name](**arguments)
        return {"content": [{"type": "text", "text": json_text(result)}], "isError": False}

    @staticmethod
    def result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @classmethod
    def tool_error(cls, request_id: Any, message: str) -> dict[str, Any]:
        return cls.result(request_id, {"content": [{"type": "text", "text": message}], "isError": True})

    @staticmethod
    def write(response: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SQLite-backed MCP memory server")
    parser.add_argument("--db", default=os.environ.get("AGENT_MEMORY_DB", str(DEFAULT_DB)))
    parser.add_argument("--seed", action="store_true", help="Seed default agents, print JSON, and exit")
    parser.add_argument("--stats", action="store_true", help="Print database stats as JSON and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = MemoryStore(Path(args.db).expanduser())
    try:
        if args.seed:
            print(json_text(store.seed_defaults()))
            return 0
        if args.stats:
            print(json_text(store.stats()))
            return 0
        MCPServer(store).serve()
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
