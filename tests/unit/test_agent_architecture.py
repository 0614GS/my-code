import ast
from pathlib import Path

import nano_code.context as context_adapter
import nano_code.providers as provider_adapter
import nano_code.sessions as session_adapter
import nano_code.tools as tool_adapter
from nano_code.agent import AgentEngine, AgentInboundPort
from nano_code.agent.ports import Compactor

_AGENT_ROOT = Path(__file__).parents[2] / "src" / "nano_code" / "agent"
_ADAPTER_PREFIXES = (
    "nano_code.context",
    "nano_code.providers",
    "nano_code.sessions",
    "nano_code.tools",
)


def test_agent_ports_are_the_single_authoritative_declarations() -> None:
    assert not hasattr(context_adapter, "ContextPort")
    assert not hasattr(context_adapter, "ContextPlan")
    assert not hasattr(provider_adapter, "ModelCompletionPort")
    assert not hasattr(provider_adapter, "ModelResponseCompleted")
    assert not hasattr(session_adapter, "SessionRepository")
    assert not hasattr(session_adapter, "ConversationState")
    assert not hasattr(tool_adapter, "ToolInteractionPort")
    assert not hasattr(tool_adapter, "ToolRoundExecutor")
    assert AgentInboundPort in AgentEngine.__mro__
    assert Compactor.__module__ == "nano_code.agent.ports"


def test_agent_core_does_not_import_concrete_adapters() -> None:
    for source_path in _AGENT_ROOT.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = (node.module,)
            else:
                continue
            assert not any(name.startswith(_ADAPTER_PREFIXES) for name in imported), (
                source_path
            )
