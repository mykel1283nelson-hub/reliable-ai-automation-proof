"""Domain exceptions for the reliable automation engine."""


class AutomationError(Exception):
    """Base class for expected automation failures."""


class ValidationError(AutomationError, ValueError):
    """Input or provider output failed schema validation."""


class IdempotencyConflict(AutomationError):
    """An idempotency key was reused with a different request."""


class JobNotFound(AutomationError):
    """No persisted job exists for the requested identifier."""


class InvalidTransition(AutomationError):
    """The requested job state transition is not allowed."""


class ProviderError(AutomationError):
    """Base class for provider adapter errors."""


class TransientProviderError(ProviderError):
    """A retryable provider failure."""


class ProviderTimeout(TransientProviderError):
    """The provider exceeded its configured deadline."""


class TokenExpired(ProviderError):
    """The provider credential must be refreshed before retrying."""


class TokenRefreshFailed(ProviderError):
    """Credential refresh did not succeed."""


class MalformedProviderOutput(ProviderError):
    """The provider responded, but its response did not match the contract."""
