"""
Centralized Configuration for Metabase RL Backend.
Contains configurable settings for model selection, browser viewport (480p default),
timeouts, hyperparameters, API execution parameters, and file locations.
"""
import os
from pathlib import Path

# Base Paths
BACKEND_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = BACKEND_DIR.parent

# Browser / Environment Settings
# Default screen size: 480p (854x480) for faster renders, lower memory footprint, and quick artifact saving
DEFAULT_SCREEN_SIZE = (
    int(os.environ.get("SCREEN_WIDTH", "854")),
    int(os.environ.get("SCREEN_HEIGHT", "480"))
)

# Gemini Model / Agent Configuration
DEFAULT_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
HIGHLIGHT_MOUSE = os.environ.get("HIGHLIGHT_MOUSE", "1").lower() in ("true", "1")

# Model Generation Hyperparameters
AGENT_TEMPERATURE = float(os.environ.get("AGENT_TEMPERATURE", "1.0"))
AGENT_TOP_P = float(os.environ.get("AGENT_TOP_P", "0.95"))

# Metabase Environment Settings
DEFAULT_METABASE_URL = os.environ.get("METABASE_URL", "http://localhost:3000")
DEFAULT_DOCKER_COMPOSE_FILE = BACKEND_DIR / "docker-compose.yml"
DEFAULT_ENV_TIMEOUT = int(os.environ.get("ENV_TIMEOUT", "180"))  # Seconds to wait for Metabase startup
ENVIRONMENT_START_TIMEOUT = int(os.environ.get("ENVIRONMENT_START_TIMEOUT", "180"))  # Seconds for environment health check
AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "300"))  # Seconds for single agent execution
GRADER_TIMEOUT = int(os.environ.get("GRADER_TIMEOUT", "30"))  # Seconds for grader execution

# Benchmark / Rollout Execution Settings
DEFAULT_TASKS_FILE = Path(os.environ.get("TASKS_FILE", str(BACKEND_DIR / "tasks.json")))
DEFAULT_ROLLOUT_TIMEOUT = int(os.environ.get("ROLLOUT_TIMEOUT", "300"))  # Seconds per rollout

# API Server Settings
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
MAX_CONCURRENT_ROLLOUTS = int(os.environ.get("MAX_CONCURRENT_ROLLOUTS", "3"))  # Bounded parallel rollout workers
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "10"))
MOCK_ROLLOUTS = os.environ.get("MOCK_ROLLOUTS", "1").lower() in ("true", "1")

# State & Artifact Storage
ARTIFACTS_ROOT = WORKSPACE_ROOT / os.environ.get("ARTIFACTS_ROOT", "rollout_artifacts")
STATE_FILE = WORKSPACE_ROOT / os.environ.get("STATE_FILE", "jobs_state.json")
