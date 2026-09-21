import json
import logging
import os
import signal
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

class AgentExecutionTimeout(TimeoutError):
    pass

def run_agent_subprocess(
    task_prompt: str,
    initial_url: str,
    model_name: str,
    artifact_dir: str,
    timeout_seconds: int,
    _runner_script_override: str = None
) -> dict:
    runner_script = _runner_script_override or os.path.join(os.path.dirname(__file__), "subprocess_runner.py")
    
    kwargs = {}
    if os.name == 'posix':
        kwargs['preexec_fn'] = os.setsid
        
    env = os.environ.copy()
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env["PYTHONPATH"] = f"{project_root}:{env.get('PYTHONPATH', '')}"

    proc = subprocess.Popen(
        [sys.executable, runner_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        **kwargs
    )
    
    input_data = {
        "task_prompt": task_prompt,
        "initial_url": initial_url,
        "model_name": model_name,
        "artifact_dir": str(artifact_dir)
    }
    
    try:
        stdout, stderr = proc.communicate(input=json.dumps(input_data), timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        logger.error(f"Agent timed out after {timeout_seconds}s. Killing process group.")
        if os.name == 'posix':
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(1)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.terminate()
        proc.communicate()
        raise AgentExecutionTimeout(f"Agent exceeded {timeout_seconds}s")
        
    if proc.returncode != 0:
        logger.error(f"Agent subprocess exited with code {proc.returncode}. Stderr: {stderr}")
        return {"success": False, "error": f"Agent crashed with exit code {proc.returncode}\n{stderr}"}
        
    result_file = os.path.join(artifact_dir, "agent_result.json")
    if os.path.exists(result_file):
        with open(result_file, "r") as f:
            return json.load(f)
    return {"success": False, "error": "Agent finished but agent_result.json not found"}
