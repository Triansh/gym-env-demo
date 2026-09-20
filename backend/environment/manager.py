import subprocess
import time
import logging
import requests
from pathlib import Path
from backend.configs import DEFAULT_METABASE_URL, DEFAULT_DOCKER_COMPOSE_FILE, DEFAULT_ENV_TIMEOUT
from .health import wait_for_metabase, check_metabase_health

logger = logging.getLogger(__name__)

class EnvironmentManager:
    """Manages the lifecycle of a fresh, isolated Dockerized Metabase environment."""

    def __init__(self, base_url: str = DEFAULT_METABASE_URL, project_dir: str = None):
        self.base_url = base_url.rstrip("/")
        if project_dir:
            self.project_dir = Path(project_dir).resolve()
            self.compose_file = self.project_dir / "docker-compose.yml"
        else:
            self.compose_file = DEFAULT_DOCKER_COMPOSE_FILE
            self.project_dir = self.compose_file.parent

    def start(self, clean: bool = True):
        """Start the Docker compose environment."""
        if clean:
            self.destroy()

        logger.info("Starting Docker containers (PostgreSQL + Metabase)...")
        cmd = ["docker", "compose", "-f", str(self.compose_file), "up", "-d"]
        res = subprocess.run(cmd, cwd=str(self.project_dir), capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to start docker compose environment:\n{res.stderr}")
        logger.info("Docker containers started.")

    def wait_until_ready(self, timeout: int = 180) -> bool:
        """Wait until Metabase service is fully up and healthy."""
        return wait_for_metabase(base_url=self.base_url, timeout=timeout)

    def verify_expected_state(self) -> bool:
        """Verify that Metabase application state and database session can be queried."""
        try:
            res = requests.get(f"{self.base_url}/api/session/properties", timeout=10)
            if res.status_code == 200:
                logger.info("Metabase session/properties endpoint verified.")
                return True
        except Exception as e:
            logger.error(f"Failed to verify Metabase expected state: {e}")
        return False

    def destroy(self):
        """Tear down Docker compose environment and purge volume state."""
        logger.info("Tearing down Docker environment (purging volumes)...")
        cmd = ["docker", "compose", "-f", str(self.compose_file), "down", "-v", "--remove-orphans"]
        subprocess.run(cmd, cwd=str(self.project_dir), capture_output=True, text=True)
        logger.info("Docker environment destroyed.")

    def reset(self, timeout: int = 180):
        """Reset environment to fresh state."""
        self.start(clean=True)
        if not self.wait_until_ready(timeout=timeout):
            raise RuntimeError("Environment failed to become healthy after reset.")
        if not self.verify_expected_state():
            raise RuntimeError("Environment state verification failed after reset.")
