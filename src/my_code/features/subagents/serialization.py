"""Stable model-visible projection of background Subagent state."""

from my_code.agent.models import AgentInvocationSucceeded, AgentMaxStepsReached
from my_code.features.subagents.models import BackgroundSubagent
from my_code.foundation.json import JsonObject


def background_subagent_payload(item: BackgroundSubagent) -> JsonObject:
    task = item.task
    payload: JsonObject = {
        "task_id": task.task_id,
        "run_id": item.run_id,
        "description": item.description,
        "agent_type": item.agent_type.value,
        "status": task.status.value,
    }
    if task.failure is not None:
        payload["error_kind"] = task.failure.kind
        payload["error"] = task.failure.message
    if isinstance(task.result, AgentInvocationSucceeded):
        payload["result"] = task.result.text
        payload["completed_steps"] = task.result.completed_steps
    elif isinstance(task.result, AgentMaxStepsReached):
        payload["result_status"] = "max_steps"
        payload["completed_steps"] = task.result.completed_steps
        payload["max_steps"] = task.result.max_steps
    return payload


__all__ = ["background_subagent_payload"]
