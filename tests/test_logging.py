from apps.api.app.core.logging import _redact_secrets


def test_redacts_common_secret_key_names():
    event = {
        "event": "broker_connect",
        "api_key": "sk-live-abc123",  # pragma: allowlist secret
        "password": "hunter2",
        "access_token": "eyJ...",
        "symbol": "AAPL",
    }
    result = _redact_secrets(None, "info", dict(event))
    assert result["api_key"] == "***REDACTED***"
    assert result["password"] == "***REDACTED***"
    assert result["access_token"] == "***REDACTED***"
    assert result["symbol"] == "AAPL"
