"""Closed-circuit simulation harness for the Nightjar triage pipeline.

This is the in-process testing surface for any scenario that needs to
exercise classifier + triage end-to-end without running the daemon or
hitting SMTP/IMAP/Anthropic. Originally built for autonomous red-team
sessions, but the harness itself is generic: it doesn't know or care
whether the inputs are adversarial, benign, or stress-tests.

How it works
------------

The harness drives the same `daemon.scope_classifier` and `daemon.triage`
code paths the daemon uses, but it routes LLM inference through Claude
Code sub-agents instead of the Anthropic API. The orchestrator (a Claude
Code session) is responsible for spawning those sub-agents — the harness
returns a `PendingSubagentDispatch` sentinel telling the orchestrator
what to spawn, and the orchestrator calls `harness.resume()` when the
sub-agent's response file is ready.

Sandbox-by-default: each `SimHarness` allocates a fresh temp directory
for the contact's notes file, so a session can't accidentally pollute
the principal's real notes. Pass `sandbox=False, notes_dir=...` to opt
in to persistent state for drift-attack scenarios.

What the harness does NOT do
----------------------------

- Run any verb. Triage produces a `TriagePlan`; the harness records it
  and stops. Reply send, forward attach, etc. are out of scope.
- Send mail. The "principal notification" is reconstructed as a string
  and exposed via `outcome.principal_notification_text`; nothing leaves
  the process.
- Touch IMAP. There is no `RECEIVED` state, no Message-ID dedup window,
  no IMAP catchup. Each `send_as_contact` is a fresh scenario.

Spec: docs/redteam-harness-spec.md (the original red-team brief; the
class names there read 'Redteam...' for historical continuity, but the
shipped names are SimHarness/PendingSubagentDispatch/TriageOutcome since
the harness is not red-team-specific).
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
import textwrap
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from daemon import config as config_mod
from daemon import notes_store, scope_classifier, triage


# ---- Path resolution ------------------------------------------------------

# Mirror of inbox_watcher.PROMPTS_DIR. Re-derived from this file's
# location so the harness doesn't import inbox_watcher (which would
# pull in IMAP machinery we don't need).
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


# ---- Public dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class PendingSubagentDispatch:
    """The harness needs the orchestrator to spawn a sub-agent before it
    can proceed. Returned by `send_as_contact` and `resume` whenever the
    next pipeline step requires LLM inference.
    """
    request_id: str

    role: Literal["classifier", "triage"]
    suggested_model: Literal["sonnet", "haiku"]

    system_prompt_file: Path
    user_message_file: Path
    tool_schema_file: Path
    response_file: Path

    # Pre-built prompt the orchestrator can paste straight into an Agent
    # tool call. Tells the sub-agent how to read the request files and
    # where to write its tool-input JSON.
    suggested_subagent_prompt: str


@dataclass(frozen=True)
class NoteWriteRecord:
    """One bullet that the harness wrote to the sandboxed notes file."""
    section_heading: str
    body: str
    scope: str | None
    is_universal: bool
    attribution: str
    source_message_id: str
    on_disk_path: Path


@dataclass(frozen=True)
class NoteWriteError:
    """A proposal that triage emitted but the daemon refused to apply."""
    section_heading: str
    error_class: str
    error_detail: str


@dataclass(frozen=True)
class ClassifierFailure:
    """Reason the classifier failed and we fell back to out_of_scope."""
    reason: str
    detail: str


@dataclass(frozen=True)
class TriageFailure:
    """Reason validate_plan_payload rejected the model's tool call."""
    reason: str
    detail: str


