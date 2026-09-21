# Combined Backend Hardening Plan
## Non-Blocking UI + Hard Rollout Timeouts + Safe Ground-Truth Visibility

## 0. Objective

Harden the current FastAPI backend so that:

1. A hung agent, Playwright operation, Docker command, or rollout worker can never block the FastAPI event loop.
2. GET endpoints used by the UI remain responsive and have bounded latency even under DB contention.
3. A hung synchronous DB operation cannot cause an unbounded queue of API requests.
4. Agent execution has a real hard timeout and cannot permanently consume a rollout worker.
5. Docker lifecycle commands have bounded execution time.
6. Ground truth remains available to the grader and to human debugging after evaluation, while it is not exposed through an agent-accessible runtime endpoint.
7. Existing benchmark semantics remain unchanged:
   `tasks.json answer = ground truth`
   → agent performs task through visible UI
   → agent emits final JSON
   → grader compares agent JSON with ground truth.

This is an implementation plan, not a redesign of the benchmark.

---

# 1. Critical invariants

The implementation agent MUST preserve these invariants.

## API/event-loop invariant

No synchronous operation involving:

- SQLite
- Docker CLI
- filesystem writes/large reads
- Playwright
- Gemini/agent execution

may run directly on the FastAPI event loop.

Do NOT add a global `asyncio.Lock` around the whole store. That would serialize reads behind writes and make the UI less responsive.

## GET responsiveness invariant

Every UI GET endpoint that touches SQLite must:

- execute the synchronous DB call off the event loop;
- have a bounded request-side deadline;
- return a fast error such as HTTP 503 when the DB executor is saturated or the DB operation exceeds the API deadline.

A GET must NOT inherit SQLite's 30-second busy timeout as its HTTP latency.

Target defaults:

- SQLite busy timeout: ~1 second
- API DB read timeout: ~2 seconds
- API executor workers: ~8, independently configurable

These are defaults, not hard requirements if testing shows better values.

## Hung-thread invariant

Do not claim that Python threads can be force-killed.

For API DB calls:
- the HTTP request must time out independently;
- the underlying Python thread may remain alive temporarily;
- its executor slot remains occupied until it exits;
- this prevents an unlimited accumulation of hung DB threads.

For agent execution:
- use a subprocess because the process can be terminated;
- a timed-out agent must not retain a rollout worker forever.

## Ground-truth invariant

Ground truth must be available to:

- the grader;
- the human/operator inspecting a finished rollout.

Ground truth must NOT be exposed through an endpoint/data path that the computer-use agent can use during rollout to bypass the benchmark.

Therefore:

### During execution
Agent can receive:
- task prompt
- normal runtime information

Agent must NOT receive:
- `answer`
- `expected_answer`
- grader ground truth
- a task API that returns the answer

### After execution
Human-facing rollout detail MAY show:
- predicted output
- ground truth
- grader reason
- pass/fail

This is required for useful failure diagnosis.

---

# 2. Current problems being fixed

## P0/P1-A: FastAPI GET handlers perform synchronous DB work

Current async endpoints directly call functions such as:

```python
store.get_job(...)
store.get_rollout(...)
store.get_rollouts_for_job(...)
store.list_jobs(...)
store.get_job_history(...)
```

These are synchronous SQLite operations.

Current SQLite connection timeout is about 30 seconds.

Consequences:
- the event loop can block;
- multiple UI requests may stop progressing;
- a DB lock can look like a hung UI.

## P0/P1-B: Rollout agent timeout is not a hard timeout

Current code uses a `ThreadPoolExecutor` context:

```python
with ThreadPoolExecutor(max_workers=1) as tpe:
    fut = tpe.submit(agent_runner.run, ...)
    fut.result(timeout=AGENT_TIMEOUT)
```

Even when `future.result()` times out, leaving the executor context can wait for the underlying thread.

A hung agent can therefore consume a rollout worker indefinitely.

