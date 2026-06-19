"""Prompt templates for the review panel — pure string builders.

ONLY two templates: ``plan_review`` and ``code_review``. NO debate templates
(native debate is CUT — /crew:debate shims to octopus). No SEAT/ROUND headers,
no DEBATE_INTEGRITY / NO_EXPLORE constraint blocks.

Both templates ask each seat to SCORE the target against its named criteria
(a short rubric -> per-criterion pass/fail + an overall confidence), not just
free-text findings — a lightweight rubric (adapted from ECC's gan evaluator
loop, affaan-m/ECC, MIT) so the measure-twice/build loops have an explicit
threshold to iterate against.

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


def build_prompt(target) -> str:
    """Dispatch to the right template based on a targets.Target.kind."""
    if target.kind == "plan":
        return plan_review(target.content, target.descriptor)
    notes = "\n".join(target.notes) if getattr(target, "notes", None) else ""
    return code_review(target.content, target.descriptor, notes)
