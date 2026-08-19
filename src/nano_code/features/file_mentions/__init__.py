"""Submitted ``@path`` mentions and workspace path discovery."""

from nano_code.features.file_mentions.loader import AttachmentLoader
from nano_code.features.file_mentions.models import (
    FileMention,
    LoadedAttachment,
    PathSuggestion,
)
from nano_code.features.file_mentions.parser import parse_file_mentions
from nano_code.features.file_mentions.reader import WorkspaceAttachmentReader
from nano_code.features.file_mentions.suggestions import WorkspacePathSuggester

__all__ = [
    "AttachmentLoader",
    "FileMention",
    "LoadedAttachment",
    "PathSuggestion",
    "WorkspacePathSuggester",
    "parse_file_mentions",
    "WorkspaceAttachmentReader",
]