## P1-C: Docker subprocesses have no bounded timeout

`EnvironmentManager` has `subprocess.run(...)` calls without explicit timeouts.

A stuck Docker CLI can therefore consume a rollout worker indefinitely.

## P1-D: Ground truth exposure is too broad

Current serialization can expose expected answers in general job/task endpoints.

The grader needs ground truth, but the agent should not be able to retrieve it through an API.

The human UI still needs ground truth for completed-run debugging.

The fix is therefore an API/data-boundary change, NOT removal of ground truth from human debugging.

---

# 3. Target architecture

Use three separate execution domains.

## Domain A — FastAPI event loop

Allowed:
- request parsing
- response serialization
- lightweight in-memory logic
- awaiting futures/tasks

Forbidden:
- direct SQLite
- Docker
- Playwright
- Gemini
- long filesystem operations

## Domain B — API blocking executor

A dedicated bounded `ThreadPoolExecutor`.

Purpose:
- synchronous SQLite reads/writes;
- bounded filesystem work associated with API requests.

This executor must be separate from rollout execution.

Admission control:
- semaphore sized to executor capacity;
- if all slots are occupied, return 503 quickly.

Important:
A timed-out API DB thread must NOT free its slot merely because the HTTP request timed out. Keep that slot reserved until the worker future actually completes.

## Domain C — rollout execution

Rollouts run outside the API executor.

Agent execution:
- child subprocess
- hard process timeout
- process-group termination on timeout

Docker lifecycle:
- synchronous calls in rollout thread
- each subprocess call has explicit timeout

---

# 4. New files

Create these files.

## `backend/api/api_runtime.py`

Purpose:
- centralized boundary for all blocking API calls;
- dedicated API executor;
- admission control;
- request deadlines.

Required public helpers:

```python
async def run_db_read(operation, fn, *args, **kwargs)
```

and:

```python
async def run_blocking_api_call(
    operation,
    fn,
    *args,
    timeout_seconds=...,
    **kwargs,
)
```

Behavior:

1. acquire API executor admission slot with a very short wait;
2. if no slot is available, raise HTTP 503 with `Retry-After`;
3. submit the synchronous function to the dedicated executor;
4. await it with a bounded timeout;
5. use `asyncio.shield(...)` so request cancellation does not accidentally imply the underlying synchronous thread has stopped;
6. on timeout:
   - return HTTP 503;
   - keep executor slot reserved until the worker future completes;
7. on normal completion:
   - release the slot exactly once.

Do not put rollout execution into this executor.

## `backend/agent/supervisor.py`

Purpose:
- start the agent in a separate subprocess;
- enforce a real hard timeout;
- terminate the whole process group.

Requirements:
- POSIX: create a new process group/session;
- timeout => SIGTERM process group;
- if still alive => SIGKILL process group;
- Windows fallback may use `terminate()`/`kill()`;
- preserve stdout/stderr to artifact log;
- return structured JSON result.

Define:

```python
class AgentExecutionTimeout(TimeoutError):
    ...
```

and:

```python
run_agent_subprocess(...)
```

## `backend/agent/subprocess_runner.py`

Purpose:
- child-process entry point;
- instantiate the existing `AgentRunner`;
- execute the exact existing benchmark operation;
- write JSON-serializable result to artifact directory;
- write screenshots directly to artifact storage.

Do NOT change benchmark task semantics here.

The parent should receive:
- agent_claim
- serialized history
- screenshot filenames

The child should write:
- screenshot PNG files
- `agent_result.json`
- process log

Do not send raw screenshot bytes through a Python queue.

## `tests/test_api_nonblocking.py`

Add regression tests proving:
- hung synchronous API function does not freeze event loop;
- API call returns within configured deadline;
- event loop remains able to run another coroutine while the sync worker is hung;
- executor slot is not falsely released before the worker really finishes.

