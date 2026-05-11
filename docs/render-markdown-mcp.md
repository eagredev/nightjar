# `render_markdown` MCP tool — design

Status: design, not yet implemented.
Branch: `main` (product-flavoured; no research content).
Author: Dylan + Claude, 2026-05-11.

## Motivation

When the principal asks Nightjar to send a `.md` file to their phone,
the file arrives as raw markdown. Mobile mail clients do not render
markdown, so the principal sees `#` and `-` characters instead of
headings and bullets. The current workaround is to ask the agent
to convert the file manually (write HTML by hand, paste it back,
attach the new file), which is verbose, error-prone, and burns
tokens on plumbing that should be one tool call.

The fix is a small MCP tool the agent can call when format conversion
is wanted. It must:

- Be **discoverable to every agent that spawns under Nightjar**, including
  future API-only agents that have no special harness. The MCP handshake
  surfaces it automatically alongside `compose_reply` / `attach_to_reply`.
- Be **opt-in, not default.** Most attachments do not need conversion;
  the agent decides per-message. (User constraint, 2026-05-11.)
- Be **standalone-runnable from the CLI** for humans and for debugging.

## Tool surface

One tool, `render_markdown`, registered on a new stdio MCP server
named `nightjar-render`.

```
render_markdown(input_path, format, output_path?)
  → returns absolute path to the rendered file as content[0].text
```

### Arguments

- `input_path` (string, required): absolute path to an existing
  readable markdown file. Rejected at tool-call time if missing,
  not a regular file, or unreadable — same fail-at-the-boundary
  posture as `attach_to_reply`.
- `format` (string, required): one of `"html"`, `"text"`, `"pdf"`.
- `output_path` (string, optional): absolute path to write to.
  Defaults to a tempfile in `/tmp/nightjar-render-<uuid>.<ext>` so
  the agent workspace stays clean. If supplied, parent directory
  must exist and be writable.

### Output

- Success: `{"content": [{"type": "text", "text": "<absolute path>"}], "isError": false}`.
  The agent feeds the returned path straight into `attach_to_reply`.
- Failure: JSON-RPC error with `code: -32602` (invalid params) for
  input/output path problems; tool-result `isError: true` with a
  human-readable message for renderer failures (e.g. missing
  `wkhtmltopdf`).

## Format details

### HTML (the common case)

- Preferred renderer: `markdown` Python package, **if importable**.
  Use extensions: `extra`, `sane_lists`, `smarty`. Wrap output in a
  standalone HTML document with inline CSS sized for mobile reading
  (max-width ~720px, system font stack, generous line-height, code
  blocks with a monospace font and subtle background).
- Fallback renderer: a stdlib-only mini-renderer that handles
  headings (`#`..`######`), unordered/ordered lists, bold/italic,
  inline code, fenced code blocks, links, and paragraphs. Tables,
  footnotes, and admonitions are not supported by the fallback;
  they will render as raw markdown text inside `<p>`. This is fine
  for our common case (schedules, notes, briefings).
- The decision (real `markdown` vs fallback) is made once at tool
  start by `try: import markdown`. Logged once to stderr so an
  operator running the MCP under `journalctl` can see which engine
  fired.

### Plain text

- Strip markdown syntax to readable prose. Heuristic:
  - Headings → uppercase line + blank line.
  - List markers preserved as `- ` and `1. `.
  - Bold/italic/code fences stripped of their delimiters.
  - Links flattened to `text (url)`.
- This format exists as a fallback for clients that mangle HTML.
  Rarely the right choice but cheap to ship alongside.

### PDF

- Implementation: shell out to `wkhtmltopdf` reading the HTML
  pipeline's output. This means PDF = HTML rendered first, then
  printed. No second markdown→PDF path.
- If `wkhtmltopdf` is not on PATH at tool-call time, return
  `isError: true` with text:
  `"wkhtmltopdf not installed; render to html or text instead."`
  No silent fallback to HTML — the agent asked for PDF, and a
  silent fallback would surprise the principal.
- Document the install hint in the error: on Arch / SteamOS,
  `pacman -S wkhtmltopdf`; user is on the immutable rootfs so they
  may need a flatpak or pacman-with-overlay route.

## Server registration

In `daemon/principal_agent.py`, the `mcp_config` dict gains a second
entry alongside `nightjar-reply`:

```python
mcp_config = json.dumps({
    "mcpServers": {
        "nightjar-reply": { ... },                    # existing
        "nightjar-render": {                          # new
            "type": "stdio",
            "command": "python3",
            "args": [str(Path(__file__).parent / "render_markdown_mcp.py")],
        },
    },
})
```

No env vars required — the tool is pure compute (no logs of its
own; the audit log captures tool_use events naturally).

## System prompt addition

