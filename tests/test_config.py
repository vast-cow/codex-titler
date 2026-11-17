import pytest
from pydantic import ValidationError

from codex_auto_title.cli import load
from codex_auto_title.config import Settings


def test_config_precedence_and_auth(tmp_path, monkeypatch):
    path = tmp_path / "titler.toml"
    path.write_text(
        '[titler]\nendpoint="ws://localhost:4500"\nworkers=3\ntoken_env="TEST_TOKEN"\n'
    )
    settings, _ = load(["--config", str(path), "--endpoint", "unix:///tmp/test.sock"])
    assert settings.endpoint == "unix:///tmp/test.sock"
    assert settings.workers == 3
    monkeypatch.setenv("TEST_TOKEN", "secret")
    assert settings.headers() == {"Authorization": "Bearer secret"}
    monkeypatch.delenv("TEST_TOKEN")
    with pytest.raises(ValueError, match="TEST_TOKEN"):
        settings.headers()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost",
        "unix://host/path",
        "unix:relative",
        "ws:///path",
        "wss://user:pass@host",
        "ws://host:invalid",
    ],
)
def test_invalid_endpoint(endpoint):
    with pytest.raises(ValidationError):
        Settings(endpoint=endpoint)


def test_tls_endpoint_and_separate_unix_uri():
    assert Settings(endpoint="wss://codex.example.com").endpoint.startswith("wss:")
    s = Settings(endpoint="unix:///tmp/my%20socket", unix_uri="ws://localhost/rpc")
    assert s.socket_path == "/tmp/my socket"
    assert s.unix_uri == "ws://localhost/rpc"