Additional tests should be added for:
- API saturation returns 503 quickly;
- rollout agent timeout terminates child process;
- Docker command timeout is enforced;
- ground-truth visibility rules.

---

# 5. Existing-file modifications

## `backend/configs.py`

Add:

```python
SQLITE_BUSY_TIMEOUT_SECONDS = float(
    os.environ.get("SQLITE_BUSY_TIMEOUT_SECONDS", "1.0")
)
```

Keep these configurable:

```python
API_DB_READ_TIMEOUT_SECONDS = ...
API_BLOCKING_CALL_TIMEOUT_SECONDS = ...
API_DB_EXECUTOR_WORKERS = ...
```

Preferred defaults:

```text
SQLITE_BUSY_TIMEOUT_SECONDS=1.0
API_DB_READ_TIMEOUT_SECONDS=2.0
API_BLOCKING_CALL_TIMEOUT_SECONDS=10.0
API_DB_EXECUTOR_WORKERS=8
```

Do not make these rollout concurrency settings.

---

# 6. `backend/db.py`

Change SQLite connection setup from the current ~30 second timeout to the configured short busy timeout.

Conceptually:

```python
conn = sqlite3.connect(
    str(self.db_path),
    timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    check_same_thread=False,
)
```

and configure the corresponding SQLite busy timeout.

Important:
- this does NOT replace API-level timeouts;
- it just makes DB lock waits shorter;
- GET endpoints still must run DB work off the event loop.

Do NOT add a global application mutex around all database operations.

---

# 7. `backend/api/server.py`

Import:

```python
from backend.api.api_runtime import (
    run_blocking_api_call,
    run_db_read,
)
```

## GET endpoints

Every GET endpoint that uses `SQLiteStore` must call the wrapper.

### `/api/jobs`

Use:

```python
jobs = await run_db_read(
    "GET /api/jobs",
    store.list_jobs,
)
```

### `/api/jobs/history`

Use:

```python
return await run_db_read(
    "GET /api/jobs/history",
    store.get_job_history,
    status=status,
    search=search,
    limit=limit,
    offset=offset,
)
```

### `/api/jobs/{job_id}`

Use:

```python
job = await run_db_read(
    "GET /api/jobs/{job_id}",
    store.get_job,
    job_id,
)
```

### `/api/jobs/{job_id}/rollouts`

Move both store operations through the DB wrapper.

### `/api/jobs/{job_id}/tasks`

Move DB work through the DB wrapper.

The remaining Python aggregation is small for the current benchmark and can remain after the DB result has been returned.

### `/api/rollouts/{rollout_id}`

Use the DB wrapper.

### `/api/rollouts/{rollout_id}/screenshots`

Use the DB wrapper.

## POST `/api/jobs`

Do not directly execute synchronous:

```python
store.create_job(...)
```

Use the blocking API wrapper.

Large artifact writes should also be moved off the event loop.

## DELETE `/api/jobs/{job_id}`

The cancellation DB mutation must also use the blocking API wrapper.

Do not use an async lock around this endpoint.

---

# 8. Ground-truth API contract

This section supersedes the earlier overly broad statement that "ground truth should not be sent to the UI."

The correct rule is:

> Ground truth is hidden from the AGENT, but available to the HUMAN UI after evaluation.

## 8.1 `/api/jobs/{job_id}/tasks.json`

Change the public task endpoint so it does NOT expose:

```json
"answer": ...
```

or:

```json
"expected_answer": ...
```

It may return:

```json
{
  "id": "problem1",
  "task": "..."
}
```

This endpoint is potentially reachable from the machine where the browser agent runs, so it must not contain the answer.

## 8.2 `/api/jobs/{job_id}`

Do not include `expected_answer` in the normal job response.

The job summary needs:
- task IDs
- task prompts if desired by the UI
- rollout counts/statuses
- timestamps
- aggregate results

but not the answer.

