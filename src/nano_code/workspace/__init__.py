"""Provider- and tool-neutral workspace safety primitives."""

from nano_code.workspace.local import Workspace, WorkspaceBoundaryError

__all__ = ["Workspace", "WorkspaceBoundaryError"]
