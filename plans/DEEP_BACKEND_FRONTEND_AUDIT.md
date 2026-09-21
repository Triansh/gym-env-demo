# Deep Backend + Frontend Code Audit

Date: 2026-09-21
Repository: `Triansh/gym-env-demo`
Audited refs: `main` (latest merged backend fixes) and the repository default `frontend`
Reference material: Milestone 1, Milestone 2 backend, and Milestone 2 frontend plans stored in the repository.

## Scope and audit posture

This review covered the benchmark/task contract, environment lifecycle, parallel execution, cancellation, timeout semantics, persistence, grader correctness, API contracts, artifact handling, frontend state/polling, metrics, security boundaries, test quality, and implementation/documentation drift.

The linked ChatGPT share could not be retrieved successfully in this environment, so I did not infer its contents. The repository's plan documents and the current code were used as the authoritative available specification.

## Executive assessment

The code has a solid MVP skeleton: bounded worker loops, unique rollout IDs, disposable Docker environments, process isolation for the browser agent, SQLite persistence, structured statuses, and a useful inspection UI.

However, several issues are correctness blockers rather than polish items. The biggest ones are:

1. Reward is still derived from the agent's final answer, not independently verified from the environment.
2. Concurrent jobs are not globally bounded and can reuse the same host ports.
3. Port-conflict cleanup can kill another rollout's live containers.
4. Cancellation is not a real cancellation protocol; running rollouts can resurrect themselves after cancellation.
5. Shared SQLite updates are read-modify-write operations rather than atomic state transitions, so concurrent updates can lose data or produce stale aggregates.
6. User-supplied task IDs can escape the artifact directory and write outside the intended root.
7. Expected answers are exposed by normal API responses.
8. A failed task-artifact write silently causes the grader to fall back to the repository's default `tasks.json`, which can grade against the wrong benchmark.
9. The environment fixture is not explicitly verified during real rollout execution.
10. The persisted-job implementation and its lifecycle recovery are incomplete.
11. Frontend polling/fetches can race and overwrite newer UI state.
12. Success-rate calculations mix infrastructure failures with evaluated failures.
13. Several of the tests do not exercise the real concurrent worker path and some use the production DB by default.

This should be treated as a hardening pass before trusting parallel benchmark numbers.

---

# Priority model

- **P0 — Blocker:** can invalidate benchmark results, cross-contaminate rollouts, leak benchmark answers/secrets, or corrupt lifecycle state.
- **P1 — High:** can cause wrong metrics, stuck jobs, significant resource waste, or brittle operation under normal use.
- **P2 — Medium:** correctness/maintainability/performance improvements that become important as the MVP grows.
- **P3 — Cleanup:** quality, documentation, and convenience improvements.

---

# P0 — Blockers

## P0-02 — Concurrency limit is per job, not global

**Files:** `backend/workers.py`, `backend/api/server.py`

Every call to `run_job()` creates its own worker pool of `MAX_CONCURRENT_ROLLOUTS`.

Therefore:

- 1 job -> max 5 active rollouts (current config)
- 5 simultaneous jobs -> potentially 25 active rollouts
- 20 simultaneous jobs -> potentially 100 active rollouts

That defeats the purpose of the bounded-concurrency setting and can exhaust Docker, Chrome, memory, CPU, and API quota.

It also directly causes the port collision problem below.

**Required change:** enforce a single application-wide concurrency budget.

Simplest design:
- one process-wide async queue;
- one worker pool;
- jobs contribute rollout IDs to the shared queue.

At minimum:
- a global `asyncio.Semaphore(MAX_CONCURRENT_ROLLOUTS)` around actual rollout execution.

The queue-based design is cleaner because the same worker slot can also own the environment-port allocation.

---

## P0-03 — Worker-slot port assignment collides across jobs

**Files:** `backend/rollout/runner.py`, `backend/workers.py`

`_port_for_slot(slot)` returns fixed ports:

`ROLLOUT_PORT_BASE + slot`

Slot 0 is therefore the same host port for every job.

Two jobs starting at the same time can both assign:
- Metabase port 3100
- PostgreSQL port 3200

Project names are unique, but host ports are not.

**Required change:** allocate ports globally, not from a local worker index.

Better:
- PostgreSQL should not be exposed to the host at all. Metabase already reaches it through the Compose network.
- Allocate only a Metabase host port.
- Reserve that port atomically before starting the environment.
- Release it only after teardown finishes.
- Prefer binding to `127.0.0.1`.

An even simpler option is to let Docker allocate an ephemeral published port and inspect the actual mapping, avoiding a hand-written port allocator.

---

## P0-04 — Cleanup can kill another job's active environment

**File:** `backend/environment/manager.py`

`_kill_containers_on_ports()` scans every Docker container on the host and force-removes anything using the target port.

Combined with P0-02/P0-03 this becomes dangerous:

1. Job A is using port 3100.
2. Job B also gets worker slot 0 and tries to use 3100.
3. Job B calls `destroy()` as part of `start(clean=True)`.
4. Job B scans host containers.
5. Job B force-removes Job A's container.
6. Job A now fails because its environment disappeared.

This is cross-job destructive behavior.

**Required change:** never perform global host cleanup by port.

Destroy only resources owned by the rollout:
- unique Compose project name;
- Compose-managed services;
- Compose project network;
- Compose project volumes.

Once port allocation is fixed, the port-scan cleanup routine should be deleted entirely.

---

## P0-05 — Cancellation is not a valid state transition

**Files:** `backend/db.py`, `backend/workers.py`, `backend/rollout/runner.py`

`cancel_job()` immediately changes every non-terminal rollout to `CANCELLED`.

But a running rollout is still executing and later calls `_update(... status=PASSED/FAILED/ERROR/TIMEOUT ...)` in its `finally` block.

So this sequence is possible:

`RUNNING -> CANCELLED -> PASSED`

The final write wins.

The job can therefore report a cancellation and later be partially or fully resurrected.

There is a second issue: queued workers only check the cancellation status before starting a rollout. There is no cancellation token for an already-running agent process.

**Required change:** make cancellation a first-class cooperative protocol.

Recommended semantics:
- QUEUED rollout: transition directly to CANCELLED.
- STARTING/RUNNING/GRADING rollout: request cancellation, do not directly mark terminal.
- Agent process receives a cancellation signal/event.
- Parent terminates the browser process if necessary.
- Finalization uses a compare-and-set transition so a terminal CANCELLED state cannot be overwritten.
- Job cancellation should also be transactionally serialized with rollout state changes.

---

## P0-06 — Rollout state updates are vulnerable to lost-update races

**File:** `backend/db.py`

`update_rollout()` does:

1. read rollout;
2. release lock;
3. mutate Python object;
4. write rollout;
5. recompute job.

Even with the SQLite lock, the read and write are not one atomic state transition.

Example:
- Thread A reads RUNNING.
- Thread B reads RUNNING.
- A writes GRADING.
- B writes CANCELLED.
- A or B can overwrite fields based on an old object.

The same issue exists at job level:

`_recompute_job_progress()` reads the job and all rollouts, computes counts in Python, then writes the job later. Another rollout can change state during that window.

Therefore two concurrent rollout completions can produce a stale aggregate.

**Required change:** move state transitions into a single SQLite transaction.

For example:
- `BEGIN IMMEDIATE`;
- `UPDATE rollouts SET ... WHERE rollout_id = ? AND status IN (...)`;
- inspect affected-row count;
- compute aggregate counts from the current DB snapshot;
- update jobs row;
- commit.

Use explicit allowed transition sets.

This is much safer than relying on a Python RLock around separate reads/writes.

---

## P0-07 — User-supplied task ID can escape the artifact root

**File:** `backend/rollout/runner.py`

Artifact directory:

`ARTIFACTS_ROOT / job_id / task_id / attempt`

Task IDs are only checked to be non-empty strings.

A task ID containing path traversal such as `../../...` or an absolute path can change the resulting filesystem target. `Path / absolute_path` can also discard the expected prefix completely.

That means the current task ingestion boundary is also a filesystem-write boundary.

**Required change:** never use user-controlled IDs as filesystem path components.

Simplest:
- use `<job_id>/<rollout_id>/` as the physical artifact directory;
- store task ID only inside metadata/result JSON.

If a human-readable path is desired, sanitize it strictly and keep UUIDs as the real directory identity.

---

## P0-08 — Expected answers are exposed through ordinary API responses

**Files:** `backend/models.py`, `backend/api/server.py`

`Job.to_dict()` includes `tasks[].expected_answer`.

Therefore:
- `GET /api/jobs/{job_id}` exposes expected answers.
- `GET /api/jobs/{job_id}/tasks.json` explicitly returns them.
- rollout detail exposes `grader_result.ground_truth`.

The Milestone 2 frontend contract says expected answers should not accidentally become available during the normal workflow.

Even though the current frontend does not display the answer by default, the browser can retrieve it directly.

**Required change:**
- Public job/task API models must contain task ID + prompt only.
- Keep expected answers in the backend's evaluation-only representation.
- Do not return `ground_truth` from normal rollout detail.
- Add a separate privileged/debug endpoint only if a human actually needs it.

For this local hackathon MVP, simply removing the answer from normal API serialization is enough.

---

## P0-09 — Failed task-artifact persistence can cause grading against the wrong task file

**File:** `backend/rollout/runner.py`

The runner uses:

`job artifact tasks.json if it exists, otherwise repository DEFAULT_TASKS_FILE`

That fallback is unsafe.