## 8.3 `/api/rollouts/{rollout_id}`

After grading, the rollout detail may contain:

```json
{
  "grader_result": {
    "passed": false,
    "reward": 0.0,
    "reason": "...",
    "predicted": {...},
    "ground_truth": {...}
  }
}
```

This is the human debugging surface.

The frontend should show it primarily for terminal rollouts:
- PASSED
- FAILED
- ERROR
- TIMEOUT
- CANCELLED

For active rollouts, do not unnecessarily display ground truth.

## 8.4 Do not weaken the grader

The grader must continue to:

1. load benchmark task data internally;
2. obtain the `answer`;
3. parse agent final JSON;
4. compare prediction to expected answer;
5. store grader evidence for debugging.

This is intentional answer-based benchmark grading.

---

# 9. `backend/rollout/runner.py`

## Replace threaded agent timeout

Remove the pattern:

```python
with ThreadPoolExecutor(max_workers=1):
    ...
```

for agent execution.

Use:

```python
agent_out = run_agent_subprocess(
    task_prompt=task_data["task"],
    initial_url=metabase_url,
    model_name=DEFAULT_MODEL_NAME,
    artifact_dir=artifact_dir,
    timeout_seconds=AGENT_TIMEOUT,
)
```

On `AgentExecutionTimeout`:

- set `ErrorType.AGENT_TIMEOUT`;
- set status `RolloutStatus.TIMEOUT`;
- set termination reason `agent_timeout`;
- allow cleanup to proceed;
- do not leave a running agent thread behind.

## Grading

The grader is currently lightweight local Python code.

Prefer direct execution rather than a `ThreadPoolExecutor` timeout wrapper.

Reason:
- the current thread timeout is not a hard timeout;
- `ThreadPoolExecutor` context exit can wait;
- grading itself is lightweight enough that direct execution keeps semantics simple.

If the grader later performs untrusted/heavy work, introduce a dedicated process boundary then.

## Artifact handling

The child process should write screenshots itself.

Parent receives screenshot filenames instead of image bytes.

Use safe numeric filenames such as:

```text
001.png
002.png
003.png
```

Store action names in transcript metadata instead of incorporating arbitrary action text into filenames.

---

# 10. `backend/environment/manager.py`

Every Docker subprocess call must have an explicit timeout.

Recommended:

| Operation | Timeout |
|---|---:|
| `docker compose up -d` | 60s |
| `docker ps` | 10s |
| `docker rm -f` | 10s |
| `docker compose down` | 30s |

A Docker timeout must become a rollout infrastructure error and proceed to cleanup.

Important:
- cleanup itself must not hang indefinitely;
- if cleanup times out, record `cleanup_error`;
- do not overwrite the original primary rollout error with cleanup failure.

---

# 11. GET endpoint latency guarantee

After these changes, the intended behavior is:

### Healthy backend

GET returns normally.

### SQLite briefly contended

GET waits for the bounded DB timeout / lock resolution.

### SQLite call becomes stuck

GET returns 503 rather than blocking indefinitely.

### All API DB executor slots are stuck

A new GET returns 503 immediately instead of waiting behind an unbounded queue.

### Agent is hung

Agent subprocess is terminated at `AGENT_TIMEOUT`.

GET endpoints remain independent.

### Docker CLI is hung

Docker operation times out.

The affected rollout fails/records infrastructure error.

GET endpoints remain independent.

---

# 12. Important caveat about API DB threads

Do not implement this incorrectly as:

```python
await asyncio.wait_for(loop.run_in_executor(...), timeout=2)
# timeout => immediately release executor slot
```

That is dangerous.

The underlying synchronous function may still be running.

If the slot is released immediately, repeated requests can create more and more hung threads.

Instead:

- HTTP request times out;
- underlying future gets a completion callback;
- slot is released only when the actual executor future finishes.

This is required for bounded resource usage.

---

