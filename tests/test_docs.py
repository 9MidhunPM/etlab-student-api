"""Keep the documented navigation and copyable snippets usable without network access."""

import ast
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = sorted({ROOT / "README.md", *ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/**/*.md")})
FENCES = re.compile(r"^\s*(`{3,}|~{3,})([^\n]*)\n(.*?)^\s*\1\s*$", re.MULTILINE | re.DOTALL)
LINKS = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")


def outside_fences(markdown: str) -> str:
    return FENCES.sub("", markdown)


def heading_anchors(markdown: str) -> set[str]:
    counts: Counter[str] = Counter()
    anchors = set()
    for match in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", outside_fences(markdown), re.MULTILINE):
        heading = re.sub(r"<[^>]*>", "", match[1]).lower()
        slug = re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")
        suffix = f"-{counts[slug]}" if counts[slug] else ""
        anchors.add(slug + suffix)
        counts[slug] += 1
    return anchors


def test_documentation_links_and_heading_anchors() -> None:
    failures = []
    for document in DOCUMENTS:
        markdown = outside_fences(document.read_text())
        for match in LINKS.finditer(markdown):
            target = match[1].strip().strip("<>")
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            destination = (document.parent / unquote(url.path)).resolve() if url.path else document
            label = f"{document.relative_to(ROOT)}: {target}"
            if not destination.exists():
                failures.append(f"Missing file: {label}")
            elif url.fragment and destination.suffix == ".md":
                if unquote(url.fragment) not in heading_anchors(destination.read_text()):
                    failures.append(f"Missing heading: {label}")
    assert not failures, "\n".join(failures)


def test_documentation_code_syntax() -> None:
    for document in DOCUMENTS:
        for match in FENCES.finditer(document.read_text()):
            language = match[2].strip()
            source = match[3]
            label = str(document.relative_to(ROOT))
            if language == "python":
                ast.parse(source, filename=label)
            elif language == "json":
                json.loads(source)
            elif language in {"sh", "bash"}:
                result = subprocess.run(
                    ["bash", "-n"], input=source, text=True, capture_output=True, check=False
                )
                assert result.returncode == 0, f"Invalid shell syntax in {label}: {result.stderr}"
