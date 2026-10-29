# Phase 2: shared LLM gateway

## Usage

The gateway accepts trusted agent definitions and explicit project/cycle/batch IDs.
`submit` returns a durable job ID immediately. `result(project_id, job_id)` returns
a validated Pydantic value or a safe pending reason. `status(project_id)` exposes
queue depth, project job counts, token usage, next-attempt delay, model cooldowns,
and seconds until the UTC quota reset. The future UI polls these without waiting
for an HTTP request.

```python
from app.config import Settings
from app.db import Database, ProjectStore
from app.gateway import AgentSpec, Gateway
from app.gateway.types import GatewayDemoOutput

settings = Settings()  # fake mode by default
db = Database(settings.database_path)
db.migrate()
project = ProjectStore.create(db, "Demo Student", "demo@example.com", "owner/repo")
cycle_id = project.create_cycle("example-head-sha")
spec = AgentSpec("repo_reviewer", "Describe evidence without asserting completion.",
                 GatewayDemoOutput, allowed_tools=("read_evidence",))
with Gateway(db, settings, [spec]) as gateway:
    job = gateway.submit(project.project_id, cycle_id, "example-batch-1",
                         "repo_reviewer", data={"changed_files": ["main.py"]})
    result = gateway.wait(project.project_id, job)  # Scripts only; UI polls.
    print(result.status)
```

Use `get_shared_gateway(db, settings, specs)` for application/Streamlit reruns to
reuse one service. An OS file lock prevents a second service or process from
recovering another worker's live jobs. Close explicitly on shutdown. Imports
never start workers or call a provider. For deterministic tests, construct
`Gateway(start=False)` and call `run_once()` to execute one eligible attempt.

Supply read-only `AgentSpec` definitions from trusted application code. Supported
roles are `baseline`, `coordinator`, `repo_reviewer`, `report_reviewer`, and
`claims_comms`. Agent-specific prompts/schemas come in Phases 4–5. The gateway
currently supplies the common safety/budget/schema envelope. Use `StrictOutput`
as the base of response schemas to forbid unknown fields and coercion.

## Queue and restart behavior

The queue rotates among projects after every eligible attempt. A retry stores
its next eligible time and releases the worker so other eligible work can proceed.
Provider calls are serialized; projects sharing a throttled model must respect its
common limit. Jobs capture input snapshots and an agent specification fingerprint.
A changed specification pauses old queued work instead of silently running
different instructions.

Queued jobs survive restart. Interrupted jobs become eligible again while their
previous attempted calls remain counted. This is at-least-once LLM execution
after an ambiguous crash, not exactly-once provider billing. Completed responses
and cache entries are committed together. The Python schema class is registered
again by application startup; its fingerprint must match before work resumes.

The queue persists project data in SQLite, including evidence snapshots, but no
API key. This database contains student information and should remain local.
Agent tools cannot enumerate all projects. Scheduling, migration recovery, and
global batch accounting intentionally aggregate across the supervisor's projects.

## Budgets and pending work

Every HTTP attempt, including retries and JSON repairs, reserves a call slot and
an input-plus-maximum-output token allowance in one SQLite write transaction.
Concurrent reservations cannot overspend the configured limits. Known provider
usage replaces the reservation; unknown or interrupted usage stays charged.
Failed attempts may therefore consume quota even when a provider did not bill them.

Batch IDs come from the deterministic application: use one ID across all projects
in a batch and retain it across restart. The default limits are six attempts per
cycle, 25 per batch, and two coordinator attempts per cycle. Specialist tool calls
are recorded separately and limited to six; `ledger.run_tool` verifies the
allowlist and reserves a slot before invoking a trusted application callback.
Agents cannot invoke this method directly or supply arbitrary callables.

Daily token quota resets at UTC midnight. Cycle/coordinator budgets do not reset
on restart or when moving to a new batch. Exhausted work becomes
`pending_evidence_review` with a gateway checkpoint; its review stage is preserved.
`resume(project_id, job_id, batch_id=...)` can explicitly retry pending work after
a daily reset or in a deliberately new batch without resetting any counters.
An exhausted cycle needs supervisor/orchestrator handling or new evidence in a
new cycle. The future orchestrator decides when to mark a cycle running/completed.

Malformed JSON, schema violations, and truncated outputs allow one regeneration
from the original input. Invalid responses and validation errors are never echoed
into repair prompts. Exhausted repair/retry allowances cannot be reset by `resume`;
they require a revised request. Authentication and non-retryable HTTP failures
produce a safe pending reason, not raw provider response text.

