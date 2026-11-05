"""Baut das Handbuch aus handbuch/handbuch_src.html und Teilen der Geräteauswahl.

    python docs/build_docs.py            schreibt docs/handbuch/handbuch.html
    node docs/render_pdf.js              macht daraus die PDFs (braucht Playwright)

Geräte, Schaltbilder und Checkliste stehen nur einmal, in geraeteauswahl/geraeteauswahl.html.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent
GA = DOCS / "geraeteauswahl" / "geraeteauswahl.html"
SRC = DOCS / "handbuch" / "handbuch_src.html"
OUT = DOCS / "handbuch" / "handbuch.html"

EXTRA_CSS = """
  h1 { font-size: 19pt; margin: 0 0 8pt; padding-bottom: 4pt; border-bottom: 2pt solid var(--accent); color: var(--accent);
       break-before: page; break-after: avoid; }
  h1 .num { display: inline-block; min-width: 26pt; }
  h1.cover { font-size: 26pt; border: none; color: var(--ink); break-before: auto; margin-top: 40pt; }
  .toc a { color: var(--ink); text-decoration: none; }
  .formula-block { font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 9pt; background: var(--soft);
       border-left: 3pt solid var(--accent); padding: 7pt 10pt; margin: 6pt 0 10pt; line-height: 1.7; break-inside: avoid; }
  pre.code { font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 8.2pt; background: var(--soft);
       border: 1pt solid var(--line); padding: 7pt 9pt; white-space: pre-wrap; break-inside: avoid; margin: 4pt 0 9pt; }
  @media screen { body { max-width: 900px; margin: 0 auto; padding: 24px; } h1 { margin-top: 36px; } }
"""


def part(html: str, start: str, end: str) -> str:
    a = html.index(start)
    b = html.index(end, a)
    s = html[a:b]
    s = re.sub(r"<h2([^>]*)>\d+ ", r"<h2\1>", s)                       # Nummern der Geräteauswahl weg
    s = s.replace('<h2 class="page">', "<h2>", 1)                      # das Kapitel bricht schon um
    return s


def main():
    ga = GA.read_text(encoding="utf-8")
    css = ga[ga.index("<style>") + 7: ga.index("</style>")]
    src = SRC.read_text(encoding="utf-8")
    geraete = part(ga, "<!-- ============================ 1 GRUNDREGELN", "<!-- ============================ 8 SCHALTBILDER")
    schalt = part(ga, "<!-- ============================ 8 SCHALTBILDER", "<!-- ============================ 10 FREQUENZ")
    check = part(ga, "<!-- ============================ 13 CHECKLISTE", "<!-- ============================ 14 SOFTWARE")
    check = re.sub(r"<h2[^>]*>[^<]*</h2>", "", check, count=1)
    body = src.replace("{{GERAETE}}", geraete).replace("{{SCHALTBILDER}}", schalt).replace("{{CHECKLISTE}}", check)
    html = f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StressCell Handbuch</title>
<style>{css}{EXTRA_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""
    OUT.write_text(html, encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
