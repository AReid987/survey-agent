# Taste (Continuously Learned by [CommandCode][cmd])

[cmd]: https://commandcode.ai/

# Architecture
- Persona must stay locked during a session; the brain only appends/refines rules, never changes the persona mid-session. Confidence: 0.85
- For the survey-agent brain MVP: use an event log (JSONL) + SQLite index for storage, not a full graph/vector system. Confidence: 0.60
- The brain digest must run autonomously (not require manual opt-in), since the system aims for long-horizon autonomous execution with no human interaction. Confidence: 0.80

# Workflow
- Before executing implementation plans, apply additional research/planning/design rounds using Plannotator, BMAD, and SEED tools to catch overlooked issues. Confidence: 0.70

