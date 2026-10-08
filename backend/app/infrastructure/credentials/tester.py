"""One explicit bounded provider request for a credential test action."""

from collections.abc import Callable

from pydantic import SecretStr

from app.application.settings_admin.errors import CredentialTestFailedError
from app.domain.settings_admin import CredentialName
from app.infrastructure.http import ProviderHttpClient
from app.ports import PortAuthenticationError, PortError


class ProviderCredentialTester:
    def __init__(self, *, openai_base_url: str,
                 http_factory: Callable[[], ProviderHttpClient] | None = None) -> None:
        self._openai_base_url = openai_base_url
        self._http_factory = http_factory or (lambda: ProviderHttpClient(
            timeout_seconds=5, max_response_bytes=16_384,
            retry_delay_seconds=0, attempts=1,
        ))

    async def test(self, name: CredentialName, value: SecretStr) -> bool:
        async with self._http_factory() as http:
            try:
                if name is CredentialName.OPENAI:
                    response = await http.get_json(
                        f"{self._openai_base_url.rstrip('/')}/models",
                        provider="openai", operation="credential_test",
                        headers={"Authorization": f"Bearer {value.get_secret_value()}"},
                    )
                    return isinstance(response, dict) and isinstance(response.get("data"), list)
                if name is CredentialName.GOOGLE_GEOCODING:
                    response = await http.get_json(
                        "https://maps.googleapis.com/maps/api/geocode/json",
                        provider="google_geocoding", operation="credential_test",
                        params={"address": "1600 Pennsylvania Ave NW, Washington, DC",
                                "key": value.get_secret_value()},
                    )
                    return isinstance(response, dict) and response.get("status") in {
                        "OK", "ZERO_RESULTS",
                    }
            except PortAuthenticationError:
                return False
            except PortError:
                raise CredentialTestFailedError() from None
        return False
