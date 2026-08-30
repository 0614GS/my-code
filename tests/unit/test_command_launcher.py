import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import my_code.workspace.launcher as launcher_module
from my_code.workspace.launcher import (
    BubblewrapSandboxLauncher,
    CommandAuthority,
    CommandBackend,
    CommandLaunchRequest,
    LocalCommandLauncher,
    MetadataPlaceholderRegistry,
    SandboxPolicyViolation,
    resolve_command_launcher,
)


def _request(workspace: Path, command: str = "printf ok") -> CommandLaunchRequest:
    return CommandLaunchRequest(
        command,
        workspace,
        {"PATH": "/usr/bin:/bin"},
        asyncio.subprocess.PIPE,
        asyncio.subprocess.STDOUT,
    )


def test_bubblewrap_argv_applies_mounts_namespaces_and_command_boundary(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".my-code").mkdir()
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    launcher = BubblewrapSandboxLauncher(workspace, Path("/usr/bin/bwrap"))

    argv = launcher.argv(_request(workspace, "printf '%s' '$HOME'"), private_tmp)

    assert argv[:7] == (
        "/usr/bin/bwrap",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(workspace),
        str(workspace),
    )
    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv
    assert "--unshare-ipc" in argv
    assert "--unshare-net" in argv
    assert ("--cap-drop", "ALL") == argv[
        argv.index("--cap-drop") : argv.index("--cap-drop") + 2
    ]
    assert argv[-4:] == ("--", "/bin/bash", "-c", "printf '%s' '$HOME'")
    for protected in (workspace / ".git", workspace / ".my-code"):
        index = argv.index(str(protected))
        assert argv[index - 1] == "--ro-bind"
        assert argv[index + 1] == str(protected)


def test_network_enabled_omits_network_namespace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".my-code").mkdir()
    private_tmp = tmp_path / "tmp"
    private_tmp.mkdir()

    argv = BubblewrapSandboxLauncher(
        workspace, Path("/usr/bin/bwrap"), network_enabled=True
    ).argv(_request(workspace), private_tmp)

    assert "--unshare-net" not in argv


def test_existing_git_file_is_a_supported_read_only_mount(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    (workspace / ".my-code").mkdir()
    registry = MetadataPlaceholderRegistry(workspace, tmp_path / "runtime")

    token, mounts = registry.acquire()
    registry.release(token)

    assert {mount.path.name for mount in mounts} == {".git", ".my-code"}
    assert (workspace / ".git").is_file()
    assert (workspace / ".my-code").is_dir()


def test_placeholder_leases_are_reference_counted_and_cleaned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = MetadataPlaceholderRegistry(workspace, tmp_path / "runtime")

    first, _ = registry.acquire()
    second, _ = registry.acquire()
    registry.release(first)
    assert (workspace / ".git").is_dir()
    assert (workspace / ".my-code").is_dir()

    registry.release(second)
    assert not (workspace / ".git").exists()
    assert not (workspace / ".my-code").exists()


def test_nonempty_placeholder_is_preserved_and_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = MetadataPlaceholderRegistry(workspace, tmp_path / "runtime")
    token, _ = registry.acquire()
    marker = workspace / ".git" / "external"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(SandboxPolicyViolation, match="not empty"):
        registry.release(token)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_stale_empty_placeholder_is_reaped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = MetadataPlaceholderRegistry(workspace, tmp_path / "runtime")
    token, _ = registry.acquire()
    raw = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    for entry in raw["entries"].values():
        entry["holders"] = [{"pid": 2**30, "token": token}]
    registry.registry_path.write_text(json.dumps(raw), encoding="utf-8")

    replacement, _ = registry.acquire()
    registry.release(replacement)

    assert not (workspace / ".git").exists()
    assert not (workspace / ".my-code").exists()


def test_corrupt_registry_is_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = MetadataPlaceholderRegistry(workspace, tmp_path / "runtime")
    registry.root.mkdir(parents=True)
    registry.registry_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(SandboxPolicyViolation, match="cannot safely read"):
        registry.acquire()


def test_auto_probe_success_freezes_bubblewrap_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bwrap = tmp_path / "bin" / "bwrap"
    bwrap.parent.mkdir()
    bwrap.write_text("", encoding="utf-8")
    bwrap.chmod(0o700)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(
        launcher_module.shutil, "which", lambda *_args, **_kwargs: str(bwrap)
    )
    monkeypatch.setattr(launcher_module, "_probe", lambda *_args, **_kwargs: None)

    launcher = resolve_command_launcher(workspace)

    assert launcher.status.backend is CommandBackend.BUBBLEWRAP
    assert launcher.status.sandboxed is True


def test_workspace_bwrap_is_rejected_without_running_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bwrap = workspace / "bwrap"
    bwrap.write_text("", encoding="utf-8")
    bwrap.chmod(0o700)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(
        launcher_module.shutil, "which", lambda *_args, **_kwargs: str(bwrap)
    )

    launcher = resolve_command_launcher(workspace)

    assert isinstance(launcher, LocalCommandLauncher)
    assert launcher.status.fallback_reason == "bwrap resolved inside the workspace"


def test_non_linux_and_missing_bwrap_are_explicit_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module.sys, "platform", "darwin")
    unsupported = resolve_command_launcher(tmp_path)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(launcher_module.shutil, "which", lambda *_args, **_kwargs: None)
    missing = resolve_command_launcher(tmp_path)

    assert unsupported.status.fallback_reason == "unsupported platform darwin"
    assert missing.status.fallback_reason == "bwrap was not found on PATH"


