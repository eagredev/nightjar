"""Stdio JSON-RPC 2.0 MCP server exposing one tool: render_markdown.

Spawned as a child of `claude -p` via `--mcp-config`, alongside the
existing nightjar-reply MCP. The agent calls:

  - `render_markdown(input_path, format, output_path?)` zero-or-more
    times when it wants to convert a markdown file to something a
    phone mail client will render. Returns the absolute path to the
    rendered output, which the agent then passes to attach_to_reply.

Why a separate server and not a method on nightjar-reply: render is
pure compute with no audit-log dependency, no per-turn state, and
no daemon round-trip. Keeping it isolated means it can ship without
touching the reply contract and can be used standalone via the
CLI mode for humans and tests.

Stdlib only on the critical path. The `markdown` package is used
when importable for higher-fidelity HTML; otherwise a stdlib
fallback renderer fires. PDF output shells out to wkhtmltopdf and
returns a clear error when the binary is absent.
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import shutil
import stat as stat_mod
import subprocess
import sys
import tempfile
import uuid


PROTOCOL_VERSION_DEFAULT = "2024-11-05"
SERVER_NAME = "nightjar-render"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 error codes (subset).
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


VALID_FORMATS = ("html", "text", "pdf")


RENDER_MARKDOWN_TOOL_SCHEMA = {
    "name": "render_markdown",
    "description": (
        "Convert a markdown file to HTML, plain text, or PDF and "
        "return the absolute path to the rendered output. Use this "
        "when the principal will be reading on a phone and the "
        "source is .md — mobile mail clients do not render markdown, "
        "but they do render HTML inline. Do NOT call this for every "
        "attachment; only when format conversion is genuinely "
        "useful. If the principal asked for the raw .md, attach the "
        "raw .md.\n"
        "\n"
        "Formats: 'html' (recommended for phone reading), 'text' "
        "(strip markdown syntax), 'pdf' (requires wkhtmltopdf; "
        "returns an error if missing — use html in that case). "
        "Output is written to a /tmp tempfile by default; supply "
        "output_path for a specific destination. Pass the returned "
        "path to attach_to_reply to send the rendered file."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "input_path": {
                "type": "string",
                "description": (
                    "Absolute path to the markdown file to convert. "
                    "Must exist and be a regular readable file."
                ),
            },
            "format": {
                "type": "string",
                "enum": list(VALID_FORMATS),
                "description": (
                    "Output format: 'html', 'text', or 'pdf'."
                ),
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Optional absolute path for the rendered output. "
                    "Defaults to /tmp/nightjar-render-<uuid>.<ext>. "
                    "Parent directory must exist and be writable."
                ),
            },
        },
        "required": ["input_path", "format"],
    },
}


ALL_TOOL_SCHEMAS = [RENDER_MARKDOWN_TOOL_SCHEMA]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  max-width: 720px;
  margin: 1.5rem auto;
  padding: 0 1rem;
  line-height: 1.5;
  color: #222;
}}
h1, h2, h3, h4, h5, h6 {{ line-height: 1.2; margin-top: 1.4em; }}
h1 {{ font-size: 1.7em; }}
h2 {{ font-size: 1.4em; }}
h3 {{ font-size: 1.2em; }}
pre {{
  background: #f4f4f4;
  padding: 0.75rem;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 0.95em;
}}
code {{
  font-family: ui-monospace, "Cascadia Code", Menlo, monospace;
  background: #f4f4f4;
  padding: 0.1em 0.3em;
  border-radius: 3px;
}}
pre code {{ background: none; padding: 0; }}
blockquote {{
  border-left: 3px solid #ccc;
  margin: 0;
  padding: 0.25rem 1rem;
  color: #555;
}}
a {{ color: #0a58ca; }}
ul, ol {{ padding-left: 1.5em; }}
hr {{ border: none; border-top: 1px solid #ddd; margin: 2em 0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _render_markdown_to_html(md_text: str, title: str) -> str:
    """Render markdown to a standalone HTML document.

    Prefers the `markdown` package when importable for higher-fidelity
    output (tables, footnotes, smartypants). Falls back to a stdlib
    mini-renderer that handles the common cases: headings, lists,
    bold/italic, inline code, fenced code, links, paragraphs.

    The decision is made per-call (cheap) so tests can patch the
    import. Production usage either has the package or doesn't; no
    runtime switching.
    """
    try:
        import markdown as md_pkg
        body = md_pkg.markdown(
            md_text,
            extensions=["extra", "sane_lists", "smarty"],
        )
    except ImportError:
        body = _stdlib_markdown_to_html(md_text)
    return _HTML_TEMPLATE.format(title=html_lib.escape(title), body=body)


_FENCED_CODE_RE = re.compile(
    r"^```([^\n]*)\n(.*?)\n```\s*$",
    re.MULTILINE | re.DOTALL,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<![*_])[*_]([^*_\n]+)[*_](?![*_])")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _stdlib_markdown_to_html(md_text: str) -> str:
    """Minimal markdown->HTML for when the `markdown` package is absent.

    Covers headings, unordered/ordered lists, bold/italic, inline
    code, fenced code blocks, links, and paragraphs. Tables, footnotes,
    and other extensions render as raw text — good enough for the
    common case (schedules, notes, briefings).
    """
    placeholders: list[str] = []

    def _stash(html: str) -> str:
        token = f"\x00PH{len(placeholders)}\x00"
        placeholders.append(html)
        return token

    def _fenced_sub(match: re.Match) -> str:
        lang = match.group(1).strip()
        code = html_lib.escape(match.group(2))
        cls = f' class="language-{html_lib.escape(lang)}"' if lang else ""
        return _stash(f"<pre><code{cls}>{code}</code></pre>")

    text = _FENCED_CODE_RE.sub(_fenced_sub, md_text)
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    in_ul = False
    in_ol = False

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def _inline(s: str) -> str:
        s = html_lib.escape(s)
        s = _INLINE_CODE_RE.sub(
            lambda m: f"<code>{m.group(1)}</code>", s,
        )
        s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
        s = _ITALIC_RE.sub(r"<em>\1</em>", s)
        s = _LINK_RE.sub(
            lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s,
        )
        return s

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            _close_lists()
            i += 1
            continue
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            _close_lists()
            level = len(heading_match.group(1))
            text_content = _inline(heading_match.group(2))
            out.append(f"<h{level}>{text_content}</h{level}>")
            i += 1
            continue
        ul_match = re.match(r"^[-*+]\s+(.+)$", line)
        if ul_match:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(ul_match.group(1))}</li>")
            i += 1
            continue
        ol_match = re.match(r"^\d+\.\s+(.+)$", line)
        if ol_match:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline(ol_match.group(1))}</li>")
            i += 1
            continue
        # Paragraph: gather consecutive non-empty non-special lines.
        _close_lists()
        para_lines = [line]
        i += 1
        while (i < len(lines) and lines[i].strip()
               and not _HEADING_RE.match(lines[i])
               and not re.match(r"^[-*+]\s+", lines[i])
               and not re.match(r"^\d+\.\s+", lines[i])):
            para_lines.append(lines[i])
            i += 1
        para = " ".join(p.strip() for p in para_lines)
        out.append(f"<p>{_inline(para)}</p>")

    _close_lists()
    rendered = "\n".join(out)
    # Restore stashed code blocks (which contain HTML we don't want
    # the inline pipeline to re-escape).
    for idx, html in enumerate(placeholders):
        rendered = rendered.replace(f"\x00PH{idx}\x00", html)
    return rendered


def _render_markdown_to_text(md_text: str) -> str:
    """Strip markdown syntax to readable plain text.

    Heuristic, not a full parser:
    - Headings -> UPPERCASE line + blank line.
    - List markers preserved as `- ` and numbered.
    - Bold/italic/inline-code delimiters stripped.
    - Fenced code blocks pass through verbatim minus the fences.
    - Links flattened to `text (url)`.
    """
    # Strip fenced code blocks first, preserving inner text.
    def _fenced_sub(match: re.Match) -> str:
        return match.group(2)
    text = _FENCED_CODE_RE.sub(_fenced_sub, md_text)

    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            content = heading.group(2)
            content = _BOLD_RE.sub(r"\1", content)
            content = _ITALIC_RE.sub(r"\1", content)
            content = _INLINE_CODE_RE.sub(r"\1", content)
            content = _LINK_RE.sub(r"\1 (\2)", content)
            out.append(content.upper())
            out.append("")
            continue
        line = _BOLD_RE.sub(r"\1", line)
        line = _ITALIC_RE.sub(r"\1", line)
        line = _INLINE_CODE_RE.sub(r"\1", line)
        line = _LINK_RE.sub(r"\1 (\2)", line)
        out.append(line)
    return "\n".join(out)


def _render_markdown_to_pdf(md_text: str, title: str, output_path: str) -> None:
    """Render markdown -> HTML -> PDF via wkhtmltopdf.

    Raises FileNotFoundError if wkhtmltopdf is not on PATH (caller
    converts this to an isError tool result). Raises
    subprocess.CalledProcessError if the conversion itself fails.
    """
    if shutil.which("wkhtmltopdf") is None:
        raise FileNotFoundError("wkhtmltopdf")
    html = _render_markdown_to_html(md_text, title)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False, encoding="utf-8",
    ) as fh:
        fh.write(html)
        html_path = fh.name
    try:
        subprocess.run(
            ["wkhtmltopdf", "--quiet", html_path, output_path],
            check=True,
            capture_output=True,
        )
    finally:
        try:
            os.unlink(html_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_EXT_BY_FORMAT = {"html": "html", "text": "txt", "pdf": "pdf"}


def _default_output_path(fmt: str) -> str:
    ext = _EXT_BY_FORMAT[fmt]
    return os.path.join(
        tempfile.gettempdir(),
        f"nightjar-render-{uuid.uuid4().hex}.{ext}",
    )


def _validate_input_path(path) -> tuple[bool, str]:
    """Return (ok, error_message). Mirrors attach_to_reply's
    fail-at-the-boundary posture."""
    if not isinstance(path, str):
        return False, "input_path must be a string"
    if not path.startswith("/"):
        return False, f"input_path must be absolute, got {path!r}"
    try:
        st = os.stat(path)
    except OSError as exc:
        return False, f"cannot stat input_path {path!r}: {exc}"
    if not stat_mod.S_ISREG(st.st_mode):
        return False, f"input_path {path!r} is not a regular file"
    if not os.access(path, os.R_OK):
        return False, f"input_path {path!r} is not readable"
    return True, ""


def _validate_output_path(path) -> tuple[bool, str]:
    if not isinstance(path, str):
        return False, "output_path must be a string"
    if not path.startswith("/"):
        return False, f"output_path must be absolute, got {path!r}"
    parent = os.path.dirname(path) or "/"
    if not os.path.isdir(parent):
        return False, f"output_path parent {parent!r} does not exist"
    if not os.access(parent, os.W_OK):
        return False, f"output_path parent {parent!r} is not writable"
    return True, ""


# ---------------------------------------------------------------------------
# Core conversion (used by both MCP and CLI paths)
# ---------------------------------------------------------------------------

class RenderError(Exception):
    """User-facing render failure (e.g. wkhtmltopdf missing)."""


def render_file(input_path: str, fmt: str, output_path: str | None) -> str:
    """Read input_path, convert to fmt, write to output_path (or a
    default tempfile), return the output path.

    Raises:
        ValueError on invalid input/output paths or unknown format.
        RenderError on user-facing render failures (PDF without
            wkhtmltopdf, conversion errors).
    """
    if fmt not in VALID_FORMATS:
        raise ValueError(
            f"format must be one of {VALID_FORMATS}, got {fmt!r}"
        )
    ok, msg = _validate_input_path(input_path)
    if not ok:
        raise ValueError(msg)
    if output_path is None:
        output_path = _default_output_path(fmt)
    else:
        ok, msg = _validate_output_path(output_path)
        if not ok:
            raise ValueError(msg)

    with open(input_path, encoding="utf-8") as fh:
        md_text = fh.read()

    title = os.path.basename(input_path)

    if fmt == "html":
        html = _render_markdown_to_html(md_text, title)
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(html)
    elif fmt == "text":
        text = _render_markdown_to_text(md_text)
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    elif fmt == "pdf":
        try:
            _render_markdown_to_pdf(md_text, title, output_path)
        except FileNotFoundError:
            raise RenderError(
                "wkhtmltopdf not installed; render to html or text "
                "instead. On Arch/SteamOS, install via pacman or a "
                "flatpak; on the immutable rootfs you may need an "
                "overlay or a flatpak."
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise RenderError(
                f"wkhtmltopdf failed (exit {exc.returncode}): {stderr.strip() or 'no stderr'}"
            )
    return output_path


# ---------------------------------------------------------------------------
# MCP wire protocol (same shape as compose_reply_mcp.py)
# ---------------------------------------------------------------------------

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })


def _handle_initialize(req_id, params: dict) -> None:
    client_pv = params.get("protocolVersion")
    pv = client_pv if isinstance(client_pv, str) else PROTOCOL_VERSION_DEFAULT
    _result(req_id, {
        "protocolVersion": pv,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })


def _handle_tools_list(req_id) -> None:
    _result(req_id, {"tools": ALL_TOOL_SCHEMAS})


def _handle_render_markdown(req_id, args: dict) -> None:
    input_path = args.get("input_path")
    fmt = args.get("format")
    output_path = args.get("output_path")

    if not isinstance(fmt, str) or fmt not in VALID_FORMATS:
        _error(req_id, ERR_INVALID_PARAMS,
               f"render_markdown `format` must be one of {VALID_FORMATS}, "
               f"got {fmt!r}")
        return
    if output_path is not None and not isinstance(output_path, str):
        _error(req_id, ERR_INVALID_PARAMS,
               "render_markdown `output_path` must be a string if provided")
        return

    try:
        result_path = render_file(input_path, fmt, output_path)
    except ValueError as exc:
        _error(req_id, ERR_INVALID_PARAMS, f"render_markdown: {exc}")
        return
    except RenderError as exc:
        # User-facing render failure: surface as tool isError so the
        # agent can fall back (e.g. HTML when PDF deps are missing).
        _result(req_id, {
            "content": [{"type": "text", "text": str(exc)}],
            "isError": True,
        })
        return
    except OSError as exc:
        _error(req_id, ERR_INTERNAL, f"render_markdown I/O error: {exc}")
        return

    _result(req_id, {
        "content": [{"type": "text", "text": result_path}],
        "isError": False,
    })


def _handle_tools_call(req_id, params: dict) -> None:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "render_markdown":
        _handle_render_markdown(req_id, args)
    else:
        _error(req_id, ERR_METHOD_NOT_FOUND, f"unknown tool: {name!r}")


def _dispatch(message: dict) -> None:
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}
    is_notification = req_id is None

    if method == "initialize":
        if is_notification:
            return
        _handle_initialize(req_id, params)
    elif method == "notifications/initialized":
        return
    elif method == "tools/list":
        if is_notification:
            return
        _handle_tools_list(req_id)
    elif method == "tools/call":
        if is_notification:
            return
        _handle_tools_call(req_id, params)
    else:
        if is_notification:
            return
        _error(req_id, ERR_METHOD_NOT_FOUND, f"method not found: {method!r}")


def _mcp_loop() -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _error(None, ERR_PARSE, "parse error")
            continue
        if not isinstance(message, dict):
            _error(None, ERR_INVALID_REQUEST, "request must be an object")
            continue
        try:
            _dispatch(message)
        except Exception as exc:  # noqa: BLE001
            req_id = message.get("id")
            _error(req_id, ERR_INTERNAL, f"internal error: {exc}")
    return 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="render_markdown_mcp",
        description=(
            "Convert a markdown file to HTML, plain text, or PDF. "
            "Defaults to MCP stdio mode when no --convert is given."
        ),
    )
    parser.add_argument(
        "--convert",
        metavar="PATH",
        help="Path to a markdown file to convert; switches to CLI mode.",
    )
    parser.add_argument(
        "--to",
        choices=VALID_FORMATS,
        default="html",
        help="Output format (default: html).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="Explicit output path; otherwise a /tmp tempfile is used.",
    )
    args = parser.parse_args(argv)

    if args.convert is None:
        return _mcp_loop()

    try:
        out_path = render_file(args.convert, args.to, args.out)
    except ValueError as exc:
        print(f"render_markdown: {exc}", file=sys.stderr)
        return 2
    except RenderError as exc:
        print(f"render_markdown: {exc}", file=sys.stderr)
        return 3
    print(out_path)
    return 0


def main() -> int:
    return _cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
