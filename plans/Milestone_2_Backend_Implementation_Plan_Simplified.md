# Milestone 2 --- Simplified Backend Execution Plan

## Local Rollout Platform with Parallel Execution and Strong Failure Handling

## 0. Objective

Extend the working Milestone 1 rollout into a small local execution
platform.

The backend must:

-   ingest `tasks.json`
-   create rollouts for each task
-   support multiple attempts per task
-   run multiple rollouts in parallel
-   retain transcript/log/grade information
-   clearly distinguish where a rollout failed

For this milestone, **do not build persistent job storage or a
production-grade API**.

Architecture:

    HTML frontend
          |
       FastAPI
          |
    In-memory Job Store
          |
    In-memory Rollout Queue
          |
      +---+---+---+
      |   |   |   |
      v   v   v   v
    Worker Worker Worker
      |   |   |
      v   v   v
    Fresh isolated environments
      |   |   |
      v   v   v
    Agent -> Grader -> Result

Rollout artifacts are written to disk so completed/failed runs remain
inspectable.

------------------------------------------------------------------------

# 1. Scope

## Build

-   JSON task ingestion
-   configurable attempts per task
-   rollout generation
-   bounded parallel worker pool
-   isolated Metabase environment per rollout
-   in-memory job/rollout state
-   basic FastAPI endpoints
-   artifact capture
-   strong error handling
-   timeout and cleanup handling

## Do not build

-   PostgreSQL for platform state
-   Redis
-   Kafka
-   Celery
-   Kubernetes
-   persistent job history
-   authentication
-   full CRUD APIs
-   distributed scheduling
-   cloud deployment

If the backend restarts, in-memory job state may disappear. That is
acceptable for this MVP.

------------------------------------------------------------------------

# 2. What Is a Rollout?

A rollout is:

> One attempt by the agent to solve one task inside one fresh Metabase
> environment.

If `tasks.json` has 10 tasks and the user chooses 3 attempts:

    10 × 3 = 30 rollouts

Example:

    problem1 / attempt1
    problem1 / attempt2
    problem1 / attempt3
    problem2 / attempt1
    ...
    problem10 / attempt3

Each is independent.

------------------------------------------------------------------------

# 3. What We Persist

Do not persist platform state in a database.

Do persist rollout artifacts:

    artifacts/
      <job_id>/
        <rollout_id>/
          metadata.json
          transcript.json
          result.json
          agent.log
          metabase.log
          screenshots/

The in-memory store tells the frontend what is happening.

The filesystem preserves evidence.

------------------------------------------------------------------------

# 4. Minimal Backend Structure

    backend/
      main.py
      models.py
      store.py
      jobs.py
      workers.py
      rollout.py
      artifacts.py
      api.py

Reuse Milestone 1 components:

    environment/
      manager.py

    agent/
      runner.py

    grader/
      grader.py

Milestone 2 should orchestrate these components rather than duplicate
them.

------------------------------------------------------------------------

# 5. In-Memory Models

Use dataclasses or Pydantic models.

## Job

    id
    status
    tasks
    rollouts
    attempts_per_task
    created_at
    started_at
    completed_at
    total
    completed
    passed
    failed
    errors
    timeouts

Statuses:

    QUEUED
    RUNNING
    COMPLETED
    FAILED
    CANCELLED

## Task

    id
    prompt
    expected_answer

The expected answer is grader data and must never be sent to Gemini.

## Rollout

    id
    job_id
    task_id
    attempt_number
    status
    reward
    started_at
    completed_at
    duration_seconds
    termination_reason
    error_type
    error_message
    transcript
    grader_result
    artifact_path

Statuses:

    QUEUED
    STARTING
    RUNNING
    GRADING
    PASSED
    FAILED
    ERROR
    TIMEOUT
    CANCELLED

------------------------------------------------------------------------

# 6. In-Memory Store

Use:

    jobs: dict[str, Job] = {}

Optionally:

    rollouts: dict[str, Rollout] = {}

Provide simple operations:

    create_job(...)
    get_job(job_id)
    get_rollout(rollout_id)
    update_rollout(...)
    update_job_progress(...)

Because workers update shared state concurrently, protect shared
mutations with an `asyncio.Lock` or equivalent synchronization.

No SQL is required.

------------------------------------------------------------------------

# 7. Task Ingestion

Accept the same structure as `tasks.json`:

    [
      {
        "id": "problem1",
        "task": "...",
        "answer": "..."
      }
    ]

When submitted:

1.  Parse JSON.
2.  Validate it is a list.
3.  Validate `id`, `task`, and `answer`.
4.  Reject duplicate IDs.
5.  Reject empty task prompts.
6.  Validate attempts \>= 1.
7.  Create the Job.
8.  Create Task objects.
9.  Create Rollout objects.
10. Put rollouts into the execution queue.

Never silently modify malformed input.

------------------------------------------------------------------------

# 8. Rollout Creation

Given:

    tasks = 10
    attempts_per_task = 3

create 30 rollout objects.

Each gets a UUID:

    rollout_id = uuid4()

Example:

    {
      "id": "7a3c...",
      "job_id": "job_123",
      "task_id": "problem4",
      "attempt_number": 2,
      "status": "QUEUED"
    }

------------------------------------------------------------------------

# 9. The Rollout Runner

The core function is:

    execute_rollout(rollout)

Lifecycle:

    Create fresh environment
        ->
    Wait for Metabase
        ->
    Load fixture
        ->
    Verify environment
        ->
    Start browser
        ->
    Start Gemini agent
        ->
    Execute task
        ->
    Grade resulting state
        ->
    Save artifacts
        ->
    Destroy environment
        ->
    Return result

This should be the Milestone 1 runner generalized into a reusable
function.

Do not create a second implementation of environment startup or grading.

------------------------------------------------------------------------

# 10. Isolation Rule

Every rollout must get its own environment.

Correct:

    Rollout A -> Metabase A
    Rollout B -> Metabase B
    Rollout C -> Metabase C

Incorrect:

    Rollout A -> Metabase A
    Rollout B -> Metabase A
    Rollout C -> Metabase A

Each rollout gets:

-   unique environment/container identity
-   unique Metabase application state
-   unique browser
-   unique artifact directory
-   unique port/network configuration where required

This prevents one agent's changes from affecting another agent.

------------------------------------------------------------------------

# 11. Parallel Worker Pool

Use a bounded async worker pool.

Conceptually:

    queue = asyncio.Queue()

    for rollout in rollouts:
        await queue.put(rollout)

    workers = [
        asyncio.create_task(worker(queue))
        for _ in range(MAX_CONCURRENT_ROLLOUTS)
    ]

A worker:

    while queue is not empty:
        rollout = get_next_rollout()
        execute_rollout(rollout)
        update result
        mark queue task complete

Start with:

    MAX_CONCURRENT_ROLLOUTS = 3

Make it configurable.

Do not create one worker per rollout.

------------------------------------------------------------------------

# 12. Why Bounded Concurrency

Each rollout can consume:

-   Docker
-   PostgreSQL
-   Metabase
-   Chrome
-   Playwright
-   CPU/RAM
-   Gemini API calls

With 30 rollouts, launching all 30 simultaneously can overwhelm a local
machine.

Instead:

    30 rollouts
        |
        v
    3 workers
        |
        v
    3 active
    27 waiting

As a worker finishes, the next rollout starts.

------------------------------------------------------------------------

# 13. Worker Lifecycle

Each worker should update the rollout through:

    QUEUED
       |
       v
    STARTING
       |
       v
    RUNNING
       |
       v
    GRADING
       |
       +----> PASSED
       |
       +----> FAILED
       |
       +----> ERROR
       |
       +----> TIMEOUT

Persist these transitions in memory so the frontend can show live
progress.

------------------------------------------------------------------------

# 14. Error Handling Is a First-Class Requirement

The most important Milestone 2 behavior is:

> If one rollout fails, other rollouts must continue.

Bad:

    rollout 1 throws exception
        ->
    entire job crashes

Good:

    rollout 1 -> ERROR
    rollout 2 -> continues
    rollout 3 -> continues
    rollout 4 -> starts

Every rollout must have its own exception boundary.

Conceptually:

    try:
        execute_rollout()

    except Exception:
        mark_this_rollout_failed()

    finally:
        cleanup_this_rollout()

An exception from one rollout must never escape and kill the worker
pool.

------------------------------------------------------------------------

# 15. Error Categories

Do not just store `failed`.

Use categories:

    TASK_VALIDATION_ERROR
    ENVIRONMENT_START_ERROR
    ENVIRONMENT_HEALTH_TIMEOUT
    ENVIRONMENT_INITIALIZATION_ERROR
    BROWSER_START_ERROR
    AGENT_ERROR
    AGENT_TIMEOUT
    GRADER_ERROR
    CLEANUP_ERROR
    TASK_FAILED

Meaning matters.

`TASK_FAILED` means the environment worked and the agent completed, but
the requested task was not achieved.

