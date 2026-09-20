# Milestone 1 Implementation Plan --- Metabase Computer-Use RL Environment

## 0. Objective

Build the smallest reliable end-to-end environment that can:

1.  Start a fresh, local Metabase instance.
2.  Initialize it at runtime with the provided `metabase_envdata.sql`.
3.  Start Google's Gemini Computer Use agent against that Metabase
    instance.
4.  Give the agent one benchmark task from `tasks.json`.
5.  Let the agent operate Metabase through the computer/browser
    interface rather than privileged APIs.
6.  Capture the agent's actions, screenshots/transcript, timing, and
    errors.
7.  Independently determine whether the task succeeded.
8.  Return a structured rollout result.

### Milestone 1 success condition

A command such as:

    python -m runner --task-id <id>

must reliably produce:

    environment started
    environment initialized
    agent started
    agent attempted task
    rollout artifacts saved
    task graded
    reward/result returned

The implementation must be repeatable from a clean environment. A human
should not need to manually configure Metabase between runs.

------------------------------------------------------------------------

# 1. Scope and Non-Goals

## In scope

-   Local Dockerized Metabase.
-   Runtime initialization from the supplied environment data.
-   Local Playwright browser.
-   Gemini Computer Use agent.
-   One-task-at-a-time rollout runner.
-   Task loading from `tasks.json`.
-   Deterministic grading wherever possible.
-   Transcript/action/screenshot capture.
-   Timeouts and failure handling.
-   Environment reset between rollouts.
-   Secrets handled only through environment variables or ignored local
    files.
-   A CLI that demonstrates the complete flow.

## Explicitly out of scope for Milestone 1

Do NOT build yet:

-   React/Next.js frontend.
-   Job queue.
-   Parallel workers.
-   Kubernetes.
-   Redis.
-   Production database.
-   Multi-user authentication.
-   RL training.
-   Model fine-tuning.
-   Complex distributed tracing.
-   Production cloud deployment.

These belong to Milestones 2 and 3.

The purpose of Milestone 1 is to prove that the environment, agent, and
reward loop work correctly before adding orchestration complexity.

------------------------------------------------------------------------

# 2. Core Architecture

The implementation should have five logical components:

    tasks.json
        |
        v
    Task Loader
        |
        v
    Rollout Runner
        |
        +----------------------+
        |                      |
        v                      v
    Environment            Computer-Use Agent
    Manager                    |
        |                      |
        v                      v
    Metabase <------------ Playwright
        |
        v
    Grader
        |
        v
    Rollout Result + Artifacts

A rollout is:

    Fresh Environment
          ->
    Agent receives task
          ->
    Agent operates browser
          ->
    Agent terminates
          ->
    Grader inspects resulting state
          ->
    Reward + artifacts

The grader must not rely on the agent saying that it succeeded.

------------------------------------------------------------------------

# 3. First Step: Inspect the Supplied Inputs

Before writing the environment bootstrap code, inspect:

-   `tasks.json`
-   `metabase_envdata.sql`
-   the exact version/commit of Metabase expected by the supplied
    environment
-   the exact version/commit of `computer-use-preview`

Do not guess the SQL format.

Commands:

    head -100 tasks.json
    head -100 metabase_envdata.sql

Then inspect SQL characteristics:

    grep -i "INSERT INTO" metabase_envdata.sql | head -50
    grep -i "CREATE TABLE" metabase_envdata.sql | head -50

Also inspect:

    file metabase_envdata.sql

### Why this is a gating step

Metabase has two distinct concepts:

1.  The Metabase application database, which stores users, questions,
    dashboards, collections, permissions, etc.
2.  The analytical/data database that Metabase connects to and queries.

The supplied SQL is explicitly described as environment data for the
Metabase application itself. Therefore it must not automatically be
treated as a normal data-warehouse import.