Consider:
1. User uploads custom tasks.
2. DB job creation succeeds.
3. Writing `artifacts/<job>/tasks.json` fails.
4. Rollout starts.
5. Grader cannot find job task file.
6. Runner silently loads repository `tasks.json`.

The agent can then be evaluated against a completely different benchmark definition.

This is a direct correctness violation.

**Required change:** make the DB job snapshot the source of truth.

The job already contains:
- task ID;
- prompt;
- expected answer.

Pass that immutable task snapshot to the grader.

Do not make grading depend on a secondary artifact file.

Artifact JSON should be evidence/copy, not runtime authority.

---

## P0-10 — Hard-coded application credentials in source

**File:** `backend/agent/prompts.py`

The prompt builder supplies fallback Metabase credentials directly from source code, including a plaintext password.

This violates the Milestone 1 secret-handling requirement.

Do not copy the secret into the audit report, but treat it as exposed.

**Required change:**
- Remove credential defaults from Python.
- Require environment variables.
- Fail fast with a clear configuration error if credentials are missing.
- Rotate the credential if it is real/reused anywhere.
- Keep only placeholders in `.env.example`.

---

## P0-11 — Root route references an undefined variable

**File:** `backend/api/server.py`

`serve_index()` refers to `FRONTEND_INDEX`, but that symbol is not defined in the inspected file.

The static frontend mount is added afterward, so the explicit root endpoint can win route matching.

This should be fixed even though the static mount may mask it in some execution paths.

**Required change:** either:
- define `FRONTEND_INDEX = frontend_path / "index.html"`, or
- remove the redundant root endpoint and let the static mount own `/`.

The latter is simpler.

---

# P1 — High

## P1-01 — Job status stays QUEUED while rollouts are actively starting/running

**Files:** `backend/db.py`, `backend/workers.py`

The job only transitions from QUEUED to RUNNING inside aggregate recomputation once at least one rollout is completed.

So during the initial startup window:
- job status = QUEUED
- rollout status = STARTING/RUNNING

That contradicts the intended lifecycle and makes the frontend misleading.

**Fix:** set job RUNNING and `started_at` before launching the worker pool, or on the first successful rollout STARTING transition.

---

## P1-02 — No global overall rollout/job task supervisor

**Files:** `backend/api/server.py`, `backend/workers.py`

Each POST creates an untracked `asyncio.create_task(run_job(...))`.

There is no registry of active job tasks, no shutdown coordination, and no central cancellation of those asyncio tasks.

Consequences:
- server shutdown/reload can interrupt orchestration;
- active jobs are not gracefully drained;
- background task exceptions are harder to diagnose;
- there is no place to implement robust job cancellation.

**Fix:** introduce a JobManager/Scheduler object that owns:
- active jobs;
- shared queue;
- worker pool;
- shutdown;
- cancellation events.

---

## P1-03 — Persisted SQLite jobs are not recovered on startup

**Files:** `backend/db.py`, `backend/api/server.py`

The code now has SQLite persistence, but `on_startup()` is empty.

If the API process dies while a rollout is RUNNING:
- the DB retains RUNNING/STARTING/GRADING;
- no worker resumes it;
- no startup repair marks it interrupted;
- the environment may be left alive.

A later frontend can reconnect to a permanently stale job.

**Fix:** explicitly choose a restart contract.

For a local MVP, simplest:
- on startup, find non-terminal rollouts from a previous process;
- mark them ERROR with `SERVER_RESTART`/interrupted classification;
- optionally run cleanup for their recorded environment IDs;
- mark affected jobs terminal once all rollouts are reconciled.

For real resume semantics, persist executable queue state and lease/owner information.

---

## P1-04 — Grader timeout is not actually hard

**File:** `backend/rollout/runner.py`

The grader runs inside:

`with ThreadPoolExecutor(...) as tpe`

Then `future.result(timeout=GRADER_TIMEOUT)` can raise a timeout.

But leaving the `with` block invokes `shutdown(wait=True)`, which waits for the worker thread.

So the configured timeout does not guarantee that execution stops at 30 seconds.

**Fix:** the grader is CPU-light and local, so the simplest solution is to run it synchronously and drop the artificial thread timeout.

If a hard timeout is truly required, use a process that can be terminated.

---

## P1-05 — The declared rollout timeout is unused

**File:** `backend/configs.py`, `backend/rollout/runner.py`

`DEFAULT_ROLLOUT_TIMEOUT` exists but is never enforced.

Actual wall-clock duration can be roughly:

environment startup timeout + agent timeout + grader timeout + teardown time

which is materially larger than the declared rollout timeout.

**Fix:** define one rollout deadline and calculate remaining time before every stage.

Or remove the unused setting and explicitly state that the system uses per-stage limits only.

---

## P1-06 — Fixture initialization is not explicitly verified during real execution

**Files:** `backend/environment/manager.py`, `backend/rollout/runner.py`