`ENVIRONMENT_START_ERROR` means we cannot infer agent performance
because the environment never started correctly.

------------------------------------------------------------------------

# 16. Error Object

Every rollout error should contain:

    error_type
    error_message
    stage
    timestamp

Example:

    {
      "status": "ERROR",
      "error_type": "ENVIRONMENT_START_ERROR",
      "stage": "environment_start",
      "error_message": "Metabase container exited with code 1"
    }

Do not expose raw stack traces as the main frontend message.

Save full tracebacks to the rollout log.

------------------------------------------------------------------------

# 17. Timeouts

At minimum configure:

    ENVIRONMENT_START_TIMEOUT
    AGENT_TIMEOUT
    GRADER_TIMEOUT

When the agent exceeds its timeout:

    mark TIMEOUT
    capture artifacts
    stop browser
    destroy environment
    continue worker

One timeout must not stop other workers.

------------------------------------------------------------------------

# 18. Cleanup Must Always Run

Use `try/finally`.

Conceptually:

    environment = None

    try:
        environment = start_environment()
        run_agent()
        grade()

    except Exception:
        record_error()

    finally:
        if environment:
            destroy_environment()

If cleanup is skipped, failed rollouts can leave
containers/ports/resources behind and cause cascading failures.

------------------------------------------------------------------------

# 19. Cleanup Failure

Never let cleanup failure replace the original error.

Example:

    Original: AGENT_TIMEOUT
    Cleanup: Docker container could not be removed

Final rollout status:

    TIMEOUT

Additional field:

    cleanup_error = ...

This preserves the real cause.

------------------------------------------------------------------------

# 20. Artifacts

For every rollout:

    artifacts/
      <job_id>/
        <rollout_id>/
          metadata.json
          transcript.json
          grader.json
          result.json
          agent.log
          metabase.log
          screenshots/

If a rollout fails before screenshots exist, still write result/error
metadata and logs.

------------------------------------------------------------------------

# 21. Result Object

Every rollout must end with a structured result.

Successful evaluation:

    {
      "rollout_id": "...",
      "task_id": "problem3",
      "attempt": 2,
      "status": "FAILED",
      "reward": 0,
      "duration_seconds": 51.4,
      "termination_reason": "task_failed",
      "error_type": null,
      "artifact_path": "artifacts/job1/rollout123/"
    }

Infrastructure failure:

    {
      "status": "ERROR",
      "reward": null,
      "termination_reason": "environment_start_failed",
      "error_type": "ENVIRONMENT_START_ERROR"
    }

Use `reward = null` when the task was never meaningfully evaluated.

Do not turn infrastructure failure into reward 0.

------------------------------------------------------------------------

# 22. Job Progress

Track:

    total_rollouts
    completed
    passed
    failed
    errors
    timeouts

Example:

    total = 30
    completed = 17
    passed = 10
    failed = 5
    errors = 1
    timeouts = 1

Execution progress:

    17 / 30

Evaluation success rate should be separate:

    evaluated = passed + failed
    success_rate = passed / evaluated

Infrastructure errors are not agent failures.

------------------------------------------------------------------------

# 23. Minimal API

Use FastAPI only as a thin interface for the HTML frontend.

## Create job

    POST /api/jobs

Input:

    tasks.json
    attempts_per_task

Return immediately:

    {
      "job_id": "...",
      "total_rollouts": 30
    }

## Get job

    GET /api/jobs/{job_id}

Return:

    status
    progress
    counts

Example:

    {
      "job_id": "...",
      "status": "RUNNING",
      "total": 30,
      "completed": 12,
      "passed": 8,
      "failed": 3,
      "errors": 1,
      "timeouts": 0
    }

## Get rollouts

    GET /api/jobs/{job_id}/rollouts

Return lightweight summaries:

    task_id
    attempt
    status
    reward
    duration
    error_type

## Get rollout

    GET /api/rollouts/{rollout_id}

Return:

    task
    status
    reward
    transcript
    grader_result
    artifact information
    error information

No CRUD API is necessary.

------------------------------------------------------------------------

# 24. Job Completion

A job is complete when every rollout reaches a terminal state:

    PASSED
    FAILED
    ERROR
    TIMEOUT
    CANCELLED

A job containing failed rollouts is still a completed job.

Example:

    30 rollouts
    24 PASS
    4 FAIL
    2 ERROR

Job status:

    COMPLETED

The individual rollout statuses explain the outcome.

------------------------------------------------------------------------

