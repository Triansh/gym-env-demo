"""
SQLite Data Access Layer for Metabase RL Backend.
Manages persistent storage for Tasks, Jobs, and Rollouts using SQLite with WAL mode.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from backend.configs import SQLITE_DB_PATH
from backend.models import ErrorType, Job, JobStatus, Rollout, RolloutStatus, Task

logger = logging.getLogger(__name__)


class SQLiteStore:
    """Synchronous/Thread-safe SQLite database manager for jobs, rollouts, and tasks."""

    def __init__(self, db_path: Optional[Union[Path, str]] = None):
        self.db_path = Path(db_path) if db_path else SQLITE_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Initialize SQLite database tables and set WAL mode."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Performance settings
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")

                # Tasks table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        prompt TEXT NOT NULL,
                        expected_answer TEXT,
                        created_at TEXT NOT NULL
                    );
                """)

                # Jobs table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        task_file TEXT DEFAULT 'tasks.json',
                        attempts_per_task INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        total INTEGER DEFAULT 0,
                        completed INTEGER DEFAULT 0,
                        passed INTEGER DEFAULT 0,
                        failed INTEGER DEFAULT 0,
                        errors INTEGER DEFAULT 0,
                        timeouts INTEGER DEFAULT 0,
                        rollout_ids TEXT NOT NULL,
                        tasks_json TEXT NOT NULL
                    );
                """)

                # Check if task_file column exists for existing SQLite database files
                cursor.execute("PRAGMA table_info(jobs);")
                columns = [column[1] for column in cursor.fetchall()]
                if "task_file" not in columns:
                    cursor.execute("ALTER TABLE jobs ADD COLUMN task_file TEXT DEFAULT 'tasks.json';")

                # Rollouts table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS rollouts (
                        rollout_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        attempt_number INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        reward REAL,
                        started_at TEXT,
                        completed_at TEXT,
                        duration_seconds REAL,
                        termination_reason TEXT,
                        error_type TEXT,
                        error_message TEXT,
                        error_stage TEXT,
                        cleanup_error TEXT,
                        artifact_path TEXT,
                        agent_claim TEXT,
                        grader_result TEXT,
                        screenshots TEXT,
                        transcript TEXT,
                        FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                    );
                """)

                # Indexes for fast lookup
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_rollouts_job_id ON rollouts(job_id);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_rollouts_status ON rollouts(status);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);")

                conn.commit()
                logger.info(f"Initialized SQLite database schema at {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize SQLite database at {self.db_path}: {e}")
            raise

    # ------------------------------------------------------------------
    # Task Operations
    # ------------------------------------------------------------------

    def save_task(self, task: Task) -> None:
        expected_ans = json.dumps(task.expected_answer) if not isinstance(task.expected_answer, str) else task.expected_answer
        now_str = datetime.utcnow().isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks (task_id, prompt, expected_answer, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    prompt=excluded.prompt,
                    expected_answer=excluded.expected_answer;
                """,
                (task.id, task.prompt, expected_ans, now_str)
            )
            conn.commit()

    def list_tasks(self) -> List[Task]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT task_id, prompt, expected_answer FROM tasks").fetchall()
            tasks = []
            for r in rows:
                try:
                    ans = json.loads(r["expected_answer"]) if r["expected_answer"] else ""
                except Exception:
                    ans = r["expected_answer"] or ""
                tasks.append(Task(id=r["task_id"], prompt=r["prompt"], expected_answer=ans))
            return tasks

    # ------------------------------------------------------------------
    # Job Operations
    # ------------------------------------------------------------------

    def save_job(self, job: Job) -> None:
        status_val = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
        tasks_json = json.dumps([{"task_id": t.id, "task": t.prompt, "expected_answer": t.expected_answer} for t in job.tasks])
        rollout_ids_json = json.dumps(job.rollout_ids)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, status, task_file, attempts_per_task, created_at, started_at, completed_at,
                    total, completed, passed, failed, errors, timeouts, rollout_ids, tasks_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    task_file=excluded.task_file,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    total=excluded.total,
                    completed=excluded.completed,
                    passed=excluded.passed,
                    failed=excluded.failed,
                    errors=excluded.errors,
                    timeouts=excluded.timeouts,
                    rollout_ids=excluded.rollout_ids,
                    tasks_json=excluded.tasks_json;
                """,
                (
                    job.id, status_val, job.task_file, job.attempts_per_task, job.created_at, job.started_at, job.completed_at,
                    job.total, job.completed, job.passed, job.failed, job.errors, job.timeouts,
                    rollout_ids_json, tasks_json
                )
            )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return self._row_to_job(row)

    def list_jobs(self) -> List[Job]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
            return [self._row_to_job(r) for r in rows]

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        try:
            tasks_raw = json.loads(row["tasks_json"]) if row["tasks_json"] else []
        except Exception:
            tasks_raw = []
        tasks = []
        for t in tasks_raw:
            tasks.append(Task(
                id=t.get("task_id") or t.get("id") or "task",
                prompt=t.get("task") or t.get("prompt") or "",
                expected_answer=t.get("expected_answer", "")
            ))

        try:
            rollout_ids = json.loads(row["rollout_ids"]) if row["rollout_ids"] else []
        except Exception:
            rollout_ids = []

        try:
            status = JobStatus(row["status"])
        except ValueError:
            status = JobStatus.QUEUED

        keys = row.keys()
        task_file = row["task_file"] if "task_file" in keys and row["task_file"] else "tasks.json"

        return Job(
            id=row["job_id"],
            tasks=tasks,
            rollout_ids=rollout_ids,
            attempts_per_task=row["attempts_per_task"],
            status=status,
            task_file=task_file,
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            total=row["total"],
            completed=row["completed"],
            passed=row["passed"],
            failed=row["failed"],
            errors=row["errors"],
            timeouts=row["timeouts"],
        )

    # ------------------------------------------------------------------
    # Rollout Operations
    # ------------------------------------------------------------------

    def save_rollout(self, rollout: Rollout) -> None:
        status_val = rollout.status.value if isinstance(rollout.status, RolloutStatus) else str(rollout.status)
        error_val = rollout.error_type.value if isinstance(rollout.error_type, ErrorType) else (str(rollout.error_type) if rollout.error_type else None)
        grader_json = json.dumps(rollout.grader_result) if rollout.grader_result is not None else "{}"
        screenshots_json = json.dumps(rollout.screenshots) if rollout.screenshots is not None else "[]"
        transcript_json = json.dumps(rollout.transcript) if rollout.transcript is not None else "[]"

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO rollouts (
                    rollout_id, job_id, task_id, attempt_number, status, reward, started_at, completed_at,
                    duration_seconds, termination_reason, error_type, error_message, error_stage,
                    cleanup_error, artifact_path, agent_claim, grader_result, screenshots, transcript
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rollout_id) DO UPDATE SET
                    status=excluded.status,
                    reward=excluded.reward,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    duration_seconds=excluded.duration_seconds,
                    termination_reason=excluded.termination_reason,
                    error_type=excluded.error_type,
                    error_message=excluded.error_message,
                    error_stage=excluded.error_stage,
                    cleanup_error=excluded.cleanup_error,
                    artifact_path=excluded.artifact_path,
                    agent_claim=excluded.agent_claim,
                    grader_result=excluded.grader_result,
                    screenshots=excluded.screenshots,
                    transcript=excluded.transcript;
                """,
                (
                    rollout.id, rollout.job_id, rollout.task_id, rollout.attempt_number, status_val, rollout.reward,
                    rollout.started_at, rollout.completed_at, rollout.duration_seconds, rollout.termination_reason,
                    error_val, rollout.error_message, rollout.error_stage, rollout.cleanup_error,
                    rollout.artifact_path, rollout.agent_claim, grader_json, screenshots_json, transcript_json
                )
            )
            conn.commit()

    def get_rollout(self, rollout_id: str) -> Optional[Rollout]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM rollouts WHERE rollout_id = ?", (rollout_id,)).fetchone()
            if not row:
                return None
            return self._row_to_rollout(row)

    def get_rollouts_for_job(self, job_id: str) -> List[Rollout]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM rollouts WHERE job_id = ? ORDER BY attempt_number ASC", (job_id,)).fetchall()
            return [self._row_to_rollout(r) for r in rows]

    def list_all_rollouts(self) -> List[Rollout]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM rollouts").fetchall()
            return [self._row_to_rollout(r) for r in rows]

    def _row_to_rollout(self, row: sqlite3.Row) -> Rollout:
        try:
            status = RolloutStatus(row["status"])
        except ValueError:
            status = RolloutStatus.QUEUED

        error_type = None
        if row["error_type"]:
            try:
                error_type = ErrorType(row["error_type"])
            except ValueError:
                error_type = None

        try:
            grader_result = json.loads(row["grader_result"]) if row["grader_result"] else {}
        except Exception:
            grader_result = {}

        try:
            screenshots = json.loads(row["screenshots"]) if row["screenshots"] else []
        except Exception:
            screenshots = []

        try:
            transcript = json.loads(row["transcript"]) if row["transcript"] else []
        except Exception:
            transcript = []

        return Rollout(
            id=row["rollout_id"],
            job_id=row["job_id"],
            task_id=row["task_id"],
            attempt_number=row["attempt_number"],
            status=status,
            reward=row["reward"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            duration_seconds=row["duration_seconds"],
            termination_reason=row["termination_reason"],
            error_type=error_type,
            error_message=row["error_message"],
            error_stage=row["error_stage"],
            cleanup_error=row["cleanup_error"],
            artifact_path=row["artifact_path"],
            agent_claim=row["agent_claim"] or "",
            grader_result=grader_result,
            screenshots=screenshots,
            transcript=transcript,
        )

    def count_jobs(self) -> int:
        with self._get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM jobs").fetchone()
            return row["cnt"] if row else 0

    # ------------------------------------------------------------------
    # Business Logic / Sync API Methods
    # ------------------------------------------------------------------

    def create_job(self, job: Job, rollouts: List[Rollout]) -> None:
        self.save_job(job)
        for r in rollouts:
            self.save_rollout(r)

    def get_job_history(
        self,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Dict[str, Any]:
        all_jobs = self.list_jobs()
        total_jobs_count = len(all_jobs)
        total_rollouts_count = sum(j.total for j in all_jobs)
        total_passed_rollouts = sum(j.passed for j in all_jobs)
        total_failed_rollouts = sum(j.failed + j.errors + j.timeouts for j in all_jobs)
        active_jobs_count = sum(
            1 for j in all_jobs
            if (j.status.value if hasattr(j.status, "value") else str(j.status)) in ("QUEUED", "RUNNING")
        )

        overall_pass_rate = (
            round((total_passed_rollouts / total_rollouts_count) * 100, 1)
            if total_rollouts_count > 0
            else 0.0
        )

        filtered_jobs = all_jobs
        if status:
            st_upper = status.upper()
            filtered_jobs = [
                j for j in filtered_jobs
                if (j.status.value if hasattr(j.status, "value") else str(j.status)) == st_upper
            ]

        if search:
            s_lower = search.lower()
            filtered_jobs = [
                j for j in filtered_jobs
                if s_lower in j.id.lower() or s_lower in j.task_file.lower() or any(
                    s_lower in t.prompt.lower() or s_lower in t.id.lower() for t in j.tasks
                )
            ]

        total_filtered = len(filtered_jobs)

        if limit is not None and limit > 0:
            paged_jobs = filtered_jobs[offset : offset + limit]
        else:
            paged_jobs = filtered_jobs[offset:]

        formatted_jobs = []
        for j in paged_jobs:
            j_dict = j.to_dict()
            j_dict["id"] = j.id
            duration_seconds = None
            if j.started_at and j.completed_at:
                try:
                    t1 = datetime.fromisoformat(j.started_at)
                    t2 = datetime.fromisoformat(j.completed_at)
                    duration_seconds = round(max(0.0, (t2 - t1).total_seconds()), 1)
                except Exception:
                    duration_seconds = None

            pass_rate = round((j.passed / j.total) * 100, 1) if j.total > 0 else 0.0

            j_dict["duration_seconds"] = duration_seconds
            j_dict["pass_rate"] = pass_rate
            j_dict["tasks_count"] = len(j.tasks)
            formatted_jobs.append(j_dict)

        return {
            "summary": {
                "total_jobs": total_jobs_count,
                "total_rollouts": total_rollouts_count,
                "total_passed_rollouts": total_passed_rollouts,
                "total_failed_rollouts": total_failed_rollouts,
                "overall_pass_rate": overall_pass_rate,
                "active_jobs_count": active_jobs_count,
            },
            "total_filtered": total_filtered,
            "offset": offset,
            "limit": limit,
            "jobs": formatted_jobs,
        }

    def cancel_job(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if not job:
            return False
        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
            return False
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.utcnow().isoformat()
        self.save_job(job)

        for r in self.get_rollouts_for_job(job_id):
            if not r.is_terminal:
                r.status = RolloutStatus.CANCELLED
                r.termination_reason = "job_cancelled"
                r.completed_at = datetime.utcnow().isoformat()
                self.save_rollout(r)

        self._recompute_job_progress(job_id)
        return True

    def update_rollout(self, rollout_id: str, **kwargs) -> None:
        rollout = self.get_rollout(rollout_id)
        if not rollout:
            logger.warning(f"update_rollout: rollout {rollout_id} not found")
            return

        for k, v in kwargs.items():
            if hasattr(rollout, k):
                setattr(rollout, k, v)

        self.save_rollout(rollout)
        self._recompute_job_progress(rollout.job_id)

    def _recompute_job_progress(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if not job:
            return

        rollouts = self.get_rollouts_for_job(job_id)
        passed = sum(1 for r in rollouts if r.status == RolloutStatus.PASSED)
        failed = sum(1 for r in rollouts if r.status == RolloutStatus.FAILED)
        errors = sum(1 for r in rollouts if r.status == RolloutStatus.ERROR)
        timeouts = sum(1 for r in rollouts if r.status == RolloutStatus.TIMEOUT)
        cancelled = sum(1 for r in rollouts if r.status == RolloutStatus.CANCELLED)
        completed = passed + failed + errors + timeouts + cancelled

        job.passed = passed
        job.failed = failed
        job.errors = errors
        job.timeouts = timeouts
        job.completed = completed
        job.total = len(rollouts)

        if completed == job.total and job.total > 0:
            if job.status not in (JobStatus.CANCELLED,):
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.utcnow().isoformat()
        elif job.status == JobStatus.QUEUED and completed > 0:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.utcnow().isoformat()

        self.save_job(job)
