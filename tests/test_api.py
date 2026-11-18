import importlib
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SUPERVISOR_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'api.db'))
    monkeypatch.setenv('CONTROL_TOKEN', 'test-owner-token')
    monkeypatch.setenv('GATEWAY_TOKEN', 'test-gateway-token')
    monkeypatch.setenv('SECRETS_DIR', str(tmp_path / 'secrets'))
    import supervisor.api as api
    with TestClient(api.app, base_url='http://localhost:8787') as c:
        yield c


def test_control_requires_token(client):
    assert client.get('/status').status_code == 401
    response = client.get('/status', headers={'Authorization': 'Bearer test-owner-token'})
    assert response.status_code == 200
    assert not response.json()['openai_configured']


def test_api_secret_is_saved_without_echoing(client):
    r = client.post('/settings/secrets', headers={'Authorization': 'Bearer test-owner-token'}, json={'openai_key': 'fake-unit-test-key'})
    assert r.status_code == 200
    assert 'fake-unit-test-key' not in r.text
    assert client.get('/status', headers={'Authorization': 'Bearer test-owner-token'}).json()['openai_configured']


def test_unmeasured_qualification_rejected(client):
    assert client.post('/qualification', headers={'Authorization': 'Bearer test-owner-token'}, json={'runs': []}).status_code == 422


def test_gateway_denies_control_token(client):
    response = client.post('/v1/chat/completions', headers={'Authorization': 'Bearer test-owner-token'}, json={'model': 'gpt-5.4-mini'})
    assert response.status_code == 401


def test_campaign_cannot_be_marked_verified_without_evidence(client):
    r = client.put('/campaigns/tjr', headers={'Authorization': 'Bearer test-owner-token'}, json={'ai_editing_allowed': True, 'submission_verified': True})
    assert r.status_code == 422


def test_local_session_survives_without_exposing_tokens(client):
    response = client.get('/session')
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert 'test-owner-token' not in response.text + response.headers['set-cookie']
    assert client.get('/status').status_code == 200


def test_remote_site_cannot_bootstrap_or_use_local_cookie(client):
    assert client.get('/session', headers={'Origin': 'https://evil.test', 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    client.get('/session')
    assert client.post('/tick', headers={'Origin': 'https://evil.test', 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/status', headers={'Host': 'evil.test'}).status_code in (400, 403)


def test_tampered_browser_cookie_rejected(client):
    client.cookies.set('clipfarm_session', 'invalid')
    assert client.get('/status').status_code == 401


def test_session_does_not_bootstrap_from_internal_hostname(client):
    assert client.get('/session', headers={'Host': 'supervisor:8787'}).status_code == 403


def test_oauth_redirect_and_user_navigation_work_cross_site(client):
    assert client.get('/', headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}).status_code == 200
    assert client.get('/oauth/callback?state=invalid', headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}).status_code == 400
