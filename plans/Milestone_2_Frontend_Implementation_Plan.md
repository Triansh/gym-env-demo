# Milestone 2 --- Frontend Implementation Plan

## Single-HTML Rollout Dashboard

## 0. Objective

Build a simple, polished frontend for the Milestone 2 backend.

The frontend must be:

-   a single HTML file,
-   easy to edit,
-   easy to run locally,
-   independent of React/Next.js,
-   visually clear during a live hackathon demo,
-   connected only to the backend API,
-   focused on jobs, task progress, rollout attempts, transcripts,
    screenshots, and grades.

Recommended deliverable:

    frontend/index.html

Do not introduce a frontend framework for this milestone.

CSS and JavaScript should live inside the same HTML file so that a
developer can open one file and modify the UI directly.

------------------------------------------------------------------------

# 1. Frontend Responsibilities

The HTML frontend should allow the user to:

1.  Upload/select a `tasks.json` problem file.
2.  Specify attempts per problem.
3.  Submit a job.
4.  See the job progress.
5.  See task-level results.
6.  Expand/select a task.
7.  See each rollout attempt.
8.  See PASS/FAIL/ERROR/TIMEOUT.
9.  Open the rollout transcript.
10. View screenshots/artifacts.
11. See the observed grade/evidence.
12. Refresh/reconnect to an existing job.

The frontend must not:

-   start Docker itself,
-   start Metabase itself,
-   run Gemini,
-   calculate the official reward,
-   contain expected benchmark answers as application logic,
-   make grading decisions.

The backend is authoritative.

------------------------------------------------------------------------

# 2. Layout

Use a three-level layout.

    ┌──────────────────────────────────────────────────────────┐
    │ HEADER                                                   │
    │ Metabase Computer-Use Evaluation                        │
    │ Job: #102   ● RUNNING                         [Refresh]  │
    ├──────────────────────────────────────────────────────────┤
    │                                                          │
    │ JOB SUMMARY                                              │
    │                                                          │
    │  Tasks      Rollouts      Passed       Success Rate      │
    │   10           30           18             60%          │
    │                                                          │
    │  ███████████████████░░░░░  60%                           │
    │                                                          │
    ├──────────────────────────────────────────────────────────┤
    │                                                          │
    │ TASKS                                                   │
    │                                                          │
    │ #  Task                         Attempts    Result        │
    │ ───────────────────────────────────────────────────────  │
    │ 1  Retrieve products...        3/3         2/3 PASS      │
    │ 2  List top 3 products...      3/3         3/3 PASS      │
    │ 3  Identify most expensive...  1/3         RUNNING       │
    │                                                          │
    └──────────────────────────────────────────────────────────┘

Clicking a task opens the rollout detail panel.

------------------------------------------------------------------------

# 3. Screen 1 --- New Job

Default landing state:

    ┌─────────────────────────────────────────┐
    │ Metabase Computer-Use Evaluation        │
    │                                         │
    │ Create a rollout job                    │
    │                                         │
    │ Problem file                            │
    │ ┌─────────────────────────────────────┐ │
    │ │ Choose tasks.json                   │ │
    │ └─────────────────────────────────────┘ │
    │                                         │
    │ Attempts per problem                   │
    │ [ 3 ]                                   │
    │                                         │
    │ 10 problems × 3 attempts = 30 rollouts │
    │                                         │
    │              [ Start Job ]              │
    └─────────────────────────────────────────┘

After file selection, parse the JSON in the browser only for
preview/validation.

Do not use browser-side parsed answers for grading.

------------------------------------------------------------------------

# 4. File Preview

After selecting `tasks.json`, show:

    Problems detected: 10

    ✓ problem1
    ✓ problem2
    ✓ problem3
    ...

For each problem show a shortened task description.

Do not show the expected answer by default.

This is important because expected answers are benchmark/evaluation data
and should not accidentally become visible during the normal workflow.

------------------------------------------------------------------------

# 5. Attempts Selector

