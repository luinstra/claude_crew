"""Prompt templates for the review panel — pure string builders.

Templates: ``plan_review``, ``code_review``, and ``council`` (a single-round
free-form council seat). NO multi-round debate machinery — no SEAT/ROUND
headers, no rounds/rebuttals, no DEBATE_INTEGRITY / NO_EXPLORE constraint
blocks, no state-threading. ``council`` is a LIGHTWEIGHT single-round
structured-independent-critical stance, not a grapple debate.

The review templates ask each seat to SCORE the target against its named
criteria (a short rubric -> per-criterion pass/fail + an overall confidence),
not just free-text findings — a lightweight rubric (adapted from ECC's gan
evaluator loop, affaan-m/ECC, MIT) so the measure-twice/build loops have an
explicit threshold to iterate against.

Both seat kinds (subprocess + Task) get CRITERIA-EQUIVALENT wording from these
builders (same content; byte-identical is NOT required).
"""

from __future__ import annotations


_PLAN_CRITERIA = """\
Score the plan against EACH criterion (PASS or FAIL, with a one-line reason):
- Clarity: Can someone execute this without guessing?
- Testability: Are acceptance criteria concrete and measurable?
- Completeness: Are edge cases and dependencies addressed?
- Context: Does the plan explain WHY, not just WHAT?"""

_CODE_CRITERIA = """\
Score the diff against EACH criterion (PASS or FAIL, with a one-line reason):
- Correctness: Does the change do what it claims, without obvious bugs?
- Completeness: Are all requirements met; any gaps or missing cases?
- Quality: Style consistency, readability, error handling.
- Safety: Security, data-loss, and performance concerns."""


def _rubric_footer() -> str:
    return (
        "After scoring, list findings. Tag each finding with a severity:\n"
        "  [BLOCKING] — must be fixed before this can be accepted.\n"
        "  [MINOR]    — should be addressed but does not block acceptance.\n"
        "Then give an overall CONFIDENCE (low / medium / high) that the target "
        "clears the bar.\n"
        "End with a one-line verdict: APPROVED (no [BLOCKING] findings) or "
        "REVISE (one or more [BLOCKING] findings)."
    )


def plan_review(content: str, descriptor: str = "plan") -> str:
    """Build the plan-review prompt for one seat."""
    return f"""You are one reviewer on a multi-model review panel. Review the \
following PLAN as a single seat. Be specific and terse.

TARGET: {descriptor}

{_PLAN_CRITERIA}

{_rubric_footer()}

--- BEGIN PLAN ---
{content}
--- END PLAN ---
"""


def code_review(content: str, descriptor: str = "code diff", notes: str = "") -> str:
    """Build the code-review prompt for one seat."""
    notes_block = f"\nCONTEXT NOTES:\n{notes}\n" if notes else ""
    return f"""You are one reviewer on a multi-model review panel. Review the \
following CODE DIFF as a single seat. Be specific and terse.

TARGET: {descriptor}{notes_block}

{_CODE_CRITERIA}

{_rubric_footer()}

--- BEGIN DIFF ---
{content}
--- END DIFF ---
"""


def plan_review_ref(ref_path: str, descriptor: str = "plan") -> str:
    """Plan-review prompt that points the seat at the file to READ ITSELF.

    Reference mode (the default): the plan body is NOT inlined. The seat opens
    the file in the repository it is already running in. Keeps the prompt tiny
    (no payload transport, no ARG_MAX pressure, no synthesis-context bloat).
    """
    return f"""You are one reviewer on a multi-model review panel. Review a \
PLAN as a single seat. Be specific and terse.

TARGET: {descriptor}

The plan is NOT inlined below — open and read it yourself in the current \
repository:
  read:  {ref_path}

{_PLAN_CRITERIA}

{_rubric_footer()}
"""


