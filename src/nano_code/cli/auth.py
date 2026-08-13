"""用于精简用户级凭据生命周期的控制台处理逻辑。"""

import getpass
import os
from collections.abc import Callable

from nano_code.auth import CredentialSource, CredentialStore, resolve_api_key
from nano_code.cli.arguments import AuthAction, AuthOptions


def run_auth_command(
    options: AuthOptions,
    *,
    secret_input: Callable[[str], str] = getpass.getpass,
) -> int:
    store = CredentialStore(options.paths.credentials_path)
    match options.action:
        case AuthAction.LOGIN:
            api_key = secret_input("Anthropic API key: ")
            store.save_api_key(api_key, options.provider_id)
            print(
                f"API key for provider {options.provider_id!r} saved to "
                f"{store.path} (mode 0600)."
            )
            if os.getenv("NANO_CODE_API_KEY") or os.getenv("ANTHROPIC_API_KEY"):
                print("Note: an environment API key is set and takes precedence.")
        case AuthAction.STATUS:
            credential = resolve_api_key(store, provider_id=options.provider_id)
            if credential.source is CredentialSource.NONE:
                print(
                    f"Provider {options.provider_id!r} has no API key. "
                    "Run `nano-code auth login`."
                )
                return 1
            print(
                f"Provider {options.provider_id!r} authenticated via "
                f"{credential.source.value}."
            )
            if credential.source is CredentialSource.STORED:
                print(f"Credential file: {store.path}")
            else:
                print("ANTHROPIC_API_KEY overrides any stored credential.")
        case AuthAction.LOGOUT:
            removed = store.delete(options.provider_id)
            print("Stored API key removed." if removed else "No stored API key found.")
            if os.getenv("NANO_CODE_API_KEY") or os.getenv("ANTHROPIC_API_KEY"):
                print("An environment API key remains active in this shell.")
    return 0