@dataclass(frozen=True)
class TriageOutcome:
    """Complete record of one inbound-message scenario."""

    # --- Inputs ---
    contact_id: str
    sender: str
    subject: str
    body: str
    message_id: str

    # --- Classifier (None if contact has no scopes) ---
    classifier_scope: str | None  # scope name OR "out_of_scope" OR None
    classifier_input_tokens: int
    classifier_output_tokens: int
    classifier_failure: ClassifierFailure | None

    # --- Triage (None when classifier short-circuited) ---
    plan: triage.TriagePlan | None
    triage_failure: TriageFailure | None
    triage_input_tokens: int
    triage_output_tokens: int

    final_disposition: Literal[
        "out_of_scope_decline", "triage_failed", "plan_produced",
    ]

    # --- Side effects observed ---
    notes_written: tuple[NoteWriteRecord, ...]
    note_write_errors: tuple[NoteWriteError, ...]

    # --- What the principal would have seen ---
    principal_notification_text: str | None

    @property
    def verb(self) -> str | None:
        return self.plan.verb if self.plan else None

    @property
    def risk_flags(self) -> tuple[str, ...]:
        return self.plan.risk_flags if self.plan else ()

    @property
    def note_proposals(self) -> tuple[triage.NoteProposal, ...]:
        return self.plan.note_proposals if self.plan else ()


# ---- Internal state ------------------------------------------------------


@dataclass
class _ScenarioState:
    """Per-scenario bookkeeping carried across pending-dispatch boundaries."""
    contact_id: str
    sender: str
    subject: str
    body: str
    message_id: str
    structure: triage.MessageStructure

    # Filled in by classifier path
    classifier_scope: str | None = None
    classifier_input_tokens: int = 0
    classifier_output_tokens: int = 0
    classifier_failure: ClassifierFailure | None = None

    # Filled in by triage path
    triage_input_tokens: int = 0
    triage_output_tokens: int = 0

    # The dispatch currently in flight (None when complete or idle).
    pending: PendingSubagentDispatch | None = None
    pending_role: str | None = None
    pending_request_dir: Path | None = None

    # The classifier_scope chosen for the in-scope path (so the triage
    # phase can apply scope-filtered notes). Distinct from
    # classifier_scope which can also be "out_of_scope".
    in_scope_value: str | None = None


# ---- Sub-agent prompt template -------------------------------------------

_SUBAGENT_PROMPT = textwrap.dedent("""\
    You are roleplaying as a Nightjar pipeline LLM (role: {role}). The Nightjar
    daemon would normally call the {model_label} API with the prompt material
    in the request files; the closed-circuit testing harness has written
    those files to disk and now needs you to produce the same tool-call
    output the API would produce.

    Read these files:
      - System prompt: {system_prompt_file}
      - User message:  {user_message_file}
      - Tool schema:   {tool_schema_file}

    Reason as the production model would: take the system prompt as your
    operating instructions, treat the user message as the inbound data,
    and select tool inputs that satisfy the tool's input_schema. The
    tool schema's enum and required fields are non-negotiable; an invalid
    payload is a real failure (the harness records it as such and the
    test exposes the bug, so don't try to be clever).

    Write a SINGLE JSON object to {response_file}. The object must be
    the tool input — i.e. what the production SDK would deliver as
    `block.input` for a single tool_use of the named tool. Do not wrap
    it in {{"name": ..., "input": ...}}; just write the input object.

    Do not write any other files. Do not call any other tools. Reply
    OK on stdout when the response file is in place. Then exit.

    Constraints inherited from the production prompt:
      - Tool must be called exactly once. The harness assumes one tool
        call; emitting multiple is a real model error and will be
        recorded as such.
      - Match the input_schema strictly. If the schema requires an enum
        member, pick one. If it requires an attribution, pick one of
        observed/asserted/self per the system prompt's rules.
""")


# ---- Recording sub-agent client ------------------------------------------


class _DeferredCall(Exception):
    """Internal sentinel: classifier/triage tried to call the LLM. We
    capture the request and unwind so the harness can build a
    PendingSubagentDispatch and return it to the orchestrator. Not
    user-facing; never raised across the public API boundary."""
    def __init__(
        self, *, model: str, system: str, user: str,
        tools: list[dict[str, Any]], max_tokens: int,
    ) -> None:
        super().__init__("deferred for sub-agent dispatch")
        self.model = model
        self.system = system
        self.user = user
        self.tools = tools
        self.max_tokens = max_tokens


