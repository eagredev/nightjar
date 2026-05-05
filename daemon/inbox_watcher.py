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
import base64
import contextlib
import email
import email.utils
import random
import secrets
import time
from email.header import decode_header, make_header
from pathlib import Path

from aioimaplib import aioimaplib

from . import (
    auth,
    cost_guard,
    dmarc,
    executor,
    notifier,
    principal_commands,
    principal_handlers,
    principal_interpret,
    status_report,
    triage,
)
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
from .principal_interpret import (
    ActionProposal,
    DaemonStateSnapshot,
    DeterministicDispatch,
    InlineResponse,
    InterpretError,
    VerbRegistrySummary,
)
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
        """Reconcile recent IMAP mail against the state-db.

        Step 6e (receipt reliability) replaced the prior `UNSEEN`-based
        catchup with a date-windowed `SINCE` search plus state-db dedup.
        Reasoning: \\Seen is not a reliable "have I processed this?"
        signal — anything that touches the mailbox (Gmail web preview,
        a phone client, a daemon crash mid-flight) can flip it and
        cause silent drops. Message-ID lookup against the messages
        table is the authoritative dedup.

        Window:
            lower_bound = max(now - catchup_window_days, watermark - 1d)
        The 1-day overlap absorbs clock skew and crashes between fetch
        and record. First run (watermark NULL) uses a wider 30-day
        sweep to surface any mail that was silently dropped under the
        old UNSEEN-based logic.
        """
        now = int(time.time())
        watermark = self.state.get_last_catchup_at(self.inbox.name)
        first_run = watermark is None

        # Window in days. First-run reconciliation gets a wider window
        # so we can find mail that the old UNSEEN logic may have
        # silently skipped. Steady-state catchup uses the configured
        # window with the standard 1-day overlap on top of the
        # watermark.
        if first_run:
            window_days = max(30, self.inbox.catchup_window_days)
            lower_ts = now - window_days * 86400
        else:
            window_lower = now - self.inbox.catchup_window_days * 86400
            watermark_lower = watermark - 86400  # 1-day overlap
            lower_ts = min(window_lower, watermark_lower)

        since_date = self._imap_since_date(lower_ts)
        result, data = await client.uid_search(f"SINCE {since_date}")
        if result != "OK":
            self.logger.event(
                "catchup_search_failed",
                inbox=self.inbox.name,
                level="warn",
                result=result,
                since=since_date,
            )
            return
        if not data or not data[0]:
            # No mail in window. Still advance watermark so we don't
            # re-search the same empty range forever.
            self.state.set_last_catchup_at(self.inbox.name, now)
            return
        uids = data[0].split()
        if not uids:
            self.state.set_last_catchup_at(self.inbox.name, now)
            return

        self.logger.event(
            "catchup_start",
            inbox=self.inbox.name,
            candidate_count=len(uids),
            since=since_date,
            first_run=first_run,
        )

        processed = 0
        skipped = 0
        errors = 0
        for uid in uids:
            try:
                outcome = await self._fetch_and_record(client, uid.decode("ascii"))
                if outcome == "processed":
                    processed += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    # Fetch surfaced a non-fatal failure (logged
                    # inside _fetch_and_record). Count as error so
                    # the catchup_complete totals add up.
                    errors += 1
            except Exception as e:
                errors += 1
                self.logger.event(
                    "catchup_fetch_error",
                    inbox=self.inbox.name,
                    level="warn",
                    uid=uid.decode("ascii", "replace"),
                    error=type(e).__name__,
                    message=str(e),
                )

        # Watermark advances on every successful pass, even if every
        # candidate dedup-skipped — we still verified the window is
        # clear up to `now`. We use the wall-clock `now` from the top
        # of the function rather than re-reading: the IMAP search ran
        # on what was visible at `now`, so that's the high-water mark
        # we can confidently claim.
        self.state.set_last_catchup_at(self.inbox.name, now)

        self.logger.event(
            "catchup_complete",
            inbox=self.inbox.name,
            candidates=len(uids),
            processed=processed,
            skipped=skipped,
            errors=errors,
        )

        # First-run reconciliation summary. This pass walked a wider
        # 30-day window to catch up any mail that the old UNSEEN-based
        # logic may have silently skipped. If we found anything new,
        # tell the principal so they can audit and decide whether
        # anything needs follow-up. Skip the ping if nothing new
        # turned up — first-run on a clean install is the common
        # case and a "found 0 messages" ping is just noise.
        if first_run and processed > 0:
            self._send_first_run_recon_summary(
                processed=processed, skipped=skipped, errors=errors,
                window_days=window_days, since=since_date,
            )

    @staticmethod
    def _imap_since_date(ts: int) -> str:
        """Format a unix timestamp as IMAP SINCE date (DD-Mon-YYYY).

        IMAP search dates are day-granular and use a fixed English
        month abbreviation regardless of locale. We use UTC because
        the watermark is stored as a UTC unix timestamp.
        """
        t = time.gmtime(ts)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        return f"{t.tm_mday:02d}-{months[t.tm_mon - 1]}-{t.tm_year:04d}"

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

    async def _fetch_and_record(self, client: aioimaplib.IMAP4_SSL, uid: str) -> str | None:
        """Fetch headers for one UID, look up sender, persist a message row.

        Returns:
            "processed" — message was new and a row was inserted (or
                          a downstream handler ran).
            "skipped"   — Message-ID already in state-db (dedup hit).
            None        — fetch failed; the failure is already logged.
        """
        result, data = await client.uid("fetch", uid, "(BODY.PEEK[HEADER])")
        if result != "OK" or not data:
            self.logger.event(
                "fetch_failed",
                inbox=self.inbox.name,
                level="warn",
                uid=uid,
                result=result,
            )
            return None

        header_blob = self._extract_literal(data)
        if header_blob is None:
            self.logger.event(
                "fetch_no_headers",
                inbox=self.inbox.name,
                uid=uid,
                level="warn",
                response_shape=[type(c).__name__ for c in data],
            )
            return None

        msg = email.message_from_bytes(header_blob)
        message_id = (msg.get("Message-ID") or "").strip()
        if not message_id:
            # Fall back to a synthetic ID combining inbox + UID. Not
            # globally unique but good enough to avoid duplicate inserts
            # for messages that lack a Message-ID header.
            message_id = f"<no-msgid-{self.inbox.name}-uid{uid}>"

        if self.state.message_exists(message_id):
            # Step 6e: log dedup hits at debug level so a catchup that
            # silently skips everything is diagnosable. The old UNSEEN
            # logic conflated "no work" with "all-skipped" and produced
            # phantom catchup_complete events with no trail.
            self.logger.event(
                "catchup_skipped_existing",
                inbox=self.inbox.name,
                level="debug",
                uid=uid,
                message_id=message_id,
            )
            return "skipped"

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
        if not inserted:
            # Race: between the message_exists() check above and the
            # record_message() call here, another path recorded this
            # ID. Treat as a dedup hit. Rare but possible if two
            # catchups overlap on a slow link.
            self.logger.event(
                "catchup_skipped_existing",
                inbox=self.inbox.name,
                level="debug",
                uid=uid,
                message_id=message_id,
                detail="race",
            )
            return "skipped"
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
            # Body is needed for approval-reply verdict extraction
            # (`yes` / `no` / `YES IRREVERSIBLE` lives in the reply
            # body, not the subject). For tier-1 verbs and free-form
            # requests the body is unused but the extra IMAP round-trip
            # is cheap. None on fetch failure is fine: the parser
            # tolerates an absent body and routes to UNCLEAR for
            # approval replies, which prompts the principal to retry.
            body_result = await self._fetch_body_text(client, uid)
            body_text = body_result[0] if body_result is not None else None
            await self._dispatch_principal_command(
                message_id=message_id, subject=subject,
                body=body_text, from_addr=from_addr,
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

        return "processed"

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

    async def _dispatch_principal_command(
        self,
        *,
        message_id: str,
        subject: str | None,
        body: str | None,
        from_addr: str,
    ) -> None:
        """Parse and handle one authenticated principal email.

        Four branches:

        1. Approval-token reply: look up the pending approval, validate
           the verdict (tier-2 needs APPROVE, tier-4 needs IRREVERSIBLE),
           on approve dispatch the executor, send confirmation. State
           transitions to APPROVED, DENIED, or APPROVAL_UNCLEAR.

        2. Tier-1 verb: dispatch the handler, send the reply, transition
           to RESPONDED.

        3. Tier-2+ verb: queue an approval row with a fresh token, ping
           the principal with [Nightjar #token] subject describing the
           proposed action, transition to AWAITING_APPROVAL.

        4. Free-form: hand directly to the principal-interpret pass
           (Claude call). The pass either answers inline (tier-1) or
           proposes a structured plan that joins the approval queue
           (tier-2+). The earlier "yes interpret" confirmation gate was
           removed: an authenticated principal sending free-form already
           authorises interpretation within the tier ceiling.

        SMTP failures here are non-fatal: the inbound message stays
        recorded; the operator just doesn't get a reply. That's a
        better failure mode than crashing the watcher.
        """
        cmd = principal_commands.parse_principal_command(subject, body)

        if cmd.approval_token is not None:
            self._resolve_approval_reply(
                message_id=message_id, command=cmd, from_addr=from_addr
            )
            return

        if cmd.tier == 1:
            await self._send_tier1_reply(
                message_id=message_id, command=cmd, from_addr=from_addr
            )
            return

        if cmd.tier is not None and cmd.tier >= 2:
            self._queue_tier2_plus(
                message_id=message_id, command=cmd, from_addr=from_addr
            )
            return

        # Free-form: direct interpretation pass via Claude.
        await self._dispatch_principal_interpret(
            message_id=message_id, command=cmd, from_addr=from_addr
        )

    async def _send_tier1_reply(
        self, *, message_id: str, command, from_addr: str
    ) -> None:
        """Run a tier-1 handler and email the reply to the principal.

        The `status` verb (Step 6g) is special-cased here because its
        report includes an IMAP walk per inbox. The walk requires a
        fresh IMAP connection (the running IDLE client is busy in
        IDLE state and cannot fetch). All other tier-1 verbs go
        through the synchronous principal_handlers.dispatch path
        unchanged.
        """
        if command.verb == "status":
            await self._dispatch_status(
                message_id=message_id, from_addr=from_addr,
            )
            return
        if command.verb == "pickup":
            await self._dispatch_pickup(
                message_id=message_id, from_addr=from_addr,
                args=command.args,
            )
            return
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

    async def _dispatch_status(
        self, *, message_id: str, from_addr: str,
    ) -> None:
        """Build and email the structured status report.

        Builds the StatusReport from state-db queries plus per-inbox
        IMAP walks (one transient connection per enabled inbox). The
        walks run sequentially; for one inbox this is the common case,
        for many it could be parallelised later.
        """
        async def _walker(inbox_name: str, walk_count: int):
            inbox_cfg = self.config.inboxes.get(inbox_name)
            if inbox_cfg is None:
                return status_report.InboxWalkResult(
                    inbox=inbox_name, walked_count=0,
                    headers=(), error="no such inbox",
                )
            return await walk_inbox_for_status(
                inbox_cfg=inbox_cfg, walk_count=walk_count,
            )

        try:
            report = await status_report.build_status_report(
                state=self.state, config=self.config,
                walker=_walker,
            )
        except Exception as e:
            self.logger.event(
                "status_report_build_failed",
                level="error",
                inbox=self.inbox.name,
                error=type(e).__name__, message=str(e),
            )
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: status report failed",
                body=(
                    f"Status report build raised {type(e).__name__}: {e}\n"
                    "The daemon is still running; this is just the report\n"
                    "builder erroring. Re-issue or check the daemon log.\n"
                ),
                next_state="EXECUTION_FAILED",
                event_name="principal_status_failed",
                detail=type(e).__name__,
            )
            return

        body = status_report.render_status_report(report)
        self.logger.event(
            "principal_status_dispatched",
            inbox=self.inbox.name, message_id=message_id,
            awaiting=len(report.awaiting),
            expiring=len(report.expiring),
            in_flight=len(report.in_flight),
            out_of_band_total=sum(len(v) for v in report.out_of_band.values()),
        )
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject="Nightjar: status",
            body=body,
            next_state="RESPONDED",
            event_name="principal_status_replied",
            detail=f"oob={sum(len(v) for v in report.out_of_band.values())}",
        )

    async def _dispatch_pickup(
        self, *, message_id: str, from_addr: str, args: dict,
    ) -> None:
        """Re-triage a single message named by Message-ID.

        `pickup` opens a fresh IMAP connection, finds the message by
        Message-ID via `UID SEARCH HEADER Message-ID "<id>"`, fetches
        its body, removes any prior state-db row (so triage starts
        clean), and routes through the same triage-and-queue path the
        IDLE-driven flow uses.

        Tier 1 (auto-execute on principal authentication). The triage
        call itself spends Claude tokens, so the cost-cap rules from
        Step 6f apply via the principal-interpret cost backstop infra.
        Failures email the principal explaining why pickup couldn't
        complete; they don't crash the watcher or alter state-db.
        """
        target_message_id = args.get("message_id", "").strip()
        # Strip optional surrounding angle brackets — operators copy
        # Message-IDs both ways and we're tolerant of either form.
        if target_message_id.startswith("<") and target_message_id.endswith(">"):
            normalised = target_message_id
        elif target_message_id:
            normalised = f"<{target_message_id}>"
        else:
            normalised = ""
        if not normalised or "@" not in normalised:
            self._send_deterministic_reply(
                message_id=message_id, from_addr=from_addr,
                subject="Nightjar: pickup needs a Message-ID",
                body=(
                    "The pickup verb needs a Message-ID argument that\n"
                    "looks like an email address in angle brackets:\n"
                    "    [<code>] pickup <abc123@mail.example>\n"
                    "\n"
                    "(Got: " + (target_message_id or "(empty)") + ")\n"
                ),
                next_state="EXECUTION_FAILED",
                event_name="principal_pickup_bad_arg",
                detail=target_message_id[:80],
            )
            return

        # Walk all enabled inboxes looking for the target. Common case
        # is one inbox; for multi-inbox setups we stop at the first hit.
        located: tuple[str, str] | None = None  # (inbox_name, uid)
        errors: list[str] = []
        for inbox_name, inbox_cfg in self.config.inboxes.items():
            if not inbox_cfg.enabled:
                continue
            uid_or_err = await _imap_find_by_message_id(
                inbox_cfg=inbox_cfg, target_message_id=normalised,
            )
            if isinstance(uid_or_err, str) and uid_or_err.startswith("err:"):
                errors.append(f"{inbox_name}: {uid_or_err[4:]}")
                continue
            if uid_or_err is not None:
                located = (inbox_name, uid_or_err)
                break

        if located is None:
            self.logger.event(
                "principal_pickup_not_found",
                level="warn",
                inbox=self.inbox.name, message_id=message_id,
                target_message_id=normalised,
                errors=errors,
            )
            self._send_deterministic_reply(
                message_id=message_id, from_addr=from_addr,
                subject="Nightjar: pickup target not found",
                body=(
                    f"Could not locate a message with Message-ID:\n"
                    f"  {normalised}\n"
                    f"\n"
                    f"Inboxes searched: "
                    f"{', '.join(self.config.inboxes.keys()) or '(none)'}\n"
                    + (
                        f"\nIMAP errors: {'; '.join(errors)}\n"
                        if errors else ""
                    ) +
                    f"\nIf the message is in Spam, the next step is\n"
                    f"the audit command (when it lands) — pickup only\n"
                    f"walks INBOX.\n"
                ),
                next_state="EXECUTION_FAILED",
                event_name="principal_pickup_not_found_replied",
                detail=normalised[:80],
            )
            return

        target_inbox_name, target_uid = located
        # Discard any prior state-db row so triage runs from scratch.
        # The state-db has no native delete-row API; we just expose the
        # message_id to triage and let the existing record-or-skip
        # logic re-record it on the path through. To force a clean
        # re-pickup we drop the existing row first.
        if self.state.message_exists(normalised):
            with self.state._connect() as conn:
                conn.execute(
                    "DELETE FROM messages WHERE id = ?", (normalised,)
                )
                conn.execute(
                    "DELETE FROM transitions WHERE message_id = ?",
                    (normalised,)
                )
            self.logger.event(
                "principal_pickup_cleared_prior_row",
                inbox=self.inbox.name, message_id=message_id,
                target_message_id=normalised,
            )

        # Confirmation reply — pickup queued. Triage runs synchronously
        # below; we email the result separately if it produces a plan,
        # the same way the IDLE flow does.
        self.logger.event(
            "principal_pickup_dispatched",
            inbox=self.inbox.name, message_id=message_id,
            target_message_id=normalised,
            target_inbox=target_inbox_name, target_uid=target_uid,
        )
        self._send_deterministic_reply(
            message_id=message_id, from_addr=from_addr,
            subject="Nightjar: pickup queued",
            body=(
                f"Picking up {normalised} from inbox '{target_inbox_name}'\n"
                f"(IMAP UID {target_uid}). Triage will run now; you'll\n"
                f"receive an approval ping or a 'no action needed' reply\n"
                f"once it completes.\n"
            ),
            next_state="RESPONDED",
            event_name="principal_pickup_queued",
            detail=normalised[:80],
        )

        # Now run triage by opening another fresh IMAP, fetching the
        # full message body via UID, and feeding it through the
        # existing _handle_contact_triage pipeline. This re-uses all
        # the existing routing including the principal-not-self-triage
        # guard, so even if the caller sneaks the principal's own
        # Message-ID, the triage path will skip it.
        await self._run_pickup_triage(
            target_message_id=normalised,
            target_inbox_name=target_inbox_name,
            target_uid=target_uid,
        )

    async def _run_pickup_triage(
        self, *, target_message_id: str,
        target_inbox_name: str, target_uid: str,
    ) -> None:
        """Open a fresh IMAP connection to the target inbox, fetch the
        full message, and route it through the same record-and-triage
        path the IDLE flow uses."""
        target_cfg = self.config.inboxes[target_inbox_name]
        client = aioimaplib.IMAP4_SSL(
            host=target_cfg.imap_host, port=target_cfg.imap_port,
        )
        try:
            await client.wait_hello_from_server()
            login_response = await client.login(
                target_cfg.imap_user, target_cfg.imap_password,
            )
            if login_response.result != "OK":
                self.logger.event(
                    "principal_pickup_login_failed",
                    level="warn",
                    inbox=self.inbox.name,
                    target_inbox=target_inbox_name,
                    target_message_id=target_message_id,
                )
                return
            select_response = await client.select("INBOX")
            if select_response.result != "OK":
                self.logger.event(
                    "principal_pickup_select_failed",
                    level="warn",
                    inbox=self.inbox.name,
                    target_inbox=target_inbox_name,
                    target_message_id=target_message_id,
                )
                return
            # Reuse _fetch_and_record on a temporary watcher view of
            # the target inbox. The current InboxWatcher instance may
            # be running for a DIFFERENT inbox than the target; we
            # need the routing to reflect target_inbox_name. To avoid
            # constructing a whole new InboxWatcher with all its
            # state, we monkey-patch self.inbox temporarily for the
            # duration of the call and revert afterwards.
            original_inbox = self.inbox
            try:
                self.inbox = target_cfg
                await self._fetch_and_record(client, target_uid)
            finally:
                self.inbox = original_inbox
        except Exception as e:
            self.logger.event(
                "principal_pickup_triage_failed",
                level="error",
                inbox=self.inbox.name,
                target_inbox=target_inbox_name,
                target_message_id=target_message_id,
                error=type(e).__name__, message=str(e),
            )
        finally:
            with contextlib.suppress(Exception):
                await client.logout()

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
            "body must be the literal phrase YES IRREVERSIBLE in uppercase.\n"
            if command.tier >= 4
            else "This is a tier-2 verb (reversible local writes). A plain\n"
                 "'yes' (or 'approve' / 'go') is enough.\n"
        )
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject=f"[Nightjar #{token}]",
            body=(
                f"Approval needed: {command.verb}\n"
                "\n"
                f"Verb:   {command.verb}\n"
                f"Args:   {dict(command.args)}\n"
                f"Tier:   {command.tier}\n"
                "\n"
                f"{tier_note}"
                "\n"
                "To approve, hit reply, paste your code at the end of the\n"
                "auto-filled subject, and put your verdict in the body:\n"
                f"  Subject: Re: [Nightjar #{token}] <code>\n"
                f"  Body:    {confirm_phrase}\n"
                "\n"
                "To deny, same subject, body 'no'.\n"
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
                    "Reply with:\n"
                    "\n"
                    f"  Subject: Re: [Nightjar #{token}] <code>\n"
                    f"  Body:    YES IRREVERSIBLE\n"
                    "\n"
                    "The body must be the literal phrase YES IRREVERSIBLE in\n"
                    "uppercase, as a standalone first line. The approval is\n"
                    "still pending until you do.\n"
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
                    f"  Subject: Re: [Nightjar #{token}] <code>\n"
                    "  Body:    yes      (or 'no' to deny)\n"
                    + (
                        f"\n  Tier-4 verbs ({verb} is tier 4) require body\n"
                        "  YES IRREVERSIBLE in uppercase.\n"
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

    async def _dispatch_principal_interpret(
        self, *, message_id: str, command, from_addr: str
    ) -> None:
        """Hand a free-form principal request to the principal-interpret
        Claude pass.

        Outcomes (with the message-state transition for each):

          - No claude_client wired:           RECEIVED -> INTERPRET_SKIPPED
          - Rate-limit cap hit:               RECEIVED -> INTERPRET_FAILED
          - SDK / validation error:           RECEIVED -> INTERPRET_FAILED
          - InlineResponse:                   RECEIVED -> RESPONDED
          - DeterministicDispatch:            RECEIVED -> RESPONDED (verb runs)
          - ActionProposal (tier 2 or 3):     RECEIVED -> AWAITING_APPROVAL

        Failure paths still send the principal a deterministic reply so
        they know the request was received but produced nothing
        actionable.
        """
        if self._claude_client is None:
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: interpret skipped (no Claude config)",
                body=(
                    "Free-form request received, but the daemon has no\n"
                    "[claude] section configured, so the principal-interpret\n"
                    "pass cannot run. Re-issue your request as a deterministic\n"
                    "verb (e.g. `[code] list pending`) or add a [claude]\n"
                    "section to nightjar.conf and restart the daemon.\n"
                ),
                next_state="INTERPRET_SKIPPED",
                event_name="principal_interpret_skipped",
                detail="no_claude_config",
            )
            return

        claude_cfg = self.config.claude
        assert claude_cfg is not None  # client is non-None iff config is

        # In-daemon rate limit: same window the contact-triage path uses.
        now = int(time.time())
        recent = self.state.count_claude_invocations_since(
            since_ts=now - RATE_LIMIT_WINDOW_SECONDS
        )
        if recent >= claude_cfg.per_hour_max_invocations:
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: interpret cap reached",
                body=(
                    f"Free-form request received, but the in-daemon Claude\n"
                    f"cap of {claude_cfg.per_hour_max_invocations}/hour has\n"
                    f"been reached. Re-issue once the rate window reopens,\n"
                    f"or use a deterministic verb (which doesn't spend the\n"
                    f"Claude budget).\n"
                ),
                next_state="INTERPRET_FAILED",
                event_name="principal_interpret_cap_blocked",
                detail=f"recent={recent}",
            )
            return

        snapshot = self._build_daemon_state_snapshot()
        registry = self._build_verb_registry_summary()

        outcome = await principal_interpret.interpret_principal_request(
            request_subject=command.raw_subject or "",
            request_body=command.payload or "",
            state_snapshot=snapshot,
            verb_registry=registry,
            config=claude_cfg,
            client=self._claude_client,
            prompts_dir=PROMPTS_DIR,
        )

        # Ledger row for every call (success or fail). The token counts
        # are zero on failure.
        if isinstance(outcome, InterpretError):
            self.state.record_claude_invocation(
                purpose="principal_interpret",
                contact_id="principal",
                model=claude_cfg.default_model,
                input_tokens=0, output_tokens=0,
                ok=False,
                error_reason=outcome.reason,
                ts=now,
            )
            self.logger.event(
                "principal_interpret_failed",
                level="warn",
                inbox=self.inbox.name, message_id=message_id,
                reason=outcome.reason, detail=outcome.detail,
            )
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: interpret failed",
                body=(
                    f"Free-form interpretation didn't produce a usable plan.\n"
                    f"\n"
                    f"Reason: {outcome.reason}\n"
                    f"Detail: {outcome.detail}\n"
                    f"\n"
                    f"Try re-issuing with a deterministic verb (see\n"
                    f"`list pending`/`status`/`show contact`/etc.) or\n"
                    f"rephrase the request more directly.\n"
                ),
                next_state="INTERPRET_FAILED",
                event_name="principal_interpret_error_replied",
                detail=outcome.reason,
            )
            return

        # Success: record token spend.
        self.state.record_claude_invocation(
            purpose="principal_interpret",
            contact_id="principal",
            model=claude_cfg.default_model,
            input_tokens=outcome.raw_input_tokens,
            output_tokens=outcome.raw_output_tokens,
            ok=True,
            ts=now,
        )

        # Cost-cap check. The interpret-gate drop (#107) replaced the
        # up-front "yes interpret" confirmation with this post-hoc
        # backstop. Three bands:
        #   ok        -> dispatch as normal
        #   over_soft -> dispatch, but prepend a cost-overage notice
        #                (loud on tier-2+ approvals, brief on tier-1)
        #   over_hard -> refuse to surface the output; reply with a
        #                cost-killed notice instead.
        cost_verdict = cost_guard.evaluate_cost(
            model=claude_cfg.default_model,
            input_tokens=outcome.raw_input_tokens,
            output_tokens=outcome.raw_output_tokens,
            soft_cap_cents=claude_cfg.principal_per_message_cost_cents,
            hard_kill_multiplier=claude_cfg.principal_hard_kill_multiplier,
        )
        if cost_verdict.verdict == cost_guard.COST_OVER_HARD:
            self.logger.event(
                "principal_interpret_cost_killed",
                level="warn",
                inbox=self.inbox.name, message_id=message_id,
                cost_cents=round(cost_verdict.cost_cents_value, 4),
                soft_cap_cents=cost_verdict.soft_cap_cents,
                hard_cap_cents=cost_verdict.hard_cap_cents,
                input_tokens=outcome.raw_input_tokens,
                output_tokens=outcome.raw_output_tokens,
            )
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: interpret refused (cost over hard cap)",
                body=(
                    f"Free-form interpretation completed but the call cost\n"
                    f"{cost_guard.format_cents(cost_verdict.cost_cents_value)}, "
                    f"which is at or above the hard kill cap of "
                    f"{cost_guard.format_cents(cost_verdict.hard_cap_cents)} "
                    f"({cost_verdict.hard_cap_cents}\n"
                    f"cents = {claude_cfg.principal_hard_kill_multiplier}x the "
                    f"soft cap of {cost_verdict.soft_cap_cents} cents).\n"
                    f"\n"
                    f"The result is being dropped to defend against runaway\n"
                    f"costs (a single interpret pass should not require\n"
                    f"this much output). Re-issue with a more focused\n"
                    f"request, or raise [claude].principal_hard_kill_multiplier\n"
                    f"if this is legitimate.\n"
                    f"\n"
                    f"Tokens: input={outcome.raw_input_tokens}, "
                    f"output={outcome.raw_output_tokens}.\n"
                ),
                next_state="INTERPRET_FAILED",
                event_name="principal_interpret_cost_killed_replied",
                detail=(
                    f"cost={cost_verdict.cost_cents_value:.2f}c "
                    f"hard={cost_verdict.hard_cap_cents}c"
                ),
            )
            return

        is_overage = cost_verdict.verdict == cost_guard.COST_OVER_SOFT
        if is_overage:
            self.logger.event(
                "principal_interpret_cost_overage",
                level="info",
                inbox=self.inbox.name, message_id=message_id,
                cost_cents=round(cost_verdict.cost_cents_value, 4),
                soft_cap_cents=cost_verdict.soft_cap_cents,
                input_tokens=outcome.raw_input_tokens,
                output_tokens=outcome.raw_output_tokens,
            )

        if isinstance(outcome, InlineResponse):
            self.logger.event(
                "principal_interpret_inline",
                inbox=self.inbox.name, message_id=message_id,
                input_tokens=outcome.raw_input_tokens,
                output_tokens=outcome.raw_output_tokens,
            )
            body = outcome.body.rstrip() + "\n"
            if is_overage:
                # Brief, non-loud line for tier-1 — the principal isn't
                # being asked to authorise anything, so the warning is
                # informational.
                body = body + (
                    "\n"
                    f"(Note: this interpret cost "
                    f"{cost_guard.format_cents(cost_verdict.cost_cents_value)}, "
                    f"above your "
                    f"{cost_guard.format_cents(cost_verdict.soft_cap_cents)} "
                    f"soft cap.)\n"
                )
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject=f"Nightjar: {outcome.summary}",
                body=body,
                next_state="RESPONDED",
                event_name="principal_interpret_inline_replied",
                detail=outcome.summary[:80],
            )
            return

        if isinstance(outcome, DeterministicDispatch):
            self.logger.event(
                "principal_interpret_dispatched",
                inbox=self.inbox.name, message_id=message_id,
                verb=outcome.verb, args=outcome.args,
                input_tokens=outcome.raw_input_tokens,
                output_tokens=outcome.raw_output_tokens,
            )
            await self._dispatch_interpreted_tier1(
                message_id=message_id, from_addr=from_addr,
                outcome=outcome,
                cost_verdict=cost_verdict if is_overage else None,
            )
            return

        if isinstance(outcome, ActionProposal):
            self.logger.event(
                "principal_interpret_action_proposed",
                inbox=self.inbox.name, message_id=message_id,
                verb=outcome.verb, tier=outcome.tier,
                input_tokens=outcome.raw_input_tokens,
                output_tokens=outcome.raw_output_tokens,
            )
            self._queue_interpreted_action(
                message_id=message_id, from_addr=from_addr,
                outcome=outcome,
                cost_verdict=cost_verdict if is_overage else None,
            )
            return

        # Defensive: unreachable given the union shape, but if a future
        # shape gets added without wiring, fail loud.
        self.logger.event(
            "principal_interpret_unhandled_outcome",
            level="error",
            inbox=self.inbox.name, message_id=message_id,
            outcome_type=type(outcome).__name__,
        )

    def _build_daemon_state_snapshot(self) -> DaemonStateSnapshot:
        """Cheap state-db snapshot for the principal-interpret user
        message. No IMAP I/O. State counts are unconstrained-time
        (count_by_state aggregates the whole messages table); for now
        that's sufficient context for the LLM. A 24h-bounded variant
        can land later if the unbounded view becomes noisy."""
        counts = self.state.count_by_state()
        pending = self.state.list_pending_approvals()
        last_catchup_ts = self.state.get_last_catchup_at(self.inbox.name)
        if last_catchup_ts is None:
            last_iso = "(never)"
        else:
            import datetime
            last_iso = datetime.datetime.fromtimestamp(
                last_catchup_ts, tz=datetime.timezone.utc
            ).isoformat()
        return DaemonStateSnapshot(
            pending_approvals=tuple(pending),
            state_counts_24h=dict(counts),
            last_catchup_iso=last_iso,
        )

    def _build_verb_registry_summary(self) -> VerbRegistrySummary:
        """Project the deterministic verb registry into the per-tier
        summary the principal-interpret prompt expects."""
        tier1: list[str] = []
        tier23: list[str] = []
        for spec in principal_commands.VERB_REGISTRY:
            if spec.tier == 1:
                tier1.append(spec.name)
            elif 2 <= spec.tier <= 3:
                tier23.append(spec.name)
        return VerbRegistrySummary(
            tier1_names=tuple(tier1),
            tier2_3_names=tuple(tier23),
        )

    async def _dispatch_interpreted_tier1(
        self, *, message_id: str, from_addr: str,
        outcome: DeterministicDispatch,
        cost_verdict: "cost_guard.CostVerdict | None" = None,
    ) -> None:
        """Run a deterministic tier-1 verb the LLM picked, email the
        result, transition to RESPONDED. The synthetic ParsedCommand
        carries the LLM-chosen verb and args; principal_handlers.dispatch
        runs the same handler the user typing the verb directly would
        hit. The `status` verb routes through the async status-report
        path because it needs a fresh IMAP walk per inbox."""
        if outcome.verb == "status":
            await self._dispatch_status(
                message_id=message_id, from_addr=from_addr,
            )
            return
        if outcome.verb == "pickup":
            await self._dispatch_pickup(
                message_id=message_id, from_addr=from_addr,
                args=dict(outcome.args),
            )
            return
        spec = next(
            (s for s in principal_commands.VERB_REGISTRY if s.name == outcome.verb),
            None,
        )
        if spec is None or spec.tier != 1:
            # Defensive: validate_payload already screened for tier-1
            # registry membership, but in case the registry changes
            # mid-flight or a future shape slips through.
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: interpret picked an unrecognised verb",
                body=(
                    f"The interpret pass suggested the verb {outcome.verb!r}\n"
                    f"but it's not in the deterministic tier-1 registry.\n"
                    f"This is a daemon-side mismatch, not your problem.\n"
                ),
                next_state="INTERPRET_FAILED",
                event_name="principal_interpret_dispatch_unknown_verb",
                detail=outcome.verb,
            )
            return
        synthetic_cmd = principal_commands.ParsedCommand(
            raw_subject=f"(interpret) {outcome.verb}",
            verb=spec.name,
            tier=spec.tier,
            args=dict(outcome.args),
            handler=spec.handler,
            payload=outcome.summary,
        )
        result = principal_handlers.dispatch(
            command=synthetic_cmd, config=self.config, state=self.state
        )
        if result is None:
            self.logger.event(
                "principal_interpret_handler_missing",
                level="warn",
                inbox=self.inbox.name, message_id=message_id,
                verb=outcome.verb,
            )
            self._send_deterministic_reply(
                message_id=message_id,
                from_addr=from_addr,
                subject="Nightjar: interpret dispatch failed",
                body=(
                    f"The interpret pass picked verb {outcome.verb!r} but no\n"
                    f"handler is wired for it. This is a daemon-side bug.\n"
                ),
                next_state="INTERPRET_FAILED",
                event_name="principal_interpret_handler_missing_replied",
                detail=outcome.verb,
            )
            return
        reply_subject, reply_body = result
        # Prepend a one-liner explaining the redirection so the principal
        # sees what the LLM thought they meant.
        annotated_body = (
            f"(Interpret picked: {outcome.verb} — {outcome.reasoning.strip()})\n"
            f"\n"
            f"{reply_body}"
        )
        if cost_verdict is not None:
            annotated_body = annotated_body.rstrip() + (
                "\n\n"
                f"(Note: this interpret cost "
                f"{cost_guard.format_cents(cost_verdict.cost_cents_value)}, "
                f"above your "
                f"{cost_guard.format_cents(cost_verdict.soft_cap_cents)} "
                f"soft cap.)\n"
            )
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject=reply_subject,
            body=annotated_body,
            next_state="RESPONDED",
            event_name="principal_interpret_dispatched_replied",
            detail=f"verb={outcome.verb}",
        )

    def _queue_interpreted_action(
        self, *, message_id: str, from_addr: str,
        outcome: ActionProposal,
        cost_verdict: "cost_guard.CostVerdict | None" = None,
    ) -> None:
        """Queue a tier-2/3 ActionProposal in the approvals table and
        ping the principal with the same shape `_queue_tier2_plus` uses
        for deterministic verbs. The verb may be a registry name OR a
        free-form action description; the approvals table doesn't care.
        The executor for free-form verbs is part of the manifest-gated
        work and may not yet be wired — the approval row will sit
        until the principal approves, at which point the existing
        executor dispatch will either find a handler or log the
        un-handled verb (failing safely)."""
        token = self._generate_approval_token()
        self.state.queue_approval(
            token=token,
            message_id=message_id,
            verb=outcome.verb,
            args=dict(outcome.args),
            tier=outcome.tier,
        )
        warning_block = ""
        if outcome.irreversible_warning:
            warning_block = f"\nWarning: {outcome.irreversible_warning}\n"
        # Loud cost-overage banner for tier-2+ approvals: the principal
        # is about to authorise a side effect, so the cost overrun is
        # material context. Goes ABOVE the summary so it's visible
        # before they read the action description.
        cost_banner = ""
        if cost_verdict is not None:
            cost_banner = (
                f"!!! COST OVERAGE !!!\n"
                f"This interpret cost "
                f"{cost_guard.format_cents(cost_verdict.cost_cents_value)} "
                f"(soft cap "
                f"{cost_guard.format_cents(cost_verdict.soft_cap_cents)}).\n"
                f"Tokens: input={outcome.raw_input_tokens}, "
                f"output={outcome.raw_output_tokens}.\n"
                f"If this is unexpected, deny the approval and re-issue\n"
                f"with a more focused request.\n"
                f"---\n"
                f"\n"
            )
        self._send_deterministic_reply(
            message_id=message_id,
            from_addr=from_addr,
            subject=f"[Nightjar #{token}]",
            body=(
                f"{cost_banner}"
                f"Approval needed (interpreted from your free-form request):\n"
                f"\n"
                f"Summary:    {outcome.summary}\n"
                f"Verb:       {outcome.verb}\n"
                f"Args:       {dict(outcome.args)}\n"
                f"Tier:       {outcome.tier}\n"
                f"\n"
                f"Reasoning:  {outcome.reasoning}\n"
                f"{warning_block}"
                f"\n"
                f"To approve, hit reply, paste your code at the end of the\n"
                f"auto-filled subject, and put your verdict in the body:\n"
                f"  Subject: Re: [Nightjar #{token}] <code>\n"
                f"  Body:    yes\n"
                f"\n"
                f"To deny, same subject, body 'no'.\n"
                f"\n"
                f"Approval expires in 7 days.\n"
            ),
            next_state="AWAITING_APPROVAL",
            event_name="principal_interpret_approval_queued",
            detail=f"verb={outcome.verb} tier={outcome.tier} token={token}",
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
        body_text, body_truncated, structure, raw_rfc822 = body_result

        # 4. The triage call itself. Step 7b: routes through
        # triage_with_scope, which handles the empty-scopes pass-through
        # (behaves like the old triage_contact_mail) AND the two-pass
        # scoped path. The orchestrator owns: classifier-then-triage,
        # scope-filtered notes injection, fail-closed out-of-scope
        # decline. Existing contacts (with scopes=[]) take the old
        # path unchanged; opted-in contacts get scope gating.
        contact = self.config.contacts[contact_id]
        plan_or_err = await triage.triage_with_scope(
            contact=contact,
            sender=from_addr,
            subject=subject or "",
            body=body_text,
            structure=structure,
            config=claude_cfg,
            client=self._claude_client,
            prompts_dir=PROMPTS_DIR,
            notes_dir=self.config.daemon.notes_dir,
            scopes_registry=self.config.scopes,
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

        # noop / flag don't queue an executor verb. They still need a
        # human-visible ping so the principal sees the triage output.
        if plan.verb in ("noop", "flag_for_review"):
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
        #
        # Step 7b: `out_of_scope_decline` is structurally identical to
        # `reply` — the orchestrator constructs a templated decline
        # body and the dispatch routes through the same executor. The
        # verb name is preserved through the approval, audit log, and
        # outbound_log so the principal can distinguish a routine
        # reply from a scope-driven decline at any point.
        if plan.verb in ("reply", "out_of_scope_decline"):
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
                plan=plan, verb=plan.verb,
                args=args, body_text=body_text,
                body_truncated=body_truncated,
            )
            return

        # `forward_to_principal` queues a tier-3 approval. The args
        # carry the raw RFC822 bytes (base64-encoded for JSON safety)
        # so the executor can attach the original message verbatim
        # without a second IMAP round-trip at execute time. Storing
        # the bytes in state.db trades a little disk for executor
        # simplicity (sync path, no IMAP at execute time).
        if plan.verb == "forward_to_principal":
            forward_subject = self._build_forward_subject(subject)
            args = {
                "contact_id": contact_id,
                "subject": forward_subject,
                "raw_rfc822_b64": base64.b64encode(raw_rfc822).decode("ascii"),
                "summary": plan.summary,
                "reasoning": plan.reasoning,
                "risk_flags": list(plan.risk_flags),
                "notes": plan.notes,
                "in_reply_to": message_id,
            }
            self._queue_triage_approval(
                message_id=message_id, contact_id=contact_id,
                from_addr=from_addr, from_header=from_header,
                subject=subject, date_header=date_header,
                plan=plan, verb="forward_to_principal",
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

        # Per-verb section that explains what `yes` will actually do.
        if verb == "reply":
            action_block = (
                "Drafted reply (will be sent if approved):\n"
                "---\n"
                f"{args.get('body', '')}\n"
                "---\n"
            )
        elif verb == "forward_to_principal":
            attachment_size = len(args.get("raw_rfc822_b64", "")) * 3 // 4
            action_block = (
                "On approval Nightjar will forward the original email to\n"
                f"{self._principal_addr()} as a message/rfc822 attachment\n"
                f"({attachment_size} bytes). The wrapper body of that\n"
                "forward will repeat the triage summary above. The\n"
                "attached .eml will preserve the message exactly as it\n"
                "arrived: full headers, HTML alternative if present,\n"
                "attachments, inline images.\n"
            )
        else:
            # Defensive: any future tier-3 verb wired into triage
            # should declare its own action block. Falling back to a
            # generic statement so the approval ping is still useful.
            action_block = (
                f"On approval Nightjar will run verb '{verb}' with the\n"
                "args attached to this approval row.\n"
            )

        approval_body = (
            f"Approval needed: {verb} (triage)\n"
            f"\n"
            f"Triage of inbound mail from {contact_id} ({from_addr}).\n"
            f"\n"
            f"Verb proposed:  {verb} (tier {plan.tier})\n"
            f"Triage summary:\n  {plan.summary}\n"
            f"\n"
            f"Reasoning:\n  {plan.reasoning}\n"
            f"{notes_line}"
            f"{flags_line}"
            f"\n"
            f"{action_block}"
            f"\n"
            f"To approve, hit reply, paste your code at the end of the\n"
            f"auto-filled subject, and put your verdict in the body:\n"
            f"  Subject: Re: [Nightjar #{token}] <code>\n"
            f"  Body:    {confirm_phrase}\n"
            f"\n"
            f"To deny, same subject, body 'no'.\n"
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
            subject=f"[Nightjar #{token}]",
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

    def _send_first_run_recon_summary(
        self, *, processed: int, skipped: int, errors: int,
        window_days: int, since: str,
    ) -> None:
        """Notify the principal that the receipt-reliability fix has run
        a wider first-pass reconciliation and found previously-untracked
        mail.

        Sent at most once per inbox (the watermark prevents re-fire).
        Best-effort: SMTP failure is logged but does not block catchup.
        """
        if self.config.smtp is None:
            return
        body = (
            f"Nightjar's catchup logic was upgraded (Step 6e: receipt\n"
            f"reliability) and ran a one-shot {window_days}-day reconciliation\n"
            f"on inbox '{self.inbox.name}' against IMAP messages SINCE {since}.\n"
            f"\n"
            f"Newly tracked: {processed} message(s).\n"
            f"Already known: {skipped} message(s).\n"
        )
        if errors:
            body += f"Fetch errors:  {errors} (see daemon logs).\n"
        body += (
            f"\n"
            f"The 'newly tracked' messages have already been processed by\n"
            f"triage and any that needed approval have generated their own\n"
            f"separate pings. This summary is one-shot — you will not see it\n"
            f"again on subsequent restarts.\n"
        )
        try:
            notifier.notify_principal(
                smtp=self.config.smtp,
                principal_addr=self._principal_addr(),
                subject=f"[Nightjar] receipt-reliability reconciliation on {self.inbox.name}",
                body=body,
                jlogger=self.logger,
                state=self.state,
            )
        except Exception as e:
            self.logger.event(
                "first_run_recon_summary_failed",
                inbox=self.inbox.name,
                level="warn",
                error=type(e).__name__,
                message=str(e),
            )

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

    @staticmethod
    def _build_forward_subject(inbound_subject: str | None) -> str:
        """Build a Fwd:-prefixed subject for the forward-to-principal
        wrapper. Existing Fwd:/Fw: prefixes are not duplicated; an
        existing Re: is kept and Fwd: stacked in front of it."""
        s = (inbound_subject or "").strip()
        if not s:
            return "Fwd: (no subject)"
        lower = s.lower()
        if lower.startswith("fwd:") or lower.startswith("fw:"):
            return s
        return f"Fwd: {s}"

    async def _fetch_body_text(
        self, client: aioimaplib.IMAP4_SSL, uid: str
    ) -> tuple[str, bool, "MessageStructure", bytes] | None:
        """Fetch the full message for `uid` and return body + structure
        + raw RFC822 bytes.

        Returns `(body_text, was_truncated, structure, raw_bytes)` on
        success, or None if no usable body could be extracted (multipart
        with no text part, decode failure, IMAP error). Body is capped
        at MAX_TRIAGE_BODY_BYTES; `was_truncated` reflects the cap.

        The raw bytes are kept on the caller's side so the
        `forward_to_principal` executor can attach the original message
        verbatim later, without a second IMAP round-trip at execute time.
        Storing the bytes inline in the approval row trades a little
        state.db size for executor simplicity (sync path, no IMAP
        connection needed at execute time).

        Body fetch is the SECOND round-trip (after the headers fetch)
        and is gated on the message being triage-eligible. Strangers,
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
            structure = self._extract_message_structure(
                msg, raw_size=len(blob), body_truncated=truncated,
            )
        except Exception as e:
            self.logger.event(
                "body_decode_failed",
                inbox=self.inbox.name, level="warn", uid=uid, error=str(e),
            )
            return None
        if text is None:
            return None
        return text, truncated, structure, blob

    @staticmethod
    def _extract_message_structure(
        msg: email.message.Message, *, raw_size: int, body_truncated: bool
    ) -> "MessageStructure":
        """Walk a parsed email and produce a structural fingerprint.

        Counts what the LLM cannot see (HTML alternative parts,
        attachments, inline images) and the byte sizes of the
        text/plain and text/html parts (separately, so the LLM can
        compare them). Used as input to triage.build_user_message
        so the LLM can ground hidden-content suspicion in facts.

        We deliberately do NOT report the size of the entire raw
        RFC822 envelope (the caller's `raw_size` parameter): MTAs
        inject 5+ KB of headers (ARC-Seal, ARC-Message-Signature,
        DKIM, Received chains) which have nothing to do with the
        sender's content and would consistently make small
        plain-text emails look "huge" and trip the hidden-content
        sweep falsely.

        Classification rules:
          - HTML alternative: any `text/html` part exists, regardless
            of whether it is the chosen body part. (We pick text/plain
            for triage; the HTML alternative is what the LLM will not
            see and what the principal might.)
          - Attachment: any non-multipart part with
            Content-Disposition: attachment, OR with a filename that
            has a non-image content-type. Inline images are tracked
            separately.
          - Inline image: any image/* part with
            Content-Disposition: inline (or no disposition AND inside
            a multipart/related, which is the typical inline-image
            shape). For simplicity we count all image/* parts that
            are not explicitly attachments.
        """
        # raw_size is intentionally unused — see docstring. Kept as a
        # parameter so the caller's contract doesn't change.
        del raw_size

        from .triage import MessageStructure

        has_html_alternative = False
        attachment_count = 0
        attachment_names: list[str] = []
        inline_image_count = 0
        plain_size_bytes = 0
        html_size_bytes = 0

        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            filename = part.get_filename() or ""

            if ctype == "text/html":
                has_html_alternative = True
                # get_payload(decode=True) returns the decoded bytes
                # (after Content-Transfer-Encoding); len() of that is
                # what the principal would see if their client
                # rendered the HTML. None on decode failure -> 0.
                payload = part.get_payload(decode=True)
                html_size_bytes += len(payload) if payload else 0
                continue

            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                plain_size_bytes += len(payload) if payload else 0
                # Fall through: text/plain isn't an attachment or an
                # image, so the rest of the loop won't claim it.
                continue

            # Inline images: an image/* part is inline unless
            # explicitly marked attachment. This catches the common
            # multipart/related case where the image has a filename and
            # a Content-ID but is rendered inline rather than offered
            # as an attachment.
            if ctype.startswith("image/") and "attachment" not in disposition:
                inline_image_count += 1
                continue

            # Anything else with attachment disposition or a filename
            # and a non-text type is an attachment.
            is_attachment = (
                "attachment" in disposition
                or (filename and not ctype.startswith("text/"))
            )
            if is_attachment:
                attachment_count += 1
                if filename:
                    attachment_names.append(filename)
                else:
                    attachment_names.append(f"(unnamed {ctype})")

        return MessageStructure(
            has_html_alternative=has_html_alternative,
            attachment_count=attachment_count,
            attachment_names=tuple(attachment_names),
            inline_image_count=inline_image_count,
            plain_size_bytes=plain_size_bytes,
            html_size_bytes=html_size_bytes,
            body_truncated_in_prompt=body_truncated,
        )

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


# ---- Status-report walker (Step 6g) ---------------------------------------
#
# Free function that opens a transient IMAP connection, fetches headers
# for the last N UIDs, and returns the parsed metadata. Decoupled from
# the InboxWatcher class so the status report doesn't have to coordinate
# with the running IDLE loop — the IDLE-active client is busy waiting
# for pushes and can't fetch without breaking IDLE. A fresh connection
# adds ~200ms of setup, well below the 2-4s walk cost itself.

async def walk_inbox_for_status(
    *, inbox_cfg: InboxConfig, walk_count: int,
) -> "status_report.InboxWalkResult":
    """Open a fresh IMAP connection to the inbox, fetch headers for
    the last `walk_count` UIDs, parse Message-ID/From/Subject/Date.

    Returns a status_report.InboxWalkResult. On any IMAP error the
    `error` field is set and `headers` is empty; the status-report
    builder treats that as "no out-of-band data for this inbox" and
    moves on rather than failing the whole report.
    """
    import datetime as _dt

    client = aioimaplib.IMAP4_SSL(host=inbox_cfg.imap_host, port=inbox_cfg.imap_port)
    headers: list[dict[str, Any]] = []
    walked = 0
    err: str | None = None
    try:
        await client.wait_hello_from_server()
        login_response = await client.login(inbox_cfg.imap_user, inbox_cfg.imap_password)
        if login_response.result != "OK":
            return status_report.InboxWalkResult(
                inbox=inbox_cfg.name, walked_count=0,
                headers=(), error=f"login: {login_response.result}",
            )
        select_response = await client.select("INBOX")
        if select_response.result != "OK":
            return status_report.InboxWalkResult(
                inbox=inbox_cfg.name, walked_count=0,
                headers=(), error=f"select: {select_response.result}",
            )

        result, data = await client.uid_search("ALL")
        if result != "OK" or not data or not data[0]:
            return status_report.InboxWalkResult(
                inbox=inbox_cfg.name, walked_count=0,
                headers=(), error=None,
            )
        uids = data[0].split()
        if not uids:
            return status_report.InboxWalkResult(
                inbox=inbox_cfg.name, walked_count=0,
                headers=(), error=None,
            )
        uids_to_walk = uids[-int(walk_count):]
        for uid_bytes in uids_to_walk:
            uid = uid_bytes.decode("ascii")
            walked += 1
            try:
                fr, fdata = await client.uid("fetch", uid, "(BODY.PEEK[HEADER])")
            except Exception as e:
                # Per-UID fetch failure should not abort the whole walk.
                continue
            if fr != "OK" or not fdata:
                continue
            blob = InboxWatcher._extract_literal(fdata)
            if blob is None:
                continue
            msg = email.message_from_bytes(blob)
            message_id = (msg.get("Message-ID") or "").strip()
            if not message_id:
                # Mirror the watcher's synthetic-id convention so dedup
                # against state-db works for messages that have already
                # been recorded with a synthetic id.
                message_id = f"<no-msgid-{inbox_cfg.name}-uid{uid}>"
            from_header = msg.get("From", "")
            _, from_addr = email.utils.parseaddr(from_header)
            subject = InboxWatcher._decode_header(msg.get("Subject"))
            date_header = msg.get("Date") or ""
            try:
                received_at = int(
                    email.utils.parsedate_to_datetime(date_header).timestamp()
                )
            except (TypeError, ValueError, OverflowError):
                received_at = 0
            headers.append({
                "uid": uid,
                "message_id": message_id,
                "from_addr": from_addr or "",
                "subject": subject or "",
                "received_at": received_at,
            })
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        with contextlib.suppress(Exception):
            await client.logout()

    return status_report.InboxWalkResult(
        inbox=inbox_cfg.name,
        walked_count=walked,
        headers=tuple(headers),
        error=err,
    )


# ---- Pickup helper (Step 6g part 2) ---------------------------------------


async def _imap_find_by_message_id(
    *, inbox_cfg: InboxConfig, target_message_id: str,
) -> str | None:
    """Look up the IMAP UID of the message with `target_message_id`
    in the named inbox. Returns the UID as a string, or None if no
    match. On IMAP error returns "err:<reason>" so the caller can log
    it without confusing 'not found' with 'lookup failed'.

    Uses `UID SEARCH HEADER Message-ID "<id>"`. Gmail supports this
    even though it's not in core IMAP4rev1 — it's an X-EXTENSION
    accepted by all major IMAP servers.
    """
    client = aioimaplib.IMAP4_SSL(
        host=inbox_cfg.imap_host, port=inbox_cfg.imap_port,
    )
    try:
        await client.wait_hello_from_server()
        login_response = await client.login(
            inbox_cfg.imap_user, inbox_cfg.imap_password,
        )
        if login_response.result != "OK":
            return f"err:login {login_response.result}"
        select_response = await client.select("INBOX")
        if select_response.result != "OK":
            return f"err:select {select_response.result}"
        # IMAP search syntax: HEADER Message-ID "<value>". The value
        # is a string literal so we don't need the angle brackets to
        # be inside extra quotes — but we DO need to escape any
        # internal quotes. Message-IDs don't normally contain quotes;
        # we strip defensively.
        safe = target_message_id.replace('"', '')
        result, data = await client.uid_search(
            f'HEADER Message-ID "{safe}"'
        )
        if result != "OK":
            return f"err:search {result}"
        if not data or not data[0]:
            return None
        uids = data[0].split()
        if not uids:
            return None
        # If multiple match (rare — Message-ID should be unique), take
        # the most recent UID.
        return uids[-1].decode("ascii")
    except Exception as e:
        return f"err:{type(e).__name__}:{e}"
    finally:
        with contextlib.suppress(Exception):
            await client.logout()
