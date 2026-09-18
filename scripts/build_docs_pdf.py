#!/usr/bin/env python3
"""Сборка PDF-версий документации из Markdown.

Markdown в ``docs/`` — источник правды, PDF собирается из него, чтобы версии
не расходились. Рендер идёт через headless Chromium (Playwright): он корректно
разбивает страницы, умеет колонтитулы и не портит псевдографику в блоках кода.

Использование::

    python scripts/build_docs_pdf.py                 # все документы
    python scripts/build_docs_pdf.py ARCHITECTURE    # только один
"""
from __future__ import annotations

import asyncio
import html as html_lib
import re
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = DOCS / "pdf"

DOCUMENTS = {
    "SPECIFICATION": "Техническое задание",
    "ARCHITECTURE": "Архитектура бэкенда",
    "PROTOCOL": "Протокол клиент ↔ сервер",
    "CLIENT_NETWORKING": "Сетевой слой тонкого клиента",
}

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

body {
  font-family: "DejaVu Sans", "Liberation Sans", "Noto Sans", sans-serif;
  font-size: 10.2pt;
  line-height: 1.55;
  color: #1c1c1e;
  margin: 0;
}

h1 {
  font-size: 19pt; line-height: 1.25; margin: 0 0 4mm 0;
  padding-bottom: 3mm; border-bottom: 2px solid #2b3a55; color: #16233a;
}
h2 {
  font-size: 14pt; margin: 9mm 0 3mm 0; color: #16233a;
  padding-bottom: 1.5mm; border-bottom: 1px solid #d5dae3;
  break-after: avoid; page-break-after: avoid;
}
h3 {
  font-size: 11.5pt; margin: 6mm 0 2mm 0; color: #2b3a55;
  break-after: avoid; page-break-after: avoid;
}
h4 { font-size: 10.5pt; margin: 5mm 0 1.5mm 0; color: #2b3a55; }

p { margin: 0 0 3mm 0; orphans: 3; widows: 3; }

ul, ol { margin: 0 0 3mm 0; padding-left: 6mm; }
li { margin-bottom: 1.2mm; }

a { color: #2b5fa8; text-decoration: none; }

code {
  font-family: "DejaVu Sans Mono", "Liberation Mono", monospace;
  font-size: 8.9pt;
  background: #f2f3f6;
  border: 1px solid #e2e5ec;
  border-radius: 3px;
  padding: 0.3mm 1.1mm;
}

pre {
  font-family: "DejaVu Sans Mono", "Liberation Mono", monospace;
  font-size: 8.2pt;
  line-height: 1.35;
  background: #f7f8fa;
  border: 1px solid #e2e5ec;
  border-left: 3px solid #2b3a55;
  border-radius: 3px;
  padding: 3mm 3.5mm;
  margin: 0 0 4mm 0;
  white-space: pre;
  overflow-wrap: normal;
  break-inside: avoid; page-break-inside: avoid;
}
pre code { background: none; border: none; padding: 0; font-size: inherit; }

table {
  border-collapse: collapse;
  width: 100%;
  margin: 0 0 4mm 0;
  font-size: 9.2pt;
}
/* длинные таблицы переносим по строкам, а не выбрасываем на новую страницу целиком */
tr { break-inside: avoid; page-break-inside: avoid; }
thead { display: table-header-group; }
/* служебная пустая шапка у таблиц-«карточек» не нужна */
thead tr:not(:has(th:not(:empty))) { display: none; }
th, td { border: 1px solid #d5dae3; padding: 1.7mm 2.2mm; text-align: left; vertical-align: top; }
th { background: #eef1f6; font-weight: 600; color: #16233a; }
tr:nth-child(even) td { background: #fafbfc; }
td code, th code { font-size: 8.4pt; }

blockquote {
  margin: 0 0 4mm 0;
  padding: 2.5mm 3.5mm;
  background: #fdf6e7;
  border-left: 3px solid #d8a531;
  border-radius: 3px;
}
blockquote p:last-child { margin-bottom: 0; }

hr { border: none; border-top: 1px solid #d5dae3; margin: 6mm 0; }

.cover { margin-bottom: 8mm; }
.cover .project { font-size: 9pt; letter-spacing: 0.12em; text-transform: uppercase; color: #6b7280; }
.cover .subtitle { font-size: 9.5pt; color: #6b7280; margin-top: 2mm; }
"""

HEADER = """
<div style="font-family:'DejaVu Sans',sans-serif;font-size:7pt;color:#9aa1ac;
            width:100%;padding:0 16mm;">
  <span style="float:left;">Neon Poker · {title}</span>
  <span style="float:right;">{subtitle}</span>
</div>
"""

FOOTER = """
<div style="font-family:'DejaVu Sans',sans-serif;font-size:7pt;color:#9aa1ac;
            width:100%;padding:0 16mm;text-align:center;">
  <span class="pageNumber"></span> / <span class="totalPages"></span>
</div>
"""


def strip_first_heading(text: str) -> tuple[str, str]:
    """Отделяет заголовок первого уровня — он уходит на «обложку» страницы."""
    lines = text.splitlines()
    title = ""
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            return title, "\n".join(lines[:i] + lines[i + 1 :])
    return title, text


def rewrite_internal_links(text: str) -> str:
    """Ссылки на соседние .md в PDF ведут в никуда — оставляем только текст."""
    return re.sub(r"\[([^\]]+)\]\((?!https?:)[^)]+\.md(?:#[^)]*)?\)", r"\1", text)


def to_html(md_path: Path, title: str) -> str:
    raw = md_path.read_text(encoding="utf-8")
    heading, body_md = strip_first_heading(raw)
    body_md = rewrite_internal_links(body_md)
    body = markdown.markdown(
        body_md,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>{html_lib.escape(heading or title)}</title>
<style>{CSS}</style></head>
<body>
<div class="cover">
  <div class="project">Neon Poker · бэкенд онлайн-покера</div>
  <h1>{html_lib.escape(heading or title)}</h1>
</div>
{body}
</body></html>"""


async def render(pairs: list[tuple[Path, Path, str]]) -> None:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        for html_path, pdf_path, title in pairs:
            await page.goto(html_path.as_uri(), wait_until="load")
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template=HEADER.format(
                    title=html_lib.escape(title), subtitle="документация"
                ),
                footer_template=FOOTER,
                margin={"top": "20mm", "bottom": "18mm", "left": "0", "right": "0"},
            )
            print(f"  {pdf_path.relative_to(ROOT)}")
        await browser.close()


def main() -> int:
    wanted = sys.argv[1:] or list(DOCUMENTS)
    tmp = ROOT / ".docs-html"
    tmp.mkdir(exist_ok=True)
    pairs: list[tuple[Path, Path, str]] = []

    for name in wanted:
        md_path = DOCS / f"{name}.md"
        if not md_path.exists():
            print(f"  пропускаю {name}: файла нет", file=sys.stderr)
            continue
        html_path = tmp / f"{name}.html"
        html_path.write_text(to_html(md_path, DOCUMENTS.get(name, name)), encoding="utf-8")
        pairs.append((html_path, OUT / f"{name}.pdf", DOCUMENTS.get(name, name)))

    if not pairs:
        print("нечего собирать", file=sys.stderr)
        return 1

    print(f"Собираю {len(pairs)} PDF…")
    asyncio.run(render(pairs))
    for path in tmp.glob("*.html"):
        path.unlink()
    tmp.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
