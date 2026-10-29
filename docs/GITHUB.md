# Phase 3: GitHub reads and push triggers

## What is implemented

- `GitHubClient`: authenticated GET requests for repository metadata, invitations,
  branch heads, comparisons, commits, trees, and blobs.
- `accept_repository_invitation`: the single, separately defined PATCH operation.
- `AccessService`: discover matching invitations, accept after an explicit
  supervisor action, verify reads, and activate an onboarded project.
- `ReviewQueue`: project-scoped push deduplication and durable trailing-edge debounce.
- `PollingTrigger` and `PollingScheduler`: scheduled checks of active repositories,
  persisted ETags/cursors/backoff, and queued review cycles.
- `read_changes`: normal comparison or current-tree fallback for rewritten/missing
  history and comparisons at the API's file cap.
- `FakeGitHubClient`: deterministic remote fixtures for tests and demos.

This phase queues reviews. It does not run agents or advance `last_reviewed_sha`.
Phase 5 supplies the checkpointed review worker; Phase 8 supplies the UI buttons.

## Offline demo

```powershell
.\.venv\Scripts\python.exe -m scripts.github_demo
```

The demo creates a temporary database and a fictional repository, accepts a fake
invitation, observes a head, exercises an unchanged poll, merges two observed
pushes into one review cycle, reconstructs the DB/trigger, and detects rewritten
history. Time advances deterministically; there is no five-minute wait. Nothing
contacts GitHub, sends email, or invokes an LLM. To retain the fixture database:

```powershell
.\.venv\Scripts\python.exe -m scripts.github_demo --database data/github_demo.sqlite3
```

The persistent option adds a fictional project each run. Its baseline-confirmed
marker is explicitly a test fixture, not proposal extraction. The normal UI uses
real baseline confirmation. The fake remote remains in memory while the local
trigger is reconstructed, representing a remote service surviving an app restart.

## Authentication and access

For live reads, set `GITHUB_MODE=live` and provide an already-issued supervisor
user token in `GITHUB_TOKEN`. Never commit it. The wrapper uses the configured API
version (`GITHUB_API_VERSION`, currently `2026-03-10`) and follows no redirects.
If a repository was renamed/transferred, verify and update its configured identity
instead of silently following a redirect. All API URLs are assembled under
`https://api.github.com`; download and pagination URLs cannot redirect credentials.

