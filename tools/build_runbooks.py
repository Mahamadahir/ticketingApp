"""Builds the Azure Automation runbooks from infra/runbooks/*.ps1.

    python tools/build_runbooks.py <output folder>

Every runbook there holds the line `# @include Common.ps1`. Azure Automation cannot dot-source a sibling file, so
this puts the text of Common.ps1 in its place and writes one `<RunbookName>.ps1` per runbook to the output folder.
The Deploy workflow does this and uploads the results (.github/scripts/publish-runbooks.sh); the tests run the same
function against fake Exchange cmdlets.
"""

import sys
from pathlib import Path

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "infra" / "runbooks"
COMMON = "Common.ps1"
INCLUDE_LINE = f"# @include {COMMON}"


def runbook_files(directory: Path = RUNBOOK_DIR) -> list[Path]:
    """The runbook scripts: every .ps1 in the folder except the shared Common.ps1."""
    return sorted(p for p in directory.glob("*.ps1") if p.name != COMMON)


def build(path: Path, directory: Path = RUNBOOK_DIR) -> str:
    """The text of one runbook with the shared helpers included. Exactly one include line is required."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    marks = [i for i, line in enumerate(lines) if line.strip() == INCLUDE_LINE]
    if len(marks) != 1:
        raise ValueError(f"{path.name} must contain the line '{INCLUDE_LINE}' exactly once")
    common = (directory / COMMON).read_text(encoding="utf-8").rstrip("\n")
    lines[marks[0]] = common
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    files = runbook_files()
    if not files:
        print("No runbooks found", file=sys.stderr)
        return 1
    for path in files:
        (out / path.name).write_text(build(path), encoding="utf-8", newline="\n")
        print(f"built {path.stem}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
