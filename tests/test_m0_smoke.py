"""M0 smoke tests. Import scripts without network and check redaction."""

import importlib.util
import os
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = ["check_meta", "check_sam", "check_ado", "check_slack"]
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name):
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"m0_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch(
        "urllib.request.urlopen", side_effect=AssertionError("network disabled")
    ):
        spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", SCRIPTS)
def test_redact_helper_exists(name):
    mod = load_module(name)
    assert hasattr(mod, "redact")
    assert callable(mod.redact)


@pytest.mark.parametrize("name", SCRIPTS)
def test_redact_masks_tokens(name):
    mod = load_module(name)
    out = mod.redact("tok xoxb-fake123 and xapp-fake456 here")
    assert "xoxb-fake123" not in out
    assert "xapp-fake456" not in out
    out = mod.redact("key github_pat_fake789 done")
    assert "github_pat_fake789" not in out
    out = mod.redact("h Bearer secret123 tail")
    assert "secret123" not in out
    with mock.patch.dict(os.environ, {"ADO_PAT": "ado-fake-pat-9999"}):
        out = mod.redact("pat ado-fake-pat-9999 end")
        assert "ado-fake-pat-9999" not in out


@pytest.mark.parametrize("name", SCRIPTS)
def test_helpers_and_help(name):
    mod = load_module(name)
    assert hasattr(mod, "load_secrets")
    assert hasattr(mod, "main")
    assert hasattr(mod, "parse_args")
    with pytest.raises(SystemExit) as e:
        mod.parse_args(["--help"])
    assert e.value.code == 0
