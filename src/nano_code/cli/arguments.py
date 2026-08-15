"""无副作用的命令行参数解析。"""

import argparse
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nano_code import __version__
from nano_code.auth import CredentialStore, resolve_api_key
from nano_code.config import NanoCodePaths, Settings, SettingsScope, SettingsStore
from nano_code.permissions import PermissionMode
from nano_code.providers.profiles import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_ID,
    ProviderProfile,
    ProviderProfileStore,
)

_DEFAULT_MAX_TURNS = 12
_DEFAULT_MAX_OUTPUT_TOKENS = 8192
_DEFAULT_CONTEXT_CHARS = 160_000


@dataclass(frozen=True, slots=True)
class CliOptions:
    settings: Settings
    prompt: str | None
    session_id: str | None


class AuthAction(StrEnum):
    LOGIN = "login"
    STATUS = "status"
    LOGOUT = "logout"


@dataclass(frozen=True, slots=True)
class AuthOptions:
    action: AuthAction
    paths: NanoCodePaths
    provider_id: str


type ParsedOptions = CliOptions | AuthOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nanocode",
        description="A small coding agent for learning Claude Code's architecture.",
    )
    parser.add_argument("-p", "--print", dest="prompt", help="run one prompt and exit")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="workspace root")
    parser.add_argument("--model", help="model override")
    parser.add_argument("--provider", help="named provider profile override")
    parser.add_argument(
        "--base-url",
        help="Anthropic-compatible API base URL override",
    )
    parser.add_argument(
        "--permission-mode",
        choices=[mode.value for mode in PermissionMode],
        help="permission mode override",
    )
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--context-chars", type=int)
    parser.add_argument("--session", dest="session_id", help="resume a session ID")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    auth_parser = subparsers.add_parser(
        "auth", help="manage the user-level Anthropic API key"
    )
    auth_commands = auth_parser.add_subparsers(dest="auth_action", required=True)
    auth_commands.add_parser("login", help="store an API key for future sessions")
    auth_commands.add_parser("status", help="show the active credential source")
    auth_commands.add_parser("logout", help="remove the stored API key")
    return parser


def parse_args(argv: list[str] | None = None) -> CliOptions:
    """解析普通对话调用；作为职责明确的公共辅助函数保留。"""

    namespace = build_parser().parse_args(argv)
    if namespace.command is not None:
        raise ValueError("An auth command cannot be parsed as a chat invocation")
    return _parse_chat_options(namespace)


def parse_cli(argv: list[str] | None = None) -> ParsedOptions:
    """解析对话调用或顶层管理命令。"""

    namespace = build_parser().parse_args(argv)
    if namespace.command == "auth":
        paths = NanoCodePaths.discover(namespace.cwd.resolve())
        user_settings = SettingsStore(paths).load_scope(SettingsScope.USER)
        provider_id = (
            namespace.provider
            or os.getenv("NANO_CODE_PROVIDER")
            or user_settings.active_provider
            or DEFAULT_PROVIDER_ID
        )
        return AuthOptions(
            action=AuthAction(namespace.auth_action),
            paths=paths,
            provider_id=provider_id,
        )
    return _parse_chat_options(namespace)


def _parse_chat_options(namespace: argparse.Namespace) -> CliOptions:
    cwd = namespace.cwd.resolve()
    if not cwd.is_dir():
        raise ValueError(f"Workspace is not a directory: {cwd}")
    paths = NanoCodePaths.discover(cwd)
    settings_store = SettingsStore(paths)
    stored = settings_store.load()
    user_settings = settings_store.load_scope(SettingsScope.USER)
    project_overrides = settings_store.load_scope(SettingsScope.PROJECT).overlay(
        settings_store.load_scope(SettingsScope.LOCAL)
    )
    provider_id = (
        namespace.provider
        or os.getenv("NANO_CODE_PROVIDER")
        or user_settings.active_provider
        or DEFAULT_PROVIDER_ID
    )
    profiles = ProviderProfileStore(paths.providers_path).load()
    if not profiles:
        profiles = {
            DEFAULT_PROVIDER_ID: ProviderProfile(
                id=DEFAULT_PROVIDER_ID,
                model=user_settings.model or DEFAULT_MODEL,
                base_url=user_settings.base_url,
            )
        }
    try:
        profile = profiles[provider_id]
    except KeyError as error:
        choices = ", ".join(sorted(profiles)) or "<none>"
        raise ValueError(
            f"Unknown provider {provider_id!r}; configured providers: {choices}"
        ) from error

    # 等所有文件层加载完成后再解析，以区分显式 CLI 参数和解析器默认值。
    # 凭据刻意使用独立的用户专属存储，使 settings.json 可以安全共享。
    credential = resolve_api_key(
        CredentialStore(paths.credentials_path), provider_id=provider_id
    )
    env_model = os.getenv("ANTHROPIC_MODEL")
    env_base_url = os.getenv("ANTHROPIC_BASE_URL")
    model = (
        namespace.model
        if namespace.model is not None
        else env_model or project_overrides.model or profile.model
    )
    permission_mode = (
        PermissionMode(namespace.permission_mode)
        if namespace.permission_mode is not None
        else stored.permission_mode or PermissionMode.DEFAULT
    )
    prompt: str | None = namespace.prompt
    interactive = prompt is None and sys.stdin.isatty() and sys.stdout.isatty()
    max_turns = (
        namespace.max_turns
        if namespace.max_turns is not None
        else stored.max_turns or _DEFAULT_MAX_TURNS
    )
    max_output_tokens = (
        namespace.max_output_tokens
        if namespace.max_output_tokens is not None
        else stored.max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS
    )
    context_chars = (
        namespace.context_chars
        if namespace.context_chars is not None
        else stored.context_chars or _DEFAULT_CONTEXT_CHARS
    )
    settings = Settings(
        paths=paths,
        provider_id=provider_id,
        model=model,
        permission_mode=permission_mode,
        max_turns=max_turns,
        max_output_tokens=max_output_tokens,
        context_chars=context_chars,
        interactive=interactive,
        api_key=credential.api_key,
        credential_source=credential.source,
        base_url=(
            namespace.base_url
            if namespace.base_url is not None
            else env_base_url or profile.base_url
        ),
    )
    return CliOptions(settings=settings, prompt=prompt, session_id=namespace.session_id)
