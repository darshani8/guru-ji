"""Never fabricate provider credentials in the local build."""

from ..domain.errors import ErrorCode, GuruJiError, PublicError


def issue_ephemeral_credential(provider_configured: bool, request_id: str) -> dict[str, str]:
    if not provider_configured:
        raise GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, "Realtime voice provider is not configured.", request_id))
    raise GuruJiError(PublicError(ErrorCode.SERVICE_UNAVAILABLE, "Realtime credential issuance is not enabled in the local reference build.", request_id))


__all__ = ["issue_ephemeral_credential"]