Use a simple numeric input:

    Attempts per problem
    [ 3 ]

Below it:

    10 problems × 3 attempts = 30 total rollouts

Validate:

    attempts >= 1
    attempts <= configured maximum

The maximum should ideally be returned by the backend or configured
centrally rather than hard-coded differently in frontend and backend.

------------------------------------------------------------------------

# 6. Job Running View

Once the job is submitted, transition to:

    JOB #102
    RUNNING

    Overall progress
    ████████████░░░░░░░░  54%

    16 / 30 rollouts complete

Summary cards:

    Tasks       10
    Rollouts    30
    Passed      16
    Failed      8
    Running     6
    Errors      0

Below that, show the task table.

Poll:

    GET /api/jobs/{job_id}

Use a modest polling interval, for example 1--2 seconds.

Do not hammer the backend.

------------------------------------------------------------------------

# 7. Task Table

Columns:

    #
    Task
    Attempts
    Passed
    Failed
    Status
    Success

Example:

    01
    Retrieve product titles...
    3/3
    2
    1
    COMPLETE
    66.7%

Status badges:

    RUNNING
    COMPLETE
    FAILED
    ERROR

Keep the task text truncated to prevent very long rows.

Clicking the row opens the task detail.

------------------------------------------------------------------------

# 8. Task Detail View

Use either:

-   a right-side drawer, or
-   a modal/panel below the task table.

Recommended:

    ┌─────────────────────────────────────────────┐
    │ problem1                              [×]   │
    │                                             │
    │ Retrieve product titles and ratings...      │
    │                                             │
    │ Attempts                                    │
    │                                             │
    │  Attempt 1   ✓ PASS    42s                  │
    │  Attempt 2   ✗ FAIL    58s                  │
    │  Attempt 3   ✓ PASS    37s                  │
    │                                             │
    └─────────────────────────────────────────────┘

------------------------------------------------------------------------

# 9. Rollout Detail

When clicking an attempt:

    ┌───────────────────────────────────────────────┐
    │ Attempt 2                                     │
    │                                               │
    │ Status       FAILED                           │
    │ Reward       0                                │
    │ Duration     58.2s                            │
    │ Termination  agent_completed_task_failed     │
    │                                               │
    │ ───────────────────────────────────────────   │
    │                                               │
    │ Agent transcript                              │
    │                                               │
    │ 09:41:02  Screenshot                          │
    │ 09:41:04  Clicked "New"                      │
    │ 09:41:07  Selected Products                  │
    │ ...                                           │
    │                                               │
    │ ───────────────────────────────────────────   │
    │                                               │
    │ Grader                                        │
    │                                               │
    │ ✗ product_titles                              │
    │ ✓ ratings                                     │
    │                                               │
    │ Expected vs observed                         │
    │ [collapsed by default]                        │
    └───────────────────────────────────────────────┘

The detailed grader output is especially useful for explaining failures.

------------------------------------------------------------------------

# 10. Screenshot Viewer

Provide a simple screenshot timeline.

    Agent trajectory

    [09:41:02]
    ┌───────────────────────────┐
    │ screenshot                │
    │                           │
    │      Metabase UI          │
    │                           │
    └───────────────────────────┘

    [09:41:07]
    ┌───────────────────────────┐
    │ screenshot                │
    └───────────────────────────┘

Use thumbnails with a click-to-expand behavior.

Do not load every full-resolution screenshot at once if a rollout has
many screenshots.

------------------------------------------------------------------------

# 11. Transcript Presentation

Use a compact event timeline:

    ● Screenshot
    ● Click: "Questions"
    ● Click: "New"
    ● Type: "Revenue"
    ● Screenshot
    ● Select: "Orders"

Do not make the transcript look like a raw log wall.

Important events should be visually distinct from screenshots.

If the backend exposes structured action metadata, use it.

If only raw transcript text exists, render it in a monospace expandable
area.

------------------------------------------------------------------------

# 12. Result Semantics

Use explicit terminology.

PASS:

    Reward = 1
    Grader passed

