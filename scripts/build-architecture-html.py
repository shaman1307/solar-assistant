"""Generate docs/energy-arbitrage-architecture.html from the Markdown source."""

from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "energy-arbitrage-architecture.md"
HTML_PATH = ROOT / "docs" / "energy-arbitrage-architecture.html"

MERMAID_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def _inline_md(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )
    return text


def _render_table(lines: list[str]) -> str:
    rows = [line.strip() for line in lines if line.strip()]
    if len(rows) < 2:
        return ""
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    body_rows = []
    for row in rows[2:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        body_rows.append(cells)
    out = ["<table>", "<thead><tr>"]
    for cell in header:
        out.append(f"<th>{_inline_md(cell)}</th>")
    out.append("</tr></thead><tbody>")
    for cells in body_rows:
        out.append("<tr>")
        for cell in cells:
            out.append(f"<td>{_inline_md(cell)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _render_markdown_chunk(chunk: str) -> str:
    lines = chunk.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        if line.strip() == "---":
            out.append("<hr>")
            i += 1
            continue

        if line.startswith("> "):
            quote_lines = []
            while i < len(lines) and lines[i].startswith("> "):
                quote_lines.append(lines[i][2:])
                i += 1
            out.append(f"<blockquote><p>{'<br>'.join(_inline_md(l) for l in quote_lines)}</p></blockquote>")
            continue

        if line.startswith("# "):
            out.append(f"<h1>{_inline_md(line[2:])}</h1>")
            i += 1
            continue
        if line.startswith("## "):
            out.append(f"<h2>{_inline_md(line[3:])}</h2>")
            i += 1
            continue
        if line.startswith("### "):
            out.append(f"<h3>{_inline_md(line[4:])}</h3>")
            i += 1
            continue

        if line.strip().startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            out.append(_render_table(table_lines))
            continue

        if re.match(r"^\d+\.\s", line):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i]):
                items.append(re.sub(r"^\d+\.\s", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline_md(it)}</li>" for it in items) + "</ol>")
            continue

        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", ">", "|", "-")) and not re.match(r"^\d+\.\s", lines[i]):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{'<br>'.join(_inline_md(l) for l in para)}</p>")

    return "\n".join(out)


def md_to_html_body(md: str) -> str:
    parts: list[str] = []
    last = 0
    for match in MERMAID_RE.finditer(md):
        before = md[last:match.start()]
        if before.strip():
            parts.append(_render_markdown_chunk(before))
        diagram = match.group(1).strip()
        parts.append(f'<pre class="mermaid">{diagram}</pre>')
        last = match.end()
    tail = md[last:]
    if tail.strip():
        parts.append(_render_markdown_chunk(tail))
    return "\n".join(parts)


def build_html(md: str) -> str:
    body = md_to_html_body(md)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Energy Arbitrage — architecture</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, sans-serif;
      max-width: 980px;
      margin: 2rem auto;
      padding: 0 1.25rem 3rem;
      line-height: 1.55;
      color: #1f2328;
    }}
    h1 {{ border-bottom: 1px solid #d0d7de; padding-bottom: 0.3em; }}
    h2 {{ margin-top: 2rem; border-bottom: 1px solid #eaeef2; padding-bottom: 0.2em; }}
    pre.mermaid {{
      background: #f6f8fa;
      border: 1px solid #d0d7de;
      border-radius: 8px;
      padding: 1rem;
      overflow-x: auto;
      margin: 1.25rem 0;
    }}
    table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.95rem; }}
    th, td {{ border: 1px solid #d0d7de; padding: 0.45rem 0.65rem; text-align: left; vertical-align: top; }}
    th {{ background: #f6f8fa; }}
    code {{ background: #f0f0f0; padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.9em; }}
    blockquote {{
      border-left: 4px solid #0969da;
      margin: 1rem 0;
      padding: 0.5rem 1rem;
      background: #f6f8fa;
      color: #424a53;
    }}
    hr {{ border: none; border-top: 1px solid #d0d7de; margin: 2rem 0; }}
    a {{ color: #0969da; }}
  </style>
</head>
<body>
{body}
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: "neutral",
    securityLevel: "loose",
    flowchart: {{ htmlLabels: true }},
  }});
</script>
</body>
</html>
"""


def main() -> None:
    md = MD_PATH.read_text(encoding="utf-8")
    html_out = build_html(md)
    HTML_PATH.write_text(html_out, encoding="utf-8")
    n_diagrams = len(MERMAID_RE.findall(md))
    print(f"Wrote {HTML_PATH} ({len(html_out)} bytes, {n_diagrams} mermaid diagrams)")


if __name__ == "__main__":
    main()
