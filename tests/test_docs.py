r"""Lint the repo's Markdown for math GitHub would mangle and for relative links that do not resolve.

GitHub runs its markdown pass before MathJax sees the page, and that broke docs/theory.md three ways (fixed in
67f7c11): backslash-punctuation inside $...$ and $$...$$ became a markdown escape, so \, reached MathJax as a bare
comma; < and > inside inline math were double-escaped and failed with "Misplaced &"; and a $ straight after a letter
or hyphen (linear-in-$k$) was not recognised as math at all. So display math goes in ```math fences, which GitHub
passes through verbatim, and inline spans use letter macros (\thinspace, \lt, \lbrace ...).

The checks take text, so the fixture tests at the end show each of those bugs, and broken links, being reported.
Each check runs over every file in one test and reports every violation as path:line: message.
Files come from git, never a directory walk: a checkout can hold whole other checkouts under .claude/worktrees/, a
.venv, recorded data and firmware build trees.
"""
import os
import re
import string
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
HTML_ANCHOR = re.compile(r"<[A-Za-z][^>]*\s(?:name|id)=\"([^\"]+)\"")
LINK = re.compile(r"!?\[([^\]]*)\]\(\s*(<[^>]*>|[^)\s]*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# Backslash-punctuation that GitHub unescapes, and the letter macro that survives its markdown pass.
MACROS = {",": r"\thinspace", ";": r"\thickspace", ":": r"\medspace", "!": r"\negthinspace", "{": r"\lbrace",
          "}": r"\rbrace", "|": r"\Vert", "<": r"\lt", ">": r"\gt"}
FENCE_HINT = "move the expression into a ```math fence"


# ---- reading Markdown -------------------------------------------------------------------------------------------

def prose_lines(text: str):
    """Yield (line number, line) for every line outside fenced code blocks (``` or ~~~, at any indent)."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if fence is None:
            # A backtick fence's info string cannot hold a backtick: ```x``` on one line is a code span.
            if match and not (match.group(1)[0] == "`" and "`" in line[match.end():]):
                fence = match.group(1)
            else:
                yield number, line
        elif (match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence)
              and not line[match.end():].strip()):
            fence = None


def blank_code_spans(line: str) -> str:
    """The line with each inline code span overwritten by NULs, so nothing inside it reads as math or a link."""
    return CODE_SPAN.sub(lambda m: "\0" * len(m.group(0)), line)


def _delimiter(line: str, i: int) -> bool:
    r"""A lone, unescaped $ (a $$ run is display math, and \$ is a literal dollar)."""
    backslashes = len(line[:i]) - len(line[:i].rstrip("\\"))
    return line[i] == "$" and backslashes % 2 == 0 and line[i - 1:i] != "$" and line[i + 1:i + 2] != "$"


def inline_math_spans(line: str) -> list[tuple[int, int]]:
    """(opening, closing) index pairs of the inline $...$ spans on a line whose code spans are blanked.

    Approximates GitHub's rule: the opening $ is not followed by whitespace, and the closing $ is not preceded by
    whitespace and not followed by a digit, on the same line. So "$5 and $10" and "$ x $" are not math.
    """
    dollars = [i for i in range(len(line)) if _delimiter(line, i)]
    spans, k = [], 0
    while k < len(dollars):
        opening = dollars[k]
        if line[opening + 1:opening + 2].strip():
            closer = next((m for m in range(k + 1, len(dollars))
                           if line[dollars[m] - 1].strip() and not line[dollars[m] + 1:dollars[m] + 2].isdigit()),
                          None)
            if closer is not None:
                spans.append((opening, dollars[closer]))
                k = closer + 1
                continue
        k += 1
    return spans


# ---- math ---------------------------------------------------------------------------------------------------------

def _span_problem(body: str) -> str | None:
    """What GitHub's markdown pass would change inside one inline span body, and the fix; None if nothing."""
    fixes, i = [], 0
    while i < len(body):
        if body[i] == "\\" and body[i + 1:i + 2] and body[i + 1] in string.punctuation:
            fixes.append(body[i:i + 2])
            i += 2
            continue
        if body[i] in "<>":
            fixes.append(body[i])
        i += 1
    if not fixes:
        return None
    hints, note = [], ""
    for found in dict.fromkeys(fixes):
        macro = MACROS.get(found[-1]) if found != "\\\\" else None
        hints.append(f"`{found}` -> `{macro}`" if macro else f"`{found}` has no macro, {FENCE_HINT}")
        note = note or (" (put a space after the macro when a letter follows)" if macro else "")
    shown = body if len(body) <= 60 else body[:57] + "..."
    return f"inline math ${shown}$: GitHub's markdown pass changes it before MathJax runs; {'; '.join(hints)}{note}"


def check_math(text: str) -> list[tuple[int, str]]:
    """(line number, message) for each display $$ block and each inline span that GitHub would not render as written."""
    problems, display_start = [], None
    for number, raw in prose_lines(text):
        line = blank_code_spans(raw)
        stripped = line.strip()
        if display_start is not None:
            if stripped.endswith("$$"):
                problems.append((display_start, f"display math $$ (lines {display_start}-{number}): GitHub applies "
                                                f"markdown escapes inside it; use a ```math fence instead"))
                display_start = None
            continue
        if stripped.startswith("$$"):
            if stripped == "$$":
                display_start = number
            else:
                problems.append((number, "display math $$...$$: GitHub applies markdown escapes inside it; "
                                         "use a ```math fence instead"))
            continue
        for opening, closing in inline_math_spans(line):
            body = line[opening + 1:closing]
            before = line[opening - 1:opening]
            if before and re.match(r"[\w-]", before):
                problems.append((number, f"inline math ${body}$ right after {before!r} is not recognised as math by "
                                         f"GitHub and renders as literal dollars; put a space before the $ or reword"))
            problem = _span_problem(body)
            if problem:
                problems.append((number, problem))
    if display_start is not None:
        problems.append((display_start, "display math $$ is never closed; use a ```math fence instead"))
    return problems


# ---- links --------------------------------------------------------------------------------------------------------

def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, drop all but word characters, spaces and hyphens, spaces -> hyphens."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link or image keeps only its text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML tags render as nothing
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def heading_anchors(text: str) -> frozenset[str]:
    """Every #anchor a Markdown file defines: heading slugs (repeats get -1, -2, ...) and HTML name/id attributes."""
    anchors, seen, previous, last = {a.lower() for a in HTML_ANCHOR.findall(text)}, {}, "", 0
    for number, line in prose_lines(text):
        if number != last + 1:  # a code fence came between: no setext heading across it
            previous = ""
        last = number
        heading = ATX_HEADING.match(line)
        if heading:
            title = heading.group(1) or ""
        elif previous.strip() and SETEXT_UNDERLINE.match(line) and not re.match(r"\s*([-*+>|#]|\d+[.)])", previous):
            title = previous
        else:
            previous = line
            continue
        base = slug(title)
        anchors.add(f"{base}-{seen[base]}" if base in seen else base)
        seen[base] = seen.get(base, 0) + 1
        previous = ""
    return frozenset(anchors)


@lru_cache(maxsize=None)
def file_anchors(path: Path) -> frozenset[str]:
    return heading_anchors(path.read_text(encoding="utf-8"))


def check_links(text: str, source: Path, root: Path) -> list[tuple[int, str]]:
    """(line number, message) for each relative link in `text` (the contents of `source`) that does not resolve.

    A path is resolved against the source file's folder (or `root` when it starts with /) and must exist inside
    `root`. An #anchor must match a heading of the target when the target is Markdown; anchors into other files
    (#L10) are not checked. Links in code, and URLs with a scheme (https:, mailto:), are skipped.
    """
    problems, own_anchors = [], None
    root, source = Path(os.path.normpath(root)), Path(os.path.normpath(source))
    for number, raw in prose_lines(text):
        line = blank_code_spans(raw)
        for opening, closing in inline_math_spans(line):
            line = line[:opening] + "\0" * (closing + 1 - opening) + line[closing + 1:]
        for match in LINK.finditer(line):
            target = match.group(2).strip("<>")
            if not target or URI_SCHEME.match(target) or target.startswith("//"):
                continue
            link = match.group(0)
            path_part, _, anchor = target.partition("#")
            path_part = unquote(path_part.split("?")[0])
            if path_part:
                base = root if path_part.startswith("/") else source.parent
                # Lexically, as GitHub resolves it; Path.resolve() would cost a system call per link on Windows.
                dest = Path(os.path.normpath(base / path_part.lstrip("/")))
                if not dest.is_relative_to(root):
                    problems.append((number, f"link {link}: {path_part} points outside the repository"))
                    continue
                if not dest.exists():
                    problems.append((number, f"link {link}: {path_part} does not exist"))
                    continue
            else:
                dest = source
            if not anchor or dest.suffix.lower() != ".md" or not (dest == source or dest.is_file()):
                continue
            if dest != source:
                anchors = file_anchors(dest)
            else:
                anchors = own_anchors = heading_anchors(text) if own_anchors is None else own_anchors
            if unquote(anchor).lower() not in anchors:
                problems.append((number, f"link {link}: no heading in {dest.name} has the anchor #{anchor}"))
    return problems


# ---- the repo's Markdown ----------------------------------------------------------------------------------------

def markdown_files() -> list[str] | None:
    """Markdown files git knows about (tracked, or new and not ignored), relative to the repo root; None without git."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
            capture_output=True, encoding="utf-8", check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = {name for name in listing.split("\0") if name and not name.startswith(".claude/")}
    return sorted(name for name in names if (ROOT / name).is_file())


MARKDOWN = markdown_files()


def report(name: str, problems: list[tuple[int, str]]) -> str:
    return "\n".join(f"{name}:{number}: {message}" for number, message in problems)


def markdown_texts():
    """(name, path, text) for every collected file; skips the calling test when git could not list them."""
    if MARKDOWN is None:
        pytest.skip("git could not list the Markdown files")
    return [(name, ROOT / name, (ROOT / name).read_text(encoding="utf-8")) for name in MARKDOWN]


# One test per check over every file, not one per file, so adding a doc does not change the suite's test count.

def test_the_docs_are_collected():
    names = [name for name, _, _ in markdown_texts()]
    assert {"README.md", "docs/theory.md", "docs/architecture.md"} <= set(names)
    assert not [name for name in names if name.startswith((".claude/", ".venv/", "data/"))]


def test_math_renders_on_github_as_written():
    failures = [report(name, check_math(text)) for name, _, text in markdown_texts()]
    failures = [failure for failure in failures if failure]
    assert not failures, "\n" + "\n".join(failures)


def test_relative_links_and_anchors_resolve():
    failures = [report(name, check_links(text, path, ROOT)) for name, path, text in markdown_texts()]
    failures = [failure for failure in failures if failure]
    assert not failures, "\n" + "\n".join(failures)


# ---- the checks catch what broke theory.md ------------------------------------------------------------------------

BROKEN_MATH = r"""# Fixture

$$
H_k=\sum_p a_p\,e^{-j2\pi f_k\tau_p}
$$

Escapes: $f_k=f_c+k\,\Delta f$ and $\cos\!\Big(x\Big)$ and $I_k\in\{-128,\dots,127\}$ and $\|x\|$.
Comparisons: $S>\theta$ and $\lvert a\rvert<1$.
No macro: $a\\b$ and $50\%$.
Glued: linear-in-$k$ phase and x$y$.
"""

CLEAN_MATH = r"""# Fixture

Shell: `. $IDF_PATH/export.sh` and `sudo usermod -aG dialout $USER`, or ``$a\,b$``.

```bash
. $IDF_PATH/export.sh   # $$ and $a\,b$ are code here
$$
```

~~~
$x<y$
~~~

Money: it costs $5 and $10, or $ 3 $, or \$4.
Math: $S\gt \theta$, $1.25\thinspace Q_{0.99}$, $\lbrace 1 \rbrace$, $a\ b$, $x$-th, ($k$), $2^{32}$.

```math
H = a\,b + \{c\} \\ x<y
```
"""


def test_each_math_bug_that_broke_theory_md_is_reported():
    problems = check_math(BROKEN_MATH)
    expected = [
        (3, "display math $$ (lines 3-5)"),
        (7, r"`\,` -> `\thinspace`"),
        (7, r"`\!` -> `\negthinspace`"),
        (7, r"`\{` -> `\lbrace`; `\}` -> `\rbrace`"),
        (7, r"`\|` -> `\Vert`"),
        (8, r"`>` -> `\gt`"),
        (8, r"`<` -> `\lt`"),
        (9, r"`\\` has no macro, move the expression into a ```math fence"),
        (9, r"`\%` has no macro"),
        (10, "inline math $k$ right after '-' is not recognised as math"),
        (10, "inline math $y$ right after 'x' is not recognised as math"),
    ]
    assert len(problems) == len(expected), "\n" + report("fixture", problems)
    for (number, message), (want_number, fragment) in zip(problems, expected):
        assert number == want_number and fragment in message, (number, message, fragment)


def test_math_that_github_renders_and_dollars_in_code_are_not_reported():
    assert check_math(CLEAN_MATH) == []


def test_heading_slugs_follow_githubs_rules():
    assert slug("6. Testing and quality") == "6-testing-and-quality"
    assert slug("3.1 One source seam: `(host_ts, line)`") == "31-one-source-seam-host_ts-line"
    assert slug("Foo -- [Bar](x.md) <br>") == "foo----bar"
    text = "# Setup\n\n## Setup\n\nTitle\n=====\n\n```\n# not a heading\n```\n\n## Setup\n"
    assert heading_anchors(text) == {"setup", "setup-1", "title", "setup-2"}


def test_broken_links_and_anchors_are_reported(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "other.md").write_text("# Other\n\n## Setup\n\n## Setup\n", encoding="utf-8")
    (root / "tool.py").write_text("", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")
    page = r"""# Page

## Repeated

## Repeated

Fine: [a](#page) [b](#repeated-1) [c](other.md#setup-1) [d](../tool.py#L10) [e](./) [f](/docs/other.md#other)
Fine: [g](https://example.com/x.md#nope) [h](mailto:a@b.c) `[i](missing.md)` ![j](other.md "title")
Broken: [k](#nope) [l](missing.md) [m](other.md#setup-2) [n](../../outside.md)
"""
    problems = check_links(page, root / "docs" / "page.md", root)
    assert [(number, message.split(": ", 1)[1]) for number, message in problems] == [
        (9, "no heading in page.md has the anchor #nope"),
        (9, "missing.md does not exist"),
        (9, "no heading in other.md has the anchor #setup-2"),
        (9, "../../outside.md points outside the repository"),
    ]