Reading private repository content requires appropriate contents access. Invitation
access is separate: `repo:invite` alone does not grant code reads. GitHub documents
the supported token types/permissions for listing and accepting invitations in
the [invitation API reference](https://docs.github.com/en/rest/collaborators/invitations).
OAuth user tokens with the `repo` scope provide private code and invitation access,
but the token's permissions are broader than this wrapper's allowed operations.
Do not assume a fine-grained read-only PAT can accept invitations. A supervisor
can also accept in GitHub itself, then call `refresh_access` using a read-capable
token. OAuth app registration/callback UI is not implemented in Phase 3; token
provisioning must currently be done outside the application.

`AccessService.discover(project_id)` stores only invitations matching the project's
registered repository. `accept(project_id, invitation_id)` refreshes the list,
checks the ID/project association, calls the isolated acceptance function once,
and verifies repository metadata and the tracked branch. Confirmation of the
proposal baseline is required before activation. A readable empty repository can
activate and waits for its first commit. Paused projects are never auto-activated.

If PATCH succeeds but access verification fails, the accepted record remains and
the project stays pending access. Retry `refresh_access`, not PATCH. A transport
failure during PATCH is ambiguous: refresh the remote invitation list/access
before making another decision. The acceptance operation is not automatically
retried and is never exposed as an agent tool.

For an already onboarded project, application wiring is:

```python
from functools import partial
from app.config import Settings
from app.db import Database
from app.integrations.github.client import GitHubClient
from app.integrations.github.invitations import accept_repository_invitation
from app.integrations.github.access import AccessService

settings = Settings()
db = Database(settings.database_path)
db.migrate()
reader = GitHubClient(settings)
access = AccessService(db, reader, partial(accept_repository_invitation, settings))
# UI reads access.discover(project_id).
# Only the supervisor's Accept action calls access.accept(project_id, invitation_id).
# Dispose reader.close() on shutdown.
```

## Polling and debounce

The default branch is discovered at activation/first poll and then pinned in
`github_state.tracked_branch`. A renamed/deleted branch requires access review.
`refresh_access(project_id, branch=...)` can explicitly select a valid branch.
Only `active` projects with a confirmed baseline are polled.

Head polling requests GitHub's SHA media type. ETags are saved and sent back via
`If-None-Match`; a 304 creates no event and does not extend debounce. The configured
poll interval is a minimum; a larger `x-poll-interval` is respected. These behaviors
follow GitHub's [commit API](https://docs.github.com/en/rest/commits/commits) and
[REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).

New heads create `push_events` unique by project and SHA. A burst matures after
`DEBOUNCE_SECONDS` (default 300) since the last changed head observed by the app.
The event and cursor update are committed together. A mature burst creates one
cycle for the latest observed head, using the current `last_reviewed_sha` as its
base. Enqueue and event consumption are atomic. Returning to an already reviewed
SHA does not accidentally enqueue an intermediate stale head.

While any cycle is queued, running, or pending evidence review for a project, new
events stay unprocessed. They are coalesced after that cycle finishes, at which
point the base is read again from saved memory. This prevents parallel reviews
with stale bases. The later review worker must finish/resolve pending cycles;
Phase 3 intentionally leaves its queued cycles untouched.

Restart preserves the branch, ETag, SHAs, pending pushes, debounce time, queued
cycles, and polling backoff. Per-project expiring leases suppress concurrent
polls. A response from a worker whose lease was replaced cannot overwrite a newer
cursor. Use one polling scheduler in the application; leases also protect against
overlapping runs. The thread is not started merely by importing a module.

Each poll makes bounded read calls; failures produce safe reason codes without
logging provider bodies. 401/configuration failures retain their cursor and defer.
Missing repository/branch or denied access returns the project to pending access.
Primary and secondary rate-limit delays are persisted and applied to all projects
because the current configuration uses one GitHub credential. `Retry-After` and
the UTC epoch reset header take precedence over the local retry delay. Subsequent
scheduled attempts use capped exponential backoff, with no tight retry loop.

Run polling against projects already onboarded and activated by application code:

```powershell
.\.venv\Scripts\python.exe -m scripts.poll_github --once
.\.venv\Scripts\python.exe -m scripts.poll_github
```

The runner requires live mode; use the offline demo for fake mode. It does not
accept invitations or execute reviews. `Ctrl+C` stops the scheduler; current reads
must return before shutdown completes. No live runner was used during validation.

## Changes, limits, and trigger replacement

`read_changes(reader, repo, before_sha, after_sha)` produces a diff when the base
is an ancestor of the new head. Diverged/behind histories use the current tree and
set `history_rewritten`. An unavailable comparison base is conservatively flagged
as `history_unavailable`, rather than asserted to be a proven force push. An empty
base uses an initial full-tree snapshot. A comparison at 300 files also switches
to the tree because GitHub caps its changed-file list. Truncated or oversized tree
results raise `incomplete_result`; they are never presented as a complete review.
Tree entries include blob SHAs for bounded, subsequent GET reads. File triage,
patch truncation, and evidence/claim verification are Phase 5 work.

Polling observes snapshots, not every push between intervals. It cannot detect a
brief head change that returns to the same SHA before the next poll. The debounce
clock therefore measures observation time. Rewrites are detected relative to the
reviewed base; a rewrite that preserves that base as an ancestor still yields a
valid diff and need not be flagged.

`PushTrigger` is a protocol, and `ReviewQueue.observe_head` is transport-neutral.
An optional future webhook receiver must authenticate its signature, resolve the
registered project/repository/branch, ignore deletions/untracked branches, and
verify the current remote head before calling the same queue. Passing a webhook
payload SHA directly could regress the cursor on out-of-order delivery. No public
webhook listener or FastAPI dependency is included in this polling-first phase.

## Validation scope

Tests cover the GET-only client boundary, the one isolated PATCH, ETag/304 behavior,
safe invitation matching/pagination, access checks, concurrency/leases, restart,
burst merging, duplicate heads/cycles, rate limits, project isolation, malformed
responses, redirects, normal diffs, rewritten history, and API truncation handling.
HTTP behavior is exercised through mocked transports; live GitHub permissions and
account behavior remain unverified. No real repository or invitation was changed.