## Input, cache, and fake mode

Preflight estimates use UTF-8 byte length plus message overhead, including the
schema, instructions, and evidence. This conservative proxy may reject input a
model-specific tokenizer would fit. Oversized requests are paused; evidence is
never silently truncated. Reduce/retrieve the evidence and submit a revised
request. Each provider request carries the configured maximum output token count.

The common system envelope injects the role, allowed tools, remaining call/tool/
token budgets, output schema, and untrusted-content rule. Evidence is JSON-encoded
inside delimiters, with angle brackets escaped to prevent delimiter breakout.
No provider tool definitions or send capabilities are exposed. These controls
restrict capabilities and budgets; claim/finding verification is implemented in
Phase 5 and must still distrust model output.

The response cache includes project, mode, model, prompt/schema/tool fingerprint,
input, evidence hashes, and token caps. Both new and cached outputs undergo
Pydantic validation. Cache hits use no provider-call or token quota. Changing a
prompt, schema, evidence, model, or mode invalidates reuse.

`FakeLLMClient` accepts scripted responses/errors or an agent-to-response fixture
mapping. It never interprets evidence as instructions. The default fixture fits
`GatewayDemoOutput`; future agents must supply their own schema-specific fixtures.
The smoke demo closes and reconstructs the gateway and demonstrates durable reuse:

```powershell
.\.venv\Scripts\python.exe -m scripts.gateway_demo
```

## Groq adapter and rate limits

The small `GroqClient` HTTP adapter uses JSON-object mode and
`max_completion_tokens`, with local Pydantic validation. Both model names come
from configuration; trusted agent definitions assign the fast/smart tier. See
[Groq's API reference](https://console.groq.com/docs/api-reference) and
[structured output documentation](https://console.groq.com/docs/structured-outputs).

The limiter maintains per-model local request/token buckets, further constrained
by observed provider headers. Groq's request headers report daily allowance;
token headers report per-minute allowance. They are tracked separately. 429
handling honors `Retry-After`; other retries use bounded exponential backoff and
jitter. Server-requested delays may exceed the local backoff cap. Relevant model
observations and cooldowns survive restart. Missing headers retain conservative
local pacing. See [Groq's rate-limit documentation](https://console.groq.com/docs/rate-limits).

Configuration additions:

| Setting | Default | Purpose |
| --- | --- | --- |
| `LLM_REQUESTS_PER_MINUTE` | 10 | Local pacing, independent of provider RPD |
| `LLM_TOKENS_PER_MINUTE` | 6000 | Local token bucket ceiling |
| `LLM_BACKOFF_BASE_SECONDS` | 1 | Initial retry delay |
| `LLM_BACKOFF_MAX_SECONDS` | 60 | Cap before respecting server delay |

No fixed Groq free-tier limits are assumed. Account-wide activity outside this
application can still cause 429s; response headers and retry handling reconcile
the local state. If a single request exceeds the observed model bucket capacity,
it becomes pending instead of waiting forever.

**Live validation:** the wire format and failures are tested through
`httpx.MockTransport`; no real Groq call has been made. To use live mode, set
`GROQ_API_KEY`, both available model IDs, and `LLM_MODE=live` in `.env`. Select
models supporting JSON-object mode and use free-tier credentials. The application
does not upgrade plans or select paid fallback services.
# Groq extraction recovery

For the configured GPT-OSS models, `.env` can set `GROQ_REASONING_EFFORT=low`
to reduce reasoning work within the existing completion cap. The setting defaults
to empty (omit the API parameter); clear it when selecting a model that does not
support `reasoning_effort`. This does not increase call, output or daily budgets.
See [Groq reasoning parameters](https://console.groq.com/docs/reasoning).

Provider errors are mapped to safe categories for authentication, access, model
availability, invalid requests and failed JSON generation. Raw provider messages
and `failed_generation` are never saved or shown. A `json_validate_failed` response
uses the same single repair allowance as a locally invalid JSON response, and all
attempts remain budgeted. Other HTTP 400 responses are not automatically retried.

Projects offers **Retry proposal extraction** for a pending saved job after fixing
settings and restarting. It resends the saved excerpt only on that explicit action,
reuses the job and retains consumed budgets. Edited/confirmed baselines are not
overwritten; exhausted repair allowances cannot be reset with Retry. For failures
saved by older versions as `provider_http_error`, the exact cause remains unknown
until a new request returns a more specific category. Limited PDF coverage and
missing calendar deadlines still require supervisor review.

