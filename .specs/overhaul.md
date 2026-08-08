# DB-Agent Overhaul — Working Checklist

## Current Codebase Structure (3 core files)
1. **CLI layer** — Rich-based terminal interface
2. **Core logic** — FastMCP, MCP server definition, tool definitions, orchestration loop
3. **Tracker logic** — logs all prompts and DB changes (current Git-like state system)

---

## 1. UX / Access Changes

- [ ] Shift from Ollama-local to **BYO API key** architecture
- [ ] Integrate multi-provider support:
  - [ ] OpenAI
  - [ ] Claude
  - [ ] Gemini
  - [ ] OpenRouter
  - [ ] Ollama Cloud (default for now — free tier)
- [ ] Per-provider model selection (let user pick model within chosen provider)
- [ ] Build Streamlit demo UI (public web showcase, sandboxed DB, limited tries)
- [ ] Streamlit UI also usable **post-install** as an alternative to CLI (not just a demo)

---

## 2. Architecture Overhaul — Orchestrator/Sub-Agent Model

**Core flow:**
`User query → Orchestrator (understands query, derives sub-requests) → Sub-agent(s) (convert to SQL) → back to Orchestrator (validates) → Orchestrator executes`

- [ ] Define Orchestrator responsibilities precisely (decomposition, routing, validation, execution authority)
- [ ] Define Sub-agent responsibilities precisely (NL→SQL only, no execution authority)
- [ ] **Human-in-the-loop gate**: risky operations require explicit user confirmation
  - [ ] Define "risky" — deletes, schema changes (ALTER/DROP), bulk updates, anything irreversible
  - [ ] Design the confirmation UX for both CLI and Streamlit

---

## 3. Security / Guardrails

- [ ] Prompt injection defense (query content shouldn't be able to hijack orchestrator instructions)
- [ ] General malicious-input handling practices
- [ ] **Access control**: user must not be able to query/access DB tables or rows they're not permitted to
  - [ ] Needs a permission model — per-connection? per-schema? per-table?
  - [ ] Decide where this is enforced (orchestrator level vs DB connection level — DB-level is more robust)
- [ ] Fallback behavior when a sub-agent produces invalid/unsafe SQL
- [ ] Re-run/retry loop design (bounded — avoid infinite retry on bad generation)

---

## 4. Data Safety — Soft Delete / Archive Model

- [ ] Open question flagged: how do we actually restore data after a delete?
- [ ] Proposed fix: deletes/destructive ops should **archive, not hard-delete**
  - [ ] Design archive table/mechanism (shadow table? soft-delete flag column? separate audit store?)
  - [ ] Decide restore UX (via tracker/revert system)
- [ ] Strengthen Git-like tracking so revert-to-any-state is actually robust, not just logged
  - [ ] Clarify: is current tracker just a log, or does it support real state reconstruction?

---

## 5. Evaluation / Testing

- [ ] Build a test dataset of diverse queries (varied complexity, varied intent — read/write/schema)
- [ ] Define "did it actually work" — correctness of SQL, correctness of result, safety (didn't touch unauthorized data), didn't trigger unnecessary risky-op confirmation, etc.
- [ ] Decide if this becomes a proper eval harness (pass/fail per query, tracked over time) — useful both for your own confidence and as a resume/defense-doc artifact

---

## Open Questions to Resolve First (good starting point for next chat)
1. Permission model shape — how granular, enforced where?
2. Archive/soft-delete mechanism design — schema-level approach?
3. Orchestrator validation step — what exactly is it checking before execution?
4. What counts as "risky" precisely enough to gate on?