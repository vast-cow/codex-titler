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


@pytest.mark.parametrize(
    "values",
    [
        {"estimation_policy": "guess"},
        {"estimate_multiplier": 0.9},
        {"context_fraction": 0},
        {"context_fraction": 1.1},
    ],
)
def test_invalid_estimation_settings(values):
    with pytest.raises(ValidationError):
        Settings(endpoint="ws://localhost", **values)


def test_estimation_cli():
    settings, _ = load(
        [
            "--endpoint",
            "ws://localhost",
            "--estimation-policy",
            "exact_only",
            "--estimate-multiplier",
            "2",
            "--context-fraction",
            "0.5",
        ]
    )
    assert settings.estimation_policy == "exact_only"
    assert settings.estimate_multiplier == 2
    assert settings.context_fraction == 0.5


def test_ephemeral_project_path_defaults_without_config():
    assert Settings(endpoint="ws://localhost").ephemeral_project_path == "/tmp"
    settings, _ = load(["--endpoint", "ws://localhost"])
    assert settings.ephemeral_project_path == "/tmp"


@pytest.mark.parametrize("project_path", [None, "/remote/server/title-helpers"])
def test_ephemeral_project_path_from_toml(tmp_path, project_path):
    path = tmp_path / "titler.toml"
    contents = '[titler]\nendpoint = "ws://localhost"\n'
    if project_path is not None:
        contents += f'ephemeral_project_path = "{project_path}"\n'
    path.write_text(contents)
    settings, _ = load(["--config", str(path)])
    assert settings.ephemeral_project_path == (project_path or "/tmp")


def test_backfill_order_default():
    assert Settings(endpoint="ws://localhost").backfill_order == "newest-first"
    assert load(["--endpoint", "ws://localhost"])[0].backfill_order == "newest-first"


@pytest.mark.parametrize("order", ["newest-first", "oldest-first"])
def test_backfill_order_config_and_cli_precedence(tmp_path, order):
    path = tmp_path / "titler.toml"
    path.write_text(
        f'[titler]\nendpoint = "ws://localhost"\nbackfill_order = "{order}"\n'
    )
    assert load(["--config", str(path)])[0].backfill_order == order
    override = "oldest-first" if order == "newest-first" else "newest-first"
    settings, _ = load(["--config", str(path), "--backfill-order", override])
    assert settings.backfill_order == override


@pytest.mark.parametrize("source", ["cli", "toml"])
def test_invalid_backfill_order(tmp_path, source):
    args = ["--endpoint", "ws://localhost"]
    if source == "cli":
        args += ["--backfill-order", "random"]
    else:
        path = tmp_path / "titler.toml"
        path.write_text('[titler]\nbackfill_order = "random"\n')
        args += ["--config", str(path)]
    with pytest.raises(SystemExit) as exc:
        load(args)
    assert exc.value.code == 2
