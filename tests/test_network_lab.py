"""Safety boundaries for a root network lab; these tests never change networks."""
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_lab():
    path = Path(__file__).resolve().parents[1] / "scripts/network-lab.py"
    spec = importlib.util.spec_from_file_location("network_lab_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_namespaces_are_never_removed_on_refusal(tmp_path, monkeypatch):
    lab = load_lab()
    calls = []

    def fake_command(*args, **kwargs):
        calls.append(args)
        assert args == ("ip", "netns", "list"), "Refused lab must not mutate any namespace"
        return subprocess.CompletedProcess(args, 0, stdout="csqa-client (id: 1)\n", stderr="")

    monkeypatch.setattr(lab, "command", fake_command)
    monkeypatch.setattr(lab, "sys", SimpleNamespace(platform="linux", version="test"))
    monkeypatch.setattr(lab.os, "geteuid", lambda: 0, raising=False)
    args = SimpleNamespace(work=tmp_path / "new-work", output=tmp_path / "report.json", source_commit="test", rtt_ms=30, rate_mbit=1000, sizes=[100])
    with pytest.raises(RuntimeError, match="already exist"):
        lab.run_lab(args)
    assert calls
    report = json.loads(args.output.read_text())
    assert report["errors"][0]["type"] == "RuntimeError"
    assert not report["namespaces_removed"]


def test_partial_setup_tracks_only_successfully_created_namespaces(monkeypatch):
    lab = load_lab()
    calls = []

    def fake_command(*args, **kwargs):
        calls.append(args)
        if args == ("ip", "netns", "add", "csqa-router"):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(lab, "command", fake_command)
    owned = []
    with pytest.raises(subprocess.CalledProcessError):
        lab.setup_network(SimpleNamespace(), owned)
    assert owned == ["csqa-client"]
    assert not any(call[:3] == ("ip", "netns", "del") for call in calls)