`EnvironmentManager.verify_expected_state()` exists but the real rollout path never calls it.

There is also no explicit `initialize()` stage in the rollout runner.

The Docker init script does run on a fresh Postgres volume, but successful process startup does not prove the expected fixture actually loaded.

Worse, `restore_db.sh` ends with:

`pg_restore ... || true`

which converts restore failure into apparent success.

This combination can produce:
- Metabase healthy;
- fixture restore failed;
- agent runs against the wrong state.

**Fix:**
- remove `|| true`, or capture and classify restore failure;
- verify known fixture state after startup;
- fail the rollout before agent execution when the fixture is wrong;
- save the startup/restore logs.

---

## P1-07 — SQLite read serialization is heavier than necessary

**File:** `backend/db.py`

The comment says WAL permits concurrent reads, but every read acquires the same RLock and uses the same connection.

So reads are effectively serialized.

Also, each rollout update recomputes job progress by loading every rollout.

This creates roughly:
- many full reads;
- many JSON deserializations;
- many writes;
- repeated job snapshots.

At 30 rollouts this is tolerable; at larger jobs it becomes needlessly expensive.

**Fix:**
- use short-lived read connections for reads;
- use a write connection/transaction for mutations;
- query aggregate counts directly in SQL;
- add summary-only query methods so list endpoints do not deserialize transcript blobs.

---

## P1-08 — Job and rollout summary endpoints deserialize huge payloads

**File:** `backend/db.py`

`get_rollouts_for_job()` selects `*` and reconstructs:
- transcript;
- screenshots;
- grader result;

even when the API only needs:
- status;
- attempt;
- reward;
- duration;
- error type.

Likewise, job history loads full task JSON and expected answers just to render a table.

This creates unnecessary CPU and memory cost and increases answer-leak exposure.

**Fix:** add purpose-specific SQL queries:
- `get_rollout_summaries_for_job()`
- `get_job_history_rows()`
- `get_job_task_aggregates()`

Load full transcript/grader payloads only for `GET /api/rollouts/{id}`.

---

## P1-09 — Job aggregate metrics use the wrong denominator

**Files:** `backend/db.py`, `backend/api/server.py`, `frontend/index.html`

Current pass-rate calculations use:

`passed / total_rollouts`

The Milestone 2 plan explicitly distinguishes:
- execution progress;
- evaluated success rate;
- infrastructure errors.

With errors/timeouts, the current metric penalizes the agent for infrastructure failures.

For example:
- 8 PASS
- 2 TASK_FAILED
- 2 ERROR

Current rate = 8/12 = 66.7%.

Evaluation-only rate = 8/(8+2) = 80%.

**Fix:** expose separate metrics:
- execution progress = terminal / total;
- evaluated success rate = PASSED / (PASSED + FAILED);
- infrastructure failures = ERROR + TIMEOUT;
- cancellation count separately.

Use the same semantics in backend and frontend.

---

## P1-10 — Task counters and status semantics are internally inconsistent

**File:** `backend/api/server.py`

Problems include:

- `completed` omits CANCELLED even though DB job progress counts CANCELLED as completed.
- `running` counts only RUNNING, not STARTING or GRADING.
- task status can say RUNNING while all non-terminal rollouts are STARTING/GRADING.
- frontend failure count ignores ERROR-only tasks.
- task "success rate" uses total attempts instead of evaluated attempts.

**Fix:** define one derived-state function in the backend and return:
- queued;
- starting;
- running;
- grading;
- passed;
- failed;
- error;
- timeout;
- cancelled;
- evaluated.

The frontend should only render these server-provided semantics.

---

## P1-11 — Cancellation and terminal writes need compare-and-set protection

This overlaps P0-05/P0-06 but deserves its own implementation item.

Any code that writes terminal state should require an allowed previous state.

Example concept:

`UPDATE rollouts
SET status='PASSED', ...
WHERE rollout_id=?
AND status='GRADING'`

If it updates zero rows:
- the rollout was already cancelled or otherwise finalized;
- do not overwrite it.

This pattern also makes duplicate callbacks/idempotency safe.

---

## P1-12 — Child agent process is not joined/closed on success

**File:** `backend/rollout/runner.py`

On success, the parent receives the queue payload but does not explicitly `join()` the child.

The queue is also not explicitly closed.

Repeated rollouts can accumulate process/pipe resources.

**Fix:** always:
- `p.join()` after receiving the result;
- close the queue;
- terminate/kill only when still alive;
- isolate all process cleanup in a dedicated helper.

---

## P1-13 — IPC payload includes raw screenshot bytes

**File:** `backend/rollout/runner.py`

The child sends `step_screenshots` containing image bytes through a multiprocessing queue.

That is expensive and can block on large trajectories.

It also means the parent timeout is partly a timeout on IPC serialization, not purely agent execution.

