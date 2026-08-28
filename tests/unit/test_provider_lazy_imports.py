"""Provider SDKs stay off the process startup path until actually needed."""

import subprocess
import sys

import pytest


def test_bootstrap_router_and_discovery_do_not_import_provider_sdks() -> None:
    script = """
import sys
import my_code.bootstrap
import my_code.providers.discovery
import my_code.providers.router
assert 'openai' not in sys.modules
assert 'anthropic' not in sys.modules
"""

    subprocess.run([sys.executable, "-c", script], check=True)


@pytest.mark.parametrize(
    ("protocol", "provider_id", "model", "loaded", "absent"),
    (
        ("OPENAI_RESPONSES", "openai", "gpt-5", "openai", "anthropic"),
        (
            "ANTHROPIC_MESSAGES",
            "anthropic",
            "claude-test",
            "anthropic",
            "openai",
        ),
    ),
)
def test_first_provider_build_only_imports_its_own_sdk(
    protocol: str,
    provider_id: str,
    model: str,
    loaded: str,
    absent: str,
) -> None:
    script = """
import sys
from my_code.auth.credentials import CredentialSource
from my_code.config.providers import ProviderProtocol
from my_code.providers.router import ProviderConnection, _build_provider
connection = ProviderConnection(
    sys.argv[1], getattr(ProviderProtocol, sys.argv[2]), sys.argv[3], None, 'test-key',
    CredentialSource.ENVIRONMENT,
)
provider = _build_provider(connection)
assert sys.argv[4] in sys.modules
assert sys.argv[5] not in sys.modules
"""

    subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            provider_id,
            protocol,
            model,
            loaded,
            absent,
        ],
        check=True,
    )