def test_probe_timeout_falls_back_but_policy_violation_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("", encoding="utf-8")
    bwrap.chmod(0o700)
    monkeypatch.setattr(launcher_module.sys, "platform", "linux")
    monkeypatch.setattr(
        launcher_module.shutil, "which", lambda *_args, **_kwargs: str(bwrap)
    )
    monkeypatch.setattr(
        launcher_module,
        "_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("bwrap", 1)
        ),
    )

    timeout = resolve_command_launcher(workspace)

    assert timeout.status.fallback_reason is not None
    assert "timed out" in timeout.status.fallback_reason

    monkeypatch.setattr(
        launcher_module,
        "_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SandboxPolicyViolation("metadata changed")
        ),
    )
    with pytest.raises(SandboxPolicyViolation, match="metadata changed"):
        resolve_command_launcher(workspace)


@pytest.mark.asyncio
async def test_sandbox_launch_failure_does_not_retry_locally(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = BubblewrapSandboxLauncher(workspace, tmp_path / "missing-bwrap")

    with pytest.raises(FileNotFoundError):
        async with launcher.launch(_request(workspace)):
            pytest.fail("unreachable")

    assert not (workspace / ".git").exists()
    assert not (workspace / ".my-code").exists()


@pytest.mark.asyncio
async def test_approved_escalation_delegates_to_host_bash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = BubblewrapSandboxLauncher(workspace, tmp_path / "missing-bwrap")
    request = CommandLaunchRequest(
        "printf elevated",
        workspace,
        {"PATH": "/usr/bin:/bin"},
        asyncio.subprocess.PIPE,
        asyncio.subprocess.STDOUT,
        CommandAuthority.REQUIRE_ESCALATED,
    )

    async with launcher.launch(request) as process:
        output, _ = await process.communicate()

    assert process.returncode == 0
    assert output == b"elevated"


@pytest.mark.asyncio
async def test_local_launcher_rejects_forged_escalation(tmp_path: Path) -> None:
    request = CommandLaunchRequest(
        "true",
        tmp_path,
        {"PATH": "/usr/bin:/bin"},
        asyncio.subprocess.PIPE,
        asyncio.subprocess.STDOUT,
        CommandAuthority.REQUIRE_ESCALATED,
    )

    with pytest.raises(SandboxPolicyViolation, match="unavailable"):
        async with LocalCommandLauncher().launch(request):
            pytest.fail("unreachable")


def test_explicit_local_never_probes(tmp_path: Path) -> None:
    launcher = resolve_command_launcher(tmp_path, mode="local")

    assert isinstance(launcher, LocalCommandLauncher)
    assert launcher.status.fallback_reason is None
    assert launcher.status.sandboxed is False
