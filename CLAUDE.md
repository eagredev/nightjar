# Nightjar — agent instructions

This is the **public-facing** Nightjar repo. It ships a single-principal
email assistant daemon. The git remote is on GitHub
(`eagredev/nightjar`); anything committed here on the `main` branch
is destined to be public, eventually if not immediately.

## The Avow / Nightjar boundary — READ BEFORE MAKING CHANGES

There is a sister project, currently called **Avow** (working
codename, may rename), which lives on the **`avow/main` branch** of
this same repository. Avow is the **private research arm**:
principal-channel architecture, ratification design, threat-modelling
for guarded delegation to AI agents.

**Nightjar is the public, product-flavoured codebase.** Avow is the
private, research-flavoured codebase. They share the same git
working tree but live on different branches:

- **`main`** — public, has GitHub remote, product-flavoured. UX
  improvements, bug fixes, configuration tweaks, refactors,
  performance, test coverage, harness integrations, documentation.
  Anything a user of an open-source email assistant would expect
  to see in the repo.
- **`avow/main`** — private, NO remote, research-flavoured. Anything
  that constitutes original research contribution the user has not
  yet decided to publish — design memos, threat models, novelty
  arguments, evaluation plans, vocabulary papers, framework names,
  the principal-channel architecture itself. Lives entirely under
  the `avow/` subdirectory on that branch.

The Avow branch INHERITS from main: when Nightjar's main branch
gets a bug fix or improvement, the avow branch picks it up via
`git merge main`. The reverse direction — pushing avow content
back to main — is what the pre-commit hook prevents.

### When something Avow-flavoured comes up during Nightjar work

If, during a Nightjar (main-branch) task, you produce or are about
to commit material that is research-flavoured rather than
product-flavoured, **stop and flag it**. The litmus test:

> Would publishing this on GitHub today affect our ability to make
> a novelty / IP / first-publication claim later? Or does it expose
> the design rationale for a security mechanism in a way that
> weakens a future paper's contribution?

If yes, even a little: it goes on the `avow/main` branch, not on
`main`.

Examples of what belongs on `avow/main`, not `main`:
- Any chapter of the Avow design memo.
- Threat-model write-ups that name novel attack classes or defence
  primitives.
- Vocabulary work (avowal, ratification, principal channel,
  guarded action sequence, adaptive attention, etc.).
- Comparative analyses of related research (CaMeL, Pipelock,
  AgentDojo, sandlock, LLMail-Inject) where the analysis is itself
  the contribution.
- Any document that argues for the novelty of a Nightjar feature
  as a research finding.

Examples of what stays on `main`:
- The defer-when-busy feature (gameplay courtesy, product-flavoured).
- Config inspection commands (operator UX).
- Bug fixes from Nightjar's audit logs.
- Per-turn truncation, MCP server, attachments passthrough.
- Operator-facing documentation, examples, READMEs.
- Test infrastructure, CI, packaging.

### How the boundary is enforced

1. **Pre-push hook** at `.git/hooks/pre-push` refuses any push of
   branches under `refs/heads/avow/`. Catches `git push origin avow/main`,
   `git push --all`, and similar. This is the primary defence.
2. **Locked remote refspec.** `remote.origin.push` is configured to
   `refs/heads/main:refs/heads/main`. A bare `git push` defaults
   to only pushing `main`. Belt-and-braces.
3. **Pre-commit hook** at `.git/hooks/pre-commit` scans staged
   diffs for Avow-vocabulary tokens (`Avow` case-sensitive,
   `avowal` / `ratify` / `ratification` / `principal channel` /
   `guarded action sequence` / `adaptive attention` lowercase) and
   refuses commits that introduce them — *but only on non-avow
   branches*. On `avow/*` branches the hook skips, because that's
   exactly where Avow content belongs.
4. **Operator review.** The user reads diffs before they ship. The
   hooks are safety nets, not the primary boundary.
5. **Agent self-flagging (this file).** If you notice mid-task
   that your work is drifting toward research-flavoured output,
   raise it with the user. Don't just commit and hope.

### Common patterns

- "I think we should add a research-y design doc explaining X"
  → write it on the `avow/main` branch under `avow/docs/`, not
  on main.
- "This bug fix relates to a deeper architectural question"
  → ship the bug fix on main; capture the architectural question
  as a memo on `avow/main`.
- "Nightjar's behaviour here is a useful data point for a paper"
  → append a dated entry to `avow/observations.md` on the
  `avow/main` branch (the research-side hopper). Keep the entry
  small; the analysis happens later in the design memo, not in
  the hopper itself.
- "I want to rename a Nightjar concept to match Avow vocabulary"
  → no. Nightjar's concepts have their own names. Avow can use
  whatever vocabulary it wants, on its own branch.

### Working with both branches

Routine workflow:
- Day-to-day Nightjar work happens on `main`. Push freely to GitHub.
- Avow work happens on `avow/main`. Never pushed.
- When Nightjar gets a meaningful update, periodically run
  `git checkout avow/main && git merge main` to bring the avow
  branch up to date. The Avow daemon (when it exists) lives on
  this branch and inherits Nightjar's improvements.
- Never `git push --all`, `git push --mirror`, or use any other
  command that pushes more than the explicit refs. The hooks will
  refuse, but it's faster to just be deliberate.

### THE ONE RULE THAT MATTERS

```
git merge main        ← inheritance, safe, run freely from avow/main
git merge avow/main   ← CATASTROPHE, NEVER run from main
```

`git merge main` (run while on `avow/main`) brings Nightjar
improvements into the research branch. Routine and safe.

`git merge avow/main` (run while on `main`) would pull every Avow
commit onto the public branch, ready to be pushed. **The hooks
cannot catch this** — they protect against commits introducing
vocab and pushes of avow refs, not against merges between
branches. Operator vigilance is the only defence.

If you ever find yourself thinking "I want to bring something from
avow back to main," the answer is `git cherry-pick` of a specific
commit, or a fresh manual edit on main. **Never a merge.** If
you're unsure, stop and ask the user before running any merge
command on the main branch.

**Restricted commands — pause and confirm with the user first:**
- Any `git merge` while standing on `main`.
- `git remote add`, `git remote set-url`, `git config remote.*`
  (could re-route the remote and undermine the locked refspec).
- `git push --force --all`, `git push --mirror`.
- Any operation that bypasses the hooks (`--no-verify`).

The cost of asking is one message. The cost of a wrong merge is
the entire research arm becoming public.

### When in doubt

Ask. The cost of pausing to check is one message. The cost of
publishing research material prematurely is a paper that can no
longer be submitted as novel work.