FAIL:

    Reward = 0
    Grader completed and found the task incorrect

ERROR:

    Infrastructure/agent/grader error prevented a normal evaluation

TIMEOUT:

    Rollout exceeded its configured execution limit

Do not display every non-PASS result simply as "FAIL."

This distinction helps users understand whether the model failed the
task or the evaluation system failed.

------------------------------------------------------------------------

# 13. Color and Visual Language

Use a restrained dashboard aesthetic.

Suggested semantic colors:

-   neutral gray for queued
-   blue for running
-   green for pass
-   red for task failure
-   amber for timeout
-   darker red/orange for infrastructure error

Avoid excessive colors.

The interface should look like an engineering/evaluation console, not a
consumer analytics dashboard.

------------------------------------------------------------------------

# 14. Header

Top bar:

    Metabase Computer-Use Evaluation

Right side:

    Job #102
    ● RUNNING
    [Refresh]

When completed:

    Job #102
    ● COMPLETE

When no job is selected:

    [New Job]

------------------------------------------------------------------------

# 15. Job Summary Cards

Use four or five compact cards:

    TOTAL TASKS
    10

    ROLLOUTS
    30

    PASSED
    18

    FAILED
    10

    ERRORS
    2

Do not show a single "AI score" or ranking.

The dashboard is showing measured rollout outcomes.

------------------------------------------------------------------------

# 16. Progress Bar

Show:

    18 / 30 completed

and:

    60%

The progress bar represents execution completion, not success rate.

Show success rate separately.

This prevents users from confusing:

    execution progress = 60%

with:

    model success = 60%

------------------------------------------------------------------------

# 17. API Integration

The HTML file should contain a small API client.

Example functions:

    createJob(file, attempts)
    getJob(jobId)
    getTasks(jobId)
    getRollout(rolloutId)
    getArtifact(...)
    cancelJob(jobId)

Keep API functions grouped near the top of the JavaScript section.

Set:

    const API_BASE = "http://localhost:8000/api";

or derive it from `window.location` if the backend serves the file.

Do not scatter API URLs throughout the UI code.

------------------------------------------------------------------------

# 18. State Management

Do not introduce a frontend state library.

Use one simple application state object:

    const state = {
      currentJob: null,
      tasks: [],
      selectedTask: null,
      selectedRollout: null,
      pollingTimer: null
    };

Rendering functions:

    renderJob()
    renderTasks()
    renderTaskDetail()
    renderRolloutDetail()
    renderJobSummary()

Keep data fetching separate from rendering.

------------------------------------------------------------------------

# 19. Polling

While a job is running:

    poll job
    poll task summaries
    update UI

Stop polling when:

    COMPLETE
    FAILED
    CANCELLED

Allow manual Refresh regardless of status.

Do not continuously poll rollout screenshots/transcripts unless the user
has opened that rollout.

------------------------------------------------------------------------

# 20. Loading and Empty States

Handle:

### No jobs

    No rollout jobs yet.

    [Create your first job]

### Loading

    Loading job...

### Uploading

    Uploading tasks.json...

### Starting

    Preparing rollout environments...

### No selected task

    Select a task to inspect its attempts.

### No screenshots

    No screenshots were captured for this rollout.

Never leave blank panels with no explanation.

------------------------------------------------------------------------

# 21. Error Handling

Frontend errors should be understandable.

Examples:

    Could not create job.
    Please check that the task file is valid.

    Could not load job.
    The backend may be unavailable.

    Artifact unavailable.
    The rollout completed but this artifact was not retained.

Do not expose raw Python stack traces to the normal user.

Provide a collapsible "technical details" section if the API supplies an
error ID/message.

------------------------------------------------------------------------

# 22. HTML-Only Structure

The deliverable should be one file:

    frontend/index.html

Recommended internal structure:

    <!doctype html>
    <html>
      <head>
        <style>
          /* all CSS */
        </style>
      </head>

      <body>
        <!-- all HTML -->
        <script>
          // API client
          // state
          // rendering
          // event handlers
          // polling
        </script>
      </body>
    </html>

