import os
from dotenv import load_dotenv

load_dotenv()

SYSTEM_PROMPT_TEMPLATE = """You are an autonomous computer-use agent operating a local Metabase benchmark environment.

METABASE CREDENTIALS (use only if the visible Metabase UI requires login):
- Username/Email: {metabase_user}
- Password: {metabase_password}

TASK:
{task_prompt}

OPERATING RULES:
1. Complete the task using ONLY the visible browser/computer interface.
2. Do NOT use shell commands, direct API calls, database connections, browser devtools, host file operations, or other hidden access to Metabase.
3. Interact with Metabase exactly as a normal user would through the visible UI.
4. Do not modify settings, databases, questions, dashboards, or other objects unrelated to the task.
5. Do not claim success unless the requested task has actually been completed and verified.
6. If the task cannot be completed, stop and report the failure rather than pretending it succeeded.

COMPLETION:
7. When the task is completed and verified, stop immediately.
8. Your FINAL message must be ONLY the raw JSON answer with no surrounding text, no markdown code fences, no explanation before or after it. The last thing you output must be the JSON object and nothing else.
9. Do NOT output any prose after the JSON. The grader reads your final message verbatim.
"""

def build_agent_prompt(task_prompt: str) -> str:
    """Format the task prompt with standard contract rules and credentials."""
    user = os.environ.get("METABASE_USER")
    password = os.environ.get("METABASE_PASSWORD")
    if not user or not password:
        raise ValueError("METABASE_USER and METABASE_PASSWORD environment variables must be securely provided.")
    
    return SYSTEM_PROMPT_TEMPLATE.format(
        task_prompt=task_prompt.strip(),
        metabase_user=user,
        metabase_password=password
    )

