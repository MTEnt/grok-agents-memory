# Grok Agent Workspace Rules

This project is a Grok agent bootstrap workspace with a real SQLite-backed memory
layer exposed through the project MCP server named `agent_memory`.

## Memory Contract

- Use the `agent_memory` MCP tools for durable agent and project memory.
- Do not claim a memory was saved unless `agent_memory__memory_write` or the
  equivalent `use_tool` call succeeds.
- Do not simulate persistence in prose. If the MCP server is unavailable, say
  SQLite memory is not connected and stop before pretending to remember.
- At the start of meaningful work, call `context_pack_get` for the active agent.
  This is the canonical startup context: agent profile, project, memories, and
  job board.
- At handoff or completion, write durable decisions, user preferences, resolved
  bugs, implementation outcomes, and agent-specific history back to SQLite.
- Keep memory entries short, factual, and searchable. Use tags.

## Job Board Contract

The SQLite job board is the source of truth for current work.

- Do not start substantial work until a job exists.
- Claim a job with `job_claim` before doing substantial work.
- Append progress, blockers, review notes, and handoffs with `job_append_event`
  or `job_report`.
- Finish with `job_complete` or `job_block`; do not leave real work silently
  in progress.
- Use `context_memory_ids` on jobs to connect work to the project brief and
  other durable memories.
- If no relevant job exists, ask `project-founder` to create one or create a
  narrowly scoped job before continuing.

## Empty Project Intake

When the working folder has no product/code files yet, the `project-founder`
agent must question the user before scaffolding. Ignore this toolkit's bootstrap
files when deciding emptiness: `.git`, `.grok`, `mcp`, `scripts`, `tests`,
`AGENTS.md`, `README.md`, `.gitignore`, and generated cache/database files.
The first response should be a short interview, not file creation or
architecture speculation.

Ask at most six questions in one message:

1. What are we building, and who is it for?
2. What is the first workflow that must be usable?
3. Is this a web app, mobile app, CLI/tool, API/service, library, game, or other?
4. Any preferred stack, constraints, integrations, or deployment target?
5. What should the experience feel like?
6. Which agent team should be active: lean, standard, or full?

After intake, write a project brief to SQLite and update the project record.
Then create job-board items for the selected specialist team. Do not spawn
implementation work before the brief and initial jobs exist in SQLite.

## Agent Team

Use persistent named agents rather than disposable role-play:

- `project-founder`: intake and orchestration
- `product-lead`: product scope, workflows, and acceptance criteria
- `architect`: technical design, risks, and boundaries
- `implementer`: implementation and verification
- `reviewer`: adversarial review
- `memory-curator`: memory cleanup and consolidation

Each named agent must load its context pack before acting, claim/report/complete
or block its job, and append a short durable memory only when the result should
survive beyond the current job.