# 13. Ground-truth leakage test matrix

Add tests for the API boundary.

| Surface | Expected answer visible? |
|---|---|
| `POST /api/jobs` response | No |
| `GET /api/jobs` | No |
| `GET /api/jobs/{id}` | No |
| `GET /api/jobs/{id}/tasks` | No |
| `GET /api/jobs/{id}/tasks.json` | No |
| `GET /api/jobs/{id}/rollouts` | No |
| `GET /api/rollouts/{id}` while active | No |
| `GET /api/rollouts/{id}` after grading | Yes, for human debugging |
| Internal `TaskGrader` | Yes |
| Agent prompt/runtime | No |

Do not put the benchmark answer into a prompt, browser-visible task API, or runtime config merely for convenience.

---

# 14. Non-blocking test plan

At minimum, implement these tests.

## Test A — Hung synchronous API call

Fake DB method:

```python
def hung_call():
    event.wait()
```

Call through API runtime wrapper.

Assert:
- wrapper raises 503/deadline exception quickly;
- wall-clock latency is bounded;
- event loop can still execute `asyncio.sleep(...)`.

## Test B — API executor saturation

Create more hung calls than executor slots.

Assert:
- initial calls occupy all slots;
- next request returns 503 quickly;
- no unlimited thread creation occurs.

## Test C — Agent hard timeout

Run a deliberately sleeping child process through the supervisor.

Assert:
- timeout exception occurs near configured deadline;
- child process is no longer alive;
- no process-group children remain where test environment permits checking.

## Test D — Docker timeout

Inject/mock `subprocess.run` to simulate a timeout.

Assert:
- timeout becomes controlled rollout error;
- cleanup/error information is preserved;
- no exception escapes and kills the worker pool.

## Test E — UI GET responsiveness during rollout hang

Use a fake rollout execution that hangs.

