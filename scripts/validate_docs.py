"""Validate the Source Pack manifest and repository-relative Markdown links."""

from pathlib import Path
import re

root = Path(__file__).resolve().parent.parent
docs = root / "docs"
expected = {f"{number:02d}" for number in range(1, 19)}
present = {path.name[:2] for path in docs.glob("[0-9][0-9]-*.md")}
if present != expected:
    raise SystemExit(f"Invalid Source Pack manifest: missing={expected-present}, extra={present-expected}")
for path in [root / "README.md", *docs.glob("*.md")]:
    source = path.read_text(encoding="utf-8")
    if not source.startswith("# "):
        raise SystemExit(f"Missing title in {path.relative_to(root)}")
    for target in re.findall(r"\]\(([^)\s]+)\)", source):
        base = target.split("#")[0]
        if base and not re.match(r"^(https?|mailto):", base):
            if not (path.parent / base).exists():
                raise SystemExit(f"Missing link {target} in {path.relative_to(root)}")
print("Source Pack manifest and Markdown links validated")
