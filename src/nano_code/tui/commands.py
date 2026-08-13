"""Declarative slash-command registry shared by all terminal frontends."""

from dataclasses import dataclass
from enum import StrEnum
from shlex import split as shell_split

from nano_code.tui.contracts import RuntimeStatus


class SlashCommandAction(StrEnum):
    HELP = "help"
    STATUS = "status"
    AUTH = "auth"
    PROVIDER = "provider"
    CLEAR = "clear"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    description: str
    action: SlashCommandAction
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    message: str = ""
    should_exit: bool = False
    clear_screen: bool = False
    open_provider_manager: bool = False


class SlashCommandRegistry:
    """Resolve local commands before ordinary prompts reach the model."""

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
                    "auth", "Show authentication status", SlashCommandAction.AUTH
                ),
                SlashCommand(
                    "provider",
                    "Configure API provider, URL, model, and key",
                    SlashCommandAction.PROVIDER,
                ),
                SlashCommand(
                    "clear", "Clear the terminal screen", SlashCommandAction.CLEAR
                ),
                SlashCommand(
                    "exit",
                    "Exit nano-code",
                    SlashCommandAction.EXIT,
                    aliases=("quit",),
                ),
            )
        )

    def dispatch(self, line: str, *, status: RuntimeStatus) -> CommandOutcome | None:
        """Return ``None`` for model input and an outcome for slash input."""

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
        if len(parts) > 1:
            return CommandOutcome(f"/{command.name} does not accept arguments yet.")

        match command.action:
            case SlashCommandAction.HELP:
                return CommandOutcome(self.render_help())
            case SlashCommandAction.STATUS:
                return CommandOutcome(_render_status(status))
            case SlashCommandAction.AUTH:
                return CommandOutcome(_render_auth(status))
            case SlashCommandAction.PROVIDER:
                return CommandOutcome(open_provider_manager=True)
            case SlashCommandAction.CLEAR:
                return CommandOutcome(clear_screen=True)
            case SlashCommandAction.EXIT:
                return CommandOutcome("Goodbye.", should_exit=True)

    def render_help(self) -> str:
        width = max(len(command.name) for command in self.commands)
        lines = ["Available commands:"]
        lines.extend(
            f"  /{command.name:<{width}}  {command.description}"
            for command in self.commands
        )
        return "\n".join(lines)

    def matching(self, text: str) -> tuple[SlashCommand, ...]:
        """Return prefix matches for a slash token, preserving registry order."""

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


def _render_status(status: RuntimeStatus) -> str:
    return "\n".join(
        (
            f"Session: {status.session_id}",
            f"Workspace: {status.cwd}",
            f"Provider: {status.provider_id}",
            f"Model: {status.model}",
            f"Permission mode: {status.permission_mode}",
            f"Authentication: {status.credential_source}",
            f"Messages: {status.message_count}",
        )
    )


def _render_auth(status: RuntimeStatus) -> str:
    if status.credential_source == "none":
        return "Not logged in. Run `nano-code auth login` outside the TUI."
    return (
        f"Authentication source: {status.credential_source}. "
        "Use `nano-code auth status` for details."
    )
