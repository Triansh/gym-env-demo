import logging
from .manager import EnvironmentManager

logging.basicConfig(level=logging.INFO)

def reset_environment(base_url: str = "http://localhost:3000", project_dir: str = "."):
    """Reset Metabase environment to a pristine state."""
    manager = EnvironmentManager(base_url=base_url, project_dir=project_dir)
    manager.reset()

if __name__ == "__main__":
    reset_environment()
