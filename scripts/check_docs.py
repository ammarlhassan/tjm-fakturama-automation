"""Check that the published docs resolve, agree with each other and match the evidence."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from image_to_cash.artifacts import source_identity

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "README.md",
    ROOT / "docs/design.md",
    ROOT / "docs/limitations.md",
    ROOT / "docs/requirements-status.md",
    ROOT / "docs/acceptance.md",
    ROOT / "docs/walkthrough.md",
]
PAGES = [ROOT / "artifacts/index.html", ROOT / "artifacts/submission-demo.html"]
# Statements that were true earlier and must not return.
STALE = [
    "remains in review",
    "must enter review",
    "header fallback",
    "table-header fallback",
    "3.8 Flash",
    "artifacts/dev/",
    "artifacts/heldout/rapidocr",
    "is blocked",
    "Commit groups will be organized",
    "Four matching",
    "four real",
    "re-reading dominates",
    "the two slowest measured stages",
    # Slots filled from measured results; none may reach a published document.
    "MEASURED_RESULTS_TABLE",
    "EXPANDED_PLAN_TABLE",
    "UNPAID_RESULT",
    "FINAL_NUMBERS",
]


def section(text: str, heading: str) -> str:
    match = re.search(rf"^#+ {re.escape(heading)}\s*$", text, flags=re.M)
    if not match:
        return ""
    rest = text[match.end() :]
    following = re.search(r"^#+ ", rest, flags=re.M)
    return " ".join((rest[: following.start()] if following else rest).split())


def main() -> int:
    problems = []
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                problems.append(f"{path.name}: broken link {target}")
        for phrase in STALE:
            if phrase.casefold() in text.casefold():
                problems.append(f"{path.name}: stale statement {phrase!r}")
    for path in PAGES:
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r'href="([^"#][^"]*)"', text):
            if target.startswith(("http://", "https://", "data:")):
                continue
            if not (path.parent / target).resolve().exists():
                problems.append(f"{path.name}: broken link {target}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/design.md").read_text(encoding="utf-8")
    answer = section(design, "Three additional hours")
    if not answer or answer != section(
        readme, "If you had 3 more hours, what would you do for this task?"
    ):
        problems.append("The three-hours answer differs between the README and the design")
    if not 1 <= len(design.split("\n## ")) - 1 <= 12:
        problems.append("Unexpected design structure")

    official = json.loads(
        (ROOT / "artifacts/official-sample-live/verification.json").read_text(encoding="utf-8")
    )
    receipt = official["receipt"]
    for value in (receipt["order_number"], receipt["invoice_number"], receipt["debtor_number"]):
        if value not in readme:
            problems.append(f"README does not state {value}")
    if not official["acceptance"]["passed"]:
        problems.append("The final official run did not pass its acceptance checks")
    if official["reused_provider_responses"] or not official["fresh_provider_calls"]:
        problems.append("The official run is published as live but reused provider responses")
    change = json.loads(
        (ROOT / "artifacts/source-change-after-r1.json").read_text(encoding="utf-8")
    )
    if official["source_identity"]["source_sha256"] != change["official_run_source_sha256"]:
        problems.append("The official run's source hash is not the recorded one")
    if source_identity()["source_sha256"] != change["final_source_sha256"]:
        problems.append(
            "The source changed after artifacts/source-change-after-r1.json was written"
        )
    checks = json.loads((ROOT / "artifacts/core-checks.json").read_text(encoding="utf-8"))
    passed = re.search(r"(\d+) passed", json.dumps(checks))
    if passed and f"{passed.group(1)} tests" not in readme:
        problems.append(f"README does not state the recorded {passed.group(1)} tests")

    for problem in problems:
        print(problem)
    print(f"{len(problems)} documentation problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
