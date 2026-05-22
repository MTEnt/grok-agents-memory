---
name: project-kickoff
description: >
  Run the short startup interview for a new or empty project before Grok builds
  anything. Use when the user asks to start, initialize, bootstrap, plan, or
  create a new project with persistent agents, or invokes /project-kickoff.
---

# Project Kickoff Skill

Use this workflow to turn an empty folder into a project with persistent named
agents and SQLite-backed history.

## Non-Negotiables

- The first response in an empty project is the interview, not implementation.
- Do not create product files, choose a stack, or spawn build work until the user
  answers the interview.
- Use the `agent_memory` MCP server. If it is unavailable, say SQLite memory is
  not connected and pause before claiming anything was remembered.
- Do not claim a memory was saved unless `memory_write` succeeds.

## Step 1: Load Memory

Use MCP tool discovery for the `agent_memory` server, then:

1. Read the startup context with `context_pack_get` for `project-founder`.
2. If `context_pack_get` is unavailable, fall back to `agent_get`,
   `memory_search`, and `job_list`, then state that the full context pack was
   unavailable.

## Step 2: Check Whether This Is Empty

Inspect the working directory. Ignore toolkit/bootstrap files:

- `.git`
- `.grok`
- `mcp`
- `scripts`
- `tests`
- `AGENTS.md`
- `README.md`
- `.gitignore`
- SQLite database files
- generated cache folders

If there are no remaining product/code/docs files yet, treat the project as
empty.

## Step 3: Ask The Short Interview

Ask all six questions in one message. Keep it short and easy to answer:

1. What are we building, and who is it for?
2. What is the first workflow that must be usable?
3. Is this a web app, mobile app, CLI/tool, API/service, library, game, or other?
4. Any preferred stack, constraints, integrations, or deployment target?
5. What should the experience feel like?
6. Which agent team should be active: lean, standard, or full?

Explain the presets in one compact line:

- lean = founder, architect, implementer
- standard = lean plus product lead and reviewer
- full = standard plus memory curator

Then stop and wait for the user's answers.

## Step 4: Persist The Brief

After the user answers, write a project brief through SQLite:

- `scope`: `project`
- `kind`: `brief`
- `title`: concise project name or "Project kickoff brief"
- `content`: user answers, inferred decisions, open questions, selected agent team
- `tags`: `kickoff`, `brief`, project type, selected stack if known
- `importance`: `5`

Then update the project record with `project_upsert`:

- `project_id`: `default`
- `name`: the project/product name
- `summary`: concise brief summary
- `brief_memory_id`: the memory id returned by `memory_write`
- `current_phase`: `kickoff`
- `active_team`: selected agents

Also write a short `project-founder` agent memory with:

- `scope`: `agent`
- `agent_name`: `project-founder`
- `kind`: `handoff`
- `title`: "Kickoff completed"
- `content`: what was learned and which agents should act next
- `tags`: `kickoff`, `handoff`
- `importance`: `4`

## Step 5: Coordinate Agents

Use the selected team:

- lean: `architect`, then `implementer`
- standard: `product-lead`, `architect`, `implementer`, `reviewer`
- full: `product-lead`, `architect`, `implementer`, `reviewer`, `memory-curator`

Create job-board items before spawning or handing off work. Each job must include
the project brief memory id in `context_memory_ids`.

Recommended initial jobs:

- `product-lead`: "Define MVP workflow and acceptance criteria"
- `architect`: "Propose technical architecture and risk notes"
- `implementer`: "Implement the first usable workflow"
- `reviewer`: "Review implementation and verification evidence"
- `memory-curator` (full team only): "Consolidate kickoff and handoff memory"

Use dependencies:

- `architect` can depend on `product-lead` when product detail is needed.
- `implementer` depends on `product-lead` and `architect`.
- `reviewer` depends on `implementer`.
- `memory-curator` depends on `reviewer`.

Use subagents only after the SQLite brief, project record, and initial jobs have
been saved. Give each specialist a specific `job_id` and tell it to call
`context_pack_get`, then `job_claim`, then `job_report`/`job_complete` or
`job_block`.

## Success Criteria

- User answered the short interview or explicitly skipped it.
- SQLite contains a project brief memory.
- SQLite contains a founder handoff memory.
- SQLite contains a project record tied to the brief.
- SQLite contains initial job-board items for the selected agent team.
- Specialist work does not begin before the brief exists.
- The final response states which jobs were created, which agents are active,
  and what happens next.
