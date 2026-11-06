"""The written setup has to run in the environment that setup creates."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = r"$env:LOCALAPPDATA\TJM-ImageToCash\environment"
PUBLISHED = ["README.md", "docs/acceptance.md", "docs/limitations.md", "docs/walkthrough.md"]


def test_setup_uses_one_environment_for_uv_commands_and_scripts():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'$env:UV_PROJECT_ENVIRONMENT = "{ENVIRONMENT}"' in readme
    definition = f'$python = "{ENVIRONMENT}\\Scripts\\python.exe"'
    assert definition in readme
    assert readme.index(definition) < readme.index("& $python scripts/")
    command = (ROOT / "itc.cmd").read_text(encoding="utf-8")
    assert r"%LOCALAPPDATA%\TJM-ImageToCash\environment\Scripts\python.exe" in command


def test_no_document_runs_a_script_with_the_base_python():
    for name in PUBLISHED:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.findall(r"(?<![$\w])python(?:\.exe)? scripts[/\\]", text), name
