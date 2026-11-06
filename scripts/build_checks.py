"""Record the repository checks and the tested environment as published evidence."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from image_to_cash.artifacts import source_identity, write_json
from image_to_cash.config import Settings
from image_to_cash.doctor import diagnose

ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    ["-m", "ruff", "check", "--no-cache", "src", "scripts", "tests"],
    ["-m", "ruff", "format", "--check", "--no-cache", "src", "scripts", "tests"],
    ["-m", "mypy", "--platform", "win32", "src/image_to_cash"],
    ["-m", "mypy", "--platform", "linux", "src/image_to_cash"],
    ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore"],
    [str(ROOT / "scripts/check_docs.py")],
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="integration")
    options = parser.parse_args()
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    results = []
    for arguments in CHECKS:
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        results.append(
            {
                "command": "python " + " ".join(arguments).replace(str(ROOT) + os.sep, ""),
                "exit_code": completed.returncode,
                "seconds": round(time.perf_counter() - started, 3),
                "output": completed.stdout[-2000:] + completed.stderr[-500:],
            }
        )
        print(results[-1]["command"], "exit", completed.returncode, flush=True)
    write_json(
        ROOT / "artifacts/core-checks.json",
        {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "platform": f"{platform.system()} {platform.machine()}",
            "source_identity": source_identity(),
            "checks": results,
            "all_passed": all(result["exit_code"] == 0 for result in results),
        },
    )
    settings = Settings()
    report = diagnose(settings, workspace=options.workspace)
    provenance = settings.runtime / "tools" / "java-provenance.json"
    if provenance.exists():
        report["java"]["provenance"] = json.loads(provenance.read_text(encoding="utf-8"))
    write_json(ROOT / "artifacts/environment.json", report)
    if not all(result["exit_code"] == 0 for result in results):
        raise SystemExit("A repository check failed; see artifacts/core-checks.json")


if __name__ == "__main__":
    main()
