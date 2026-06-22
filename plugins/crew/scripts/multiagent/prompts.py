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


def build_prompt(target) -> str:
    """Dispatch to the right template based on a targets.Target.kind."""
    if target.kind == "plan":
        return plan_review(target.content, target.descriptor)
    notes = "\n".join(target.notes) if getattr(target, "notes", None) else ""
    return code_review(target.content, target.descriptor, notes)
