"""Permission and collaboration mode transitions over narrow state capsules."""

from my_code.application.contracts.permissions import (
    PermissionModeSwitch,
    PermissionModeView,
)
from my_code.permissions.models import PermissionMode
from my_code.runtime.application import PermissionRuntime
from my_code.sessions.models import CollaborationMode
from my_code.sessions.session import Session

_MODE_NAMES = {
    PermissionMode.DEFAULT: "Ask for me",
    PermissionMode.ACCEPT_EDITS: "Approve edits",
    PermissionMode.BYPASS: "Full access",
}


def _permission_mode_view(
    mode: PermissionMode,
    *,
    current: bool,
    sandbox_active: bool,
    requires_confirmation: bool,
) -> PermissionModeView:
    return PermissionModeView(
        mode.value,
        _MODE_NAMES.get(mode, mode.value),
        current,
        mode is PermissionMode.BYPASS,
        sandbox_active,
        requires_confirmation,
    )


class ModeOperations:
    @staticmethod
    def collaboration_mode(session: Session) -> CollaborationMode:
        return CollaborationMode(session.collaboration_mode)

    def cycle_collaboration(
        self, session: Session, permissions: PermissionRuntime
    ) -> CollaborationMode:
        current = self.collaboration_mode(session)
        target = (
            CollaborationMode.PLAN
            if current is CollaborationMode.DEFAULT
            else CollaborationMode.DEFAULT
        )
        session.set_collaboration_mode(target.value)
        effective = (
            PermissionMode.PLAN
            if target is CollaborationMode.PLAN
            else PermissionMode(session.permission_mode)
        )
        permissions.restore_mode(effective)
        return target

    def permission_modes(
        self, session: Session, permissions: PermissionRuntime
    ) -> tuple[PermissionModeView, ...]:
        if self.collaboration_mode(session) is CollaborationMode.PLAN:
            return ()
        current = permissions.policy.mode
        return tuple(
            _permission_mode_view(
                mode,
                current=mode is current,
                sandbox_active=permissions.sandbox_active,
                requires_confirmation=permissions.requires_full_access_confirmation(
                    mode
                ),
            )
            for mode in (
                PermissionMode.DEFAULT,
                PermissionMode.ACCEPT_EDITS,
                PermissionMode.BYPASS,
            )
        )

    def current_permission_mode(
        self, session: Session, permissions: PermissionRuntime
    ) -> PermissionModeView:
        current = permissions.policy.mode
        if current is PermissionMode.PLAN:
            current = PermissionMode(session.permission_mode)
        return _permission_mode_view(
            current,
            current=True,
            sandbox_active=permissions.sandbox_active,
            requires_confirmation=permissions.full_access_pending,
        )

    def cycle_permission(
        self, session: Session, permissions: PermissionRuntime
    ) -> PermissionModeSwitch:
        self._require_default_collaboration(session)
        target, needs_confirmation = permissions.request_cycle(
            lambda mode: session.set_permission_mode(mode.value)
        )
        return PermissionModeSwitch(
            _permission_mode_view(
                target,
                current=not needs_confirmation,
                sandbox_active=permissions.sandbox_active,
                requires_confirmation=needs_confirmation,
            ),
            not needs_confirmation,
            needs_confirmation,
        )

    def select_permission(
        self, value: str, session: Session, permissions: PermissionRuntime
    ) -> PermissionModeSwitch:
        self._require_default_collaboration(session)
        try:
            requested = PermissionMode(value)
        except ValueError as error:
            raise ValueError(f"Unknown permission mode: {value}") from error
        current = permissions.policy.mode
        target, needs_confirmation = permissions.request_mode(
            requested, lambda mode: session.set_permission_mode(mode.value)
        )
        return PermissionModeSwitch(
            _permission_mode_view(
                target,
                current=not needs_confirmation,
                sandbox_active=permissions.sandbox_active,
                requires_confirmation=needs_confirmation,
            ),
            target is not current and not needs_confirmation,
            needs_confirmation,
        )

    @staticmethod
    def confirm_full_access(
        allow: bool, session: Session, permissions: PermissionRuntime
    ) -> PermissionModeView:
        mode = permissions.confirm_full_access(
            allow, lambda selected: session.set_permission_mode(selected.value)
        )
        return _permission_mode_view(
            mode,
            current=True,
            sandbox_active=permissions.sandbox_active,
            requires_confirmation=False,
        )

    @staticmethod
    def _require_default_collaboration(session: Session) -> None:
        if CollaborationMode(session.collaboration_mode) is CollaborationMode.PLAN:
            raise RuntimeError("Permissions cannot change in Plan mode")


__all__ = ["ModeOperations"]
