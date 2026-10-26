"""User-facing error types.

Errors deriving from :class:`OpsError` are expected failures: the CLI reports
them as a one-line message instead of a traceback.
"""

from __future__ import annotations


class OpsError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigurationError(OpsError):
    """Raised when required configuration (such as credentials) is missing."""


class IndexDefinitionError(OpsError):
    """Raised when one or more index definition files are invalid."""


class SearchDefinitionError(OpsError):
    """Raised when one or more search index definition files are invalid."""


class TriggerExportError(OpsError):
    """Raised when trigger definitions cannot be written unambiguously."""


class TriggerDefinitionError(OpsError):
    """Raised when one or more trigger definition files are invalid."""


class TriggerSyncError(OpsError):
    """Raised when trigger synchronization cannot complete."""


class AtlasOperationError(OpsError):
    """Raised when the Atlas API cannot complete an operation."""
