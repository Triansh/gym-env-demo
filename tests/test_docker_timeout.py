import pytest
import subprocess
from unittest import mock
from backend.environment.manager import EnvironmentManager

def test_docker_start_timeout():
    env = EnvironmentManager(project_name="timeout_test")
    
    # Clean is disabled to avoid the destroy call which also could timeout (but it catches it)
    with mock.patch("backend.environment.manager._run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["docker", "compose", "up"], timeout=60)
        
        with pytest.raises(RuntimeError) as exc_info:
            env.start(clean=False)
            
        assert "Docker command timeout" in str(exc_info.value)

def test_docker_destroy_timeout():
    env = EnvironmentManager(project_name="timeout_test")
    
    with mock.patch("backend.environment.manager._run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["docker", "compose", "down"], timeout=5)
        
        # destroy should catch TimeoutExpired and log a warning, NOT raise
        try:
            env.destroy()
        except subprocess.TimeoutExpired:
            pytest.fail("destroy() should not raise TimeoutExpired")
