import os
from dotenv import load_dotenv

load_dotenv()

SYSTEM_PROMPT_TEMPLATE = """You are operating a local Metabase benchmark environment.

METABASE CREDENTIALS (if required on login screen):
- Username/Email: {metabase_user}
- Password: {metabase_password}

TASK:
{task_prompt}

RULES:
1. Use ONLY the visible browser computer interface to complete the task.
2. Do NOT attempt shell commands, direct API calls, database connections, or host file operations.
3. Do NOT claim task completion unless you have actually performed the required actions in Metabase.
4. Do NOT change any settings, databases, questions, or dashboards unrelated to the task.
5. Return the final answer in the exact JSON format requested in the task.
6. When the requested task is completed, stop immediately.
"""

def build_agent_prompt(task_prompt: str) -> str:
    """Format the task prompt with standard contract rules and credentials."""
    user = os.environ.get("METABASE_USER", "daksh@deeptune.com")
    password = os.environ.get("METABASE_PASSWORD", "Daksh@123")
    return SYSTEM_PROMPT_TEMPLATE.format(
        task_prompt=task_prompt.strip(),
        metabase_user=user,
        metabase_password=password
    )

