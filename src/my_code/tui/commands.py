"""所有终端前端共用的声明式 slash 命令注册表。"""

from dataclasses import dataclass
from enum import StrEnum
from shlex import split as shell_split

from my_code.application.contracts.status import RuntimeStatus


class SlashCommandAction(StrEnum):
    HELP = "help"
    STATUS = "status"
    CONTEXT = "context"
    COMPACT = "compact"
    PROVIDER = "provider"
    MODEL = "model"
    RESUME = "resume"
    CLEAR = "clear"
    EXIT = "exit"
    USAGE = "usage"
    TOOLS = "tools"
    SKILLS = "skills"
    MCP = "mcp"
    TASKS = "tasks"
    AGENTS = "agents"
    VIEW = "view"
    PERMISSIONS = "permissions"


class CommandConcurrency(StrEnum):
    CONCURRENT_READ = "concurrent_read"
    CONCURRENT_UI = "concurrent_ui"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    description: str
    action: SlashCommandAction
    aliases: tuple[str, ...] = ()
    concurrency: CommandConcurrency = CommandConcurrency.CONCURRENT_READ


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    message: str = ""
    show_status: bool = False
    should_exit: bool = False
    clear_screen: bool = False
    open_provider_manager: bool = False
    open_model_picker: bool = False
    open_session_picker: bool = False
    show_context: bool = False
    compact_context: bool = False
    show_usage: bool = False
    show_tools: bool = False
    skill_operation: str | None = None
    mcp_operation: tuple[str, str] | None = None
    show_tasks: bool = False
    show_agents: bool = False
    open_view_picker: bool = False
    open_permission_picker: bool = False
    view_operation: str | None = None


