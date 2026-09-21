import subprocess
import time
import logging
import requests
from pathlib import Path
from backend.configs import DEFAULT_METABASE_URL, DEFAULT_DOCKER_COMPOSE_FILE, DEFAULT_ENV_TIMEOUT
from .health import wait_for_metabase, check_metabase_health

logger = logging.getLogger(__name__)

# Subprocess timeout (seconds) for individual docker CLI calls.
# Keeps cleanup bounded even if dockerd is unresponsive.
_DOCKER_CMD_TIMEOUT = 60


def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """
    Thin wrapper around subprocess.run with safe defaults:
      - close_fds=True  → never inherit open file descriptors into child processes
      - timeout         → bounded; raises subprocess.TimeoutExpired if dockerd hangs
      - capture_output  → pipes don't leak to the terminal
    Callers should not need to set these individually.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("close_fds", True)
    kwargs.setdefault("timeout", _DOCKER_CMD_TIMEOUT)
    return subprocess.run(cmd, **kwargs)


class EnvironmentManager:
    """
    Manages the lifecycle of a fresh, isolated Dockerized Metabase environment.

    Each rollout gets its own docker-compose project (--project-name), unique
    container names, and unique host ports so parallel rollouts never conflict.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_METABASE_URL,
        project_dir: str = None,
        project_name: str = "metabase_rl",
        metabase_port: int = 3000,
        postgres_port: int = 5432,
    ):
        self.base_url = base_url.rstrip("/")
        self.project_name = project_name
        self.metabase_port = metabase_port
        self.postgres_port = postgres_port

        if project_dir:
            self.project_dir = Path(project_dir).resolve()
            self.compose_file = self.project_dir / "docker-compose.yml"
        else:
            self.compose_file = DEFAULT_DOCKER_COMPOSE_FILE
            self.project_dir = self.compose_file.parent

    def _compose_env(self) -> dict:
        """Environment variables passed to docker compose."""
        import os
        env = os.environ.copy()
        env["COMPOSE_PROJECT_NAME"] = self.project_name
        env["METABASE_PORT"] = str(self.metabase_port)
        env["POSTGRES_PORT"] = str(self.postgres_port)
        return env

    def _compose_cmd(self, *args) -> list:
        return [
            "docker", "compose",
            "-p", self.project_name,
            "-f", str(self.compose_file),
            *args,
        ]

    def start(self, clean: bool = True):
        """Start the Docker compose environment."""
        if clean:
            self.destroy()

        logger.info(f"[{self.project_name}] Starting containers (Metabase:{self.metabase_port}, PG:{self.postgres_port})...")
        cmd = self._compose_cmd("up", "-d")
        res = _run(cmd, cwd=str(self.project_dir), env=self._compose_env())
        if res.returncode != 0:
            raise RuntimeError(f"[{self.project_name}] Failed to start environment:\n{res.stderr}")
        logger.info(f"[{self.project_name}] Containers started.")

    def wait_until_ready(self, timeout: int = 180) -> bool:
        """Wait until Metabase service is fully up and healthy."""
        return wait_for_metabase(base_url=self.base_url, timeout=timeout)

    def verify_expected_state(self) -> bool:
        """Verify that Metabase application state can be queried."""
        try:
            res = requests.get(f"{self.base_url}/api/session/properties", timeout=10)
            if res.status_code == 200:
                logger.info(f"[{self.project_name}] Metabase session/properties endpoint verified.")
                return True
        except Exception as e:
            logger.error(f"[{self.project_name}] Failed to verify Metabase state: {e}")
        return False

    def destroy(self):
        """Tear down Docker compose environment and remove all volumes — no data left behind."""
        logger.info(f"[{self.project_name}] Destroying environment...")

        # Compose down with --volumes removes named volumes scoped to this project
        cmd = self._compose_cmd("down", "-v", "--remove-orphans", "--timeout", "5")
        try:
            res = _run(cmd, cwd=str(self.project_dir), env=self._compose_env())
            if res.returncode != 0:
                logger.warning(f"[{self.project_name}] compose down returned {res.returncode}: {res.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logger.warning(f"[{self.project_name}] compose down timed out; containers may still be running")

        logger.info(f"[{self.project_name}] Environment destroyed and volumes purged.")

    def reset(self, timeout: int = 180):
        """Reset environment to fresh state."""
        self.start(clean=True)
        if not self.wait_until_ready(timeout=timeout):
            raise RuntimeError(f"[{self.project_name}] Environment failed to become healthy after reset.")
        if not self.verify_expected_state():
            raise RuntimeError(f"[{self.project_name}] State verification failed after reset.")
