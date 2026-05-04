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
from email.header import decode_header, make_header

from aioimaplib import aioimaplib

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
    ) -> None:
        self.inbox = inbox
        self.config = config
        self.state = state
        self.logger = logger
        self._stop_event = asyncio.Event()
        self._backoff = INITIAL_BACKOFF_SECONDS

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
            try:
                # Wait until either we see EXISTS-style activity, or
                # the refresh timer fires.
                await asyncio.wait_for(
                    self._wait_for_activity(client),
                    timeout=IDLE_REFRESH_SECONDS,
                )
                self.logger.event("idle_activity", inbox=self.inbox.name)
                # Don't fetch yet; finalize IDLE first, then search.
            except asyncio.TimeoutError:
                self.logger.event("idle_refresh", inbox=self.inbox.name)
        finally:
            client.idle_done()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(idle_task, timeout=10)

        # Whether activity or refresh, do an UNSEEN search to be safe.
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

        # aioimaplib returns a list with mixed bytes / strings. The header
        # block is the bytes element that contains MIME headers.
        header_blob = b""
        for chunk in data:
            if isinstance(chunk, bytes) and b"\r\n" in chunk:
                header_blob = chunk
                break
        if not header_blob:
            self.logger.event("fetch_no_headers", inbox=self.inbox.name, uid=uid, level="warn")
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
            else:
                # Build Step 1 doesn't yet implement triage. We mark the
                # message RECEIVED and stop; later steps will pick it up.
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

    @staticmethod
    def _decode_header(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value
