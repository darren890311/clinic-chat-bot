"""Turn the delivered documents into something a reader can open.

The Markdown files in `docs/` are the source. This renders each one to a
self-contained HTML page styled for print, which Chrome saves as a PDF
(File > Print > Save as PDF, margins "Default", background graphics on).

Deliberately no dependencies. Installing a Markdown library to typeset two
documents means the project's dependencies no longer describe the application,
and the subset used here — headings, bold, italics, tables, fenced blocks,
rules and paragraphs — is small enough to be worth the fifty lines.

The one thing that matters typographically is the architecture diagram: it is
drawn with characters, so it has to stay in a monospaced font at a size where
the boxes still line up. Everything else is ordinary prose.

    python -m scripts.render_docs
"""

from __future__ import annotations

import html
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

CSS = """
@page { size: A4; margin: 18mm 16mm; }
:root { --ink: #1a1d21; --muted: #5b6570; --rule: #d8dde2; --accent: #0f6d6d; }
* { box-sizing: border-box; }
body {
  font-family: "Charter", "Georgia", "Times New Roman", serif;
  font-size: 10.5pt; line-height: 1.55; color: var(--ink);
  max-width: 44rem; margin: 0 auto; padding: 2rem 1.5rem 4rem;
  -webkit-font-smoothing: antialiased;
}
h1 { font-size: 20pt; line-height: 1.2; margin: 0 0 .4rem; letter-spacing: -0.01em; }
h2 {
  font-size: 13.5pt; margin: 2.2rem 0 .7rem; padding-top: .9rem;
  border-top: 1px solid var(--rule); letter-spacing: -0.005em;
}
h3 { font-size: 11.5pt; margin: 1.5rem 0 .5rem; color: var(--accent); }
h2, h3 { break-after: avoid; }
p { margin: 0 0 .75rem; }
strong { font-weight: 600; }
em { color: var(--muted); }
hr { display: none; }           /* the h2 rule already separates sections */
table {
  width: 100%; border-collapse: collapse; margin: .8rem 0 1.1rem;
  font-size: 9.5pt; break-inside: avoid;
}
th, td { text-align: left; padding: .38rem .6rem; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { font-weight: 600; font-size: 8.5pt; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
tbody tr:last-child td { border-bottom: none; }
/* The diagram. Characters draw the boxes, so this must stay monospaced and
   must not wrap; the size is chosen so an 52-column drawing fits the page. */
pre {
  font-family: "SF Mono", "Menlo", "Consolas", monospace;
  font-size: 7.6pt; line-height: 1.32; white-space: pre;
  background: #f6f8f9; border: 1px solid var(--rule); border-radius: 4px;
  padding: .8rem 1rem; overflow: visible; break-inside: avoid; margin: 1rem 0 1.2rem;
}
ul { margin: 0 0 .75rem; padding-left: 1.2rem; }
li { margin-bottom: .2rem; }
.lede { color: var(--muted); font-size: 10pt; margin-bottom: 1.6rem; }
@media print { body { padding: 0; } }
"""


def inline(text: str) -> str:
    """Bold, italics and inline code, after escaping everything else."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    return out


def render(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i, para, first_para = 0, [], True

    def flush() -> None:
        nonlocal para, first_para
        if para:
            klass = ' class="lede"' if first_para and out and out[0].startswith("<h1") else ""
            out.append(f"<p{klass}>{inline(' '.join(para))}</p>")
            first_para = False
            para = []

    while i < len(lines):
        line = lines[i]

        if line.startswith("```"):
            flush()
            i += 1
            block = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(html.escape(lines[i]))
                i += 1
            out.append("<pre>" + "\n".join(block) + "</pre>")

        elif line.startswith("#"):
            flush()
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")

        elif line.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            i -= 1
            # Row two is the alignment separator and is dropped.
            head, body = rows[0], rows[2:] if len(rows) > 2 else []
            cells = "".join(f"<th>{inline(c)}</th>" for c in head)
            table = [f"<table><thead><tr>{cells}</tr></thead><tbody>"]
            for row in body:
                table.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
            table.append("</tbody></table>")
            out.append("".join(table))

        elif line.strip() in ("---", "***", "___"):
            flush()

        elif line.startswith(("- ", "* ")):
            flush()
            items = []
            while i < len(lines) and lines[i].startswith(("- ", "* ")):
                items.append(f"<li>{inline(lines[i][2:])}</li>")
                i += 1
            i -= 1
            out.append("<ul>" + "".join(items) + "</ul>")

        elif not line.strip():
            flush()

        else:
            para.append(line.strip())

        i += 1

    flush()
    return "\n".join(out)


def main() -> None:
    for source in sorted(DOCS.glob("*.md")):
        body = render(source.read_text())
        title = html.escape(source.stem.replace("-", " ").title())
        page = (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f"<title>{title}</title><style>{CSS}</style></head><body>\n{body}\n</body></html>\n"
        )
        target = source.with_suffix(".html")
        target.write_text(page)
        print(f"  {source.name} -> {target.name}")
    print("\n  Open in Chrome, then File > Print > Save as PDF.")
    print("  Margins: Default. Tick 'Background graphics'. Untick headers and footers.")


if __name__ == "__main__":
    main()
