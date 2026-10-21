"""
Build the GitHub wiki from the repository: the pages in ``wiki/``, plus the reference
docs at the top of the repository, which are copied in under page names of their own so
that they are maintained in one place only. Every link between pages, and every link to
a heading, is checked, and the build fails on a broken one.

Usage: python .github/scripts/build_wiki.py OUT_DIR
"""

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "wiki"
REPO_URL = "https://github.com/johannes-langer/PyICs"

# reference docs of the repository, by the wiki page they become
REFERENCES = {
    "SimpleTest-Reference": "simple_test.md",
    "StructureTest-Reference": "structure_test.md",
    "GeneralTest-Reference": "general_test.md",
}

# files of wiki/ that aren't pages
SKIPPED = {"README.md"}

LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def anchor(heading: str) -> str:
    """The anchor GitHub gives a heading, e.g. ``step-3-hints`` for "Step 3: Hints"."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links: their text
    text = re.sub(r"<[^>]+>", "", text).lower()  # html tags
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def outside_code(text: str):
    """The lines of ``text`` that aren't in fenced code blocks."""
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield line


def anchors(text: str) -> set[str]:
    found: set[str] = set()
    for line in outside_code(text):
        if m := HEADING.match(line):
            base = anchor(m.group(1))
            name, n = base, 0
            while name in found:  # repeated headings get -1, -2, ...
                n += 1
                name = f"{base}-{n}"
            found.add(name)
    return found


def broken_links(pages: dict[str, str]) -> list[str]:
    known = {name: anchors(text) for name, text in pages.items()}
    problems = []
    for name, text in pages.items():
        for line in outside_code(text):
            # inline code can't hold links
            for target in LINK.findall(re.sub(r"`[^`]*`", "", line)):
                if re.match(r"[a-z]+:", target):  # https:, mailto:, ...
                    continue
                page, _, fragment = target.partition("#")
                page = page or name
                if page not in known:
                    problems.append(f"{name}: link to missing page {target!r}")
                elif fragment and fragment not in known[page]:
                    problems.append(f"{name}: link to missing heading {target!r}")
    return problems


def build(out: Path) -> dict[str, str]:
    pages = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(PAGES.glob("*.md"))
        if path.name not in SKIPPED
    }
    for name, source in REFERENCES.items():
        text = (ROOT / source).read_text(encoding="utf-8")
        note = (
            f"> This page is generated from [`{source}`]({REPO_URL}/blob/main/{source}) "
            "in the repository. See [Choosing Tests](Choosing-Tests) for an overview "
            "of all tests.\n\n"
        )
        # the note goes below the page's title
        title, sep, rest = text.partition("\n")
        pages[name] = f"{title}{sep}\n{note}{rest.lstrip()}" if title.startswith("# ") else note + text

    if problems := broken_links(pages):
        sys.exit("Broken wiki links:\n  " + "\n  ".join(problems))

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name, text in pages.items():
        (out / f"{name}.md").write_text(text, encoding="utf-8")
    return pages


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[-1])
    built = build(Path(sys.argv[1]))
    print(f"Built {len(built)} wiki pages: {', '.join(built)}")