Metabase documentation confirms that the application database is
distinct from the database containing the data being analyzed. [Metabase
application database
documentation](https://www.metabase.com/docs/latest/installation-and-operation/configuring-application-database)

The exact SQL format determines the correct bootstrap mechanism. Do not
alter or reinterpret the file until this has been established.

------------------------------------------------------------------------

# 4. Pin the Dependencies

Create a project-level record of the exact versions used.

At minimum record:

-   Metabase Git commit/tag/image
-   Gemini computer-use-preview Git commit
-   Python version
-   Playwright version
-   browser version
-   Docker version, where useful
-   Gemini model used

Recommended files:

    VERSION_MATRIX.md
    requirements.txt
    .env.example

### Why

Computer-use behavior and Metabase UI behavior can change between
versions. Reproducibility is essential for an evaluation environment.

Do not use an unpinned `latest` image for the final Milestone 1
demonstration if the supplied environment expects a particular version.

------------------------------------------------------------------------

# 5. Project Layout

Use a simple structure:

    metabase-cu-rl/
    |
    +-- environment/
    |   +-- manager.py
    |   +-- health.py
    |   +-- reset.py
    |
    +-- agent/
    |   +-- runner.py
    |   +-- prompts.py
    |
    +-- grader/
    |   +-- grader.py
    |   +-- checks.py
    |
    +-- rollout/
    |   +-- runner.py
    |   +-- models.py
    |
    +-- tasks.json
    +-- metabase_envdata.sql
    +-- docker-compose.yml
    +-- .env
    +-- .env.example
    +-- .gitignore
    +-- requirements.txt
    +-- README.md

Keep Metabase and Google's agent code as external
dependencies/repositories where practical. Do not fork or heavily modify
either project unless necessary.

------------------------------------------------------------------------

# 6. Secret Handling

Never commit API keys.

Create:

    .env

with the required Gemini credential locally.

Create:

    .env.example

containing only placeholders, for example:

    GEMINI_API_KEY=

Add to `.gitignore`:

    .env
    .venv/
    __pycache__/
    rollout_artifacts/
    screenshots/
    logs/

If an API key has been exposed in chat, source code, terminal history,
or Git, rotate/revoke it and use a new key.

Never put the API key in:

-   Python source
-   Docker image
-   docker-compose.yml committed to Git
-   task files
-   screenshots
-   transcripts
-   README
-   Git history

------------------------------------------------------------------------

# 7. Environment Manager

Implement an `EnvironmentManager` responsible for exactly one thing:

> Create, initialize, health-check, and destroy a clean Metabase
> environment.

Suggested interface:

    environment = EnvironmentManager(config)

    environment.start()

    environment.wait_until_ready()

    environment.initialize()

    environment.verify_expected_state()

    # run rollout

    environment.destroy()

The manager should expose:

    base_url
    container_id/name
    environment_id
    logs_path

### Required behavior

`start()`:

-   create a fresh environment
-   expose Metabase locally
-   avoid reusing stale application state

`wait_until_ready()`:

-   poll a health endpoint or reliable readiness condition
-   use a timeout
-   capture startup logs on failure

`initialize()`:

-   apply the supplied environment data using the verified mechanism
-   do not silently fall back to an empty Metabase instance

`verify_expected_state()`:

-   verify that the supplied environment actually loaded
-   verify the expected user/login state or other known fixture state
-   fail loudly if initialization did not happen

`destroy()`:

-   remove the environment and associated state
-   ensure the next rollout cannot inherit changes from the previous
    rollout

------------------------------------------------------------------------

# 8. Fresh-State Requirement

Every rollout must begin from a clean known state.

This is non-negotiable.

Bad:

    rollout 1 modifies Metabase
       |
       v
    rollout 2 starts from rollout 1's state

Good:

    Fixture
      |
      +--> Rollout 1 -> destroy
      |
      +--> Rollout 2 -> destroy
      |
      +--> Rollout 3 -> destroy

### Why

Otherwise the benchmark can produce false positives.

Example:

-   Attempt 1 creates a dashboard.
-   Attempt 2 is asked to create the same dashboard.
-   Attempt 2 appears successful without actually demonstrating the
    required behavior.

The environment must therefore be reset before every attempt.

------------------------------------------------------------------------

# 9. Metabase Initialization Strategy

Do not assume that copying the SQL into an arbitrary database will work.

Determine first whether the provided file is:

-   a dump of Metabase's application DB,
-   SQL intended for a particular DB engine,
-   an initialization script,
-   or another fixture representation.

Then implement the minimum reliable bootstrap.

Metabase's official documentation states that its application database
stores application state such as users, questions, dashboards, and
collections, and that the default local H2 database is suitable for
local demos. [Metabase
documentation](https://www.metabase.com/docs/latest/installation-and-operation/configuring-application-database)

The chosen mechanism should satisfy:

    clean machine
       ->
    bootstrap command
       ->
    exact supplied fixture
       ->
    expected Metabase state

Document the mechanism in `README.md`.

------------------------------------------------------------------------

# 10. Health Checks

Do not use "Docker container is running" as the only readiness signal.

A container can be running while Metabase is still starting or while its
application DB is broken.

Implement:

    wait_for_metabase()

which:

1.  checks the container is running,
2.  checks Metabase's HTTP endpoint/health state,
3.  retries with backoff,
4.  times out,
5.  saves logs if startup fails.

Metabase's Docker troubleshooting documentation specifically recommends
checking the container, server, application database, and host/container
connectivity when diagnosing startup failures.

------------------------------------------------------------------------

# 11. Agent Integration

Use Google's existing `computer-use-preview` implementation rather than
rebuilding the browser-control loop.

The current repository provides a `PlaywrightComputer`, `BrowserAgent`,
initial URL support, and a CLI path for a natural-language query.
[Google
computer-use-preview](https://github.com/google-gemini/computer-use-preview)

The integration should conceptually be:

    BrowserComputer(
        initial_url=metabase_url,
        screen_size=(1440, 900)
    )

    BrowserAgent(
        browser_computer=browser,
        query=task_prompt,
        model_name=configured_model
    )

    agent.agent_loop()

Do not initially modify the agent's core decision loop.

Wrap it.

------------------------------------------------------------------------

# 12. Agent Prompt Contract

The agent must receive a task-specific prompt that is clear about:

1.  The goal.
2.  The environment.
3.  The allowed interaction mechanism.
4.  The stopping condition.
5.  What it must not do.

Use a wrapper such as:

    You are operating a local Metabase benchmark environment.

    TASK:
    <task prompt>

    RULES:
    - Use only the visible browser/computer interface to complete the task.
    - Do not use Metabase APIs, database clients, shell commands, filesystem access,
      or hidden application state to complete the task.
    - Do not claim success unless you have actually completed the requested action.
    - Do not invent data or results.
    - Do not change anything unrelated to the task.
    - If the task cannot be completed, stop and report failure rather than pretending
      it was completed.
    - When the requested end state is reached, stop.

### Why

The agent is not the evaluator.

Its natural-language statement "done" must never become the reward.

The environment's state is the source of truth.

------------------------------------------------------------------------

# 13. Preventing Agent "Rogue" Behavior

For Milestone 1, constrain the agent at multiple levels.

## Prompt-level controls

Explicitly prohibit:

-   shell access
-   arbitrary HTTP/API calls
-   database access
-   reading local files
-   changing system configuration
-   actions outside Metabase
-   unrelated modifications
-   pretending an action succeeded

## Browser-level controls

Use Playwright as the only computer interface.

The agent should receive:

-   screenshots
-   browser interaction tools
-   the Metabase page

It should not receive:

-   direct database credentials
-   Metabase API credentials
-   Docker socket
-   host filesystem access
-   arbitrary subprocess execution

## Environment-level controls

Run Metabase in a disposable Docker environment.

Do not mount:

    /var/run/docker.sock

Do not mount the host home directory.

Do not expose unrelated services.

Do not provide production credentials.

## Execution-level controls

Every rollout gets:

-   maximum wall-clock time
-   maximum agent step count if supported
-   browser timeout
-   environment startup timeout

On timeout:

    stop agent
    capture artifacts
    grade current state
    mark rollout TIMEOUT
    destroy environment

------------------------------------------------------------------------

# 14. Preventing the Agent From Misleading the Evaluator

The result object must distinguish:

    agent_claim
    observed_state
    reward
    termination_reason

Example:

    {
      "task_id": "task_001",
      "agent_claim": "I created the dashboard.",
      "observed_state": {
        "dashboard_exists": false
      },
      "reward": 0,
      "termination_reason": "completed_but_failed_grader"
    }

Never do:

    if agent_says_success:
        reward = 1

Always do:

    reward = grader.inspect_environment()

The grader is authoritative.

------------------------------------------------------------------------

# 15. Transcript and Artifact Capture

For every rollout create:

    rollout_artifacts/
      <rollout_id>/
        metadata.json
        transcript.json
        screenshots/
        agent.log
        metabase.log
        result.json

At minimum capture:

-   task ID
-   rollout ID
-   start/end timestamps
-   environment ID
-   Metabase version
-   agent/model version
-   prompt
-   agent actions
-   screenshots if available
-   stdout/stderr
-   Metabase logs on failure
-   grader output
-   reward
-   termination reason

### Why

Computer-use failures are difficult to debug from a final PASS/FAIL
alone.

The artifacts let you answer:

-   What did the agent see?
-   What did it click?
-   Where did it go wrong?
-   Was Metabase broken?
-   Was the model wrong?
-   Was the grader wrong?

------------------------------------------------------------------------

# 16. Grader Design

The grader should be independent from the agent.

Preferred grading hierarchy:

1.  Direct application state check.
2.  Database/application-state check where appropriate and safe.
3.  UI-visible state check as a fallback.
4.  LLM judge only for genuinely ambiguous tasks.

Do not use screenshots + LLM judgment as the default if deterministic
state can be inspected.

Example:

    Task:
    "Create a dashboard named Monthly Revenue."

    Grader:
    1. Find dashboard by name.
    2. Verify it exists.
    3. Verify expected contents.
    4. Return PASS/FAIL plus reasons.

The grader should produce structured evidence:

    {
      "passed": true,
      "checks": [
        {
          "name": "dashboard_exists",
          "passed": true
        },
        {
          "name": "contains_revenue_question",
          "passed": true
        }
      ]
    }

------------------------------------------------------------------------

# 17. Avoid Grader Leakage

Do not give the agent access to the grader's expected answer.

The agent receives:

    task

The grader knows:

    expected outcome

The agent must not receive:

    hidden grading criteria

unless those criteria are inherently part of the user task.

This keeps the evaluation meaningful.

------------------------------------------------------------------------

# 18. Task Runner

Implement:

    run_task(task)

which performs:

    1. validate task
    2. start fresh environment
    3. initialize fixture
    4. verify environment
    5. start browser
    6. start agent
    7. execute task
    8. stop agent
    9. grade environment
    10. save artifacts
    11. destroy environment
    12. return result

Use `try/finally` so cleanup happens even when the agent crashes.

Pseudo-code:

    def run_task(task):
        env = None
        try:
            env = environment_manager.start_fresh()
            env.wait_until_ready()
            env.initialize()
            env.verify_expected_state()

            transcript = agent_runner.run(
                url=env.base_url,
                prompt=build_prompt(task)
            )

            grade = grader.grade(task, env)

            return build_result(transcript, grade)

        except Exception as exc:
            save_failure(exc)
            return failure_result(exc)

        finally:
            if env:
                env.destroy()

------------------------------------------------------------------------

# 19. Result Schema

Define one stable schema early.

Example:

    {
      "rollout_id": "...",
      "task_id": "...",
      "status": "PASS | FAIL | ERROR | TIMEOUT",
      "reward": 0,
      "started_at": "...",
      "finished_at": "...",
      "duration_seconds": 42.3,
      "agent": {
        "model": "...",
        "version": "..."
      },
      "environment": {
        "metabase_version": "...",
        "environment_id": "..."
      },
      "grader": {
        "passed": false,
        "checks": []
      },
      "artifacts": {
        "transcript": "...",
        "screenshots": "...",
        "logs": "..."
      },
      "termination_reason": "..."
    }

Keep this schema independent of the eventual frontend.

------------------------------------------------------------------------

# 20. CLI

Create one obvious command:

    python -m rollout.runner \
      --tasks tasks.json \
      --task-id <task-id>

Optional:

    --model <model>
    --timeout <seconds>
    --keep-environment-on-failure

The default behavior should clean up.

For debugging, `--keep-environment-on-failure` can be useful, but it
must never be the default because it can destroy reproducibility.

------------------------------------------------------------------------

# 21. Testing Strategy

Do not test only the full agent.

Test each layer independently.

## Test A --- Environment

Expected:

    start -> ready -> fixture loaded -> verify -> destroy

## Test B --- Browser

Expected:

    browser starts -> localhost Metabase loads -> browser closes

## Test C --- Agent

Use a tiny harmless task to confirm:

    screenshot -> action -> screenshot -> completion

## Test D --- Grader

Create a known good state and a known bad state.

Expected:

    good -> reward 1
    bad  -> reward 0

## Test E --- Full rollout

Run:

    task -> environment -> agent -> grader -> cleanup

This is the actual Milestone 1 acceptance test.

------------------------------------------------------------------------

# 22. Failure Taxonomy

Do not collapse every failure into "agent failed."

Use categories:

    ENVIRONMENT_START_FAILURE
    ENVIRONMENT_INITIALIZATION_FAILURE
    ENVIRONMENT_HEALTH_TIMEOUT
    BROWSER_START_FAILURE
    AGENT_ERROR
    AGENT_TIMEOUT
    AGENT_COMPLETED_TASK_FAILED
    GRADER_ERROR
    PASS

This distinction will be extremely useful in Milestone 2.

For example, a 50% success rate caused by Metabase startup failures is
not equivalent to a 50% success rate caused by model behavior.

------------------------------------------------------------------------

# 23. Milestone 1 Acceptance Criteria

Milestone 1 is complete only when all are true:

### Environment

-   [ ] Metabase runs locally in Docker.
-   [ ] Metabase is reachable at a known local URL.
-   [ ] The provided environment fixture is loaded automatically.
-   [ ] The fixture is verified after initialization.
-   [ ] A fresh rollout does not inherit the previous rollout's state.

### Agent

-   [ ] Google's Computer Use agent starts successfully.
-   [ ] The agent can interact with Metabase through Playwright.
-   [ ] The task is provided as natural-language input.
-   [ ] Agent actions/transcript are captured.
-   [ ] Agent execution has a timeout.

### Grading

-   [ ] At least one real task from `tasks.json` can be executed.
-   [ ] At least one task has an independent grader.
-   [ ] The grader determines success from observed environment state.
-   [ ] Agent claims are not treated as proof of success.

### Reliability

-   [ ] Environment cleanup occurs after success and failure.
-   [ ] Logs are retained on failures.
-   [ ] Secrets are not committed.
-   [ ] A clean-machine setup is documented.
-   [ ] The complete rollout can be reproduced with a single command.

------------------------------------------------------------------------

# 24. Recommended Implementation Order

Do these in exactly this order.

## Phase 1 --- Understand the fixtures

1.  Inspect `tasks.json`.
2.  Inspect `metabase_envdata.sql`.
3.  Identify Metabase application DB format.
4.  Identify exact expected login/environment state.
5.  Identify at least one task that is feasible for the MVP.

Do not write orchestration yet.

## Phase 2 --- Run Metabase manually

1.  Start Metabase.
2.  Confirm localhost access.
3.  Load the fixture manually once if necessary to understand it.
4.  Confirm expected data/state.
5.  Document the initialization process.

## Phase 3 --- Automate Metabase

Implement:

    EnvironmentManager

Get:

    python environment_test.py

to pass repeatedly.

## Phase 4 --- Validate Gemini independently

Run Google's example against a harmless website.

Confirm:

-   Python environment works.
-   Playwright works.
-   Chrome works.
-   Gemini API works.
-   agent loop works.

## Phase 5 --- Connect Gemini to Metabase

Point the initial URL at Metabase.

Start with a trivial navigation/login task.

Do not start with the hardest benchmark task.

## Phase 6 --- Build one grader

Pick one task.

Implement the narrowest deterministic success check.

## Phase 7 --- Build the rollout runner

Connect:

    fresh environment
        +
    agent
        +
    grader
        +
    artifact capture

## Phase 8 --- Repeat from scratch

Run the exact same rollout several times from fresh environments.

Investigate every discrepancy.

Only after this is stable should Milestone 1 be declared complete.

------------------------------------------------------------------------

# 25. Definition of "Done"

The final demonstration should look like:

    $ python -m rollout.runner \
        --tasks tasks.json \
        --task-id task_001

    [ENV] Starting fresh Metabase...
    [ENV] Waiting for readiness...
    [ENV] Loading environment fixture...
    [ENV] Fixture verified.

    [AGENT] Starting Gemini Computer Use...
    [AGENT] Executing task...
    [AGENT] Finished.

    [GRADER] Checking expected state...
    [GRADER] PASS

    [RESULT]
    task_id: task_001
    reward: 1
    duration: 38.2s

    Artifacts:
      rollout_artifacts/abc123/

This is the minimum convincing Milestone 1 demo.

------------------------------------------------------------------------

# 26. Important Decisions to Preserve

### Decision 1: The environment is the source of truth.

The model's text is not evidence of success.

### Decision 2: Every rollout is isolated.

Never evaluate an agent on state left behind by another attempt.

### Decision 3: Deterministic grading first.

Do not introduce an LLM judge where application state can answer the
question directly.

### Decision 4: Wrap, don't rewrite, Google's agent.

The hackathon value is the environment/evaluation infrastructure, not
reimplementing computer use.

### Decision 5: Fail loudly.

An incorrectly initialized environment must be an initialization
failure, not an apparently successful rollout.

### Decision 6: Preserve artifacts.

Every failed run should leave enough evidence to debug it.

### Decision 7: Minimize privileges.

The agent should only have the computer interaction capability needed to
complete the task.

------------------------------------------------------------------------

# 27. Open Inputs Required Before Finalizing the Bootstrap

The following information is required to turn the implementation plan
into exact commands:

1.  The actual `tasks.json`.
2.  The actual `metabase_envdata.sql`.
3.  Whether the hackathon specifies a particular Metabase commit/image.
4.  Whether the organizers expect the supplied Metabase Git repository
    to be built locally or permit an official Docker image.

The first two are the most important. The SQL format and task structure
determine the exact initialization and grading implementation.

Once those files are inspected, replace the corresponding
"determine/inspect" steps above with concrete commands and exact
configuration.

------------------------------------------------------------------------

# 28. External References

-   Metabase Docker documentation:
    https://www.metabase.com/docs/latest/installation-and-operation/running-metabase-on-docker
-   Metabase application database documentation:
    https://www.metabase.com/docs/latest/installation-and-operation/configuring-application-database
-   Google Gemini Computer Use Preview:
    https://github.com/google-gemini/computer-use-preview
-   Google's current `main.py` integration pattern uses
    `PlaywrightComputer`, `BrowserAgent`, an initial URL, and a
    natural-language query:
    https://github.com/google-gemini/computer-use-preview/blob/main/main.py
