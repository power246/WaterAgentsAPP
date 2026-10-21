import asyncio
import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.application.settings_admin.credential_service import CredentialService
from app.application.transcripts.redaction import registered_private_values
from app.domain.settings_admin import CredentialName
from app.infrastructure.config import AppSettings
from app.infrastructure.credentials.embedding import embedding_api_key, embedding_base_url, lease_embedding_api_key
from app.infrastructure.credentials.memory_key_store import InMemoryKeyStore, OfflineCredentialTester
from app.infrastructure.observability.json_logging import JsonFormatter
from app.main import create_app
from tests.infrastructure.test_m4_runtime import FixedClock


def test_embedding_environment_overrides_are_paired_and_keep_analysis_connection(monkeypatch, tmp_path):
    config = tmp_path / '.env'
    config.write_text("WATERAGENTS_EMBEDDING_BASE_URL='https://file.test/v1'\n"
                      "WATERAGENTS_EMBEDDING_API_KEY='file-fixture-key'\n", encoding='utf-8')
    monkeypatch.setenv('WATERAGENTS_EMBEDDING_BASE_URL', ' https://embedding.test/v1/ ')
    monkeypatch.setenv('WATERAGENTS_EMBEDDING_API_KEY', ' independent-fixture-key ')
    settings = AppSettings(_env_file=config, openai_base_url='https://analysis.test/v1', openai_api_key='cfgparser-f6a0e79025c3')
    assert embedding_base_url(settings) == 'https://embedding.test/v1'
    assert embedding_api_key(settings).get_secret_value() == ' independent-fixture-key '
    assert settings.openai_base_url == 'https://analysis.test/v1'
    assert settings.openai_api_key.get_secret_value() == 'cfgparser-f6a0e79025c3'
    assert 'independent-fixture-key' not in repr(settings)
    assert 'independent-fixture-key' not in settings.model_dump_json()


@pytest.mark.parametrize('change', [
    {'embedding_base_url': 'https://embedding.test/v1'}, {'embedding_api_key': 'fixture-key'},
    {'embedding_base_url': 'https://embedding.test/v1', 'embedding_api_key': ''},
    {'embedding_base_url': 'https://embedding.test/v1', 'embedding_api_key': '  '},
])
def test_partial_embedding_connection_never_falls_back_to_analysis_credentials(change):
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, openai_api_key='cfgparser-f6a0e79025c3', **change)


@pytest.mark.parametrize('url', [
    'embedding.test/v1', 'https://', 'https://user:fixture-key@embedding.test/v1',
    'https://embedding.test/v1?key=fixture-key', 'https://embedding.test/v1#fragment', '',
])
def test_embedding_base_url_uses_the_same_safe_provider_url_contract(url):
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, embedding_base_url=url, embedding_api_key='fixture-key')


def test_independent_embedding_does_not_read_missing_or_overridden_analysis_keys():
    async def exercise():
        store = InMemoryKeyStore()
        credentials = CredentialService(store, dict.fromkeys(CredentialName), FixedClock(), OfflineCredentialTester())
        settings = AppSettings(_env_file=None, embedding_base_url='https://embedding.test/v1',
                               embedding_api_key='independent-fixture-key')
        first = await lease_embedding_api_key(settings, credentials)
        await credentials.save(CredentialName.OPENAI, SecretStr('cfgparser-52f07d6e3b91'))
        store.available = False
        assert (await lease_embedding_api_key(settings, credentials)) == first
        assert first.get_secret_value() == 'independent-fixture-key'
        assert 'independent-fixture-key' in registered_private_values()
    asyncio.run(exercise())


def test_embedding_secret_is_redacted_before_any_provider_dispatch():
    secret = 'startup-embedding-fixture-key'
    create_app(AppSettings(_env_file=None, embedding_base_url='https://embedding.test/v1', embedding_api_key=secret))
    record = logging.LogRecord('wateragents', logging.ERROR, __file__, 1, 'failure: %s', (secret,), None)
    rendered = JsonFormatter().format(record)
    assert secret not in rendered
    assert '[REDACTED]' in json.loads(rendered)['message']


@pytest.mark.parametrize('independent', [False, True])
def test_key_management_describes_only_the_services_using_the_analysis_key(independent):
    async def exercise():
        service = CredentialService(InMemoryKeyStore(), dict.fromkeys(CredentialName), FixedClock(),
            OfflineCredentialTester(), embedding_uses_openai=not independent)
        for source in ('none', 'user_store', 'deployment'):
            if source == 'user_store':
                await service.save(CredentialName.OPENAI, SecretStr('cfgparser-f6a0e79025c3'))
            elif source == 'deployment':
                await service.delete(CredentialName.OPENAI)
                service = CredentialService(InMemoryKeyStore(), {CredentialName.OPENAI: SecretStr('deployment-fixture-key'),
                    CredentialName.GOOGLE_GEOCODING: None}, FixedClock(), OfflineCredentialTester(),
                    embedding_uses_openai=not independent)
            status = await service.status(CredentialName.OPENAI)
            assert ('embedding' in status.purpose) is not independent
            assert {'query_parser', 'waterbody', 'run_summary'} <= set(status.purpose)
    asyncio.run(exercise())


def test_standalone_m8_uses_independent_embedding_without_an_analysis_key(monkeypatch):
    from app.application.transcripts.registry import create_production_registry
    from app.infrastructure.config import ModelCatalog
    from app.infrastructure.http import ProviderHttpClient
    from app.infrastructure.persistence.database import create_database_engine
    from app.infrastructure.runtime import m8
    from scripts.test_database import temporary_migrated_schema
    from tests.infrastructure.embeddings.support import canonical_text
    from tests.infrastructure.test_m4_runtime import catalog

    seen = []
    async def handler(request):
        assert str(request.url) == 'https://embedding.test/v1/embeddings'
        assert request.headers['Authorization'] == 'Bearer standalone-fixture-key'
        body = json.loads(request.content)
        seen.append(body['model'])
        return httpx.Response(200, json={'object': 'list', 'model': body['model'],
            'data': [{'object': 'embedding', 'index': 0, 'embedding': [0.125] * body['dimensions']}]})

    monkeypatch.setattr(m8, 'ProviderHttpClient', lambda **kwargs: ProviderHttpClient(
        **kwargs, transport=httpx.MockTransport(handler)))
    async def exercise(database):
        settings = database.model_copy(update={'openai_api_key': None, 'memory_enabled': False,
            'embedding_base_url': 'https://embedding.test/v1', 'embedding_api_key': SecretStr('standalone-fixture-key')})
        engine = create_database_engine(settings)
        try:
            async with m8.build_m8_runtime(settings, engine=engine, transcript_reader=object(),
                transcript_registry=create_production_registry(), model_catalog=ModelCatalog(catalog()).snapshot(),
                clock=FixedClock(), foundation_validated=True) as runtime:
                await runtime.worker._embedding.embed(runtime.desired_profile, (canonical_text(),), None, None)
                assert seen == [runtime.desired_profile.model]
        finally:
            await engine.dispose()
    with temporary_migrated_schema() as database:
        asyncio.run(exercise(database))