**Fix:** child writes screenshots directly into the rollout artifact directory and returns only metadata/path strings.

This is both simpler and faster.

---

## P1-14 — Screenshot filenames are derived from action names

**File:** `backend/rollout/runner.py`

`{idx}_{action_name}.png` assumes tool/action names are filesystem-safe.

Prefer a stable safe filename:
- `001.png`
- store action name inside transcript JSON.

---

## P1-15 — API is synchronous for SQLite work inside async job creation

**File:** `backend/api/server.py`

`create_new_job()` is async but calls synchronous:
- DB creation;
- filesystem writes

directly on the event-loop thread.

For large uploads or many attempts this blocks request processing.

**Fix:** either make the endpoint synchronous, or move DB/file operations to a controlled threadpool.

Since the rest of the API already uses sync endpoints for blocking SQLite operations, making this endpoint sync after request parsing is probably simplest.

---

## P1-16 — Frontend polling can overlap and return out of order

**File:** `frontend/index.html`

Polling uses `setInterval(async ...)`.

If one `refreshDashboard()` call takes longer than the polling period:
- the next interval starts another request;
- responses may complete out of order;
- an older response can overwrite a newer one.

The same pattern can affect `selectRollout()`.

**Fix:** use a single async polling loop:

`while(active) {
  await refresh();
  await sleep(interval);
}`

Or use an `AbortController` + request generation number.

---

## P1-17 — Frontend polling is 10 seconds, not the planned 1–2 seconds

**File:** `frontend/index.html`

The plan recommends roughly 1–2 seconds for live job progress; the implementation polls every 10 seconds.

That makes a hackathon demo feel stale.

**Fix:** 1.5–2 seconds while active, with a small backoff on repeated errors.

Do not poll screenshots/transcripts continuously unless selected, as the plan already recommends.

---

## P1-18 — Frontend hard-codes maximum attempts

**File:** `frontend/index.html`

Backend exposes `/api/config`, but the UI uses:

`Math.min(10, state.attempts + delta)`

If backend configuration changes, UI and API can disagree.

**Fix:** load max attempts from `/api/config` and clamp to that value.

---

## P1-19 — Frontend task IDs are inserted into inline JavaScript unescaped

**File:** `frontend/index.html`

Examples include:
- `onclick="selectTask('${t.task_id}')"`
- `onclick="selectRollout('${r.id}')"`

Task IDs are user-controlled and not restricted to safe characters.

A quote inside a task ID can break the attribute and inject JavaScript.

**Fix:** stop using inline JS with user-controlled strings. Prefer:
- event listeners;
- data attributes;
- DOM element references.

At minimum, HTML/JS-escape values correctly.

---

## P1-20 — Artifact URLs are inserted without robust attribute escaping

**File:** `frontend/index.html`

Screenshot URLs are interpolated directly into HTML attributes.

Artifact paths include task IDs, which are user-controlled.

This compounds P0-07 and P1-19.

**Fix:** create elements with DOM APIs or HTML-escape the complete URL before interpolation.

---

## P1-21 — Frontend time display interprets UTC timestamps as local timestamps

**Files:** backend timestamp generation + `frontend/index.html`

Backend writes naive `datetime.utcnow().isoformat()` strings with no timezone marker.

The browser then passes those strings to `new Date(...)`, which interprets them as local time.

The rendered history timestamp can therefore be shifted.

**Fix:** emit timezone-aware UTC:
`datetime.now(timezone.utc).isoformat()`

and/or use a trailing `Z`.

---

## P1-22 — Test suite does not exercise the real worker/concurrency implementation

**Files:** `tests/test_m2_backend.py`, `tests/test_mock_rollouts.py`

The strongest "failure isolation" test creates a custom worker in the test itself.

The table-driven failure tests directly update SQLite state instead of running:
- real `_worker()`;
- real `run_job()`;
- real `execute_rollout()`.

So important properties are not actually tested:
- worker exception isolation;
- concurrent execution;
- slot/port allocation;
- cancellation;
- process timeout;
- cleanup;
- terminal write races.

**Fix:** create tests that instrument the real worker/runner with injectable fake executors.

---

## P1-23 — Some tests write to the default production DB path

**File:** `tests/test_mock_rollouts.py`

Several tests instantiate:

`SQLiteStore()`

instead of passing a temp path.

That can modify `backend/deeptune.db` during the test run.

**Fix:** every test should inject a temporary DB path, or use a fixture that sets the DB dependency before store creation.

Also avoid module-level DB singletons during tests.

---

## P1-24 — API tests can accidentally launch real rollout jobs

**File:** `tests/test_m2_backend.py`

POST `/api/jobs` starts `asyncio.create_task(run_job(...))`.

The tests do not consistently replace the worker/executor with a fake.