No React.

No JSX.

No build step.

No npm dependency required for the basic UI.

This is intentionally chosen so the layout can be edited quickly during
the hackathon.

------------------------------------------------------------------------

# 23. Responsive Layout

Primary target:

    desktop browser

Minimum useful width:

    ~1100px

Still make the main panels usable at smaller widths.

Use CSS Grid/Flexbox.

Avoid fixed pixel positioning.

------------------------------------------------------------------------

# 24. Frontend Safety

The browser must never have access to:

-   Gemini API keys
-   Metabase admin credentials
-   expected answers beyond what the backend intentionally exposes
-   Docker credentials
-   filesystem paths

The frontend only talks to the backend.

Do not put secrets in `index.html`.

------------------------------------------------------------------------

# 25. Demo Flow

The frontend should support this clean demo:

1.  Open `index.html`.

2.  Click "Create Job."

3.  Select `tasks.json`.

4.  Set attempts to `3`.

5.  UI displays:

        10 problems × 3 attempts = 30 rollouts

6.  Click "Start Job."

7.  Dashboard appears.

8.  Progress updates live.

9.  Click `problem1`.

10. See three attempts.

11. Click an attempt.

12. See:

    -   status
    -   reward
    -   duration
    -   transcript
    -   screenshots
    -   grader evidence

13. Wait for job completion.

14. View aggregate pass/fail statistics.

This is the core Milestone 2 demo.

------------------------------------------------------------------------

# 26. Frontend Acceptance Criteria

-   [ ] Single `index.html` file.
-   [ ] No frontend framework required.
-   [ ] User can submit `tasks.json`.
-   [ ] User can choose attempts per task.
-   [ ] User can start a job.
-   [ ] Job progress is visible.
-   [ ] Task-level status is visible.
-   [ ] Individual attempts are visible.
-   [ ] Rollout status/reward/duration are visible.
-   [ ] Transcript can be inspected.
-   [ ] Screenshots can be inspected.
-   [ ] Grader evidence can be inspected.
-   [ ] PASS/FAIL/ERROR/TIMEOUT are visually distinct.
-   [ ] Page updates while a job runs.
-   [ ] Completed jobs can be revisited/refreshed.
-   [ ] No secrets exist in the HTML.
-   [ ] Frontend does not perform grading.
-   [ ] Frontend does not interact directly with Docker or Metabase.

------------------------------------------------------------------------

# 27. Recommended Implementation Order

1.  Create static HTML layout.
2.  Add CSS and visual states.
3.  Add mock data to verify the layout.
4.  Build API client.
5.  Connect Create Job.
6.  Connect Job Status.
7.  Connect Task Table.
8.  Connect Task Detail.
9.  Connect Rollout Detail.
10. Connect transcript.
11. Connect screenshots.
12. Add polling.
13. Add error states.
14. Remove mock data.
15. Test against a real running backend.

Build the layout with realistic mock data first. This makes frontend
iteration fast and prevents backend availability from slowing down UI
design.

------------------------------------------------------------------------

# 28. Suggested Visual Hierarchy

The user should understand the state of the evaluation in this order:

    1. Is the job running?
    2. How much has completed?
    3. How many passed?
    4. Which tasks are problematic?
    5. What happened during an individual attempt?
    6. Why did the grader pass/fail it?

Do not make raw transcripts the primary screen.

The primary screen is an evaluation overview; detailed trajectories
belong one level deeper.

------------------------------------------------------------------------

# 29. Definition of Done

The final frontend demo should allow a reviewer to open one HTML file
and see:

    Create Job
       ->
    upload tasks.json
       ->
    choose attempts
       ->
    start
       ->
    live progress
       ->
    task table
       ->
    select task
       ->
    select rollout
       ->
    transcript/screenshots/grader
       ->
    final job statistics

The frontend should make the backend's rollout infrastructure
understandable without requiring the reviewer to inspect terminal
output.
