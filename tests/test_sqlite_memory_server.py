#!/usr/bin/env python3
"""Smoke test the SQLite memory MCP server through real JSON-RPC calls."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "sqlite_memory_server.py"


def request(proc: subprocess.Popen[str], method: str, params: dict | None = None, request_id: int = 1) -> dict:
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise AssertionError("server closed stdout")
    return json.loads(line)


def call_tool(proc: subprocess.Popen[str], name: str, arguments: dict, request_id: int) -> dict:
    response = request(proc, "tools/call", {"name": name, "arguments": arguments}, request_id)
    result = response["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "memory.sqlite"
        proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--db", str(db_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            init = request(proc, "initialize", {"protocolVersion": "2024-11-05"}, 1)
            assert init["result"]["serverInfo"]["name"] == "sqlite-agent-memory"

            tools = request(proc, "tools/list", {}, 2)
            tool_names = {tool["name"] for tool in tools["result"]["tools"]}
            assert {
                "agent_register",
                "memory_write",
                "memory_search",
                "memory_stats",
                "project_upsert",
                "context_pack_get",
                "job_create",
                "job_claim",
                "job_report",
                "job_complete",
            } <= tool_names

            call_tool(
                proc,
                "agent_register",
                {
                    "name": "tester",
                    "display_name": "Tester",
                    "role": "Test agent",
                    "personality": "Persistent and precise.",
                    "responsibilities": "Verify SQLite memory.",
                },
                3,
            )
            written = call_tool(
                proc,
                "memory_write",
                {
                    "agent_name": "tester",
                    "scope": "agent",
                    "kind": "test",
                    "title": "Durable SQLite smoke memory",
                    "content": "The MCP server persisted this row through sqlite3.",
                    "tags": ["sqlite", "smoke"],
                    "importance": 5,
                },
                4,
            )
            assert written["id"] > 0

            found = call_tool(proc, "memory_search", {"agent_name": "tester", "query": "sqlite persisted", "limit": 5}, 5)
            assert found["memories"], found
            assert found["memories"][0]["title"] == "Durable SQLite smoke memory"

            stats = call_tool(proc, "memory_stats", {}, 6)
            assert stats["agents"] == 1
            assert stats["memories"] == 1
            assert stats["projects"] == 0

            project = call_tool(
                proc,
                "project_upsert",
                {
                    "project_id": "default",
                    "name": "Smoke Project",
                    "summary": "Verify job-board coordination state.",
                    "active_team": ["tester"],
                },
                7,
            )
            assert project["project_id"] == "default"

            job = call_tool(
                proc,
                "job_create",
                {
                    "title": "Verify job board",
                    "description": "Claim, report, and complete a job.",
                    "owner_agent": "tester",
                    "created_by": "tester",
                    "priority": 5,
                    "context_memory_ids": [written["id"]],
                    "acceptance_criteria": "Job reaches done status with an event trail.",
                },
                8,
            )
            assert job["id"] > 0
            assert job["status"] == "pending"

            claimed = call_tool(
                proc,
                "job_claim",
                {"job_id": job["id"], "agent_name": "tester"},
                9,
            )
            assert claimed["status"] == "in_progress"
            assert claimed["owner_agent"] == "tester"

            reported = call_tool(
                proc,
                "job_report",
                {
                    "job_id": job["id"],
                    "agent_name": "tester",
                    "report": "The job board accepts reports.",
                    "status": "review",
                },
                10,
            )
            assert reported["status"] == "review"

            completed = call_tool(
                proc,
                "job_complete",
                {
                    "job_id": job["id"],
                    "agent_name": "tester",
                    "result_summary": "Job-board workflow verified.",
                },
                11,
            )
            assert completed["status"] == "done"
            assert completed["result_summary"] == "Job-board workflow verified."
            assert any(event["event_type"] == "completed" for event in completed["events"])

            context = call_tool(
                proc,
                "context_pack_get",
                {"agent_name": "tester", "job_id": job["id"]},
                12,
            )
            assert context["agent"]["name"] == "tester"
            assert context["project"]["id"] == "default"
            assert context["job"]["id"] == job["id"]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            assert count == 1
            job_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            assert job_count == 1
            event_count = conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0]
            assert event_count >= 4
        finally:
            conn.close()
    print("sqlite memory + job board MCP smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
