from pydantic import SecretStr

from app.application.settings_admin.credential_service import CredentialService
from app.application.settings_admin.errors import CredentialMissingError, CredentialStoreUnavailableError
from app.application.transcripts.redaction import register_private_value
from app.domain.settings_admin import CredentialName
from app.infrastructure.config import AppSettings
from app.ports import PortAuthenticationError


def embedding_base_url(settings: AppSettings) -> str:
    return settings.embedding_base_url or settings.openai_base_url


def embedding_api_key(settings: AppSettings, analysis_key: SecretStr | None = None) -> SecretStr:
    key = (settings.embedding_api_key if settings.embedding_base_url is not None
           else analysis_key if analysis_key is not None else settings.openai_api_key)
    if key is None or not key.get_secret_value():
        raise PortAuthenticationError("Embedding credential is unavailable.", provider="openai", operation="embed")
    register_private_value(key.get_secret_value())
    return key


async def lease_embedding_api_key(settings: AppSettings, credentials: CredentialService) -> SecretStr:
    if settings.embedding_base_url is not None:
        return embedding_api_key(settings)
    try:
        lease = await credentials.lease(CredentialName.OPENAI)
    except (CredentialMissingError, CredentialStoreUnavailableError):
        raise PortAuthenticationError("Embedding credential is unavailable.", provider="openai", operation="embed") from None
    return embedding_api_key(settings, lease.value)