That makes API unit tests coupled to:
- Docker;
- Metabase;
- Playwright;
- Gemini;
- timing.

**Fix:** dependency-inject the job scheduler / executor and make API tests pure.

Then have one separate explicit end-to-end test suite for the real environment.

---

## P1-25 — Benchmark dependency versions are not actually pinned

**Files:** `requirements.txt`, `VERSION_MATRIX.md`

The plan calls for reproducibility.

Current requirements use ranges such as:
- `google-genai>=...`
- `playwright>=...`

and VERSION_MATRIX uses `^1.49.0`/minimum Python versions.

This is not a reproducible environment.

There is also a direct mismatch:
- VERSION_MATRIX says Gemini 2.5 Computer Use Preview;
- runtime config says Gemini 3 Flash Preview;
- frontend displays Gemini 3 Flash Preview.

**Fix:** maintain one version manifest:
- Python exact version;
- package exact versions;
- browser version;
- Metabase image digest/tag;
- computer-use-preview submodule commit;
- model identifier.

Generate frontend display data from that configuration instead of duplicating it.

---

## P1-26 — Artifact capture is incomplete relative to the plan

**File:** `backend/rollout/runner.py`

The plan calls for:
- `metadata.json`;
- transcript;
- screenshots;
- agent log;
- Metabase log;
- grader output;
- result.

Current runner writes some of these, but not a full `metadata.json` and not a captured `metabase.log`.

The result also does not fully record:
- model version;
- environment version;
- task snapshot/hash;
- computer-use-preview commit.

**Fix:** write a single immutable `metadata.json` at rollout start.

---

## P1-27 — Environment cleanup claims success without verifying it

**File:** `backend/environment/manager.py`

Cleanup logs "environment destroyed" even when:
- `compose down` times out;
- containers remain;
- cleanup commands return errors.

This can make resource leakage invisible.

**Fix:** after destroy:
- verify project containers absent;
- verify project network absent;
- verify project volumes absent;
- return/record cleanup_error if not.

Do not log success unless verified.

---

## P1-28 — `restore_db.sh` suppresses restore failures

**File:** `backend/restore_db.sh`

It ends the restore command with `|| true`.

That is dangerous in evaluation infrastructure because the process reports success even when the fixture load fails.

**Fix:** let restore failure fail the initialization path, capture the error, and make the rollout ERROR before agent execution.

---

## P1-29 — Default Postgres host port is unnecessary

**File:** `backend/docker-compose.yml`

Metabase reaches Postgres through the Compose service name `postgres`.

There is no reason for the benchmark to publish Postgres to the host.

Publishing it:
- increases attack surface;
- creates another collision problem;
- complicates isolation.

**Fix:** remove the Postgres `ports` mapping entirely.

---

## P1-30 — Compose `container_name` is unnecessary complexity

**File:** `backend/docker-compose.yml`

Compose project names already provide isolated service naming and networking.

Explicit `container_name` adds:
- naming collision risk;
- scaling limitations;
- extra cleanup code.

**Fix:** remove `container_name` and rely on Compose project scoping.

---

## P1-31 — Hardcoded localhost HTTP ports are exposed to all interfaces

**File:** `backend/docker-compose.yml`

Host port mappings without an explicit host address typically bind broadly.

For a local-only benchmark, bind to:
`127.0.0.1:<port>:3000`

and do not publish Postgres.

---

## P1-32 — API artifact path containment check is brittle

**File:** `backend/api/server.py`

The check uses:

`str(safe_path).startswith(str(ARTIFACTS_ROOT.resolve()))`

String-prefix containment is not a reliable filesystem boundary check.

A sibling directory whose name starts with the same prefix can pass the string test.

**Fix:** use `Path.is_relative_to()` on supported Python versions, or a proper `os.path.commonpath()` comparison.

Also restrict the served file types rather than exposing an arbitrary artifact subtree.

---

# P2 — Medium

## P2-01 — History queries are implemented in Python instead of SQL

`get_job_history()` loads all jobs and filters/searches/paginates them in memory.

Use SQL `WHERE`, `LIKE`, `COUNT`, `LIMIT`, and `OFFSET` for scalable behavior.

---

## P2-02 — Store API mixes data access and business-state recomputation

`SQLiteStore` is doing:
- persistence;
- lifecycle state transitions;
- aggregate calculation;
- cancellation behavior.

A dedicated `JobRepository` + `JobManager` split would make race handling much easier.

Simpler MVP structure:

`JobManager`
-> lifecycle/concurrency/cancellation

`SQLiteRepository`
-> atomic persistence

`RolloutExecutor`
-> environment/agent/grader

`ArtifactStore`
-> files

---

## P2-03 — Remove dead/legacy `RolloutResult`

**File:** `backend/rollout/models.py`

It uses fields such as `finished_at` and `artifacts_dir`, which do not match the active runner's `completed_at` and `artifact_path`.

