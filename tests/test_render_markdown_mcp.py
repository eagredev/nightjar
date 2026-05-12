"""Tests for daemon/render_markdown_mcp.py.

Exercises both the CLI path (render_file + the argparse front-door)
and the MCP stdio JSON-RPC loop. Stdlib I/O only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from daemon import render_markdown_mcp as rm


SERVER_PATH = (
    Path(__file__).resolve().parent.parent
    / "daemon" / "render_markdown_mcp.py"
)


SAMPLE_MD = """# Heading 1

Some intro paragraph with **bold** and *italic* and `inline code`.

## Heading 2

- bullet one
- bullet two with [a link](https://example.com)

1. ordered one
2. ordered two

```python
def hello():
    return "world"
```

> a blockquote line
"""


# ---------------------------------------------------------------------------
# In-process render_file / render-helper tests
# ---------------------------------------------------------------------------

def test_render_file_html_writes_document(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text(SAMPLE_MD)
    out = rm.render_file(str(src), "html", None)
    assert out.endswith(".html")
    assert os.path.exists(out)
    html = Path(out).read_text()
    assert "<!DOCTYPE html>" in html
    assert "<h1>" in html
    assert "Heading 1" in html
    assert "<strong>bold</strong>" in html
    # Inline-CSS sized for mobile reading.
    assert "max-width: 720px" in html


def test_render_file_text_strips_markdown(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text(SAMPLE_MD)
    out = rm.render_file(str(src), "text", None)
    assert out.endswith(".txt")
    text = Path(out).read_text()
    # Headings upper-cased.
    assert "HEADING 1" in text
    # Bold/italic delimiters stripped.
    assert "**bold**" not in text
    assert "*italic*" not in text
    assert "bold" in text
    # Link flattened.
    assert "a link (https://example.com)" in text


def test_render_file_explicit_output_path_respected(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text("# Hi\n")
    target = tmp_path / "result.html"
    out = rm.render_file(str(src), "html", str(target))
    assert out == str(target)
    assert target.exists()


def test_render_file_invalid_format_raises(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text("x")
    with pytest.raises(ValueError, match="format must be one of"):
        rm.render_file(str(src), "docx", None)


def test_render_file_missing_input_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot stat"):
        rm.render_file(str(tmp_path / "nope.md"), "html", None)


def test_render_file_relative_input_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        rm.render_file("relative.md", "html", None)


def test_render_file_unwritable_output_dir_raises(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text("x")
    with pytest.raises(ValueError, match="does not exist"):
        rm.render_file(str(src), "html", "/nonexistent-dir-xyz/out.html")


def test_render_file_pdf_produces_valid_pdf(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text("# Hi\n\nA paragraph.\n")
    target = tmp_path / "out.pdf"
    out = rm.render_file(str(src), "pdf", str(target))
    assert out == str(target)
    assert target.exists()
    head = target.read_bytes()[:5]
    assert head == b"%PDF-", f"not a PDF header: {head!r}"


def test_render_file_pdf_without_inkmd_raises_RenderError(
    tmp_path: Path,
) -> None:
    """If inkmd is uninstalled (e.g. on a fresh checkout that hasn't
    yet pulled the dep), the PDF path surfaces a clean RenderError
    rather than a bare ImportError."""
    src = tmp_path / "in.md"
    src.write_text("# Hi\n")
    target = tmp_path / "out.pdf"

    real_import = __builtins__["__import__"] if isinstance(
        __builtins__, dict,
    ) else __import__

    def fake_import(name, *args, **kwargs):
        if name == "inkmd":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(rm.RenderError, match="inkmd not installed"):
            rm.render_file(str(src), "pdf", str(target))


def test_stdlib_fallback_renders_when_markdown_pkg_missing(
    tmp_path: Path,
) -> None:
    """If the `markdown` package fails to import, the stdlib mini
    renderer must still produce HTML covering the common cases."""
    src = tmp_path / "in.md"
    src.write_text(SAMPLE_MD)

    real_import = __builtins__["__import__"] if isinstance(
        __builtins__, dict,
    ) else __import__

    def fake_import(name, *args, **kwargs):
        if name == "markdown":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=fake_import):
        out = rm.render_file(str(src), "html", None)

    html = Path(out).read_text()
    # Headings, lists, bold, italic, inline code, links all present.
    assert "<h1>" in html and "Heading 1" in html
    assert "<h2>" in html and "Heading 2" in html
    assert "<ul>" in html and "<li>bullet one</li>" in html
    assert "<ol>" in html and "bullet two" not in html.split("<ol>")[1].split("</ol>")[0]
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>inline code</code>" in html
    assert '<a href="https://example.com">a link</a>' in html
    # Fenced code block preserved.
    assert "<pre><code" in html
    assert "def hello" in html


# ---------------------------------------------------------------------------
# CLI mode tests
# ---------------------------------------------------------------------------

def test_cli_convert_html_prints_path(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text("# Hi\n")
    out_path = tmp_path / "out.html"
    proc = subprocess.run(
        [sys.executable, str(SERVER_PATH),
         "--convert", str(src),
         "--to", "html",
         "--out", str(out_path)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(out_path)
    assert out_path.exists()


def test_cli_convert_text_default_to_html_when_to_omitted(
    tmp_path: Path,
) -> None:
    src = tmp_path / "in.md"
    src.write_text("# Hi\n")
    proc = subprocess.run(
        [sys.executable, str(SERVER_PATH), "--convert", str(src)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith(".html")


def test_cli_convert_missing_file_returns_nonzero(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SERVER_PATH),
         "--convert", str(tmp_path / "nope.md"),
         "--to", "html"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2
    assert "cannot stat" in proc.stderr


# ---------------------------------------------------------------------------
# MCP stdio protocol tests
# ---------------------------------------------------------------------------

def _spawn() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _send(proc: subprocess.Popen[str], message: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[str]) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout before responding")
    return json.loads(line)


def _close(proc: subprocess.Popen[str]) -> None:
    if proc.stdin is not None:
        proc.stdin.close()
    proc.wait(timeout=5)


def test_mcp_initialize_handshake() -> None:
    proc = _spawn()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        resp = _recv(proc)
        assert resp["id"] == 1
        assert resp["result"]["serverInfo"]["name"] == "nightjar-render"
        assert resp["result"]["protocolVersion"] == "2024-11-05"
    finally:
        _close(proc)


def test_mcp_tools_list_advertises_render_markdown() -> None:
    proc = _spawn()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        _recv(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _recv(proc)
        names = {t["name"] for t in resp["result"]["tools"]}
        assert names == {"render_markdown"}
        tool = resp["result"]["tools"][0]
        assert "input_path" in tool["inputSchema"]["properties"]
        assert "format" in tool["inputSchema"]["properties"]
        assert tool["inputSchema"]["properties"]["format"]["enum"] == [
            "html", "text", "pdf",
        ]
    finally:
        _close(proc)


def test_mcp_tools_call_render_html(tmp_path: Path) -> None:
    src = tmp_path / "in.md"
    src.write_text("# Hello phone\n\nA paragraph.\n")
    proc = _spawn()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "render_markdown",
                "arguments": {
                    "input_path": str(src),
                    "format": "html",
                },
            },
        })
        resp = _recv(proc)
        assert resp["result"]["isError"] is False
        out_path = resp["result"]["content"][0]["text"]
        assert os.path.exists(out_path)
        assert "<h1>" in Path(out_path).read_text()
    finally:
        _close(proc)


def test_mcp_tools_call_missing_input_returns_invalid_params(
    tmp_path: Path,
) -> None:
    proc = _spawn()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "render_markdown",
                "arguments": {
                    "input_path": str(tmp_path / "nope.md"),
                    "format": "html",
                },
            },
        })
        resp = _recv(proc)
        assert "error" in resp
        assert resp["error"]["code"] == rm.ERR_INVALID_PARAMS
        assert "cannot stat" in resp["error"]["message"]
    finally:
        _close(proc)


def test_mcp_tools_call_bad_format_returns_invalid_params(
    tmp_path: Path,
) -> None:
    src = tmp_path / "in.md"
    src.write_text("x")
    proc = _spawn()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "render_markdown",
                "arguments": {
                    "input_path": str(src),
                    "format": "docx",
                },
            },
        })
        resp = _recv(proc)
        assert "error" in resp
        assert resp["error"]["code"] == rm.ERR_INVALID_PARAMS
    finally:
        _close(proc)


def test_mcp_unknown_tool_returns_method_not_found() -> None:
    proc = _spawn()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        _recv(proc)
        _send(proc, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        })
        resp = _recv(proc)
        assert resp["error"]["code"] == rm.ERR_METHOD_NOT_FOUND
    finally:
        _close(proc)