class SlashCommandRegistry:
    """在普通提示到达模型前解析本地命令。"""

    def __init__(self, commands: tuple[SlashCommand, ...]) -> None:
        self.commands = commands
        self._lookup: dict[str, SlashCommand] = {}
        for command in commands:
            for name in (command.name, *command.aliases):
                normalized = name.casefold()
                if normalized in self._lookup:
                    raise ValueError(f"Duplicate slash command name: {name}")
                self._lookup[normalized] = command

    @classmethod
    def default(cls) -> "SlashCommandRegistry":
        return cls(
            (
                SlashCommand(
                    "help", "Show available commands", SlashCommandAction.HELP
                ),
                SlashCommand(
                    "status",
                    "Show session and runtime status",
                    SlashCommandAction.STATUS,
                ),
                SlashCommand(
                    "context",
                    "Show context budget and compaction state",
                    SlashCommandAction.CONTEXT,
                ),
                SlashCommand(
                    "compact",
                    "Compact the current conversation",
                    SlashCommandAction.COMPACT,
                    concurrency=CommandConcurrency.EXCLUSIVE,
                ),
                SlashCommand(
                    "usage", "Show session token usage", SlashCommandAction.USAGE
                ),
                SlashCommand(
                    "tools", "List active tools and sources", SlashCommandAction.TOOLS
                ),
                SlashCommand(
                    "skills", "List or reload skills", SlashCommandAction.SKILLS
                ),
                SlashCommand(
                    "mcp", "Show or refresh MCP servers", SlashCommandAction.MCP
                ),
                SlashCommand(
                    "tasks",
                    "List Bash and Subagent tasks",
                    SlashCommandAction.TASKS,
                ),
                SlashCommand(
                    "agents", "Open the live Subagent viewer", SlashCommandAction.AGENTS
                ),
                SlashCommand(
                    "view",
                    "Show or set concise/detailed output",
                    SlashCommandAction.VIEW,
                    concurrency=CommandConcurrency.CONCURRENT_UI,
                ),
                SlashCommand(
                    "permissions",
                    "Choose the tool permission mode",
                    SlashCommandAction.PERMISSIONS,
                    concurrency=CommandConcurrency.CONCURRENT_UI,
                ),
                SlashCommand(
                    "provider",
                    "Configure API provider, URL, model, and key",
                    SlashCommandAction.PROVIDER,
                    concurrency=CommandConcurrency.EXCLUSIVE,
                ),
                SlashCommand(
                    "model",
                    "Switch models from the current provider catalog",
                    SlashCommandAction.MODEL,
                    concurrency=CommandConcurrency.EXCLUSIVE,
                ),
                SlashCommand(
                    "resume",
                    "Resume a previous conversation",
                    SlashCommandAction.RESUME,
                    aliases=("continue",),
                    concurrency=CommandConcurrency.EXCLUSIVE,
                ),
                SlashCommand(
                    "clear", "Clear the terminal screen", SlashCommandAction.CLEAR
                ),
                SlashCommand(
                    "exit",
                    "Exit my-code",
                    SlashCommandAction.EXIT,
                    aliases=("quit",),
                    concurrency=CommandConcurrency.EXCLUSIVE,
                ),
            )
        )

    def dispatch(self, line: str, *, status: RuntimeStatus) -> CommandOutcome | None:
        """模型输入返回 ``None``，slash 输入返回本地执行结果。"""

        stripped = line.strip()
        if not stripped.startswith("/"):
            return None
        try:
            parts = shell_split(stripped[1:])
        except ValueError as error:
            return CommandOutcome(f"Invalid command syntax: {error}")
        if not parts:
            return CommandOutcome(self.render_help())
        command = self._lookup.get(parts[0].casefold())
        if command is None:
            return CommandOutcome(
                f"Unknown command: /{parts[0]}. Type /help to list commands."
            )
        arguments = parts[1:]
        if command.action is SlashCommandAction.SKILLS:
            if not arguments:
                return CommandOutcome(skill_operation="list")
            if len(arguments) == 1 and arguments[0].casefold() == "reload":
                return CommandOutcome(skill_operation="reload")
            return CommandOutcome("Usage: /skills [reload]")
        if command.action is SlashCommandAction.MCP:
            if not arguments:
                return CommandOutcome(mcp_operation=("list", ""))
            operation = arguments[0].casefold() if arguments else ""
            if len(arguments) == 2 and operation in {"refresh", "reconnect"}:
                return CommandOutcome(mcp_operation=(operation, arguments[1]))
            return CommandOutcome("Usage: /mcp [refresh|reconnect <server>]")
        if command.action is SlashCommandAction.VIEW:
            if not arguments:
                return CommandOutcome(open_view_picker=True)
            if len(arguments) == 1 and arguments[0].casefold() in {
                "concise",
                "detailed",
            }:
                return CommandOutcome(view_operation=arguments[0].casefold())
            return CommandOutcome("Usage: /view [concise|detailed]")
        if arguments:
            return CommandOutcome(f"/{command.name} does not accept arguments.")

        match command.action:
            case SlashCommandAction.HELP:
                return CommandOutcome(self.render_help())
            case SlashCommandAction.STATUS:
                return CommandOutcome(show_status=True)
            case SlashCommandAction.CONTEXT:
                return CommandOutcome(show_context=True)
            case SlashCommandAction.COMPACT:
                return CommandOutcome(compact_context=True)
            case SlashCommandAction.USAGE:
                return CommandOutcome(show_usage=True)
            case SlashCommandAction.TOOLS:
                return CommandOutcome(show_tools=True)
            case SlashCommandAction.TASKS:
                return CommandOutcome(show_tasks=True)
            case SlashCommandAction.AGENTS:
                return CommandOutcome(show_agents=True)
            case SlashCommandAction.PERMISSIONS:
                return CommandOutcome(open_permission_picker=True)
            case SlashCommandAction.PROVIDER:
                return CommandOutcome(open_provider_manager=True)
            case SlashCommandAction.MODEL:
                return CommandOutcome(open_model_picker=True)
            case SlashCommandAction.RESUME:
                return CommandOutcome(open_session_picker=True)
            case SlashCommandAction.CLEAR:
                return CommandOutcome(clear_screen=True)
            case SlashCommandAction.EXIT:
                return CommandOutcome("Goodbye.", should_exit=True)

    def concurrency(self, line: str) -> CommandConcurrency | None:
        """Return the declaration for a syntactically recognizable command."""

        stripped = line.strip()
        if not stripped.startswith("/"):
            return None
        try:
            parts = shell_split(stripped[1:])
        except ValueError:
            return CommandConcurrency.CONCURRENT_READ
        if not parts:
            return CommandConcurrency.CONCURRENT_READ
        command = self._lookup.get(parts[0].casefold())
        if command is None:
            return CommandConcurrency.CONCURRENT_READ
        if command.action is SlashCommandAction.SKILLS and any(
            item.casefold() == "reload" for item in parts[1:]
        ):
            return CommandConcurrency.EXCLUSIVE
        if command.action is SlashCommandAction.MCP and len(parts) > 1:
            return CommandConcurrency.EXCLUSIVE
        return command.concurrency

    def render_help(self) -> str:
        width = max(len(command.name) for command in self.commands)
        lines = ["Available commands:"]
        lines.extend(
            f"  /{command.name:<{width}}  {command.description}"
            for command in self.commands
        )
        return "\n".join(lines)

    def matching(self, text: str) -> tuple[SlashCommand, ...]:
        """按注册表顺序返回 slash token 的前缀匹配项。"""

        if not text.startswith("/") or any(character.isspace() for character in text):
            return ()
        query = text[1:].casefold()
        return tuple(
            command
            for command in self.commands
            if not query
            or any(
                name.casefold().startswith(query)
                for name in (command.name, *command.aliases)
            )
        )


__all__ = [
    "CommandConcurrency",
    "SlashCommandRegistry",
]
