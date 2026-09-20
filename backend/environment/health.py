import time
import requests
import logging

logger = logging.getLogger(__name__)

def check_metabase_health(base_url: str = "http://localhost:3000") -> bool:
    """Check if Metabase health endpoint returns status OK."""
    try:
        response = requests.get(f"{base_url}/api/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "ok":
                return True
    except Exception as err:
        logger.debug(f"Health check failed: {err}")
    return False

def wait_for_metabase(base_url: str = "http://localhost:3000", timeout: int = 180, poll_interval: int = 3) -> bool:
    """Poll Metabase health endpoint until ready or timeout."""
    start_time = time.time()
    logger.info(f"Waiting for Metabase at {base_url} (timeout={timeout}s)...")
    while time.time() - start_time < timeout:
        if check_metabase_health(base_url):
            logger.info("Metabase is healthy and ready!")
            return True
        time.sleep(poll_interval)
    logger.error("Timed out waiting for Metabase startup.")
    return False