Repeatedly issue:
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/rollouts`
- `GET /api/rollouts/{id}`

Assert these endpoints continue responding within their API deadline.

This test specifically protects the user's requirement that the UI never be blocked by a hung rollout.

## Test F — Ground-truth leakage

Create a task with a distinctive answer.

Assert:
- task/job endpoints do not contain that answer;
- terminal rollout detail does contain it in grader evidence.

---

# 15. Do not introduce these anti-patterns

The implementation agent must NOT:

### Anti-pattern 1
Add one global lock around `SQLiteStore`.

Why:
- serializes reads;
- makes UI latency worse;
- can turn normal DB contention into application-level serialization.

### Anti-pattern 2
Run rollout execution in the same executor as API reads.

Why:
- a hung agent could consume every API worker.

### Anti-pattern 3
Treat `future.cancel()` as killing the agent.

Why:
- cancelling a Python future does not kill a running thread.

### Anti-pattern 4
Immediately release executor capacity when a sync function times out.

Why:
- the underlying thread may still be running.

### Anti-pattern 5
Remove ground truth from terminal rollout results.

Why:
- humans need predicted-vs-expected evidence to diagnose benchmark failures.

### Anti-pattern 6
Expose the answer via a normal tasks endpoint because "the UI needs it."

Why:
- the agent can potentially navigate to that endpoint.

### Anti-pattern 7
Change the benchmark from answer-based grading to state-based grading.

Why:
- that is a separate benchmark-design decision and is not part of this fix.

---

# 16. Secondary cleanup while touching these files

These are not prerequisites for the non-blocking architecture, but should be addressed if low-risk:

1. Import `Dict` in `backend/api/server.py`.
2. Remove unused `json` imports where appropriate.
3. Change stale server/model docstrings that still say "in-memory".
4. Use timezone-aware UTC timestamps.
5. Replace the unsafe artifact path construction using `task_id` with `job_id/rollout_id/...`.
6. Fix `/api/artifacts/...` path containment using `Path.is_relative_to(...)` rather than string `startswith`.
7. Add a real job supervisor rather than raw untracked `asyncio.create_task(...)` for production-grade lifecycle management.
8. Add startup recovery for orphaned in-flight rollouts.
9. Fix cancellation race so a rollout cannot transition:
   `CANCELLED -> PASSED/FAILED/ERROR`
   after the user has cancelled the job.
10. Add compare-and-set/atomic lifecycle transition semantics to rollout updates.

These are separate correctness issues but interact with the same concurrency hardening effort.

---

# 17. Implementation order

Follow this order.

### Phase 1 — API boundary
- add `api_runtime.py`
- add config values
- shorten SQLite busy timeout
- migrate all SQLite-using GETs
- migrate POST/DELETE blocking store mutations
- add non-blocking tests

### Phase 2 — Agent hard timeout
- add `supervisor.py`
- add `subprocess_runner.py`
- migrate `runner.py`
- add timeout/process-kill tests

### Phase 3 — Docker hard timeout
- add explicit Docker subprocess timeouts
- add mocked timeout tests

### Phase 4 — Ground-truth boundary
- remove answers from normal job/task APIs
- retain ground truth in grader
- retain predicted vs ground truth in terminal rollout detail
- add leakage tests

### Phase 5 — Integration validation
Run:
- unit tests
- API endpoint tests
- concurrency tests
- timeout tests
- ground-truth leakage tests
- frontend manual polling test during a deliberately hung rollout

Only after all phases pass should the change be considered complete.

---

# 18. Definition of done

The implementation is complete only when all of these are true:

- [ ] No FastAPI GET directly calls synchronous SQLite.
- [ ] API executor is separate from rollout executor.
- [ ] API DB requests have bounded latency.
- [ ] API saturation produces 503 rather than queueing indefinitely.
- [ ] SQLite busy timeout is short.
- [ ] Agent timeout actually terminates the agent process.
- [ ] No `ThreadPoolExecutor` context is used as a supposedly hard agent timeout.
- [ ] Docker CLI operations have explicit timeouts.
- [ ] A hung rollout does not prevent GET endpoints from responding.
- [ ] `/api/jobs/{id}/tasks.json` does not expose answers.
- [ ] Normal job/task responses do not expose expected answers.
- [ ] Terminal rollout detail exposes grader `predicted` + `ground_truth` for human debugging.
- [ ] Grader semantics remain answer-based and unchanged.
- [ ] Tests verify event-loop responsiveness under hung sync calls.
- [ ] Tests verify hard agent process termination.
- [ ] Tests verify ground-truth leakage boundaries.
- [ ] No global store lock has been introduced.

---

# 19. Final mental model

The implementation agent should think of the system as:

```text
                 ┌─────────────────────────┐
                 │      FastAPI event      │
                 │         loop            │
                 │                         │
                 │  GET / POST / DELETE    │
                 └────────────┬────────────┘
                              │
                ┌─────────────┴──────────────┐
                │                            │
        ┌───────▼────────┐          ┌────────▼────────┐
        │  API executor  │          │ Rollout manager │
        │  bounded pool  │          │                 │
        │                │          │ Docker thread   │
        │ SQLite/files   │          │ + agent process │
        └────────────────┘          └────────┬────────┘
                                             │
                                      ┌──────▼──────┐
                                      │ Agent child │
                                      │ subprocess  │
                                      │ hard timeout│
                                      └─────────────┘
```

Ground truth flows separately:

```text
tasks.json
    │
    ├──► Agent receives task prompt only
    │
    └──► Grader receives task + answer
                 │
                 ▼
          predicted vs ground truth
                 │
                 ▼
       terminal rollout evidence
                 │
                 ▼
             Human UI
```

The key principle is:

> **The UI must remain responsive even when execution is not.**
> 
> A rollout may hang and fail.
> A database operation may be contended.
> Docker may stall.
> None of those conditions should make the FastAPI event loop hostage to the workload.
