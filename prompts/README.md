# Agent prompts

Phase 4 adds `baseline.txt` for proposal extraction. `baseline_spec()` binds it to
its Pydantic schema, fast model tier, and empty tool allowlist. The shared gateway
injects the remaining budgets, schema, and untrusted-data delimiters at runtime.

`app.agents.review.all_specs()` registers the baseline plus the separate
Coordinator, Repo Reviewer, Report Reviewer, and Claims/Comms prompts/schemas,
model tiers, and read-only tool allowlists. They use the same budget-enforcing
envelope. No live provider calls have been used to validate
the build so far.
