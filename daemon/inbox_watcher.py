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
import time
from email.header import decode_header, make_header

from aioimaplib import aioimaplib

from . import auth
from .config import InboxConfig, Config
from .log import JSONLLogger
from .state import State


# Gmail's documented IDLE timeout is 29 minutes. We re-IDLE at 27 to be
# safe (the server kicks us at ~29 if we don't move first).
IDLE_REFRESH_SECONDS = 27 * 60
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 300.0


class InboxWatcher:
    def __init__(
        self,
        *,
        inbox: InboxConfig,
        config: Config,
        state: State,
        logger: JSONLLogger,
        on_panic: "callable | None" = None,
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

        if contact_id is None:
            state = "DROPPED"
            detail = "stranger"
        else:
            contact = self.config.contacts[contact_id]
            if contact_id not in self.inbox.allowed_contacts:
                state = "DROPPED"
                detail = "contact_not_allowed_on_inbox"
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
