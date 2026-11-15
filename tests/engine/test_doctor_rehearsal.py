"""The doctor's fake-site booking, exactly as `run.sh doctor --full` runs it.

This is the check the Grok Bot's first run relies on to prove Chrome works on
its computer, so it gets its own end-to-end test: a settings change that breaks
it would otherwise surface only on a fresh install.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_ptbot():
    spec = importlib.util.spec_from_file_location("ptbot_doctor", ROOT / "scripts" / "ptbot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ptbot_doctor"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.e2e
def test_doctor_fake_site_rehearsal_books_with_real_chrome(chrome_paths, tmp_path, monkeypatch):
    binary, driver = chrome_paths
    monkeypatch.setenv("CHROME_BIN", binary)
    if driver:
        monkeypatch.setenv("CHROME_DRIVER_PATH", driver)
    else:
        monkeypatch.delenv("CHROME_DRIVER_PATH", raising=False)

    ptbot = _load_ptbot()
    monkeypatch.setattr(ptbot, "STATE", tmp_path)

    ok, detail = ptbot._fake_site_rehearsal()

    assert ok, detail
    assert "booked 11:30 AM for 60 min" in detail


@pytest.mark.parametrize("config", ["config.template.yaml", "config.sample.yaml"])
def test_doctor_accepts_the_shipped_configs(config, tmp_path, monkeypatch, capsys):
    """The quick doctor reads every config check without a traceback.

    No browser: this guards the doctor's own code against settings changes,
    such as a removed key it still reads.
    """
    monkeypatch.delenv("USERID", raising=False)
    monkeypatch.delenv("PASSWORD", raising=False)
    ptbot = _load_ptbot()
    monkeypatch.setattr(ptbot, "CONFIG", ROOT / "config" / config)
    monkeypatch.setattr(ptbot, "STATE", tmp_path)
    monkeypatch.setattr(ptbot, "SECRETS_FILE", tmp_path / "absent.env")

    ptbot.cmd_doctor(ptbot.build_parser().parse_args(["doctor"]))

    out = capsys.readouterr().out
    config_lines = [line for line in out.splitlines() if "] config:" in line]
    assert config_lines, out
    if config == "config.template.yaml":
        # A fresh bot names no club: a warning that says what to do, never a pass.
        assert config_lines == ["[WARN] config: no club set yet; say 'set up my schedule' "
                                "(nothing can book until then)"], out
    else:
        assert all(line.startswith("[PASS]") for line in config_lines), out
    assert "verdict:" in out


@pytest.mark.e2e
def test_doctor_fake_portal_rehearsal_books_with_real_chrome(chrome_paths, tmp_path, monkeypatch):
    binary, driver = chrome_paths
    monkeypatch.setenv("CHROME_BIN", binary)
    if driver:
        monkeypatch.setenv("CHROME_DRIVER_PATH", driver)
    else:
        monkeypatch.delenv("CHROME_DRIVER_PATH", raising=False)

    ptbot = _load_ptbot()
    monkeypatch.setattr(ptbot, "STATE", tmp_path)

    ok, detail = ptbot._fake_portal_rehearsal()

    assert ok, detail
    assert "booked 6:30 PM for 60 min on the fake portal" in detail
