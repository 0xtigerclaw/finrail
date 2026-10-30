"""The web console is a plugin: registered, self-declaring, and off means off."""
import pytest

from finrail.core import registry


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    registry.load(force=True)
    yield
    registry.load(force=True)


def test_console_is_a_registered_extension_with_no_capabilities():
    plugin = next(p for p in registry.installed() if p["name"] == "console")
    assert plugin["kind"] == "extension" and plugin["enabled"]
    assert not [c for c in registry.commands().values() if c.bundle == "console"]


def test_console_declares_its_own_libraries():
    from finrail.extensions import console
    assert {r.module for r in console.REQUIREMENTS()} == {"fastapi", "uvicorn", "multipart"}


def test_a_disabled_console_refuses_to_start(monkeypatch):
    pytest.importorskip("fastapi")
    import uvicorn
    from console import app
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    registry.disable("console")
    with pytest.raises(SystemExit, match="console plugin is disabled"):
        app.main()
    assert not started
    registry.enable("console")
    app.main()
    assert started == [1]