It is not part of the current execution path.

Delete it or make it the one canonical result model.

---

## P2-04 — Server comments describe an in-memory store, but implementation is SQLite

**File:** `backend/api/server.py`

The architecture comment says state is in-memory "per M2 spec", but current code uses SQLite.

Update the docs/plans to reflect the actual architecture.

---

## P2-05 — Frontend only shows the file count, not the requested task preview

The frontend plan calls for a pre-submit preview of the detected tasks and their shortened prompts.

Current file selection only shows count/name.

Add a compact preview panel without displaying answers.

---

## P2-06 — Frontend has hard-coded model/version text

Model text appears in multiple places in HTML.

Use `/api/config` to populate:
- model;
- max attempts;
- concurrency;
- benchmark version.

This eliminates drift.

---

## P2-07 — Frontend has no dedicated active-job "Refresh" control

The plan calls for manual refresh regardless of status.

Add one next to the active job header.

---

## P2-08 — History filters do not map cleanly to rollout failures

The UI offers "FAILED" at job level, but job-level code generally marks mixed jobs `COMPLETED`.

Either:
- remove the job FAILED filter;
- or define it as "completed jobs containing one or more failed rollouts".

Likewise add ERROR/TIMEOUT filters if useful.

---

## P2-09 — Frontend re-fetches full rollout details on every refresh for selected rollout

During polling, `refreshDashboard()` can call `selectRollout()` again.

This is wasteful if the rollout is terminal and unchanged.

Use:
- lightweight rollout summary while running;
- full detail only on selection and when state changes;
- ETag/version if desired.

---

## P2-10 — Job creation should be idempotency-aware

A browser double-click, retry, or network failure after the server accepted the request can create two jobs.

For a demo, disable the button while creating.

For a more robust platform, accept an `Idempotency-Key` and return the original job.

---

## P2-11 — Large task uploads have no size/task-count guard

`uploaded_file.read()` loads the full upload into memory.

Add reasonable MVP limits:
- maximum JSON size;
- maximum number of tasks;
- maximum attempts;
- maximum calculated rollouts.

This also prevents accidentally creating hundreds/thousands of browser environments.

---

## P2-12 — `JobCreateRequest` silently ignores unknown fields

`extra="ignore"` is friendly, but it conflicts with the plan's strict-input philosophy.

Prefer `extra="forbid"` for API payloads.

---

## P2-13 — Attempt parsing uses truthiness instead of explicit None checks

Current logic uses patterns like:

`req.attempts or req.attempts_per_task or attempts_val`

So `0` does not behave as an explicit invalid input; it falls back.

Use `is not None`.

---

## P2-14 — `load_dotenv(override=True)` is surprising operational behavior

**Files:** `backend/rollout/runner.py`, `backend/agent/runner.py`

Import-time environment loading with override can replace actual process configuration unexpectedly.

Prefer:
- `override=False`;
- or load explicitly in the CLI entrypoint.

---

## P2-15 — Browser network boundaries are only prompt-level

The agent prompt says "do not use other access", but there is no technical browser-side policy preventing navigation outside the local Metabase environment.

For a stronger benchmark boundary:
- allow only the Metabase origin;
- block external navigation/requests;
- optionally block file:// URLs.

This also reduces credential-exfiltration risk.

---

## P2-16 — Use a content-addressed/hashed task snapshot

Store:
- task JSON SHA-256;
- model name;
- environment version;
- runner code revision.

This makes later result comparisons meaningful.

---

## P2-17 — Save environment logs on startup/cleanup failures

Capture:
`docker compose logs --no-color`

into `metabase.log`/environment log artifact.

This is more useful than only writing a Python exception message.

---

# P3 — Cleanup / Quality

## P3-01 — Use timezone-aware datetime everywhere

Replace deprecated/ambiguous naive UTC helpers with timezone-aware UTC values.

---

## P3-02 — Remove unused imports

Examples exist in `backend/rollout/runner.py` and elsewhere.

This makes the concurrency code easier to reason about.

---

## P3-03 — Centralize status transition logic

Avoid scattered direct assignments such as:

`rollout.status = ...`

Define a small state machine with allowed transitions.

---

## P3-04 — Add explicit persistence/version migration metadata

If SQLite remains:
- schema version table;
- migration function;
- startup migration logging.

---

## P3-05 — Add frontend unit-ish tests for derived state

At minimum test:
- pass/fail/error/timeout rendering;
- success-rate denominator;
- task-state aggregation;
- polling race prevention;
- URL/artifact generation.

---

# Recommended simplified architecture

The current system has accumulated several layers while still being an MVP. The cleanest simplification is:

```
FastAPI
  |
  v
JobManager
  |
  +---- SQLiteRepository
  |
  +---- Global rollout queue
  |
  +---- N workers
           |
           v
      execute_rollout()
        |
        +-- EnvironmentManager
        +-- AgentRunner
        +-- Grader
        +-- ArtifactStore
```

