"""IMAP IDLE watcher for one inbox.

Build Step 1: connect, IDLE, on new mail look up the sender against the
contact directory, persist a row to SQLite. No outbound, no LLM, no
auth, no notifier. The point is to validate that the IDLE loop survives
Steam Deck sleep/wake and Gmail's ~29-minute IDLE timeout cleanly.

State machine for the watcher itself:

    DISCONNECTED -> CONNECTING -> AUTHENTICATING -> CATCHING_UP -> IDLING
                          ^                                          |
                          |-------- error / timeout / wake ----------|

Each inbox is one asyncio task. Backoff on errors is exponential, capped
at 5 minutes. Gmail's IDLE timeout (~29 min) triggers a planned reconnect,
not an error.
"""
from __future__ import annotations

import asyncio
import contextlib
import email
import email.utils
import random
import secrets
import time
from email.header import decode_header, make_header
from pathlib import Path

from aioimaplib import aioimaplib

from . import auth, dmarc, executor, notifier, principal_commands, principal_handlers, triage
from .config import InboxConfig, Config
from .dmarc import (
    DMARC_FAIL,
    DMARC_MISSING,
    DMARC_NONE,
    DMARC_NO_TRUSTED_HEADER,
    DMARC_PASS,
    DMARC_TEMPERROR,
)
from .log import JSONLLogger
from .state import State
from .triage import TriageError, TriagePlan


# Token format for approval pings. Hex, 8 chars, generated from
# secrets.token_hex(4). Long enough that two simultaneous pings can't
# collide; short enough that a phone screen can show it without
# wrapping. The parser accepts 6+ chars so we can grow this later
# without breaking existing pending tokens.
_APPROVAL_TOKEN_BYTES = 4


# Gmail's documented IDLE timeout is 29 minutes. We re-IDLE at 27 to be
# safe (the server kicks us at ~29 if we don't move first).
IDLE_REFRESH_SECONDS = 27 * 60
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 300.0

# Max plaintext body bytes the watcher will hand to triage. Anything
# longer is truncated and a "body_truncated" flag is added to the plan's
# notes. 32 KiB is well above a normal email and below the prompt-token
# budget per call: ~8000 input tokens budgeted, ~4 chars per token, so
# ~32 KiB is the natural ceiling.
MAX_TRIAGE_BODY_BYTES = 32 * 1024

# Directory holding common.md and triage_default.md. Resolved relative
# to this module so the daemon doesn't depend on the cwd at start time.
# nightjar/daemon/inbox_watcher.py -> nightjar/prompts/.
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Rate-limit window for the in-daemon spend cap. The first line of cost
# defence is the Anthropic console's monthly cap; this is the second:
# even if a single daemon goes haywire (loop, bad state machine), it
# cannot burn more than [claude].per_hour_max_invocations calls in any
# rolling hour. 1 hour matches DESIGN.md's cost model.
RATE_LIMIT_WINDOW_SECONDS = 3600


