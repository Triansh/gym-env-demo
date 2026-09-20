import sys
import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

CUP_PATH = Path(__file__).resolve().parent.parent.parent / "computer-use-preview"
if str(CUP_PATH) not in sys.path:
    sys.path.insert(0, str(CUP_PATH))

import importlib.util

# Import BrowserAgent directly from computer-use-preview/agent.py to avoid collision with local 'agent' folder
cup_agent_path = CUP_PATH / "agent.py"
spec = importlib.util.spec_from_file_location("cup_agent", cup_agent_path)
cup_agent_mod = importlib.util.module_from_spec(spec)
sys.modules["cup_agent"] = cup_agent_mod
spec.loader.exec_module(cup_agent_mod)
BrowserAgent = cup_agent_mod.BrowserAgent

# Import PlaywrightComputer from computers package in computer-use-preview
from computers.playwright.playwright import PlaywrightComputer

from backend.configs import (
    DEFAULT_MODEL_NAME,
    DEFAULT_SCREEN_SIZE,
    DEFAULT_METABASE_URL,
    HIGHLIGHT_MOUSE
)
from .prompts import build_agent_prompt

logger = logging.getLogger(__name__)

class AgentRunner:
    """Wrapper around Gemini Computer Use Preview agent."""

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME):
        self.model_name = model_name

    def run(self, task_prompt: str, initial_url: str = DEFAULT_METABASE_URL, screen_size=DEFAULT_SCREEN_SIZE) -> dict:
        full_prompt = build_agent_prompt(task_prompt)
        logger.info(f"Starting agent run against {initial_url} using model {self.model_name} (viewport: {screen_size})...")

        env = PlaywrightComputer(
            screen_size=screen_size,
            initial_url=initial_url,
            highlight_mouse=HIGHLIGHT_MOUSE
        )

        agent_claim = ""
        history = []

        try:
            with env as browser_computer:
                agent = BrowserAgent(
                    browser_computer=browser_computer,
                    query=full_prompt,
                    model_name=self.model_name,
                    verbose=True
                )
                agent.agent_loop()
                agent_claim = agent.final_reasoning or ""
                history = agent._contents
                step_screenshots = getattr(agent, "step_screenshots", [])
        except Exception as e:
            logger.error(f"Agent loop error: {e}")
            raise e

        return {
            "agent_claim": agent_claim,
            "history": history,
            "screenshots": step_screenshots
        }