def code_review_ref(diff_cmd: str | None, descriptor: str = "code diff", notes: str = "") -> str:
    """Code-review prompt that points the seat at the diff to FETCH ITSELF.

    Reference mode (the default): the diff is NOT inlined. The seat runs the
    given git command in the repository it is already in, and reads the changed
    files for context. If there is no diff command (e.g. no commits yet), it is
    told to inspect the working tree directly.
    """
    notes_block = f"\nCONTEXT NOTES:\n{notes}\n" if notes else ""
    scope_rule = (
        "Read what you need to judge the change well: the changed files, AND any "
        "file the change AFFECTS even if it wasn't itself modified — callers of "
        "changed code, tests, and docs/configs/READMEs that describe or reference "
        "the changed behavior (flag any the change leaves stale, inconsistent, or "
        "unupdated; that is a real finding). What to AVOID is a ground-up audit of "
        "unrelated subsystems the change doesn't touch — follow the change's blast "
        "radius, don't re-review the whole repository for its own sake."
    )
    if diff_cmd:
        how = (
            "The diff is NOT inlined below — reproduce it yourself in the "
            "current repository:\n"
            f"  run:  {diff_cmd}\n"
            f"Then review the changes that command shows. {scope_rule}"
        )
    else:
        how = (
            "The diff is NOT inlined below — there is no diff command for this "
            "target (e.g. no commits yet). Inspect the new/changed working-tree "
            f"files directly in the current repository and review them. {scope_rule}"
        )
    return f"""You are one reviewer on a multi-model review panel. Review a \
CODE DIFF as a single seat. Be specific and terse.

TARGET: {descriptor}{notes_block}

{how}
If any untracked/new files are noted above, read those files directly too — \
they are not part of the diff command.

{_CODE_CRITERIA}

{_rubric_footer()}
"""


def council(question: str) -> str:
    """Build the single-round council prompt for one seat.

    Structured-independent-critical stance: each seat gives its OWN take on the
    question, plus the strongest objection and the key risks/tradeoffs others
    might miss. Evidence-based — no manufactured contrarianism, no
    rubber-stamping. Single round only; no rebuttals, no judge step (the
    orchestrating Claude synthesizes). Both seat kinds get this same wording.
    """
    return f"""You are one seat on a multi-model council. You are given a \
QUESTION and must give an INDEPENDENT, CRITICAL take. You are NOT reviewing a \
plan or diff — you are weighing in on a free-form question alongside other \
seats whose answers you cannot see. Be specific and terse.

Give exactly these three things:
1. DIRECT TAKE: your direct answer / recommendation, stated plainly up front.
2. STRONGEST OBJECTION: the single strongest objection to your own take — the \
case against it, argued honestly.
3. RISKS / TRADEOFFS: the key risks, costs, or tradeoffs others on the panel \
might miss.

Ground every claim in evidence or concrete reasoning. Do NOT manufacture \
contrarianism for its own sake, and do NOT rubber-stamp the obvious answer — \
if the answer really is clear, say so and say why, then still surface the \
strongest objection and the real tradeoffs.

--- QUESTION ---
{question}
--- END QUESTION ---
"""


def build_prompt(target, *, inline: bool = False) -> str:
    """Dispatch to the right template based on a targets.Target.kind.

    ``inline=False`` (the default) is REFERENCE mode: the seat is told where to
    find the target (a git command for diffs, a file path for plans) and fetches
    it itself in the repo it is already running in. ``inline=True`` embeds the
    pre-computed ``content`` in the prompt (the legacy behavior) — use it when a
    seat can't reach the repo, or for an uncommitted working tree you want
    pinned to the exact bytes reviewed.
    """
    notes = "\n".join(target.notes) if getattr(target, "notes", None) else ""
    if target.kind == "plan":
        if inline:
            return plan_review(target.content, target.descriptor)
        return plan_review_ref(
            getattr(target, "ref_path", None) or target.scope, target.descriptor
        )
    if inline:
        return code_review(target.content, target.descriptor, notes)
    return code_review_ref(
        getattr(target, "diff_cmd", None), target.descriptor, notes
    )
