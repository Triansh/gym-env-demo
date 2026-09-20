from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional

@dataclass
class RolloutResult:
    rollout_id: str
    task_id: str
    status: str  # PASS | FAIL | ERROR | TIMEOUT
    reward: float
    started_at: str
    finished_at: str
    duration_seconds: float
    agent_claim: str
    grader_result: Dict[str, Any]
    termination_reason: str
    artifacts_dir: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
