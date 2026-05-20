"""`nightjar-send` — direct CLI for sending mail to the principal.

Wraps `notifier.notify_principal()` so an operator (or an agent
running under the same user account) can send a one-off email to
the principal without reimplementing SMTP, looking up secrets,
or going through the daemon's inbox loop.

Typical uses:

    nightjar-send --subject "TORCH layouts memo" --attach /tmp/memo.pdf
    nightjar-send --subject "FYI" --body "two-line note" --attach foo.pdf
    nightjar-send --subject "Notes" --attach-md ~/some/notes.md
    echo "body text" | nightjar-send --subject "Hi" --body-stdin

The CLI only ever sends to the principal. No footer is appended,
no audit copy is sent (the principal IS the recipient). For
third-party sends use the inbox-driven send flow; that path has
the safety prompts the headless CLI deliberately omits.

`--attach-md PATH` renders a markdown file to PDF via
`daemon.render_markdown_mcp.render_file` (which calls inkmd
under the hood, the same path the MCP server uses) and attaches
the result. The rendered PDF lands in /tmp by default; use
`--attach-md PATH --rendered-out OUTPATH` to keep the result at
a known location.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, ConfigError, load as load_config
from .log import JSONLLogger
from . import notifier
from .state import State


def _find_principal(config: Config):
    return next(
        (c for c in config.contacts.values() if c.is_principal),
        None,
    )


def _resolve_body(args: argparse.Namespace) -> str:
    """Pick the body text from the mutually-exclusive body flags.

    Returns a sensible default when none is supplied — the most
    common headless use is "here is an attachment", so the body
    is allowed to be near-empty.
    """
    sources = [
        ("body", args.body),
        ("body_file", args.body_file),
        ("body_stdin", args.body_stdin),
    ]
    set_sources = [name for name, value in sources if value]
    if len(set_sources) > 1:
        raise SystemExit(
            f"nightjar-send: pass at most one of --body / --body-file / "
            f"--body-stdin (got {', '.join('--' + s.replace('_', '-') for s in set_sources)})"
        )
    if args.body is not None:
        return args.body
    if args.body_file is not None:
        path = Path(args.body_file).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"nightjar-send: cannot read --body-file {path}: {e}")
    if args.body_stdin:
        return sys.stdin.read()
    n = len(args.attach) + len(args.attach_md)
    if n == 0:
        return "(empty body)\n"
    noun = "file" if n == 1 else "files"
    return f"Attached: {n} {noun}.\n"


def _build_attachments(
    attach_paths: list[str],
    attach_md_paths: list[str],
    rendered_out: str | None,
) -> list[notifier.AttachmentSpec]:
    """Materialise attachment specs from CLI args.

    `--attach` takes any file as-is. `--attach-md` first renders the
    markdown to PDF via the existing render_markdown_mcp.render_file
    helper — the same code the MCP server uses, so behaviour is
    consistent between agent-driven sends and operator-driven sends.

    `--rendered-out` is only meaningful when exactly one `--attach-md`
    is supplied; otherwise we use /tmp tempfiles to avoid an
    output-path collision.
    """
    specs: list[notifier.AttachmentSpec] = []
    for raw in attach_paths:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"nightjar-send: --attach {p} is not a regular file")
        specs.append(notifier.AttachmentSpec(path=p))

    if attach_md_paths:
        from . import render_markdown_mcp
        if rendered_out is not None and len(attach_md_paths) != 1:
            raise SystemExit(
                "nightjar-send: --rendered-out only works with a single --attach-md"
            )
        for raw in attach_md_paths:
            p = Path(raw).expanduser().resolve()
            if not p.is_file():
                raise SystemExit(
                    f"nightjar-send: --attach-md {p} is not a regular file"
                )
            out = rendered_out if rendered_out else None
            try:
                pdf_path = render_markdown_mcp.render_file(
                    str(p), "pdf", out,
                )
            except (ValueError, render_markdown_mcp.RenderError) as e:
                raise SystemExit(f"nightjar-send: render failed for {p}: {e}")
            specs.append(notifier.AttachmentSpec(path=Path(pdf_path)))
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightjar-send",
        description=(
            "Send a one-off email to the configured principal. "
            "Wraps notifier.notify_principal — no footer, no audit copy, "
            "no inbox round-trip. Uses the daemon's SMTP creds from "
            "secrets.toml."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to nightjar.conf (default: ~/.config/nightjar/nightjar.conf)",
    )
    parser.add_argument(
        "--subject",
        required=True,
        help="email subject line",
    )
    body_group = parser.add_argument_group("body (pick at most one; default: auto)")
    body_group.add_argument("--body", default=None, help="body text as a literal string")
    body_group.add_argument(
        "--body-file",
        default=None,
        help="read body from this file",
    )
    body_group.add_argument(
        "--body-stdin",
        action="store_true",
        help="read body from stdin",
    )
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a file as-is; repeat for multiple attachments",
    )
    parser.add_argument(
        "--attach-md",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "render this markdown file to PDF via inkmd and attach the "
            "result; repeat for multiple. The PDF lands in /tmp unless "
            "--rendered-out is given."
        ),
    )
    parser.add_argument(
        "--rendered-out",
        default=None,
        metavar="PATH",
        help=(
            "destination for the rendered PDF when exactly one "
            "--attach-md is given; ignored otherwise"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the success line on stdout",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config) if args.config else load_config()
    except (ConfigError, FileNotFoundError) as e:
        print(f"nightjar-send: config error: {e}", file=sys.stderr)
        return 2

    if config.smtp is None:
        print(
            "nightjar-send: [smtp] is required in nightjar.conf.",
            file=sys.stderr,
        )
        return 2

    principal = _find_principal(config)
    if principal is None or not principal.addresses:
        print("nightjar-send: no principal contact configured.", file=sys.stderr)
        return 2

    body = _resolve_body(args)
    attachments = _build_attachments(args.attach, args.attach_md, args.rendered_out)

    state = State(db_path=config.daemon.state_dir / "state.db")
    jlogger = JSONLLogger(log_dir=config.daemon.log_dir)
    try:
        result = notifier.notify_principal(
            smtp=config.smtp,
            principal_addr=principal.addresses[0],
            subject=args.subject,
            body=body,
            jlogger=jlogger,
            attachments=tuple(attachments),
            state=state,
        )
    finally:
        jlogger.close()

    if result.primary_sent:
        if not args.quiet:
            print(
                f"sent to {principal.addresses[0]} "
                f"(id {result.primary_message_id}, "
                f"{len(attachments)} attachment(s))"
            )
        return 0
    print(f"nightjar-send: send failed: {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
