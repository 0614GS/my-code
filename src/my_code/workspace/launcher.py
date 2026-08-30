"""Command process launchers shared by every Bash execution path."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

BASH_EXECUTABLE = "/bin/bash"
PROTECTED_WORKSPACE_ENTRIES = (".git", ".my-code")


class CommandBackend(StrEnum):
    LOCAL = "local"
    BUBBLEWRAP = "bubblewrap"


class CommandAuthority(StrEnum):
    USE_DEFAULT = "use_default"
    REQUIRE_ESCALATED = "require_escalated"


@dataclass(frozen=True, slots=True)
class ExecutionEnvironmentStatus:
    backend: CommandBackend
    sandboxed: bool
    fallback_reason: str | None = None

    @property
    def display(self) -> str:
        if self.fallback_reason:
            return f"local (sandbox fallback: {self.fallback_reason})"
        return self.backend.value


@dataclass(frozen=True, slots=True)
class CommandLaunchRequest:
    command: str
    cwd: Path
    env: dict[str, str]
    stdout: Any
    stderr: Any
    authority: CommandAuthority = CommandAuthority.USE_DEFAULT


class CommandLauncher(Protocol):
    @property
    def status(self) -> ExecutionEnvironmentStatus: ...

    def launch(
        self, request: CommandLaunchRequest
    ) -> AbstractAsyncContextManager[asyncio.subprocess.Process]: ...


@dataclass(frozen=True, slots=True)
class LocalCommandLauncher:
    status: ExecutionEnvironmentStatus = ExecutionEnvironmentStatus(
        CommandBackend.LOCAL, False
    )

    @asynccontextmanager
    async def launch(
        self, request: CommandLaunchRequest
    ) -> AsyncIterator[asyncio.subprocess.Process]:
        if request.authority is CommandAuthority.REQUIRE_ESCALATED:
            raise SandboxPolicyViolation(
                "elevated authority is unavailable without an active sandbox"
            )
        async with self._launch_host(request) as process:
            yield process

    @asynccontextmanager
    async def _launch_host(
        self, request: CommandLaunchRequest
    ) -> AsyncIterator[asyncio.subprocess.Process]:
        process = await asyncio.create_subprocess_exec(
            BASH_EXECUTABLE,
            "-c",
            request.command,
            cwd=request.cwd,
            env=request.env,
            stdout=request.stdout,
            stderr=request.stderr,
            start_new_session=True,
        )
        yield process


class SandboxPolicyViolation(RuntimeError):
    """Protected workspace metadata changed while a sandbox lease was active."""


@dataclass(frozen=True, slots=True)
class _Placeholder:
    path: Path
    device: int
    inode: int


class MetadataPlaceholderRegistry:
    """Cross-process leases for synthetic protected mount points."""

    def __init__(self, workspace: Path, runtime_root: Path | None = None) -> None:
        digest = hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()[:24]
        uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
        self.base = runtime_root or (
            Path(tempfile.gettempdir()) / f"my-code-{uid}" / "sandbox"
        )
        self.root = self.base / digest
        self.lock_path = self.root / "registry.lock"
        self.registry_path = self.root / "registry.json"
        self.workspace = workspace.resolve()

    def acquire(self) -> tuple[str, tuple[_Placeholder, ...]]:
        _ensure_private_directory(self.base)
        _ensure_private_directory(self.root)
        token = uuid4().hex
        with self._locked() as registry:
            self._reap(registry)
            placeholders: list[_Placeholder] = []
            entries = registry.setdefault("entries", {})
            assert isinstance(entries, dict)
            for name in PROTECTED_WORKSPACE_ENTRIES:
                path = self.workspace / name
                if path.is_symlink():
                    raise SandboxPolicyViolation(
                        f"protected workspace path is a symbolic link: {path}"
                    )
                if path.exists():
                    info = path.stat(follow_symlinks=False)
                    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                        raise SandboxPolicyViolation(
                            "protected workspace path is not a file or directory: "
                            f"{path}"
                        )
                    placeholders.append(_Placeholder(path, info.st_dev, info.st_ino))
                    continue
                path.mkdir(mode=0o700)
                info = path.stat(follow_symlinks=False)
                entry = {
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "holders": [],
                }
                entries[name] = entry
                placeholders.append(_Placeholder(path, info.st_dev, info.st_ino))
            for name, raw in entries.items():
                if not isinstance(raw, dict):
                    continue
                path = self.workspace / name
                if path.is_symlink():
                    raise SandboxPolicyViolation(
                        f"protected workspace path became a symbolic link: {path}"
                    )
                info = path.stat(follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (
                    raw.get("device"),
                    raw.get("inode"),
                ):
                    raise SandboxPolicyViolation(
                        f"protected workspace placeholder identity changed: {path}"
                    )
                holders = raw.setdefault("holders", [])
                assert isinstance(holders, list)
                holders.append(
                    {
                        "pid": os.getpid(),
                        "process_start": _process_start_identity(os.getpid()),
                        "token": token,
                    }
                )
                if not any(item.path == path for item in placeholders):
                    placeholders.append(_Placeholder(path, info.st_dev, info.st_ino))
        return token, tuple(placeholders)

    def release(self, token: str) -> None:
        violations: list[str] = []
        with self._locked() as registry:
            entries = registry.setdefault("entries", {})
            assert isinstance(entries, dict)
            for name, raw in tuple(entries.items()):
                if not isinstance(raw, dict):
                    continue
                holders = raw.get("holders", [])
                raw["holders"] = [
                    holder
                    for holder in holders
                    if not isinstance(holder, dict) or holder.get("token") != token
                ]
                if raw["holders"]:
                    continue
                path = self.workspace / name
                violation = self._remove_verified(path, raw)
                if violation:
                    violations.append(violation)
                entries.pop(name, None)
        if violations:
            raise SandboxPolicyViolation("; ".join(violations))

    def _reap(self, registry: dict[str, object]) -> None:
        entries = registry.setdefault("entries", {})
        assert isinstance(entries, dict)
        for name, raw in tuple(entries.items()):
            if not isinstance(raw, dict):
                entries.pop(name, None)
                continue
            holders = raw.get("holders", [])
            live = [
                holder
                for holder in holders
                if isinstance(holder, dict) and _holder_is_alive(holder)
            ]
            raw["holders"] = live
            if not live:
                violation = self._remove_verified(self.workspace / name, raw)
                entries.pop(name, None)
                if violation is not None:
                    raise SandboxPolicyViolation(violation)

    @staticmethod
    def _remove_verified(path: Path, raw: dict[str, object]) -> str | None:
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return f"protected placeholder disappeared: {path}"
        if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (
            raw.get("device"),
            raw.get("inode"),
        ):
            return f"protected placeholder identity changed: {path}"
        try:
            path.rmdir()
        except OSError:
            return f"protected placeholder is not empty: {path}"
        return None

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[tuple[_Placeholder, ...]]:
        token, placeholders = self.acquire()
        try:
            yield placeholders
        finally:
            self.release(token)

    class _Lock:
        def __init__(self, owner: MetadataPlaceholderRegistry) -> None:
            self.owner = owner
            self.fd: int | None = None
            self.registry: dict[str, object] = {}

        def __enter__(self) -> dict[str, object]:
            self.fd = os.open(self.owner.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            try:
                raw = json.loads(self.owner.registry_path.read_text())
                if isinstance(raw, dict):
                    self.registry = raw
                else:
                    raise SandboxPolicyViolation(
                        f"sandbox registry is not an object: {self.owner.registry_path}"
                    )
            except FileNotFoundError:
                self.registry = {}
            except SandboxPolicyViolation:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
                self.fd = None
                raise
            except (OSError, json.JSONDecodeError) as error:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
                self.fd = None
                raise SandboxPolicyViolation(
                    f"cannot safely read sandbox registry: {self.owner.registry_path}"
                ) from error
            return self.registry

        def __exit__(self, *_args: object) -> None:
            assert self.fd is not None
            temp = self.owner.registry_path.with_suffix(f".{os.getpid()}.tmp")
            temp.write_text(json.dumps(self.registry, sort_keys=True), encoding="utf-8")
            os.replace(temp, self.owner.registry_path)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)

    def _locked(self) -> _Lock:
        return self._Lock(self)


@dataclass(frozen=True, slots=True)
class BubblewrapSandboxLauncher:
    workspace: Path
    bwrap_path: Path
    network_enabled: bool = False
    runtime_root: Path | None = None
    status: ExecutionEnvironmentStatus = ExecutionEnvironmentStatus(
        CommandBackend.BUBBLEWRAP, True
    )
    host_launcher: LocalCommandLauncher = LocalCommandLauncher()

    @asynccontextmanager
    async def launch(
        self, request: CommandLaunchRequest
    ) -> AsyncIterator[asyncio.subprocess.Process]:
        if request.authority is CommandAuthority.REQUIRE_ESCALATED:
            async with self.host_launcher._launch_host(request) as process:
                yield process
            return
        registry = MetadataPlaceholderRegistry(self.workspace, self.runtime_root)
        async with registry.lease() as placeholders:
            with tempfile.TemporaryDirectory(prefix="my-code-command-") as raw_tmp:
                private_tmp = Path(raw_tmp)
                environment = dict(request.env)
                environment["TMPDIR"] = str(private_tmp)
                argv = self.argv(request, private_tmp, placeholders)
                _verify_mounts(placeholders)
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=request.cwd,
                    env=environment,
                    stdout=request.stdout,
                    stderr=request.stderr,
                    start_new_session=True,
                )
                try:
                    _verify_mounts(placeholders)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    raise
                yield process

    def argv(
        self,
        request: CommandLaunchRequest,
        private_tmp: Path,
        placeholders: tuple[_Placeholder, ...] = (),
    ) -> tuple[str, ...]:
        _verify_mounts(placeholders)
        workspace = self.workspace.resolve()
        args = [
            str(self.bwrap_path),
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(workspace),
            str(workspace),
        ]
        for name in PROTECTED_WORKSPACE_ENTRIES:
            protected = workspace / name
            if protected.is_symlink():
                raise SandboxPolicyViolation(
                    f"protected workspace path is a symbolic link: {protected}"
                )
            args.extend(("--ro-bind", str(protected), str(protected)))
        args.extend(("--bind", str(private_tmp), str(private_tmp)))
        args.extend(("--unshare-user", "--unshare-pid", "--unshare-ipc"))
        if not self.network_enabled:
            args.append("--unshare-net")
        args.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--cap-drop",
                "ALL",
                "--new-session",
                "--die-with-parent",
                "--chdir",
                str(request.cwd),
                "--",
                BASH_EXECUTABLE,
                "-c",
                request.command,
            )
        )
        return tuple(args)


def resolve_command_launcher(
    workspace: Path,
    *,
    mode: str = "auto",
    network_enabled: bool = False,
    environ: dict[str, str] | None = None,
    probe_timeout_seconds: float = 3.0,
    runtime_root: Path | None = None,
) -> CommandLauncher:
    """Probe once and freeze the command backend for this application lifetime."""

    if mode == "local":
        return LocalCommandLauncher()
    reason: str | None = None
    if not sys.platform.startswith("linux"):
        reason = f"unsupported platform {sys.platform}"
    else:
        candidate = shutil.which("bwrap", path=(environ or os.environ).get("PATH"))
        if candidate is None:
            reason = "bwrap was not found on PATH"
        else:
            bwrap = Path(candidate).resolve()
            resolved_workspace = workspace.resolve()
            if bwrap.is_relative_to(resolved_workspace):
                reason = "bwrap resolved inside the workspace"
            elif not bwrap.is_file() or not os.access(bwrap, os.X_OK):
                reason = "bwrap is not an executable file"
            else:
                launcher = BubblewrapSandboxLauncher(
                    resolved_workspace, bwrap, network_enabled, runtime_root
                )
                try:
                    _probe(launcher, environ or dict(os.environ), probe_timeout_seconds)
                except (OSError, subprocess.SubprocessError) as error:
                    reason = f"bubblewrap probe failed: {error}"
                else:
                    return launcher
    status = ExecutionEnvironmentStatus(CommandBackend.LOCAL, False, reason)
    return LocalCommandLauncher(status)


def _probe(
    launcher: BubblewrapSandboxLauncher,
    environ: dict[str, str],
    timeout_seconds: float,
) -> None:
    registry = MetadataPlaceholderRegistry(launcher.workspace, launcher.runtime_root)
    token, placeholders = registry.acquire()
    try:
        with tempfile.TemporaryDirectory(prefix="my-code-probe-") as raw_tmp:
            request = CommandLaunchRequest(
                "true",
                launcher.workspace,
                dict(environ),
                subprocess.DEVNULL,
                subprocess.STDOUT,
            )
            private_tmp = Path(raw_tmp).resolve()
            environment = dict(environ)
            environment["TMPDIR"] = str(private_tmp)
            completed = subprocess.run(
                launcher.argv(request, private_tmp, placeholders),
                cwd=launcher.workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
            _verify_mounts(placeholders)
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise subprocess.SubprocessError(
                    detail or f"bwrap exited with code {completed.returncode}"
                )
    finally:
        registry.release(token)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _holder_is_alive(holder: dict[object, object]) -> bool:
    pid = holder.get("pid")
    if not isinstance(pid, int) or not _pid_is_alive(pid):
        return False
    recorded_start = holder.get("process_start")
    current_start = _process_start_identity(pid)
    return (
        recorded_start is None
        or current_start is None
        or recorded_start == current_start
    )


def _process_start_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except (OSError, UnicodeError):
        return None
    return fields[21] if len(fields) > 21 else None


def _verify_mounts(mounts: tuple[_Placeholder, ...]) -> None:
    for mount in mounts:
        if mount.path.is_symlink():
            raise SandboxPolicyViolation(
                f"protected workspace path became a symbolic link: {mount.path}"
            )
        try:
            info = mount.path.stat(follow_symlinks=False)
        except FileNotFoundError as error:
            raise SandboxPolicyViolation(
                f"protected workspace path disappeared: {mount.path}"
            ) from error
        if (info.st_dev, info.st_ino) != (mount.device, mount.inode):
            raise SandboxPolicyViolation(
                f"protected workspace path identity changed: {mount.path}"
            )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise SandboxPolicyViolation(f"sandbox runtime path is not a directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise SandboxPolicyViolation(
            f"sandbox runtime path has a different owner: {path}"
        )
    path.chmod(0o700)


__all__ = [
    "BASH_EXECUTABLE",
    "BubblewrapSandboxLauncher",
    "CommandBackend",
    "CommandAuthority",
    "CommandLaunchRequest",
    "CommandLauncher",
    "ExecutionEnvironmentStatus",
    "LocalCommandLauncher",
    "MetadataPlaceholderRegistry",
    "SandboxPolicyViolation",
    "resolve_command_launcher",
]