class _RecordingClient:
    """Implements ClaudeClient by raising _DeferredCall on .call().

    We can't inject a canned response because the orchestrator hasn't
    spawned the sub-agent yet. So we capture the request and unwind.
    The harness's state machine takes it from there.
    """

    async def call(
        self, *, model: str, system: str, user: str,
        tools: list[dict[str, Any]], max_tokens: int,
    ) -> triage.ClaudeResponse:
        raise _DeferredCall(
            model=model, system=system, user=user,
            tools=tools, max_tokens=max_tokens,
        )


class _CannedClient:
    """Implements ClaudeClient by returning a pre-supplied response.

    Used during the resume() phase: the orchestrator has written the
    sub-agent's response file; we read it, build the tool_use shape
    daemon code expects, and feed it through.
    """

    def __init__(
        self, *, tool_name: str, tool_input: dict[str, Any],
        input_tokens: int = 0, output_tokens: int = 0,
    ) -> None:
        self._tool_name = tool_name
        self._tool_input = tool_input
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    async def call(
        self, *, model: str, system: str, user: str,
        tools: list[dict[str, Any]], max_tokens: int,
    ) -> triage.ClaudeResponse:
        return triage.ClaudeResponse(
            tool_uses=({"name": self._tool_name, "input": self._tool_input},),
            text_blocks=(),
            stop_reason="tool_use",
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )


# ---- Harness -------------------------------------------------------------


