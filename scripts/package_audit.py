"""Build the package outside the repository, load it from the wheel and scan the submission.

Configured key values are only compared. They are never printed or written anywhere.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.extraction import api_key

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "google_api_key": rb"AIza[0-9A-Za-z_-]{30,}",
    "openai_api_key": rb"sk-[A-Za-z0-9_-]{20,}",
    "bearer_token": rb"Bearer [A-Za-z0-9._-]{20,}",
}
VARIABLES = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ITC_API_TOKEN")
# Embedded screenshots are long base64 runs, where a four-letter prefix matches by chance.
EMBEDDED = re.compile(rb"base64,[A-Za-z0-9+/=]+")
IMAGES = {".png", ".jpg", ".jpeg", ".pdf"}


def submission_files() -> list[Path]:
    git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    listed = subprocess.run(
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [ROOT / name for name in listed.split("\0") if name]


def scan(files: list[Path]) -> dict:
    values = [value.encode() for value in map(api_key, VARIABLES) if value]
    patterns, configured = set(), set()
    for path in files:
        data = path.read_bytes()
        relative = path.relative_to(ROOT).as_posix()
        if any(value in data for value in values):
            configured.add(relative)
        if path.suffix.lower() in IMAGES:
            continue
        text = EMBEDDED.sub(b"base64,", data)
        if any(re.search(pattern, text) for pattern in PATTERNS.values()):
            patterns.add(relative)
    return {
        "files_scanned": len(files),
        "bytes_scanned": sum(path.stat().st_size for path in files),
        "configured_values_compared": len(values),
        "files_with_a_configured_value": sorted(configured),
        "files_matching_a_key_pattern": sorted(patterns),
        "secret_values_printed": False,
    }


def build(output: Path) -> dict:
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(output), str(ROOT)],
        check=True,
        capture_output=True,
    )
    wheel = next(output.glob("*.whl"))
    sdist = next(output.glob("*.tar.gz"))
    unpacked = output / "unpacked"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(unpacked)
    environment = dict(os.environ, PYTHONPATH=str(unpacked), PYTHONDONTWRITEBYTECODE="1")
    loaded = json.loads(
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import image_to_cash, json;"
                "from image_to_cash.artifacts import source_identity as s;"
                "print(json.dumps({'file': image_to_cash.__file__, **s()}))",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    validate = subprocess.run(
        [
            sys.executable,
            "-m",
            "image_to_cash.cli",
            "validate",
            str(ROOT / "fixtures/sample.json"),
        ],
        env=environment,
        capture_output=True,
    )
    return {
        "wheel": wheel.name,
        "wheel_bytes": wheel.stat().st_size,
        "sdist": sdist.name,
        "sdist_bytes": sdist.stat().st_size,
        "loaded_from_the_wheel": Path(loaded["file"]).is_relative_to(unpacked),
        "wheel_source_matches_the_repository": loaded["source_sha256"]
        == source_identity()["source_sha256"],
        "validate_command_passed": validate.returncode == 0,
        "report_template_packaged": (unpacked / "image_to_cash/templates/report.html").exists(),
        "scope": "Existing locked dependencies on this machine; not a clean-machine installation",
    }


def main():
    with tempfile.TemporaryDirectory(prefix="tjm-package-") as folder:
        package = build(Path(folder))
    result = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source_identity": source_identity(),
        "package": package,
        "submission_scan": scan(submission_files()),
    }
    write_json(ROOT / "artifacts/package-audit.json", result)
    print(json.dumps(result, indent=1))
    scan_result = result["submission_scan"]
    failed = (
        scan_result["files_with_a_configured_value"]
        or scan_result["files_matching_a_key_pattern"]
        or not all(value for key, value in package.items() if not key.endswith(("bytes", "scope")))
    )
    if failed:
        raise SystemExit("Package audit failed; see artifacts/package-audit.json")


if __name__ == "__main__":
    main()
