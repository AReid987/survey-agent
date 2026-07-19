# Brain Autoresearch Pipeline Design

## Problem
The brain currently generates passive "skip" rules when failures are detected.
The user wants an active pipeline: when repeated failures are detected, the brain
should research the root cause, determine a fix, implement it, and document the learning.

## Architecture

```
Events → Promote → Digest → FixProposal → Research → Implement → Verify → Document
```

### Phase 1: Diagnose (enhanced digest)
When promotions trigger, the digest LLM now receives:
- The failure events (existing)
- Relevant source code excerpts (new)
- Past fix proposals and their outcomes (new)
- Web search results for the failure pattern (new)

Output: `FixProposal` with diagnosis, research findings, and implementation plan.

### Phase 2: Research (new module)
`survey_agent/brain/research.py`
- `search_web(query)` — searches for known solutions
- `analyze_codebase(failure_context)` — finds relevant code
- `check_learnings(failure_signature)` — checks past brain fixes

### Phase 3: Implement (new module)  
`survey_agent/brain/fixer.py`
- `execute_fix(proposal)` — runs the implementation steps
- Steps can be: code edit, config change, dependency install, test run
- Each step is traced with before/after state

### Phase 4: Verify
- Run relevant tests
- Re-attempt the failed operation
- Compare success rate before/after

### Phase 5: Document
- Write learning to `data/brain/learnings.jsonl`
- Update rules.yaml with the fix (not skip)
- Commit changes with descriptive message

## New Schemas

### FixProposal
```python
class FixProposal(BaseModel):
    proposal_id: str  # UUID
    trigger_event_type: str
    site: str
    diagnosis: str  # Root cause analysis
    research_findings: list[str]  # What we learned
    solution: str  # Proposed fix description
    implementation_steps: list[ImplementationStep]
    confidence: float  # 0-1
    status: Literal["proposed", "researching", "implementing", "verified", "applied", "failed"]
    created_at: str
    applied_at: str | None
    trace: list[TraceEntry]  # Execution log
```

### ImplementationStep
```python
class ImplementationStep(BaseModel):
    step_type: Literal["code_edit", "config_change", "command", "web_search", "test"]
    description: str
    target_file: str | None  # For code edits
    command: str | None  # For commands
    expected_outcome: str
    actual_outcome: str | None
    status: Literal["pending", "running", "success", "failed", "skipped"]
```

### TraceEntry
```python
class TraceEntry(BaseModel):
    timestamp: str
    phase: str  # "research", "implement", "verify"
    action: str
    result: str
    duration_ms: int
```

## Integration Points

### With orchestrator.py
When `brain.record()` captures a failure, check if a fix proposal already exists.
If not, queue a research task.

### With digest.py
The digest prompt changes from "generate rules" to "diagnose and propose fixes".
The LLM receives code context and past learnings.

### With CLI
`survey-agent brain fix [--auto]` — execute pending fix proposals
`survey-agent brain learnings` — show past fix outcomes
`survey-agent brain research <proposal_id>` — run research phase manually

## Execution Model

Fix proposals have two modes:
1. **Manual**: `survey-agent brain fix <id>` — human reviews and approves
2. **Auto**: When confidence > 0.8 and fix is non-destructive, execute automatically

Auto-fix is limited to:
- Code changes in the survey-agent codebase
- Config changes
- Adding dependencies
- Running tests

Auto-fix NEVER:
- Changes profile.json demographics (denylist)
- Deletes data
- Makes network requests to external services (except web search)
- Modifies system files

## Learning Loop

After a fix is applied:
1. Re-run the failed operation
2. If it succeeds → mark fix as "verified", increase confidence
3. If it fails → mark as "failed", feed back into next digest cycle
4. Document the full trace in learnings.jsonl

The next digest cycle receives past fix outcomes as context, so the LLM
can learn from previous attempts and propose better solutions.
