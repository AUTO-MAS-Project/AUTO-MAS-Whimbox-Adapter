"""Run against a matching host checkout without touching user configuration."""

import os
import sys
from pathlib import Path

import pytest

source = Path(os.environ["AUTO_MAS_HOST_SOURCE"]).resolve()
adapter = Path(
    os.environ.get("AUTO_MAS_ADAPTER_SITE", Path(__file__).resolve().parents[1] / "src")
)
sys.path[:0] = [
    str(source),
    str(source / "plugins/auto_mas_core/src"),
    str(adapter),
]


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    from app.config import config_manager
    from app.core import Config

    roots = dict(config_manager._roots)
    config_manager._roots.clear()
    monkeypatch.chdir(tmp_path)
    for name, directory in {
        "work_path": ".",
        "install_path": ".",
        "data_path": "data",
        "config_path": "config",
        "debug_path": "debug",
        "history_path": "history",
        "script_path": "script",
        "temp_path": "temp",
        "plugins_path": "plugins",
    }.items():
        target = tmp_path / directory
        target.mkdir(exist_ok=True)
        monkeypatch.setattr(Config, name, target)
    yield Config
    handle = config_manager._save_handle
    if handle is not None and not handle.done() and not handle.get_loop().is_closed():
        handle.cancel()
    config_manager._save_handle = None
    config_manager._roots.clear()
    config_manager._roots.update(roots)
