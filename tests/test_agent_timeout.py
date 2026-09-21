import os
import signal
import time
import pytest
from backend.agent.supervisor import run_agent_subprocess, AgentExecutionTimeout

def test_agent_hard_timeout(tmp_path):
    fake_script = tmp_path / "fake_agent.py"
    fake_script.write_text("""
import time
import sys
# ignores sigterm to test posix sigkill
import signal
def handler(signum, frame):
    pass
signal.signal(signal.SIGTERM, handler)
while True:
    time.sleep(1)
""")

    start_time = time.time()
    try:
        run_agent_subprocess(
            task_prompt="test",
            initial_url="http://test",
            model_name="test",
            artifact_dir=str(tmp_path),
            timeout_seconds=1,
            _runner_script_override=str(fake_script)
        )
        pytest.fail("Expected AgentExecutionTimeout")
    except AgentExecutionTimeout as e:
        assert "exceeded" in str(e).lower()
        
    elapsed = time.time() - start_time
    assert elapsed < 3.0  # Should timeout quickly (1s + 1s sigterm wait)
    
    # We could theoretically check if there are no child processes left, but python cross platform is tricky,
    # and the timeout proves it didn't hang indefinitely.
