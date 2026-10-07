import importlib.util
from pathlib import Path


def _catalog_module():
    path = Path(__file__).parents[1] / "scripts" / "sign-update-catalog.py"
    spec = importlib.util.spec_from_file_location("sign_update_catalog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_version_key_supports_beta_releases() -> None:
    version_key = _catalog_module().version_key
    versions = ["0.10.7", "0.10.8a2", "0.10.8b1", "0.10.8rc1", "0.10.8"]
    assert sorted(versions, key=version_key) == versions