`build_system_prompt` in `daemon/principal_agent.py` already has a
"how to attach files" section. Add a sibling section immediately
after it:

```
### ...convert a markdown file to something the principal's phone can render?

Use the `render_markdown` MCP tool when the principal will be reading
on a phone and the source is .md. Mobile mail clients do not render
markdown; HTML renders inline.

render_markdown(input_path="/abs/path/in.md", format="html")
  → returns "/tmp/nightjar-render-<uuid>.html"

Then pass that path to attach_to_reply. Do NOT call this for every
attachment — only when format conversion is genuinely useful. If the
principal asked for the raw .md, attach the raw .md.

Formats: "html" (default for phone reading), "text" (strip syntax),
"pdf" (requires wkhtmltopdf; returns an error if missing — use html
in that case).
```

The phrase "Do NOT call this for every attachment" enforces the
opt-in posture per user constraint.

## CLI mode

The MCP server doubles as a CLI for humans and tests:

```
python3 -m daemon.render_markdown_mcp \
    --convert /path/to/in.md --to html [--out /path/out.html]
```

Prints the output path on stdout, exits 0 on success and non-zero
with an error message on stderr otherwise. This is the surface
unit tests will drive, and it gives the operator a working tool
even if no agent is around.

When invoked without `--convert`, defaults to MCP stdio loop (same
shape as `compose_reply_mcp.py`).

## Failure modes and edge cases

- **Input path missing / not a file / unreadable** → JSON-RPC error
  at tool-call time. Same fail-at-the-boundary as `attach_to_reply`.
- **Output path's parent directory missing** → JSON-RPC error.
- **`format` not in {html, text, pdf}** → JSON-RPC error.
- **PDF requested, `wkhtmltopdf` missing** → tool result `isError`
  with install hint. Agent should fall back to HTML or surface the
  problem to the principal.
- **`markdown` package missing** → stdlib fallback fires silently
  (logged once to stderr). Not an error; just lower-fidelity HTML.
- **Output already exists at chosen path** → overwrite. The default
  path uses a uuid so collisions are negligible; explicit
  `output_path` is the caller's responsibility.
- **Very large input file** → no special handling; `attach_to_reply`
  already caps the final attachment size at 18 MiB. A markdown
  file large enough to render past that limit is pathological.

## Testing

New test file: `tests/test_render_markdown_mcp.py`. Covers:

1. CLI mode: convert known markdown → HTML, assert output contains
   expected tags.
2. CLI mode: convert → text, assert markdown delimiters are stripped.
3. CLI mode: convert → pdf with `wkhtmltopdf` absent (mock by
   patching `shutil.which`), assert non-zero exit and clear error.
4. MCP mode: drive the stdio JSON-RPC loop directly with three
   fixtures (init → tools/list → tools/call), assert the returned
   path exists and is readable.
5. MCP mode: tools/call with missing input_path → invalid params
   error.
6. MCP mode: tools/call with bad format → invalid params error.
7. Stdlib fallback: with `markdown` import patched to raise
   ImportError, assert HTML still produced (lower-fidelity is fine).

Smoke test: `daemon/render_markdown_smoke.py`, mirroring
`compose_reply_smoke.py` — spawn the server as a subprocess, send
a full handshake + tool call, assert the rendered file exists.

## Out of scope

- Rendering other formats (docx, org, rst). Markdown only; that
  is what the principal's notes are written in.
- A "smart" auto-detect that picks a format from the file extension.
  The agent decides; the tool is dumb.
- Persisting rendered files alongside the source. `/tmp/` is fine;
  the daemon's SMTP send pulls the bytes before the tempfile is
  GC'd by the host's tmpwatch.
- Theming. The HTML template is one inline-CSS block tuned for
  mobile mail readability; no user-configurable theme. Anyone who
  wants a different look can call the CLI and pipe through their
  own template.

## Implementation order

1. New file `daemon/render_markdown_mcp.py` — MCP server skeleton
   copied from `compose_reply_mcp.py`, one tool registered, stdlib
   markdown→HTML fallback inlined, CLI mode plumbed.
2. Wire into `daemon/principal_agent.py` `mcp_config`.
3. Add system-prompt section in `build_system_prompt`.
4. Tests as above.
5. Smoke test.
6. Update `example.conf` and README if relevant (probably not —
   tool is automatic, no operator-facing config).

Estimated scope: ~400 LOC production + ~250 LOC tests. One commit
on `main`.

## Open question for implementation session

The stdlib-only markdown→HTML fallback is the longest single chunk
of new code in this design. If it grows past ~150 LOC during
implementation, reconsider whether to pin `markdown` as a real
dependency. Nightjar's stdlib-only ethos is strong but not
absolute; principle is "no third-party deps unless they meaningfully
reduce our maintenance burden," and a battle-tested markdown
renderer probably qualifies if the fallback grows large.
