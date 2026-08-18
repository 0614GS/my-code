"""Context attachment contracts.

Sources and projection live in explicit submodules so importing the model from
an Agent contract does not initialize code that depends on Agent snapshots.
"""

from nano_code.context.attachments.models import (
    AttachmentContent,
    AttachmentRetention,
    ContextAttachment,
    ContextObservation,
)

__all__ = [
    "AttachmentContent",
    "AttachmentRetention",
    "ContextAttachment",
    "ContextObservation",
]