Key invariants:

```
ONE ACTIVE ROLLOUT
    =
ONE QUEUE LEASE
+ ONE UNIQUE ENVIRONMENT
+ ONE UNIQUE METABASE PORT
+ ONE AGENT PROCESS
+ ONE TERMINAL RESULT
```

and:

```
GLOBAL MAX ACTIVE ROLLOUTS
<= MAX_CONCURRENT_ROLLOUTS
```

and:

```
terminal rollout state is immutable
```

The database should own lifecycle state, while the environment/agent runner owns execution.

---

# Recommended implementation order

## Phase 1 — Fix benchmark correctness

1. Remove/secure hard-coded credentials.
2. Make task snapshot come from the job DB record; remove default-file fallback.
3. Decide grader contract: environment-state grader vs answer-only benchmark grader.
4. Stop returning expected answers from normal API responses.
5. Fix artifact path construction to use rollout UUIDs.
6. Make rollout status transitions atomic.

## Phase 2 — Fix concurrency/isolation

1. Replace per-job worker pools with one global bounded queue/pool.
2. Allocate unique Metabase ports globally.
3. Remove host-wide port/container killing.
4. Remove Postgres host publication.
5. Remove `container_name`.
6. Add real cancellation tokens/process termination.
7. Join/close child processes and queues.

## Phase 3 — Fix lifecycle/recovery

1. Set job RUNNING when execution actually starts.
2. Add startup recovery for orphaned RUNNING rollouts.
3. Add one overall rollout deadline.
4. Make cleanup verified and observable.
5. Persist metadata/log artifacts.

## Phase 4 — Fix metrics and UI

1. Separate execution progress from evaluation success rate.
2. Fix task status counters.
3. Replace overlapping polling with serialized polling.
4. Reduce polling interval to ~2s.
5. Pull max attempts/model/concurrency from backend config.
6. Remove inline event handlers for user-controlled IDs.
7. Add task preview and active-job refresh.

## Phase 5 — Tests

Add the following as required regression coverage:

- two simultaneous jobs never share a Metabase port;
- three rollouts run concurrently but never exceed global concurrency;
- cancellation of a running rollout cannot later become PASSED/FAILED;
- cancellation of queued rollouts never starts them;
- one rollout exception does not stop siblings;
- agent timeout terminates the child process;
- child process/queue resources are released;
- environment restore failure becomes ERROR;
- missing fixture verification blocks the agent;
- two concurrent DB updates preserve both rollout states and correct aggregates;
- custom task files are never graded against repository defaults;
- malicious task IDs cannot escape artifact root;
- expected answers are absent from normal API JSON;
- success rate ignores infrastructure failures;
- server restart reconciles in-flight jobs;
- frontend polling cannot apply stale results over a newer selection.

---

# Final priority checklist

### P0 — must fix before trusting benchmark results
- [ ] Independent/explicit grader contract
- [ ] Global concurrency bound
- [ ] Global unique port allocation
- [ ] Remove destructive port/container scanning
- [ ] Real cancellation + immutable terminal states
- [ ] Atomic rollout/job state transitions
- [ ] Artifact path traversal fix
- [ ] Expected-answer API leakage fix
- [ ] Remove task-file fallback to repository defaults
- [ ] Remove hard-coded credential
- [ ] Fix undefined FRONTEND_INDEX

### P1 — fix before serious parallel/demo use
- [ ] Job RUNNING transition
- [ ] Global job supervisor / shutdown handling
- [ ] Restart recovery
- [ ] True grader/rollout timeouts
- [ ] Fixture verification and restore error propagation
- [ ] DB/query efficiency
- [ ] Correct success-rate semantics
- [ ] Frontend polling race
- [ ] Frontend injection surfaces
- [ ] UTC timestamps
- [ ] Real worker/concurrency tests
- [ ] Remove production DB writes from tests
- [ ] Pin versions
- [ ] Complete artifact metadata/logging
- [ ] Verified cleanup

### P2/P3 — then harden
- [ ] SQL-side history filtering
- [ ] Architecture cleanup
- [ ] UI preview/config
- [ ] upload limits
- [ ] idempotency
- [ ] network allowlist
- [ ] test frontend derived state
- [ ] schema/version cleanup

## Bottom line

The repository is not "bug free" yet. The most important problems are not cosmetic: there are multiple paths where parallel rollouts can interfere with each other, cancelled work can overwrite terminal state, and benchmark scores can be wrong because grading is coupled to the agent's own answer.

The good news is that the fixes can actually make the system simpler. The largest simplification is to replace per-job worker pools + slot-based ports + host-wide Docker cleanup with one global rollout queue, one bounded worker pool, one safe environment allocator, and atomic DB state transitions.