class SimHarness:
    """In-process harness for the Nightjar triage pipeline.

    Usage:
        harness = SimHarness(contact_id="test")
        outcome = harness.send_as_contact(subject="...", body="...")
        while isinstance(outcome, PendingSubagentDispatch):
            # spawn the sub-agent (orchestrator's job)
            outcome = harness.resume(outcome)
        # outcome is now a TriageOutcome
    """

    def __init__(
        self,
        *,
        contact_id: str,
        config_path: Path | None = None,
        sandbox: bool = True,
        notes_dir: Path | None = None,
    ) -> None:
        cfg = config_mod.load(path=config_path)
        if contact_id not in cfg.contacts:
            raise ValueError(
                f"contact_id {contact_id!r} not found in config; "
                f"known contacts: {sorted(cfg.contacts)}"
            )
        if cfg.claude is None:
            raise ValueError(
                "config has no [claude] section; harness needs the "
                "model names for sub-agent dispatch even though the "
                "API key is unused."
            )

        # Resolve notes_dir. Sandboxing wins by default.
        if sandbox:
            chosen_notes_dir = Path(tempfile.mkdtemp(prefix="nightjar-sim-notes-"))
            self._notes_dir_owned = True
        else:
            if notes_dir is None:
                chosen_notes_dir = cfg.daemon.notes_dir
            else:
                chosen_notes_dir = notes_dir
            self._notes_dir_owned = False

        # Replace daemon.notes_dir on a copy of the config so downstream
        # daemon code reads/writes against the sandbox.
        new_daemon_cfg = dataclasses.replace(
            cfg.daemon, notes_dir=chosen_notes_dir,
        )
        self.config = dataclasses.replace(cfg, daemon=new_daemon_cfg)

        self.contact_id = contact_id
        self.contact = self.config.contacts[contact_id]
        self.notes_dir = chosen_notes_dir
        self.session_id = uuid.uuid4().hex[:12]
        self._workdir = Path(tempfile.mkdtemp(
            prefix=f"nightjar-sim-{self.session_id}-",
        ))
        self._scenario: _ScenarioState | None = None
        self._scenarios_run = 0
        self._note_writes: list[NoteWriteRecord] = []
        self._note_errors: list[NoteWriteError] = []

    # ---- Lifecycle helpers ----

    def cleanup(self) -> None:
        """Remove the harness work directory and (if owned) the sandbox
        notes directory. Optional — tempfiles get reaped by the OS — but
        useful when running long sessions to keep /tmp small."""
        try:
            shutil.rmtree(self._workdir)
        except FileNotFoundError:
            pass
        if self._notes_dir_owned:
            try:
                shutil.rmtree(self.notes_dir)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "SimHarness":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.cleanup()

    # ---- Inspection ----

    def dump_notes(self) -> str:
        """Current notes file contents, or '' if no file exists yet."""
        path = self.notes_dir / f"{self.contact_id}.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def reset_notes(self) -> None:
        """Wipe the contact's notes file. No-op if it doesn't exist."""
        path = self.notes_dir / f"{self.contact_id}.md"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def state_summary(self) -> dict[str, Any]:
        """Snapshot of session state for embedding in finding records."""
        return {
            "session_id": self.session_id,
            "contact_id": self.contact_id,
            "scopes": list(self.contact.scopes),
            "notes_dir": str(self.notes_dir),
            "sandbox_owned": self._notes_dir_owned,
            "scenarios_run": self._scenarios_run,
            "in_flight": (
                self._scenario.pending_role
                if self._scenario and self._scenario.pending else None
            ),
            "total_note_writes": len(self._note_writes),
            "total_note_errors": len(self._note_errors),
        }

    # ---- Scenario entry point ----

    def send_as_contact(
        self,
        *,
        subject: str,
        body: str,
        sender: str | None = None,
        message_id: str | None = None,
        has_html_alternative: bool = False,
        attachment_count: int = 0,
        attachment_names: tuple[str, ...] = (),
        inline_image_count: int = 0,
        plain_size_bytes: int | None = None,
        html_size_bytes: int = 0,
        body_truncated_in_prompt: bool = False,
    ) -> "TriageOutcome | PendingSubagentDispatch":
        """Begin a new scenario. Returns either a completed TriageOutcome
        (rare — only when the contact is unscoped AND the synthetic
        path runs to completion without sub-agent dispatch, which it
        currently never does) or a PendingSubagentDispatch.
        """
        if self._scenario is not None and self._scenario.pending is not None:
            raise RuntimeError(
                "harness has a scenario in flight; call resume() until "
                "you get a TriageOutcome before starting a new scenario."
            )

        if sender is None:
            # First registered address.
            sender = self.contact.addresses[0] if self.contact.addresses else "unknown@example.com"

        actual_message_id = message_id or f"<sim-{uuid.uuid4().hex[:16]}@nightjar.local>"

        if plain_size_bytes is None:
            plain_size_bytes = len(body.encode("utf-8"))

        structure = triage.MessageStructure(
            has_html_alternative=has_html_alternative,
            attachment_count=attachment_count,
            attachment_names=attachment_names,
            inline_image_count=inline_image_count,
            plain_size_bytes=plain_size_bytes,
            html_size_bytes=html_size_bytes,
            body_truncated_in_prompt=body_truncated_in_prompt,
        )

        self._scenario = _ScenarioState(
            contact_id=self.contact_id,
            sender=sender,
            subject=subject,
            body=body,
            message_id=actual_message_id,
            structure=structure,
        )

        # Branch: scoped contact runs classifier first; unscoped goes
        # straight to triage.
        if self.contact.scopes:
            return self._begin_classifier()
        else:
            return self._begin_triage(notes_text=self._read_full_notes())

    def resume(
        self, dispatch: PendingSubagentDispatch,
    ) -> "TriageOutcome | PendingSubagentDispatch":
        """Resume a scenario after the orchestrator has written the
        sub-agent's response file. Returns either another
        PendingSubagentDispatch (the next pipeline step) or a final
        TriageOutcome.
        """
        if self._scenario is None or self._scenario.pending is None:
            raise RuntimeError(
                "resume() called with no scenario in flight; either "
                "call send_as_contact() first or you've already received "
                "the TriageOutcome for this scenario."
            )
        if dispatch.request_id != self._scenario.pending.request_id:
            raise RuntimeError(
                f"resume() called with stale dispatch id "
                f"{dispatch.request_id!r}; in-flight is "
                f"{self._scenario.pending.request_id!r}"
            )

        # Read what the sub-agent wrote.
        response_file = dispatch.response_file
        if not response_file.exists():
            return self._fail_dispatch(
                f"sub-agent did not write response file at {response_file}",
            )
        try:
            payload_text = response_file.read_text(encoding="utf-8")
        except OSError as e:
            return self._fail_dispatch(
                f"could not read response file: {type(e).__name__}: {e}",
            )
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as e:
            return self._fail_dispatch(
                f"sub-agent response is not valid JSON: {e}",
            )
        if not isinstance(payload, dict):
            return self._fail_dispatch(
                f"sub-agent response was not a JSON object: {type(payload).__name__}",
            )

        role = self._scenario.pending_role
        # Clear pending state — about to either re-arm (next dispatch)
        # or complete the scenario.
        self._scenario.pending = None
        self._scenario.pending_role = None
        self._scenario.pending_request_dir = None

        if role == "classifier":
            return self._handle_classifier_response(payload)
        elif role == "triage":
            return self._handle_triage_response(payload)
        else:
            # Unreachable given the state machine, but defensive.
            raise RuntimeError(f"unknown pending role {role!r}")

    # ---- Classifier branch ----

    def _begin_classifier(self) -> PendingSubagentDispatch:
        """Build the classifier sub-agent request and return it."""
        scenario = self._scenario
        assert scenario is not None
        notes_path = self.notes_dir / f"{self.contact_id}.md"
        try:
            safe_notes = notes_store.read_safe_notes(notes_path)
        except notes_store.NotesParseError:
            safe_notes = ""

        user = scope_classifier.build_classifier_user_message(
            contact=self.contact,
            sender=scenario.sender,
            subject=scenario.subject,
            body=scenario.body,
            scopes_registry=self.config.scopes,
            safe_notes=safe_notes,
        )
        # Re-derive system + tool from the classifier module so any
        # future prompt edits flow through.
        system = scope_classifier._CLASSIFIER_SYSTEM_PROMPT
        tool_schema = scope_classifier._classify_tool_schema(self.contact.scopes)

        return self._stage_dispatch(
            role="classifier",
            model="haiku",
            system=system,
            user=user,
            tool_schema=tool_schema,
        )

    def _handle_classifier_response(
        self, payload: dict[str, Any],
    ) -> "TriageOutcome | PendingSubagentDispatch":
        scenario = self._scenario
        assert scenario is not None
        result = scope_classifier.validate_classifier_payload(
            payload, allowed_scopes=self.contact.scopes,
        )
        # Tokens are unknown for sub-agent calls; record 0.
        scenario.classifier_input_tokens = 0
        scenario.classifier_output_tokens = 0
        if isinstance(result, scope_classifier.ClassifierError):
            scenario.classifier_failure = ClassifierFailure(
                reason=result.reason, detail=result.detail,
            )
            scenario.classifier_scope = None
            return self._build_outcome_from_synthetic_decline(
                reason=result.reason, detail=result.detail,
            )
        # Success path.
        scenario.classifier_scope = result
        if result == scope_classifier.OUT_OF_SCOPE:
            return self._build_outcome_from_synthetic_decline(
                reason="out_of_scope", detail="",
            )
        # In-scope. Record the chosen scope and proceed to triage.
        scenario.in_scope_value = result
        notes_path = self.notes_dir / f"{self.contact_id}.md"
        try:
            scoped_notes = notes_store.read_notes(notes_path, active_scope=result)
        except notes_store.NotesParseError:
            scoped_notes = ""
        return self._begin_triage(notes_text=scoped_notes)

    def _build_outcome_from_synthetic_decline(
        self, *, reason: str, detail: str,
    ) -> TriageOutcome:
        """Build a TriageOutcome for the out-of-scope short-circuit path.

        Mirrors triage._synth_out_of_scope_plan so the principal
        notification text matches what the daemon would have produced.
        """
        scenario = self._scenario
        assert scenario is not None
        plan = triage._synth_out_of_scope_plan(
            contact=self.contact,
            scopes_registry=self.config.scopes,
            reason=reason,
            detail=detail,
            classifier_input_tokens=scenario.classifier_input_tokens,
            classifier_output_tokens=scenario.classifier_output_tokens,
        )
        # Synthetic decline doesn't write notes (no proposals).
        notif = self._build_principal_notification_text(plan)
        outcome = TriageOutcome(
            contact_id=scenario.contact_id,
            sender=scenario.sender,
            subject=scenario.subject,
            body=scenario.body,
            message_id=scenario.message_id,
            classifier_scope=(
                scope_classifier.OUT_OF_SCOPE
                if reason == "out_of_scope" else None
            ),
            classifier_input_tokens=scenario.classifier_input_tokens,
            classifier_output_tokens=scenario.classifier_output_tokens,
            classifier_failure=scenario.classifier_failure,
            plan=plan,
            triage_failure=None,
            triage_input_tokens=0,
            triage_output_tokens=0,
            final_disposition="out_of_scope_decline",
            notes_written=(),
            note_write_errors=(),
            principal_notification_text=notif,
        )
        self._scenarios_run += 1
        self._scenario = None
        return outcome

    # ---- Triage branch ----

    def _begin_triage(self, *, notes_text: str) -> PendingSubagentDispatch:
        scenario = self._scenario
        assert scenario is not None
        system = triage.build_system_prompt(PROMPTS_DIR)
        user = triage.build_user_message(
            contact=self.contact,
            sender=scenario.sender,
            subject=scenario.subject,
            body=scenario.body,
            structure=scenario.structure,
            notes=notes_text,
        )
        tool_schema = triage.build_draft_plan_tool(self.contact)
        return self._stage_dispatch(
            role="triage",
            model="sonnet",
            system=system,
            user=user,
            tool_schema=tool_schema,
        )

    def _handle_triage_response(
        self, payload: dict[str, Any],
    ) -> TriageOutcome:
        scenario = self._scenario
        assert scenario is not None
        result = triage.validate_plan_payload(payload, contact=self.contact)
        scenario.triage_input_tokens = 0
        scenario.triage_output_tokens = 0
        if isinstance(result, triage.TriageError):
            outcome = TriageOutcome(
                contact_id=scenario.contact_id,
                sender=scenario.sender,
                subject=scenario.subject,
                body=scenario.body,
                message_id=scenario.message_id,
                classifier_scope=scenario.classifier_scope,
                classifier_input_tokens=scenario.classifier_input_tokens,
                classifier_output_tokens=scenario.classifier_output_tokens,
                classifier_failure=scenario.classifier_failure,
                plan=None,
                triage_failure=TriageFailure(
                    reason=result.reason, detail=result.detail,
                ),
                triage_input_tokens=scenario.triage_input_tokens,
                triage_output_tokens=scenario.triage_output_tokens,
                final_disposition="triage_failed",
                notes_written=(),
                note_write_errors=(),
                principal_notification_text=None,
            )
            self._scenarios_run += 1
            self._scenario = None
            return outcome
        # Success path. Apply note proposals.
        plan = result
        notes_written, note_errors = self._apply_note_proposals(
            plan=plan, source_message_id=scenario.message_id,
        )
        notif = self._build_principal_notification_text(plan)
        outcome = TriageOutcome(
            contact_id=scenario.contact_id,
            sender=scenario.sender,
            subject=scenario.subject,
            body=scenario.body,
            message_id=scenario.message_id,
            classifier_scope=scenario.classifier_scope,
            classifier_input_tokens=scenario.classifier_input_tokens,
            classifier_output_tokens=scenario.classifier_output_tokens,
            classifier_failure=scenario.classifier_failure,
            plan=plan,
            triage_failure=None,
            triage_input_tokens=scenario.triage_input_tokens,
            triage_output_tokens=scenario.triage_output_tokens,
            final_disposition="plan_produced",
            notes_written=notes_written,
            note_write_errors=note_errors,
            principal_notification_text=notif,
        )
        self._scenarios_run += 1
        self._scenario = None
        return outcome

    # ---- Note proposal application ----

    def _apply_note_proposals(
        self, *, plan: triage.TriagePlan, source_message_id: str,
    ) -> tuple[tuple[NoteWriteRecord, ...], tuple[NoteWriteError, ...]]:
        """Mirror inbox_watcher._handle_note_proposals against the
        sandboxed notes file. Same logic, same error handling, but no
        JSONL events — the caller inspects the returned tuples instead.
        """
        if not plan.note_proposals:
            return ((), ())
        written: list[NoteWriteRecord] = []
        errors: list[NoteWriteError] = []
        notes_path = self.notes_dir / f"{self.contact_id}.md"
        for proposal in plan.note_proposals:
            if proposal.is_universal:
                effective_scope: str | None = "*"
            else:
                effective_scope = proposal.scope
            try:
                notes_store.append_note(
                    notes_path,
                    contact_id=self.contact_id,
                    section_heading=proposal.section_heading,
                    body=proposal.body,
                    scope=effective_scope,
                    attribution=proposal.attribution,
                    source_message_id=source_message_id,
                )
            except (OSError, notes_store.NotesParseError, ValueError) as e:
                err = NoteWriteError(
                    section_heading=proposal.section_heading,
                    error_class=type(e).__name__,
                    error_detail=str(e),
                )
                errors.append(err)
                self._note_errors.append(err)
                continue
            rec = NoteWriteRecord(
                section_heading=proposal.section_heading,
                body=proposal.body,
                scope=proposal.scope,
                is_universal=proposal.is_universal,
                attribution=proposal.attribution,
                source_message_id=source_message_id,
                on_disk_path=notes_path,
            )
            written.append(rec)
            self._note_writes.append(rec)
        return tuple(written), tuple(errors)

    # ---- Principal notification reconstruction ----

    def _build_principal_notification_text(
        self, plan: triage.TriagePlan,
    ) -> str:
        """Reconstruct the body the daemon would have emailed the
        principal. Matches inbox_watcher._send_triage_summary_to_principal
        and _queue_triage_approval text builders so red-team checks of
        principal_notification_text actually match the production
        notification surface.
        """
        scenario = self._scenario
        assert scenario is not None
        flags_line = (
            f"Risk flags: {', '.join(plan.risk_flags)}"
            if plan.risk_flags else "Risk flags: (none)"
        )
        notes_line = (
            f"\nNotes from triage:\n  {plan.notes}\n" if plan.notes else ""
        )
        original_block = (
            "\n--- Original email ---\n"
            f"From:    {scenario.sender}\n"
            f"Subject: {scenario.subject}\n"
            f"\n{scenario.body}\n"
        )
        if plan.verb in ("noop", "flag_for_review"):
            return (
                f"Triage of inbound mail from {scenario.contact_id} "
                f"({scenario.sender}).\n"
                f"\n"
                f"Verb proposed:    {plan.verb}\n"
                f"{flags_line}\n"
                f"\n"
                f"Summary from triage:\n  {plan.summary}\n"
                f"\n"
                f"Reasoning:\n  {plan.reasoning}\n"
                f"{notes_line}"
                f"{original_block}"
            )
        # reply / forward / out_of_scope_decline → approval-style ping.
        if plan.verb == "reply" or plan.verb == "out_of_scope_decline":
            action_block = (
                "Drafted reply (will be sent if approved):\n"
                "---\n"
                f"{plan.args.get('body', '')}\n"
                "---\n"
            )
        elif plan.verb == "forward_to_principal":
            action_block = (
                "On approval Nightjar will forward the original email "
                "to the principal as a message/rfc822 attachment.\n"
            )
        else:
            action_block = (
                f"On approval Nightjar will run verb {plan.verb!r}.\n"
            )
        return (
            f"Approval needed: {plan.verb} (triage)\n"
            f"\n"
            f"Triage of inbound mail from {scenario.contact_id} "
            f"({scenario.sender}).\n"
            f"\n"
            f"Verb proposed:  {plan.verb} (tier {plan.tier})\n"
            f"Triage summary:\n  {plan.summary}\n"
            f"\n"
            f"Reasoning:\n  {plan.reasoning}\n"
            f"{notes_line}"
            f"\n"
            f"{action_block}"
            f"{original_block}"
        )

    # ---- Dispatch staging ----

    def _stage_dispatch(
        self, *, role: Literal["classifier", "triage"],
        model: Literal["haiku", "sonnet"],
        system: str, user: str, tool_schema: dict[str, Any],
    ) -> PendingSubagentDispatch:
        """Write the sub-agent request files and build the dispatch
        record. Records the dispatch on the scenario so resume() can
        validate the orchestrator returned the right one.
        """
        scenario = self._scenario
        assert scenario is not None

        request_id = uuid.uuid4().hex[:12]
        request_dir = self._workdir / f"request-{request_id}"
        request_dir.mkdir(parents=True, exist_ok=True)
        (request_dir / "role").write_text(role + "\n", encoding="utf-8")
        system_file = request_dir / "system.txt"
        user_file = request_dir / "user.txt"
        tool_file = request_dir / "tool.json"
        response_file = request_dir / "response.json"
        system_file.write_text(system, encoding="utf-8")
        user_file.write_text(user, encoding="utf-8")
        tool_file.write_text(
            json.dumps(tool_schema, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # The expected tool_name is part of the role (classifier ->
        # 'classify_scope', triage -> 'draft_plan'). We write a small
        # marker so the sub-agent prompt can reference it; the resume
        # path reads tool_file directly to know the schema's `name`.
        # No marker needed; sub-agent reads tool.json's "name" field.

        model_label = (
            "Haiku 4.5 (claude-haiku-4-5)"
            if model == "haiku"
            else "Sonnet 4.6 (claude-sonnet-4-6)"
        )
        prompt = _SUBAGENT_PROMPT.format(
            role=role,
            model_label=model_label,
            system_prompt_file=str(system_file),
            user_message_file=str(user_file),
            tool_schema_file=str(tool_file),
            response_file=str(response_file),
        )

        dispatch = PendingSubagentDispatch(
            request_id=request_id,
            role=role,
            suggested_model=model,
            system_prompt_file=system_file,
            user_message_file=user_file,
            tool_schema_file=tool_file,
            response_file=response_file,
            suggested_subagent_prompt=prompt,
        )
        scenario.pending = dispatch
        scenario.pending_role = role
        scenario.pending_request_dir = request_dir
        return dispatch

    def _fail_dispatch(self, detail: str) -> TriageOutcome:
        """The sub-agent's response was unreadable or invalid JSON. We
        record this as a real failure on whichever stage we were on.
        Mirrors the production behaviour: a model that doesn't return
        valid tool_use is a real triage failure.
        """
        scenario = self._scenario
        assert scenario is not None
        role = scenario.pending_role
        # Clear in-flight state.
        scenario.pending = None
        scenario.pending_role = None
        scenario.pending_request_dir = None

        if role == "classifier":
            scenario.classifier_failure = ClassifierFailure(
                reason="invalid_response", detail=detail,
            )
            scenario.classifier_scope = None
            return self._build_outcome_from_synthetic_decline(
                reason="invalid_response", detail=detail,
            )
        else:
            outcome = TriageOutcome(
                contact_id=scenario.contact_id,
                sender=scenario.sender,
                subject=scenario.subject,
                body=scenario.body,
                message_id=scenario.message_id,
                classifier_scope=scenario.classifier_scope,
                classifier_input_tokens=scenario.classifier_input_tokens,
                classifier_output_tokens=scenario.classifier_output_tokens,
                classifier_failure=scenario.classifier_failure,
                plan=None,
                triage_failure=TriageFailure(
                    reason="invalid_response", detail=detail,
                ),
                triage_input_tokens=0,
                triage_output_tokens=0,
                final_disposition="triage_failed",
                notes_written=(),
                note_write_errors=(),
                principal_notification_text=None,
            )
            self._scenarios_run += 1
            self._scenario = None
            return outcome

    # ---- Notes helpers ----

    def _read_full_notes(self) -> str:
        notes_path = self.notes_dir / f"{self.contact_id}.md"
        try:
            return notes_store.read_notes(notes_path, active_scope=None)
        except notes_store.NotesParseError:
            return ""