# 25. Critical Failure-Isolation Test

Create a test with:

    Rollout 1 -> intentional error
    Rollout 2 -> success
    Rollout 3 -> success

Expected:

    Job completes
    Rollout 1 = ERROR
    Rollout 2 = PASS
    Rollout 3 = PASS

This is one of the most important Milestone 2 tests.

------------------------------------------------------------------------

# 26. Parallelism Test

Before expensive Gemini tasks, use a lightweight test mode if possible.

Run:

    3 rollouts
    MAX_CONCURRENT_ROLLOUTS=3

Record start/end timestamps.

Expected:

    rollout1 start ≈ 10:00:00
    rollout2 start ≈ 10:00:00
    rollout3 start ≈ 10:00:01

rather than:

    rollout1 start = 10:00:00
    rollout2 start = 10:01:00
    rollout3 start = 10:02:00

This proves workers are actually concurrent.

Then repeat with real Metabase environments.

------------------------------------------------------------------------

# 27. Recommended Implementation Order

## Step 1 --- Generalize Milestone 1

Turn the existing one-off runner into:

    execute_rollout(rollout)

Make one rollout reliable first.

## Step 2 --- Load tasks

Implement:

    load_tasks("tasks.json")

Verify all supplied tasks parse correctly.

## Step 3 --- Generate rollouts

Implement:

    create_rollouts(tasks, attempts_per_task)

Example:

    10 tasks × 3 attempts = 30 Rollout objects

## Step 4 --- Run sequentially

Run all rollouts one after another.

Do not add concurrency until sequential execution is reliable.

## Step 5 --- Add worker pool

Add:

    MAX_CONCURRENT_ROLLOUTS=3

Run three at a time.

## Step 6 --- Add failure isolation

Intentionally fail one rollout.

Verify the other workers continue.

## Step 7 --- Add timeout handling

Intentionally exceed one rollout's timeout.

Verify:

    timeout
    cleanup
    next rollout starts

## Step 8 --- Add artifact capture

Verify every rollout has its own artifact directory.

## Step 9 --- Add in-memory Job store

Track:

    job
    tasks
    rollouts
    progress

## Step 10 --- Add minimal FastAPI

Expose only:

    POST /api/jobs
    GET /api/jobs/{id}
    GET /api/jobs/{id}/rollouts
    GET /api/rollouts/{id}

## Step 11 --- Connect HTML frontend

Only after the backend works without the UI.

------------------------------------------------------------------------

# 28. Final Acceptance Criteria

### Task ingestion

-   [ ] Can ingest `tasks.json`.
-   [ ] Can specify attempts per task.
-   [ ] Correct number of rollouts is generated.

### Parallel execution

-   [ ] One rollout executes successfully.
-   [ ] Multiple rollouts execute concurrently.
-   [ ] Concurrency is bounded.
-   [ ] Each rollout gets a fresh isolated Metabase environment.

### Failure handling

-   [ ] Agent failure does not kill the job.
-   [ ] Environment failure does not kill other workers.
-   [ ] Timeout stops only the affected rollout.
-   [ ] Failed rollout environment is cleaned up.
-   [ ] Cleanup failure does not hide the original error.
-   [ ] Errors identify the stage at which failure occurred.

### Observability

-   [ ] Every rollout has a unique ID.
-   [ ] Every rollout has a status.
-   [ ] Every evaluated rollout has a reward.
-   [ ] Every rollout has logs/artifacts.
-   [ ] Frontend can see live progress.

### API

-   [ ] Can create a job.
-   [ ] Can query job status.
-   [ ] Can query rollout summaries.
-   [ ] Can query individual rollout details.

### Scope

-   [ ] No platform database required.
-   [ ] No Redis required.
-   [ ] No distributed queue required.
-   [ ] No Kubernetes required.
-   [ ] No production persistence required.

------------------------------------------------------------------------

# 29. Core Principle

Keep this invariant throughout implementation:

    ONE ROLLOUT
        =
    ONE TASK
        +
    ONE ATTEMPT
        +
    ONE FRESH ENVIRONMENT
        +
    ONE AGENT
        +
    ONE GRADER
        +
    ONE RESULT

Then:

    JOB
        =
    MANY INDEPENDENT ROLLOUTS

And:

    PARALLEL EXECUTION
        =
    BOUNDED WORKERS EXECUTING INDEPENDENT ROLLOUTS

The most important Milestone 2 engineering property is:

> **A failure in one rollout must be isolated, observable, cleaned up,
> and must not prevent the remaining rollouts from executing.**