class InboxWatcher:
    def __init__(
        self,
        *,
        inbox: InboxConfig,
        config: Config,
        state: State,
        logger: JSONLLogger,
        on_panic: "callable | None" = None,
        claude_client: "object | None" = None,
    ) -> None:
        self.inbox = inbox
        self.config = config
        self.state = state
        self.logger = logger
        self._stop_event = asyncio.Event()
        self._backoff = INITIAL_BACKOFF_SECONDS
        # Called with (reason: str) when the dead-man's-switch trips.
        # main.py uses this to trigger a clean daemon shutdown.
        self._on_panic = on_panic
        # Anthropic Messages API client for contact-mail triage. None
        # when [claude] is missing from config or when triage is
        # otherwise disabled. The watcher checks this BEFORE issuing a
        # body fetch so an unconfigured daemon doesn't waste a round
        # trip pulling bodies it can't triage.
        self._claude_client = claude_client

    async def run(self) -> None:
        """Run forever (until stop() is called or asyncio cancels us)."""
        self.logger.event("watcher_start", inbox=self.inbox.name, imap_user=self.inbox.imap_user)
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                self._backoff = INITIAL_BACKOFF_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.event(
                    "idle_error",
                    inbox=self.inbox.name,
                    level="warn",
                    error=type(e).__name__,
                    message=str(e),
                )
                await self._wait_backoff()
        self.logger.event("watcher_stop", inbox=self.inbox.name)

    def stop(self) -> None:
        self._stop_event.set()

    async def _wait_backoff(self) -> None:
        # Exponential backoff with jitter, capped.
        delay = min(self._backoff * (1.0 + random.random() * 0.25), MAX_BACKOFF_SECONDS)
        self.logger.event(
            "idle_backoff",
            inbox=self.inbox.name,
            delay_seconds=round(delay, 2),
        )
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        self._backoff = min(self._backoff * 2.0, MAX_BACKOFF_SECONDS)

    async def _run_once(self) -> None:
        """Connect, authenticate, catch up, then IDLE until the next reset.

        One full reconnect cycle. The outer run() loop will call this
        again after backoff if it raises.
        """
        client = aioimaplib.IMAP4_SSL(host=self.inbox.imap_host, port=self.inbox.imap_port)
        await client.wait_hello_from_server()
        self.logger.event("idle_connect", inbox=self.inbox.name, host=self.inbox.imap_host)

        try:
            login_response = await client.login(self.inbox.imap_user, self.inbox.imap_password)
            if login_response.result != "OK":
                raise RuntimeError(f"login failed: {login_response.result} {login_response.lines!r}")

            select_response = await client.select("INBOX")
            if select_response.result != "OK":
                raise RuntimeError(f"select failed: {select_response.result} {select_response.lines!r}")

            await self._catch_up(client)

            while not self._stop_event.is_set():
                await self._idle_once(client)
        finally:
            with contextlib.suppress(Exception):
                await client.logout()

    async def _catch_up(self, client: aioimaplib.IMAP4_SSL) -> None:
        """Process any UNSEEN messages that arrived while we were off."""
        result, data = await client.uid_search("UNSEEN")
        if result != "OK":
            self.logger.event(
                "catchup_search_failed",
                inbox=self.inbox.name,
                level="warn",
                result=result,
            )
            return
        if not data or not data[0]:
            return
        uids = data[0].split()
        if not uids:
            return
        self.logger.event(
            "catchup_start",
            inbox=self.inbox.name,
            unseen_count=len(uids),
        )
        for uid in uids:
            try:
                await self._fetch_and_record(client, uid.decode("ascii"))
            except Exception as e:
                self.logger.event(
                    "catchup_fetch_error",
                    inbox=self.inbox.name,
                    level="warn",
                    uid=uid.decode("ascii", "replace"),
                    error=type(e).__name__,
                    message=str(e),
                )
        self.logger.event("catchup_complete", inbox=self.inbox.name, processed=len(uids))

    async def _idle_once(self, client: aioimaplib.IMAP4_SSL) -> None:
        """Issue one IDLE, wait for activity or refresh timeout, then DONE.

        On activity (untagged response indicating EXISTS), runs a fresh
        UNSEEN search and processes anything new. On timeout, returns;
        the outer loop re-IDLEs.
        """
        idle_task = await client.idle_start(timeout=IDLE_REFRESH_SECONDS + 30)
        try:
            # Race three conditions: server push (activity), stop_event
            # (clean shutdown), and the refresh timer. Whichever resolves
            # first wins; the others are cancelled in the finally block.
            activity_task = asyncio.create_task(self._wait_for_activity(client))
            stop_task = asyncio.create_task(self._stop_event.wait())
            timer_task = asyncio.create_task(asyncio.sleep(IDLE_REFRESH_SECONDS))
            try:
                done, pending = await asyncio.wait(
                    {activity_task, stop_task, timer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # Surface any exception from the winning task.
                for t in done:
                    exc = t.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        raise exc
                if stop_task in done:
                    self.logger.event("idle_stop_requested", inbox=self.inbox.name)
                elif activity_task in done:
                    self.logger.event("idle_activity", inbox=self.inbox.name)
                else:
                    self.logger.event("idle_refresh", inbox=self.inbox.name)
            finally:
                for t in (activity_task, stop_task, timer_task):
                    if not t.done():
                        t.cancel()
                # Drain cancellations.
                with contextlib.suppress(asyncio.CancelledError, BaseException):
                    await asyncio.gather(activity_task, stop_task, timer_task, return_exceptions=True)
        finally:
            client.idle_done()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(idle_task, timeout=10)

        # If we're stopping, don't bother running another catch-up.
        if self._stop_event.is_set():
            return
        # Otherwise, do an UNSEEN search regardless of which path woke us.
        # Catching up twice is cheaper than missing a message once.
        await self._catch_up(client)

    async def _wait_for_activity(self, client: aioimaplib.IMAP4_SSL) -> None:
        """Block until the server pushes us something interesting.

        aioimaplib pushes IDLE responses via client.wait_server_push().
        Any push wakes us up; we re-search rather than try to be clever
        about parsing the push payload.
        """
        while not self._stop_event.is_set():
            push = await client.wait_server_push()
            if push is None:
                # Server-pushed empty: keep waiting unless we've been told to stop.
                continue
            return  # any non-empty push counts as activity

    async def _fetch_and_record(self, client: aioimaplib.IMAP4_SSL, uid: str) -> None:
        """Fetch headers for one UID, look up sender, persist a message row."""
        result, data = await client.uid("fetch", uid, "(BODY.PEEK[HEADER])")
        if result != "OK" or not data:
            self.logger.event(
                "fetch_failed",
                inbox=self.inbox.name,
                level="warn",
                uid=uid,
                result=result,
            )
            return

        header_blob = self._extract_literal(data)
        if header_blob is None:
            self.logger.event(
                "fetch_no_headers",
                inbox=self.inbox.name,
                uid=uid,
                level="warn",
                response_shape=[type(c).__name__ for c in data],
            )
            return

        msg = email.message_from_bytes(header_blob)
        message_id = (msg.get("Message-ID") or "").strip()
        if not message_id:
            # Fall back to a synthetic ID combining inbox + UID. Not
            # globally unique but good enough to avoid duplicate inserts
            # for messages that lack a Message-ID header.
            message_id = f"<no-msgid-{self.inbox.name}-uid{uid}>"

        if self.state.message_exists(message_id):
            return  # already seen on a previous catch-up

        from_header = msg.get("From", "")
        _, from_addr = email.utils.parseaddr(from_header)
        from_addr = from_addr.lower()

        subject = self._decode_header(msg.get("Subject"))

        contact_id = self.config.address_index.get(from_addr)

        panic_trip_reason: str | None = None

        # DMARC gate (Step 5b hardening). The trusted authserv stamps an
        # Authentication-Results header on every message it accepts. We
        # require dmarc=pass before any inbound contact path runs, so an
        # attacker who spoofs a known contact's address cannot reach
        # triage with attacker-controlled content. Per operator decision
        # (2026-05-04), dmarc=none / missing / temperror / no_trusted_header
        # are ALL treated as adversarial: the alternative (trust senders
        # whose domain doesn't publish DMARC) gives spoofers a free pass
        # for any sender on a non-compliant domain.
        verdict = dmarc.parse_authentication_results(
            msg, trusted_authserv=self.inbox.trusted_authserv,
        )
        # Cross-check: the verdict applies to the domain in
        # `header.from=`, but the visible From: header could differ.
        # An attacker who controls attacker.com can get dmarc=pass for
        # attacker.com while spoofing a From of composer@hotmail.co.uk.
        # We require the verdict's domain to match the From header's
        # domain.
        from_domain = dmarc.from_header_domain(msg)
        verdict_domain_matches = (
            verdict.header_from_domain is not None
            and from_domain is not None
            and verdict.header_from_domain == from_domain
        )

        dmarc_dropped = False
        dmarc_disposition: str | None = None
        if not verdict.authenticated:
            dmarc_dropped = True
            dmarc_disposition = f"dmarc_{verdict.verdict}"
        elif not verdict_domain_matches:
            dmarc_dropped = True
            dmarc_disposition = "dmarc_from_mismatch"

        if dmarc_dropped:
            state = "DROPPED"
            detail = dmarc_disposition
        elif contact_id is None:
            state = "DROPPED"
            detail = "stranger"
        else:
            contact = self.config.contacts[contact_id]
            if contact_id not in self.inbox.allowed_contacts:
                state = "DROPPED"
                detail = "contact_not_allowed_on_inbox"
            elif self.state.is_contact_blocked(contact_id):
                # Live block flag set by the `block` tier-2 verb. The
                # principal can lift it with `unblock`; nightjar.conf is
                # not touched. We check this BEFORE is_principal so a
                # block on the principal is honoured (defensive: a
                # principal who blocks themselves can recover via the
                # state DB directly, but the daemon will refuse their
                # mail until then).
                state = "DROPPED"
                detail = "contact_blocked"
            elif contact.daily_limit == 0:
                state = "DROPPED"
                detail = "blocked"
            elif contact.is_principal:
                # Principal mail must carry a valid TOTP code in the
                # subject prefix. No code or a bad code is a switch
                # counter increment; threshold trips the dead-man's-switch.
                state, detail, panic_trip_reason = self._authenticate_principal(
                    subject=subject, from_addr=from_addr
                )
            else:
                # Allowlisted, in-quota contact mail. Build Step 2 still
                # doesn't implement triage; later steps will pick this up.
                state = "RECEIVED"
                detail = "ok"

        inserted = self.state.record_message(
            message_id=message_id,
            inbox=self.inbox.name,
            from_addr=from_addr,
            subject=subject,
            contact_id=contact_id,
            state=state,
        )
        if inserted:
            self.logger.event(
                "mail_received",
                inbox=self.inbox.name,
                message_id=message_id,
                from_addr=from_addr,
                contact_id=contact_id,
                state=state,
                disposition=detail,
                subject_preview=(subject or "")[:80],
            )
            # Impersonation alert: if DMARC dropped the message AND the
            # claimed sender matches one of our known contacts (or the
            # principal), the principal should know someone tried to
            # spoof them. Silent on stranger spoofing because spam is
            # high-volume and would drown the principal in pings.
            if (
                dmarc_dropped
                and contact_id is not None
            ):
                self._notify_principal_of_impersonation(
                    message_id=message_id,
                    from_addr=from_addr,
                    contact_id=contact_id,
                    subject=subject,
                    verdict=verdict,
                )

        # Step 4a: post-auth dispatch for authenticated principal mail.
        # We only run dispatch when auth succeeded (state == RECEIVED on
        # a principal contact); other states already terminated the flow.
        if (
            inserted
            and state == "RECEIVED"
            and contact_id is not None
            and self.config.contacts[contact_id].is_principal
        ):
            self._dispatch_principal_command(
                message_id=message_id, subject=subject, from_addr=from_addr
            )

        # Step 5b: triage for non-principal contact mail. Only runs when
        # the contact-mail branch produced RECEIVED (i.e. allowlisted,
        # not blocked, not the principal) AND the daemon has a Claude
        # client configured. Strangers, blocked contacts, and principals
        # all bypass this branch.
        if (
            inserted
            and state == "RECEIVED"
            and contact_id is not None
            and not self.config.contacts[contact_id].is_principal
        ):
            await self._handle_contact_triage(
                client=client,
                uid=uid,
                message_id=message_id,
                contact_id=contact_id,
                from_addr=from_addr,
                from_header=from_header,
                subject=subject,
                date_header=msg.get("Date", ""),
            )

        if panic_trip_reason is not None:
            self._trip_dead_mans_switch(panic_trip_reason)

    def _authenticate_principal(
        self, *, subject: str | None, from_addr: str
    ) -> tuple[str, str, str | None]:
        """Verify the TOTP code on a principal-claimed email.

        Returns (state, disposition, panic_reason). `panic_reason` is
        non-None iff this failure tripped the switch. The caller writes
        the message row, then trips the switch (so the failure that
        tripped it is durably recorded before shutdown).
        """
        security = self.config.security
        if security is None:
            # No [security] block: refuse to auth principal mail at all.
            # Treat as a misconfiguration, not a switch trip.
            self.logger.event(
                "principal_auth_misconfigured",
                inbox=self.inbox.name,
                level="warn",
                from_addr=from_addr,
            )
            return "DROPPED", "no_security_config", None

        code = auth.extract_code_from_subject(subject)

        if code is None:
            return self._handle_auth_failure(
                from_addr=from_addr, reason="no_auth_code", security=security
            )

        if security.auth_mode == "hotp":
            return self._authenticate_principal_hotp(
                code=code, from_addr=from_addr, security=security
            )
        return self._authenticate_principal_totp(
            code=code, from_addr=from_addr, security=security
        )

    def _authenticate_principal_totp(
        self, *, code: str, from_addr: str, security
    ) -> tuple[str, str, str | None]:
        if not auth.verify_totp(secret=security.totp_secret, code=code):
            return self._handle_auth_failure(
                from_addr=from_addr, reason="bad_totp_code", security=security
            )
        if not self.state.mark_totp_code_used(code):
            return self._handle_auth_failure(
                from_addr=from_addr, reason="totp_replay", security=security
            )
        self.state.prune_used_totp_codes()
        self.logger.event("principal_auth_ok", inbox=self.inbox.name, from_addr=from_addr, mode="totp")
        return "RECEIVED", "ok", None

    def _authenticate_principal_hotp(
        self, *, code: str, from_addr: str, security
    ) -> tuple[str, str, str | None]:
        last = self.state.get_hotp_counter()
        matched = auth.verify_hotp(
            secret=security.totp_secret, code=code, last_counter=last
        )
        if matched is None:
            return self._handle_auth_failure(
                from_addr=from_addr, reason="bad_hotp_code", security=security
            )
        # Advance to the matched counter. RFC 4226 lookahead burns the
        # skipped counters so they can't be replayed if the operator
        # accidentally tapped past them on the authenticator.
        skipped = matched - last - 1
        self.state.set_hotp_counter(matched)
        self.logger.event(
            "principal_auth_ok",
            inbox=self.inbox.name,
            from_addr=from_addr,
            mode="hotp",
            counter=matched,
            skipped=skipped,
        )
        return "RECEIVED", "ok", None

    def _handle_auth_failure(
        self,
        *,
        from_addr: str,
        reason: str,
        security,
    ) -> tuple[str, str, str | None]:
        """Record an auth failure, return classification + panic_reason if tripped."""
        self.state.record_auth_failure(from_addr=from_addr, reason=reason)
        window_seconds = security.dead_mans_switch_window_minutes * 60
        since = int(time.time()) - window_seconds
        recent_failures = self.state.count_auth_failures_since(since)
        self.logger.event(
            "principal_auth_failed",
            inbox=self.inbox.name,
            level="warn",
            from_addr=from_addr,
            reason=reason,
            recent_failures=recent_failures,
            threshold=security.dead_mans_switch_threshold,
        )
        if recent_failures >= security.dead_mans_switch_threshold:
            panic_reason = (
                f"{recent_failures} invalid TOTP attempts within "
                f"{security.dead_mans_switch_window_minutes} minutes "
                f"from {from_addr}"
            )
            return "DROPPED", reason, panic_reason
        return "DROPPED", reason, None

    def _trip_dead_mans_switch(self, reason: str) -> None:
        """Persist panic state and signal the daemon to halt."""
        self.state.trip_panic(reason=reason)
        self.logger.event(
            "panic_tripped",
            inbox=self.inbox.name,
            level="error",
            reason=reason,
        )
        if self._on_panic is not None:
            self._on_panic(reason)
        # Set our own stop event so this watcher exits its loop promptly.
        self._stop_event.set()

    def _dispatch_principal_command(
        self, *, message_id: str, subject: str | None, from_addr: str
    ) -> None:
        """Parse and handle one authenticated principal email.

        Five branches:

        1. Approval-token reply: look up the pending approval, validate
           the verdict (tier-2 needs APPROVE, tier-4 needs IRREVERSIBLE),
           on approve dispatch the executor, send confirmation. State
           transitions to APPROVED, DENIED, or APPROVAL_UNCLEAR.

        2. Interpret-choice reply: 'yes interpret' transitions to
           INTERPRETING and sends the LLM-stubbed reply (Step 5 wires
           the actual call). 'no' transitions to INTERPRET_DECLINED.

        3. Tier-1 verb: dispatch the handler, send the reply, transition
           to RESPONDED.

        4. Tier-2+ verb: queue an approval row with a fresh token, ping
           the principal with [Nightjar #token] subject describing the
           proposed action, transition to AWAITING_APPROVAL.

        5. Free-form: send the 'interpret with LLM?' prompt, transition
           to INTERPRET_OFFERED.

        SMTP failures here are non-fatal: the inbound message stays
        recorded; the operator just doesn't get a reply. That's a
        better failure mode than crashing the watcher.
        """
        cmd = principal_commands.parse_principal_command(subject)

        if cmd.approval_token is not None:
            self._resolve_approval_reply(
                message_id=message_id, command=cmd, from_addr=from_addr
            )
            return

        if cmd.interpret_choice is not None:
            self._resolve_interpret_reply(
                message_id=message_id, command=cmd, from_addr=from_addr
            )
            return

        if cmd.tier == 1:
            self._send_tier1_reply(
                message_id=message_id, command=cmd, from_addr=from_addr
            )
            return

        if cmd.tier is not None and cmd.tier >= 2:
            self._queue_tier2_plus(
                message_id=message_id, command=cmd, from_addr=from_addr
            )
            return

        # Free-form fallback.
        grammar = principal_commands.describe_grammar()
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject="Nightjar: free-form request, interpret with LLM?",
            body=(
                "I didn't recognise this as a deterministic command.\n"
                "\n"
                "Your request:\n"
                f"> {cmd.payload or '(empty)'}\n"
                "\n"
                "Reply with one of:\n"
                "  - '[<code>] yes interpret' to spend tokens on parsing this\n"
                "    into a structured plan (LLM call lands in Step 5;\n"
                "    will currently stub a no-op response).\n"
                "  - '[<code>] no' to drop the request.\n"
                "  - '[<code>] <recognised verb>' to issue a fresh command.\n"
                "\n"
                "Recognised verbs:\n"
                f"{grammar}\n"
            ),
            next_state="INTERPRET_OFFERED",
            event_name="principal_free_form",
            detail=cmd.payload[:80] if cmd.payload else "",
        )

    def _send_tier1_reply(
        self, *, message_id: str, command, from_addr: str
    ) -> None:
        """Run a tier-1 handler and email the reply to the principal."""
        result = principal_handlers.dispatch(
            command=command, config=self.config, state=self.state
        )
        if result is None:
            self.logger.event(
                "principal_handler_missing",
                inbox=self.inbox.name,
                level="warn",
                message_id=message_id,
                verb=command.verb,
            )
            return
        reply_subject, reply_body = result
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject=reply_subject,
            body=reply_body,
            next_state="RESPONDED",
            event_name="principal_tier1_dispatched",
            detail=f"verb={command.verb}",
        )

    def _queue_tier2_plus(
        self, *, message_id: str, command, from_addr: str
    ) -> None:
        """Queue a tier-2+ verb and ping the principal for approval.

        The token is the public handle that comes back in the
        principal's reply subject. We generate it here (so the executor
        layer doesn't need to know about token uniqueness) and check
        for collisions defensively, even though 4 random bytes makes
        collision implausible.
        """
        token = self._generate_approval_token()
        self.state.queue_approval(
            token=token,
            message_id=message_id,
            verb=command.verb,
            args=dict(command.args),
            tier=command.tier,
        )
        confirm_phrase = (
            "YES IRREVERSIBLE" if command.tier >= 4 else "yes"
        )
        tier_note = (
            "This is a tier-4 verb (irreversible local writes). The reply\n"
            "must be the literal phrase YES IRREVERSIBLE in uppercase.\n"
            if command.tier >= 4
            else "This is a tier-2 verb (reversible local writes). A plain\n"
                 "'yes' (or 'approve' / 'go') is enough.\n"
        )
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject=f"[Nightjar #{token}] approval needed: {command.verb}",
            body=(
                f"Verb:   {command.verb}\n"
                f"Args:   {dict(command.args)}\n"
                f"Tier:   {command.tier}\n"
                "\n"
                f"{tier_note}"
                "\n"
                "To approve, reply with:\n"
                f"  Subject: [<code>] [Nightjar #{token}] {confirm_phrase}\n"
                "\n"
                "To deny, reply with:\n"
                f"  Subject: [<code>] [Nightjar #{token}] no\n"
                "\n"
                "Approval expires in 7 days; offline time is not deducted\n"
                "automatically (see DESIGN.md).\n"
            ),
            next_state="AWAITING_APPROVAL",
            event_name="principal_approval_queued",
            detail=f"verb={command.verb} tier={command.tier} token={token}",
        )

    def _generate_approval_token(self) -> str:
        """Hex token for the [Nightjar #...] tag.

        Re-rolls if the freshly-generated token already exists in the
        approvals table (PRIMARY KEY collision). At 4 bytes the
        collision space is 1/4B per active token, so this is mostly
        belt-and-braces; expired-but-still-resident rows are still
        in the keyspace.
        """
        for _ in range(8):
            token = secrets.token_hex(_APPROVAL_TOKEN_BYTES)
            if self.state.get_approval(token) is None:
                return token
        # Improbable. If we hit it the daemon is in a bad state anyway.
        raise RuntimeError("could not generate a non-colliding approval token")

    def _resolve_approval_reply(
        self, *, message_id: str, command, from_addr: str
    ) -> None:
        """Look up an approval by token and act on the principal's verdict.

        Cases handled:

          - Unknown token: log + reply 'no such pending approval'.
          - Already-resolved or expired: log + reply 'already
            resolved' / 'expired'.
          - Tier-2 + APPROVE: dispatch executor, transition APPROVED,
            send result.
          - Tier-2 + DENY: transition DENIED, send acknowledgement.
          - Tier-4 + IRREVERSIBLE: dispatch executor, transition
            APPROVED, send result.
          - Tier-4 + APPROVE (i.e. plain 'yes'): reject as insufficient,
            keep approval PENDING, reply with the friction note.
          - Any tier + DENY: transition DENIED.
          - UNCLEAR: reply with the verdict-format hint, keep approval
            PENDING so the principal can retry.

        On approve, executor errors are caught (executor.execute does
        its own try/except) and surfaced in the reply.
        """
        token = command.approval_token
        verdict = command.approval_verdict
        approval = self.state.get_approval(token)

        if approval is None:
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject=f"[Nightjar #{token}] unknown approval token",
                body=(
                    f"No pending approval matches token #{token}.\n"
                    "It may have already been resolved, expired, or be a\n"
                    "typo. Use 'list pending' to see active approvals.\n"
                ),
                next_state="APPROVAL_REPLY_NOTED",
                event_name="principal_approval_unknown_token",
                detail=f"token={token}",
            )
            return

        # Catch already-resolved / expired before checking verdict.
        if approval["state"] != "PENDING":
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject=f"[Nightjar #{token}] approval already resolved",
                body=(
                    f"The approval for '{approval['verb']}' is already\n"
                    f"in state {approval['state']}. No action taken.\n"
                ),
                next_state="APPROVAL_REPLY_NOTED",
                event_name="principal_approval_stale",
                detail=f"token={token} state={approval['state']}",
            )
            return

        # Lazy expiry check: if expires_at has passed but the row is
        # still PENDING, flip it now so a too-late reply is treated
        # consistently.
        now = int(time.time())
        if approval["expires_at"] <= now:
            self.state.resolve_approval(
                token=token, outcome="EXPIRED", detail="reply arrived after window", at=now
            )
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject=f"[Nightjar #{token}] approval expired",
                body=(
                    f"The approval for '{approval['verb']}' expired at\n"
                    f"{approval['expires_at']} (epoch). Resubmit the verb\n"
                    "to start a fresh approval.\n"
                ),
                next_state="APPROVAL_REPLY_NOTED",
                event_name="principal_approval_expired",
                detail=f"token={token}",
            )
            return

        tier = approval["tier"]
        verb = approval["verb"]
        args = approval["args"]

        if verdict == "DENY":
            self.state.resolve_approval(token=token, outcome="DENIED", detail="principal said no")
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject=f"[Nightjar #{token}] denied: {verb}",
                body=(
                    f"Approval for '{verb}' denied. No action taken.\n"
                ),
                next_state="DENIED",
                event_name="principal_approval_denied",
                detail=f"token={token} verb={verb}",
            )
            return

        # Approve / IRREVERSIBLE branches.
        if tier >= 4 and verdict != "IRREVERSIBLE":
            # Tier-4 with plain 'yes' is rejected; approval stays PENDING.
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject=f"[Nightjar #{token}] tier-4 needs YES IRREVERSIBLE",
                body=(
                    f"'{verb}' is a tier-4 verb. A plain 'yes' is not enough.\n"
                    "Reply with the literal phrase:\n"
                    "\n"
                    f"  Subject: [<code>] [Nightjar #{token}] YES IRREVERSIBLE\n"
                    "\n"
                    "in uppercase, as the entire post-token text. The\n"
                    "approval is still pending until you do.\n"
                ),
                next_state="APPROVAL_REPLY_NOTED",
                event_name="principal_approval_insufficient",
                detail=f"token={token} verb={verb} verdict={verdict}",
            )
            return

        if tier < 4 and verdict == "IRREVERSIBLE":
            # Tier-2 verb with the tier-4 phrase: still approve, but log
            # the surplus. Conservative: treat as APPROVE.
            verdict = "APPROVE"

        if verdict not in ("APPROVE", "IRREVERSIBLE"):
            # UNCLEAR or unexpected. Approval stays PENDING; principal
            # gets a hint.
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject=f"[Nightjar #{token}] verdict unclear",
                body=(
                    "I couldn't parse your reply as a verdict. Reply with:\n"
                    f"  Subject: [<code>] [Nightjar #{token}] yes\n"
                    "       or: [<code>] [Nightjar #{token}] no\n"
                    + (
                        f"\n  Tier-4 verbs ({verb} is tier 4) require\n"
                        f"  [<code>] [Nightjar #{token}] YES IRREVERSIBLE\n"
                        if tier >= 4 else ""
                    )
                ),
                next_state="APPROVAL_REPLY_NOTED",
                event_name="principal_approval_unclear",
                detail=f"token={token} verb={verb} verdict={verdict}",
            )
            return

        # Approved. Mark APPROVED, run executor, send result.
        self.state.resolve_approval(
            token=token, outcome="APPROVED",
            detail=f"verdict={verdict}", at=now,
        )
        result = executor.execute(
            verb=verb, args=args, config=self.config, state=self.state, now=now,
            jlogger=self.logger,
        )
        self.logger.event(
            "principal_approval_executed",
            inbox=self.inbox.name,
            level=("info" if result.ok else "error"),
            message_id=message_id,
            token=token,
            verb=verb,
            ok=result.ok,
            summary=result.summary,
        )
        outcome_word = "executed" if result.ok else "failed"
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject=f"[Nightjar #{token}] {verb} {outcome_word}: {result.summary}",
            body=result.body,
            next_state=("EXECUTED" if result.ok else "EXECUTION_FAILED"),
            event_name="principal_approval_resolved",
            detail=f"token={token} verb={verb} ok={result.ok}",
        )

    def _resolve_interpret_reply(
        self, *, message_id: str, command, from_addr: str
    ) -> None:
        """Handle 'yes interpret' / 'no' replies to a free-form prompt.

        The LLM call is stubbed (Step 5 wires claude-agent-sdk). For
        now, 'yes interpret' produces a polite 'not yet wired' reply
        and transitions to INTERPRETING (which then settles to
        EXECUTION_FAILED via a follow-up in this same call). 'no'
        transitions to INTERPRET_DECLINED, terminal.
        """
        if command.interpret_choice == "NO_INTERPRET":
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: interpret declined",
                body=(
                    "Got it; the free-form request was dropped without\n"
                    "interpretation. No action taken.\n"
                ),
                next_state="INTERPRET_DECLINED",
                event_name="principal_interpret_declined",
            )
            return

        # INTERPRET. Step 5 will replace this stub with a real Claude call.
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject="Nightjar: LLM interpret not yet wired",
            body=(
                "You replied 'yes interpret', but the Claude call hasn't\n"
                "landed yet (Step 5). The state machine recorded the\n"
                "request but no plan was produced and no action will run.\n"
                "\n"
                "When Step 5 ships, this same reply will trigger the\n"
                "interpretation pass and produce a structured plan for\n"
                "your approval.\n"
            ),
            next_state="INTERPRET_STUBBED",
            event_name="principal_interpret_stubbed",
        )

    def _send_deterministic_reply(
        self,
        *,
        message_id: str,
        from_addr: str,
        subject: str,
        body: str,
        next_state: str,
        event_name: str,
        detail: str = "",
    ) -> None:
        """Common path: notify_principal + state transition + log event.

        Robust to SMTP failures: logs + transitions to RESPONDED_FAILED
        if the send didn't go out. The inbound message stays recorded
        regardless.
        """
        if self.config.smtp is None:
            self.logger.event(
                "principal_reply_skipped",
                level="warn",
                inbox=self.inbox.name,
                message_id=message_id,
                reason="no_smtp_config",
            )
            return
        try:
            send = notifier.notify_principal(
                smtp=self.config.smtp,
                principal_addr=from_addr,
                subject=subject,
                body=body,
                jlogger=self.logger, state=self.state, related_message_id=message_id,
                in_reply_to=message_id,
            )
        except Exception as e:
            self.logger.event(
                "principal_reply_error",
                level="error",
                inbox=self.inbox.name,
                message_id=message_id,
                error=type(e).__name__,
                message=str(e),
            )
            return
        if send.primary_sent:
            self.state.transition(
                message_id=message_id,
                from_state="RECEIVED",
                to_state=next_state,
                detail=detail,
            )
            self.logger.event(
                event_name,
                inbox=self.inbox.name,
                message_id=message_id,
                detail=detail,
                reply_message_id=send.primary_message_id,
            )
        else:
            self.state.transition(
                message_id=message_id,
                from_state="RECEIVED",
                to_state="RESPONDED_FAILED",
                detail=f"send error: {send.error}",
            )
            self.logger.event(
                "principal_reply_failed",
                level="error",
                inbox=self.inbox.name,
                message_id=message_id,
                error=send.error,
            )

    @staticmethod
    def _extract_literal(data: list) -> bytes | None:
        """Pull the literal payload out of an aioimaplib fetch response.

        A typical single-UID fetch response is structured as:
            [0] bytes      "1 FETCH (UID 1 BODY[HEADER] {N}"
            [1] bytearray  <N bytes of payload>
            [2] bytes      ")"
            [3] bytes      "Success"  (or similar trailing token)

        The robust extraction is: find the literal-size descriptor `{N}`
        in any element, then look for a subsequent element of length N.
        Falls back to the largest bytearray/bytes blob if the descriptor
        is malformed.
        """
        import re

        expected_size: int | None = None
        for chunk in data:
            if isinstance(chunk, (bytes, bytearray)):
                m = re.search(rb"\{(\d+)\}", bytes(chunk))
                if m:
                    expected_size = int(m.group(1))
                    break

        if expected_size is not None:
            for chunk in data:
                if isinstance(chunk, (bytes, bytearray)) and len(chunk) == expected_size:
                    return bytes(chunk)

        # Fallback: pick the largest bytes/bytearray that looks like
        # a real header block (contains "From:" or "Date:").
        candidate: bytes | None = None
        for chunk in data:
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            blob = bytes(chunk)
            if (b"From:" in blob or b"Date:" in blob) and (
                candidate is None or len(blob) > len(candidate)
            ):
                candidate = blob
        return candidate

    @staticmethod
    def _decode_header(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    async def _handle_contact_triage(
        self,
        *,
        client: aioimaplib.IMAP4_SSL,
        uid: str,
        message_id: str,
        contact_id: str,
        from_addr: str,
        from_header: str,
        subject: str | None,
        date_header: str,
    ) -> None:
        """Run a triage call on contact mail, queue an approval if a
        plan came back, transition the message accordingly.

        Outcomes (with the message state transition recorded for each):

        - No claude_client wired:        RECEIVED -> TRIAGE_SKIPPED
        - Rate-limit cap hit:            RECEIVED -> TRIAGE_FAILED
        - Body fetch failed:             RECEIVED -> TRIAGE_FAILED
        - SDK error:                     RECEIVED -> TRIAGE_FAILED
        - Plan validation error:         RECEIVED -> TRIAGE_FAILED
        - Plan with verb=noop:           RECEIVED -> TRIAGED (no approval queued)
        - Plan with action verb:         RECEIVED -> TRIAGED + approval queued

        Failures ping the principal so they know an email arrived but
        triage didn't complete; they can act manually if needed.
        """
        # 1. Bail early if no client (no [claude] section).
        if self._claude_client is None:
            self.state.transition(
                message_id=message_id,
                from_state="RECEIVED",
                to_state="TRIAGE_SKIPPED",
                detail="no_claude_config",
            )
            self.logger.event(
                "triage_skipped",
                inbox=self.inbox.name, message_id=message_id,
                contact_id=contact_id, reason="no_claude_config",
            )
            return

        claude_cfg = self.config.claude
        assert claude_cfg is not None  # client is non-None iff claude config is set

        # 2. Rate limit (in-daemon, second-line defence after console cap).
        now = int(time.time())
        recent = self.state.count_claude_invocations_since(
            since_ts=now - RATE_LIMIT_WINDOW_SECONDS
        )
        if recent >= claude_cfg.per_hour_max_invocations:
            self.state.transition(
                message_id=message_id,
                from_state="RECEIVED",
                to_state="TRIAGE_FAILED",
                detail="cap_blocked",
            )
            self.logger.event(
                "triage_cap_blocked",
                level="warn",
                inbox=self.inbox.name, message_id=message_id,
                contact_id=contact_id,
                recent_invocations=recent,
                cap=claude_cfg.per_hour_max_invocations,
            )
            self._notify_principal_of_triage_problem(
                message_id=message_id, contact_id=contact_id, from_addr=from_addr,
                from_header=from_header, subject=subject, date_header=date_header,
                body_text=None, body_truncated=False,
                reason="cap_blocked",
                detail=(
                    f"Triage cap of {claude_cfg.per_hour_max_invocations}/hour\n"
                    f"reached. The email is preserved in the inbox; respond\n"
                    f"manually or wait for the rate window to reopen.\n"
                ),
            )
            return

        # 3. Body fetch (second IMAP round trip, only for triage-eligible mail).
        body_result = await self._fetch_body_text(client, uid)
        if body_result is None:
            self.state.transition(
                message_id=message_id,
                from_state="RECEIVED",
                to_state="TRIAGE_FAILED",
                detail="body_fetch_failed",
            )
            self._notify_principal_of_triage_problem(
                message_id=message_id, contact_id=contact_id, from_addr=from_addr,
                from_header=from_header, subject=subject, date_header=date_header,
                body_text=None, body_truncated=False,
                reason="body_fetch_failed",
                detail="Could not extract a plaintext body from the email.\n"
                       "It may be HTML-only or use an unrecognised encoding.\n",
            )
            return
        body_text, body_truncated = body_result

        # 4. The triage call itself.
        contact = self.config.contacts[contact_id]
        plan_or_err = await triage.triage_contact_mail(
            contact=contact,
            sender=from_addr,
            subject=subject or "",
            body=body_text,
            config=claude_cfg,
            client=self._claude_client,
            prompts_dir=PROMPTS_DIR,
        )

        # 5. Ledger row regardless of outcome (audit trail, rate counter).
        if isinstance(plan_or_err, TriagePlan):
            self.state.record_claude_invocation(
                purpose="triage",
                contact_id=contact_id,
                model=claude_cfg.default_model,
                input_tokens=plan_or_err.raw_input_tokens,
                output_tokens=plan_or_err.raw_output_tokens,
                ok=True,
                ts=now,
            )
        else:
            self.state.record_claude_invocation(
                purpose="triage",
                contact_id=contact_id,
                model=claude_cfg.default_model,
                input_tokens=0, output_tokens=0,
                ok=False,
                error_reason=plan_or_err.reason,
                ts=now,
            )

        # 6. Failure path.
        if isinstance(plan_or_err, TriageError):
            self.state.transition(
                message_id=message_id,
                from_state="RECEIVED",
                to_state="TRIAGE_FAILED",
                detail=plan_or_err.reason,
            )
            self.logger.event(
                "triage_failed",
                level="warn",
                inbox=self.inbox.name, message_id=message_id,
                contact_id=contact_id,
                reason=plan_or_err.reason,
                detail=plan_or_err.detail,
            )
            self._notify_principal_of_triage_problem(
                message_id=message_id, contact_id=contact_id, from_addr=from_addr,
                from_header=from_header, subject=subject, date_header=date_header,
                body_text=body_text, body_truncated=body_truncated,
                reason=plan_or_err.reason,
                detail=(
                    f"Triage failed: {plan_or_err.reason}\n"
                    f"Detail: {plan_or_err.detail}\n"
                ),
            )
            return

        # 7. Success path.
        plan = plan_or_err
        self.logger.event(
            "triage_complete",
            inbox=self.inbox.name, message_id=message_id,
            contact_id=contact_id,
            verb=plan.verb, tier=plan.tier,
            risk_flags=list(plan.risk_flags),
            input_tokens=plan.raw_input_tokens,
            output_tokens=plan.raw_output_tokens,
        )
        self.state.transition(
            message_id=message_id,
            from_state="RECEIVED",
            to_state="TRIAGED",
            detail=f"verb={plan.verb}",
        )

        # noop / forward / flag don't queue an executor verb. They still
        # need a human-visible ping so the principal sees the triage
        # output.
        if plan.verb in ("noop", "forward_to_principal", "flag_for_review"):
            self._send_triage_summary_to_principal(
                message_id=message_id, contact_id=contact_id,
                from_addr=from_addr, from_header=from_header,
                subject=subject, date_header=date_header,
                plan=plan, body_text=body_text,
                body_truncated=body_truncated,
            )
            return

        # `reply` queues an approval. The args carry everything the
        # tier-3 _exec_reply needs (contact_id, body, subject,
        # in_reply_to). Subject is built from the inbound subject so
        # the reply threads correctly.
        if plan.verb == "reply":
            reply_subject = self._build_reply_subject(subject)
            args = {
                "contact_id": contact_id,
                "body": plan.args["body"],
                "subject": reply_subject,
                "in_reply_to": message_id,
            }
            self._queue_triage_approval(
                message_id=message_id, contact_id=contact_id,
                from_addr=from_addr, from_header=from_header,
                subject=subject, date_header=date_header,
                plan=plan, verb="reply",
                args=args, body_text=body_text,
                body_truncated=body_truncated,
            )
            return

        # Defensive: unreachable given TRIAGE_VERBS, but if a future
        # prompt revision adds a verb without wiring it, fail loud.
        self.logger.event(
            "triage_unhandled_verb",
            level="error",
            inbox=self.inbox.name, message_id=message_id,
            verb=plan.verb,
        )

    def _send_triage_summary_to_principal(
        self,
        *,
        message_id: str,
        contact_id: str,
        from_addr: str,
        from_header: str,
        subject: str | None,
        date_header: str,
        plan: TriagePlan,
        body_text: str | None,
        body_truncated: bool,
    ) -> None:
        """For verbs that don't queue an executor (noop, forward, flag),
        ping the principal with the triage output so they're not blind
        to the email. Always appends the verbatim original email so the
        principal can verify the LLM's reading."""
        flags_line = (
            f"Risk flags: {', '.join(plan.risk_flags)}"
            if plan.risk_flags else "Risk flags: (none)"
        )
        notes_line = f"\nNotes from triage:\n  {plan.notes}\n" if plan.notes else ""
        body = (
            f"Triage of inbound mail from {contact_id} ({from_addr}).\n"
            f"\n"
            f"Verb proposed:    {plan.verb}\n"
            f"{flags_line}\n"
            f"\n"
            f"Summary from triage:\n  {plan.summary}\n"
            f"\n"
            f"Reasoning:\n  {plan.reasoning}\n"
            f"{notes_line}"
            + self._format_original_email_block(
                from_header=from_header, subject=subject,
                date_header=date_header, body_text=body_text,
                body_truncated=body_truncated,
            )
        )
        send = notifier.notify_principal(
            smtp=self.config.smtp,
            principal_addr=self._principal_addr(),
            subject=f"[Nightjar] triage: {plan.verb} from {contact_id}",
            body=body,
            jlogger=self.logger, state=self.state, related_message_id=message_id,
        )
        if not send.primary_sent:
            self.logger.event(
                "triage_summary_send_failed",
                level="warn",
                message_id=message_id, contact_id=contact_id,
                error=send.error,
            )

    def _queue_triage_approval(
        self,
        *,
        message_id: str,
        contact_id: str,
        from_addr: str,
        from_header: str,
        subject: str | None,
        date_header: str,
        plan: TriagePlan,
        verb: str,
        args: dict,
        body_text: str | None,
        body_truncated: bool,
    ) -> None:
        """Queue a triage-derived approval row + ping the principal.

        Mirrors `_queue_tier2_plus` but the args come from the LLM,
        not the deterministic principal-command parser. The approval
        row is normal; the existing `_resolve_approval_reply` handles
        principal yes/no. The executor (`_exec_reply`) runs the verb
        once approved. Appends the verbatim original email at the
        bottom so the principal can read what the contact actually
        sent before approving.
        """
        token = self._generate_approval_token()
        self.state.queue_approval(
            token=token,
            message_id=message_id,
            verb=verb,
            args=args,
            tier=plan.tier,
        )
        flags_line = (
            f"Risk flags: {', '.join(plan.risk_flags)}\n"
            if plan.risk_flags else ""
        )
        notes_line = f"\nNotes from triage:\n  {plan.notes}\n" if plan.notes else ""
        confirm_phrase = "yes"  # triage caps at tier 3, single-confirm.
        approval_body = (
            f"Triage of inbound mail from {contact_id} ({from_addr}).\n"
            f"\n"
            f"Verb proposed:  {verb} (tier {plan.tier})\n"
            f"Triage summary:\n  {plan.summary}\n"
            f"\n"
            f"Reasoning:\n  {plan.reasoning}\n"
            f"{notes_line}"
            f"{flags_line}"
            f"\n"
            f"Drafted reply (will be sent if approved):\n"
            f"---\n"
            f"{args.get('body', '')}\n"
            f"---\n"
            f"\n"
            f"To approve, reply with:\n"
            f"  Subject: [<code>] [Nightjar #{token}] {confirm_phrase}\n"
            f"\n"
            f"To deny, reply with:\n"
            f"  Subject: [<code>] [Nightjar #{token}] no\n"
            f"\n"
            f"Approval expires in 7 days.\n"
            + self._format_original_email_block(
                from_header=from_header, subject=subject,
                date_header=date_header, body_text=body_text,
                body_truncated=body_truncated,
            )
        )
        send = notifier.notify_principal(
            smtp=self.config.smtp,
            principal_addr=self._principal_addr(),
            subject=f"[Nightjar #{token}] approval needed: {verb} (triage)",
            body=approval_body,
            jlogger=self.logger, state=self.state, related_message_id=message_id,
        )
        if send.primary_sent:
            self.state.transition(
                message_id=message_id,
                from_state="TRIAGED",
                to_state="AWAITING_APPROVAL",
                detail=f"verb={verb} tier={plan.tier} token={token}",
            )
            self.logger.event(
                "triage_approval_queued",
                inbox=self.inbox.name, message_id=message_id,
                contact_id=contact_id, verb=verb, tier=plan.tier,
                token=token,
            )
        else:
            self.logger.event(
                "triage_approval_send_failed",
                level="warn",
                message_id=message_id, contact_id=contact_id,
                error=send.error,
            )
            # Approval row remains queued; if the principal manages
            # to find the token through the log they can still approve.

    def _notify_principal_of_impersonation(
        self,
        *,
        message_id: str,
        from_addr: str,
        contact_id: str,
        subject: str | None,
        verdict,
    ) -> None:
        """Ping the principal when a DMARC-failing message claimed to
        come from a known contact (or the principal themselves). This
        is the impersonation-attempt signal."""
        is_principal_target = (
            contact_id in self.config.contacts
            and self.config.contacts[contact_id].is_principal
        )
        target_label = "you (the principal)" if is_principal_target else f"contact '{contact_id}'"
        body = (
            f"DMARC verdict failed for inbound mail claiming to come from\n"
            f"{target_label}.\n"
            f"\n"
            f"From header: {from_addr}\n"
            f"Subject:     {subject or '(no subject)'}\n"
            f"Verdict:     {verdict.verdict}\n"
            f"Reason:      {verdict.reason or '(none recorded)'}\n"
            f"\n"
            f"The message was DROPPED. No triage ran, no auth was attempted,\n"
            f"no further action will be taken.\n"
            f"\n"
            f"This is the signal that someone has attempted to impersonate\n"
            f"{target_label} via inbound mail. The trusted authserv\n"
            f"({self.inbox.trusted_authserv!r}) refused to authenticate the\n"
            f"sender's domain.\n"
            f"\n"
            f"You do NOT need to take action. This is informational so that\n"
            f"sustained impersonation attempts are visible.\n"
        )
        try:
            send = notifier.notify_principal(
                smtp=self.config.smtp,
                principal_addr=self._principal_addr(),
                subject=f"[Nightjar] DMARC failed for {from_addr}",
                body=body,
                jlogger=self.logger, state=self.state, related_message_id=message_id,
            )
            if not send.primary_sent:
                self.logger.event(
                    "impersonation_notify_failed",
                    level="warn",
                    message_id=message_id, error=send.error,
                )
        except Exception as e:
            # Best-effort: never let the notify path break message
            # processing.
            self.logger.event(
                "impersonation_notify_error",
                level="warn",
                message_id=message_id, error=type(e).__name__, message=str(e),
            )

    def _notify_principal_of_triage_problem(
        self,
        *,
        message_id: str,
        contact_id: str,
        from_addr: str,
        from_header: str,
        subject: str | None,
        date_header: str,
        body_text: str | None,
        body_truncated: bool,
        reason: str,
        detail: str,
    ) -> None:
        """Best-effort principal ping when triage couldn't produce a
        plan. Failures here are logged but don't escalate further.
        Appends the verbatim original email when one was available so
        the principal can decide whether to respond manually."""
        body = (
            f"An email arrived from {contact_id} ({from_addr}) but triage\n"
            f"could not produce a plan.\n\n"
            f"Reason: {reason}\n\n"
            f"{detail}\n"
            f"The original email is still in the inbox. You can reply\n"
            f"manually or block the contact via Nightjar if appropriate.\n"
            + self._format_original_email_block(
                from_header=from_header, subject=subject,
                date_header=date_header, body_text=body_text,
                body_truncated=body_truncated,
            )
        )
        send = notifier.notify_principal(
            smtp=self.config.smtp,
            principal_addr=self._principal_addr(),
            subject=f"[Nightjar] triage failed for {contact_id}",
            body=body,
            jlogger=self.logger, state=self.state, related_message_id=message_id,
        )
        if not send.primary_sent:
            self.logger.event(
                "triage_problem_notify_failed",
                level="warn",
                message_id=message_id, error=send.error,
            )

    def _principal_addr(self) -> str:
        """Resolve the principal's first address. Fails loud if there
        is no principal — the daemon shouldn't have started."""
        for c in self.config.contacts.values():
            if c.is_principal and c.addresses:
                return c.addresses[0]
        raise RuntimeError("no principal configured (config validation should have caught this)")

    @staticmethod
    def _format_original_email_block(
        *,
        from_header: str,
        subject: str | None,
        date_header: str,
        body_text: str | None,
        body_truncated: bool,
    ) -> str:
        """Render the verbatim original email for the principal-side
        audit ping. Goes at the bottom of every triage-related email so
        the principal can verify the LLM read what was actually there.

        body_text=None covers cases where the body never made it: the
        block still appears, with a placeholder, so the structure is
        consistent and the audit log is auditable even on failures.
        """
        body_part: str
        if body_text is None:
            body_part = "(body was not available to triage)\n"
        else:
            body_part = body_text
            if not body_part.endswith("\n"):
                body_part += "\n"
            if body_truncated:
                body_part += "\n[TRUNCATED at 32 KiB; see raw mail for the rest]\n"

        return (
            "\n========== ORIGINAL EMAIL ==========\n"
            f"From:    {from_header or '(missing)'}\n"
            f"Subject: {subject or '(no subject)'}\n"
            f"Date:    {date_header or '(missing)'}\n"
            "----------\n"
            f"{body_part}"
            "========== END ORIGINAL ==========\n"
        )

    @staticmethod
    def _build_reply_subject(inbound_subject: str | None) -> str:
        """Build a Re:-prefixed subject for the reply, deduplicating
        existing Re:/Fwd: prefixes."""
        s = (inbound_subject or "").strip()
        if not s:
            return "Re: (no subject)"
        # Don't double-prefix Re:.
        lower = s.lower()
        if lower.startswith("re:") or lower.startswith("fwd:") or lower.startswith("fw:"):
            return s
        return f"Re: {s}"

    async def _fetch_body_text(
        self, client: aioimaplib.IMAP4_SSL, uid: str
    ) -> tuple[str, bool] | None:
        """Fetch the full message for `uid` and return its plaintext body.

        Returns `(body_text, was_truncated)` on success, or None if no
        usable body could be extracted (multipart with no text part,
        decode failure, IMAP error). Body is capped at MAX_TRIAGE_BODY_BYTES
        and `was_truncated` reflects whether the cap fired.

        Body fetch is a SECOND round-trip (after the headers fetch) and
        is gated on the message being triage-eligible. Strangers,
        blocked contacts, and principal mail never trigger a body fetch
        because they don't need triage.
        """
        result, data = await client.uid("fetch", uid, "(BODY.PEEK[])")
        if result != "OK" or not data:
            self.logger.event(
                "body_fetch_failed",
                inbox=self.inbox.name, level="warn", uid=uid, result=result,
            )
            return None
        blob = self._extract_literal(data)
        if blob is None:
            self.logger.event(
                "body_fetch_no_literal",
                inbox=self.inbox.name, level="warn", uid=uid,
            )
            return None
        try:
            msg = email.message_from_bytes(blob)
            text, truncated = self._extract_plain_text(msg)
        except Exception as e:
            self.logger.event(
                "body_decode_failed",
                inbox=self.inbox.name, level="warn", uid=uid, error=str(e),
            )
            return None
        if text is None:
            return None
        return text, truncated

    @staticmethod
    def _extract_plain_text(msg: email.message.Message) -> tuple[str | None, bool]:
        """Walk a parsed email and return its plaintext body.

        Strategy: find the first `text/plain` part (or use the message
        body itself if not multipart). Decode using the part's stated
        charset, falling back to utf-8 with replacement so a single
        bad byte doesn't drop the whole body. Cap at
        MAX_TRIAGE_BODY_BYTES; returns `truncated=True` if we cut.

        Returns (None, False) if no plaintext part is found at all.
        Multipart/alternative messages where only HTML is present are
        intentionally rejected: we don't HTML-strip in v1, because
        getting that wrong is a triage-quality regression and we'd
        rather drop to TRIAGE_FAILED than feed garbage to the LLM.
        """
        chosen: email.message.Message | None = None
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.is_multipart():
                    chosen = part
                    break
        else:
            if msg.get_content_type() == "text/plain":
                chosen = msg

        if chosen is None:
            return None, False

        payload = chosen.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)):
            return None, False

        charset = chosen.get_content_charset() or "utf-8"
        try:
            text = bytes(payload).decode(charset, errors="replace")
        except (LookupError, TypeError):
            text = bytes(payload).decode("utf-8", errors="replace")

        truncated = False
        if len(text.encode("utf-8")) > MAX_TRIAGE_BODY_BYTES:
            text = text.encode("utf-8")[:MAX_TRIAGE_BODY_BYTES].decode(
                "utf-8", errors="ignore"
            )
            truncated = True

        return text, truncated
