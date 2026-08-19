"""用于精简用户级凭据生命周期的控制台处理逻辑。"""

import getpass
import os
from collections.abc import Callable

from my_code.auth.credentials import (
    CredentialSource,
    CredentialStore,
    resolve_api_key,
)
from my_code.cli.arguments import AuthAction, AuthOptions
from my_code.config.paths import MyCodePaths


def run_auth_command(
    options: AuthOptions,
    paths: MyCodePaths,
    provider_id: str,
    *,
    protocol: str = "anthropic-messages",
    secret_input: Callable[[str], str] = getpass.getpass,
) -> int:
    store = CredentialStore(paths.credentials_path)
    match options.action:
        case AuthAction.LOGIN:
            label = "OpenAI" if protocol == "openai-responses" else "Anthropic"
            api_key = secret_input(f"{label} API key: ")
            store.save_api_key(api_key, provider_id)
            print(
                f"API key for provider {provider_id!r} saved to "
                f"{store.path} (mode 0600)."
            )
            variable = (
                "OPENAI_API_KEY"
                if protocol == "openai-responses"
                else "ANTHROPIC_API_KEY"
            )
            if os.getenv("MY_CODE_API_KEY") or os.getenv(variable):
                print("Note: an environment API key is set and takes precedence.")
        case AuthAction.STATUS:
            credential = resolve_api_key(
                store, provider_id=provider_id, protocol=protocol
            )
            if credential.source is CredentialSource.NONE:
                print(
                    f"Provider {provider_id!r} has no API key. Run `mycode auth login`."
                )
                return 1
            print(
                f"Provider {provider_id!r} authenticated via {credential.source.value}."
            )
            if credential.source is CredentialSource.STORED:
                print(f"Credential file: {store.path}")
            else:
                print(f"The protocol-specific environment key overrides {store.path}.")
        case AuthAction.LOGOUT:
            removed = store.delete(provider_id)
            print("Stored API key removed." if removed else "No stored API key found.")
            variable = (
                "OPENAI_API_KEY"
                if protocol == "openai-responses"
                else "ANTHROPIC_API_KEY"
            )
            if os.getenv("MY_CODE_API_KEY") or os.getenv(variable):
                print("An environment API key remains active in this shell.")
    return 0


__all__ = [
    "run_auth_command",
]
