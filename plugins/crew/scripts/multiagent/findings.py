"""Parse + group structured review findings across panel seats (pure, no I/O).

This is the context-efficiency keystone: every seat is ASKED (via the shared
``prompts._rubric_footer``) to emit a structured ``## VERDICT / ## CRITERIA /
## FINDINGS / ## CONFIDENCE`` block. This module PARSES those blocks and GROUPS
the findings so the orchestrator carries ONE deduped digest ("N/N seats flagged
X") instead of every seat's full prose verdict.

Design contracts (do NOT regress):
  * **Pure.** No file/network/model I/O. ``parse_seat`` NEVER raises — it
    operates on an already-coerced ``ProviderResult`` and degrades to "not
    parsed" on anything it can't read.
  * **Never DROP a finding.** ``findings_parsed`` gates the RAW
    skip on FINDINGS parseability ALONE — NOT on VERDICT/CRITERIA presence. A
    seat with metadata headers but PROSE findings has ``findings_parsed=False``
    and is rendered VERBATIM (its verdict/criteria may still feed the
    roster/matrix; its prose body still appears raw, so nothing is lost).
  * **Never over-merge.** Severity is the ONLY hard partition.
    Within a partition, COMPLETE-LINKAGE clustering: a finding joins a cluster
    only if it is ``compatible(...)`` with EVERY existing member
    (``path_compatible AND line_compatible AND jaccard>=0.5``). Because
    ``path_compatible`` is not transitive, complete-linkage keeps distinct-dir
    findings apart and forbids single-link transitive bridging.
  * **Two distinct counts.** The VERDICTS roster is over every seat
    that RAN; each finding's ``M/N`` denominator N is over findings-parsed seats
    ONLY. They are labeled visibly differently and never conflated.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import render
from .providers import ProviderResult


# =============================================================================
# Data shapes
# =============================================================================

@dataclass
class Finding:
    severity: str            # "BLOCKING" | "MINOR"
    raw_path: str | None     # the path token as written by the seat (or None)
    line: int | None         # extracted line number (or None — a wildcard)
    description: str          # prose AFTER the path/line (for similarity)
    seat: str                # the seat that flagged it
    text: str = ""           # the full finding content (path+desc) — representative


@dataclass
class SeatParse:
    seat: str
    verdict: str | None = None          # "APPROVED" | "REVISE" | None
    confidence: str | None = None       # "low" | "medium" | "high" | None
    criteria: dict[str, str] = field(default_factory=dict)  # name -> PASS|FAIL
    findings: list[Finding] = field(default_factory=list)
    findings_parsed: bool = False


@dataclass
class Group:
    severity: str
    members: list[Finding]

    @property
    def seats(self) -> list[str]:
        """Distinct flagging seats, in member-discovery order."""
        seen: list[str] = []
        for m in self.members:
            if m.seat not in seen:
                seen.append(m.seat)
        return seen

    @property
    def count(self) -> int:
        return len(self.seats)

    @property
    def representative(self) -> str:
        """The longest complete finding line in the cluster (severity is uniform)."""
        return max((m.text for m in self.members), key=len, default="")


# =============================================================================
# Section splitting + metadata extraction (restricted to the seat's own headers)
# =============================================================================

_HEADER_RE = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")

# A criteria line: tolerant of bullet/bold/table-cell punctuation around the
# name AND the verdict — matches codex `- Correctness: FAIL`, agy
# `**Correctness:** PASS`, and cursor table cells `| **Correctness** | **FAIL** |`.
# The name class allows a hyphen so compound criteria like `Error-handling` parse.
_CRITERIA_RE = re.compile(
    r"^\s*[-*|\s]*(?:\*\*)?(?P<name>[A-Za-z][A-Za-z -]+?)(?:\*\*)?"
    r"\s*[:—|-]\s*(?:\*\*)?\s*(?P<verdict>PASS|FAIL)\b",
    re.IGNORECASE,
)

# Anchored form first: the verdict word ALONE on its line (optionally bolded) —
# the shape the schema asks for. Prose like "This is not APPROVED" can't match
# it, so a compliant own-line verdict always beats an incidental mention. The
# loose form is the fallback for tolerated shapes like `**Verdict: REVISE**`.
_VERDICT_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?(APPROVED|REVISE)(?:\*\*)?\s*\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_VERDICT_RE = re.compile(r"\b(APPROVED|REVISE)\b", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"\b(low|medium|high)\b", re.IGNORECASE)

# STEP 1 — the severity tag at the head of a finding line.
_SEVERITY_RE = re.compile(r"^\s*[-*\s]*(?:\*\*)?\[(BLOCKING|MINOR)\]\**\s*", re.IGNORECASE)

# STEP 2 path/line forms.
#  (a) markdown link  [label](path) / [label](path:line)
_LINK_RE = re.compile(r"^\s*\[[^\]]*\]\(\s*(?P<p>[^)]+?)\s*\)")
#  (b) bare path:line — path must contain a '.' or '/', line is digits
_PATHLINE_RE = re.compile(r"^\s*(?P<p>[^\s:]*[./][^\s:]*):(?P<l>\d+)\b")
#  (c) a bare path token whose FINAL segment carries a dot + 1-8 alnum extension
_BAREEXT_RE = re.compile(r"^\s*(?P<p>\S+\.[A-Za-z0-9]{1,8})(?=$|\s)")
#  (c2) a bare, well-known EXTENSIONLESS filename (Dockerfile, Makefile, …) — these
#  carry no dot/slash so (a)/(b)/(c) miss them and they'd otherwise parse as a
#  no-file finding and lose path-aware grouping. A fixed known set (NOT a generic
#  capitalized-word rule) keeps it cheap and free of prose false-positives. An
#  optional ``.suffix`` (Dockerfile.dev) and trailing ``:line`` are honored.
_KNOWN_BARE_FILES = (
    "Dockerfile", "Containerfile", "Makefile", "GNUmakefile", "Rakefile",
    "Gemfile", "Procfile", "Jenkinsfile", "Vagrantfile", "Brewfile", "Justfile",
    "Caddyfile", "Berksfile", "Guardfile", "Capfile", "Podfile", "Cartfile",
    "Fastfile", "Appfile", "Thorfile", "Dangerfile",
)
_BAREFILE_RE = re.compile(
    r"^\s*(?P<p>(?:" + "|".join(_KNOWN_BARE_FILES) + r")(?:\.[A-Za-z0-9_-]+)?)"
    r"(?::(?P<l>\d+))?(?=$|[\s:—–-])"
)

_NO_FILE_RE = re.compile(r"^\s*\(no file\)\s*", re.IGNORECASE)
# leading separator between path and description: em-dash, en-dash, hyphen, colon.
_SEP_RE = re.compile(r"^\s*[—–:-]+\s*")


def _sections(text: str) -> dict[str, str]:
    """Split markdown into ``{UPPER-HEADER-NAME: body}`` by ``#``-headers.

    Only the seat's OWN section bodies are returned — metadata (verdict,
    criteria, confidence) is read EXCLUSIVELY from its named section, so a free-
    prose seat with no ``## VERDICT`` header yields no verdict (the
    real-prose fixture has 'no metadata').
    """
    sections: dict[str, str] = {}
    current: str | None = None
    current_level = 0
    buf: list[str] = []
    for line in text.split("\n"):
        m = _HEADER_RE.match(line)
        if m:
            level = len(m.group("hashes"))
            # A DEEPER subheading (h3+ inside an h2 section) is BODY content, not a
            # section boundary — a `### Sub-section` inside `## FINDINGS` must not
            # prematurely terminate FINDINGS and drop the tagged lines below it.
            # Only a peer-or-shallower header (level <= the open section's level)
            # closes the current section.
            if current is not None and level > current_level:
                buf.append(line)
                continue
            if current is not None:
                sections[current] = "\n".join(buf)
            current = m.group("title").strip().upper()
            current_level = level
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)
    return sections


def _parse_path_line(remainder: str) -> tuple[str | None, int | None, str]:
    """Two-step leading path/line extractor; returns
    ``(raw_path, line, description)``. ``description`` is the remainder with the
    matched path/line + a leading separator stripped (or the whole remainder when
    no path matched)."""
    # explicit (no file)
    m = _NO_FILE_RE.match(remainder)
    if m:
        return None, None, _SEP_RE.sub("", remainder[m.end():]).strip()
    # (a) markdown link
    m = _LINK_RE.match(remainder)
    if m:
        raw = m.group("p").strip()
        line = None
        lm = re.search(r":(\d+)\s*$", raw)
        if lm:
            line = int(lm.group(1))
            raw = raw[: lm.start()]
        desc = _SEP_RE.sub("", remainder[m.end():]).strip()
        return raw or None, line, desc
    # (b) bare path:line
    m = _PATHLINE_RE.match(remainder)
    if m:
        rest = remainder[m.end():]
        # strip a trailing `:col` (path:line:col) so the column digits don't leak
        # into the description.
        rest = re.sub(r"^:\d+\b", "", rest)
        desc = _SEP_RE.sub("", rest).strip()
        return m.group("p"), int(m.group("l")), desc
    # (c) bare path with a known extension
    m = _BAREEXT_RE.match(remainder)
    if m:
        desc = _SEP_RE.sub("", remainder[m.end():]).strip()
        return m.group("p"), None, desc
    # (c2) a bare well-known extensionless filename (Dockerfile, Makefile, …)
    m = _BAREFILE_RE.match(remainder)
    if m:
        line = int(m.group("l")) if m.group("l") else None
        desc = _SEP_RE.sub("", remainder[m.end():]).strip()
        return m.group("p"), line, desc
    # (d) no path
    return None, None, _SEP_RE.sub("", remainder).strip()


_NONE_SENTINELS = ("", "none", "none.", "(none)", "- none", "* none", "n/a")


def _parse_findings_section(body: str, seat: str) -> tuple[list[Finding], bool]:
    """Parse the ``## FINDINGS`` body. Returns ``(findings, findings_parsed)``.

    ``findings_parsed`` is True IFF the section had >=1 parseable ``- [SEVERITY]``
    line with NO leftover untagged prose, OR was explicitly empty/`none` (a
    legitimate zero-finding result).

    MIXED guard (never-drop): a section with >=1 tagged finding AND >=1 untagged
    NON-BLANK, NON-HEADER prose line is ambiguous — silently keeping only the
    tagged lines would DROP the untagged prose findings from BOTH the grouped
    digest and the raw fallback. So a mixed section is treated as NOT parsed
    (``findings_parsed=False``): the whole seat body surfaces VERBATIM under RAW,
    guaranteeing no finding text is lost. (A deeper ``### sub-heading`` inside the
    body is structural, not prose — it does not trip the guard.)"""
    findings: list[Finding] = []
    leftover_prose = False
    for line in body.split("\n"):
        sm = _SEVERITY_RE.match(line)
        if sm:
            severity = sm.group(1).upper()
            remainder = line[sm.end():].strip()
            raw_path, lineno, desc = _parse_path_line(remainder)
            findings.append(Finding(
                severity=severity, raw_path=raw_path, line=lineno,
                description=desc, seat=seat, text=remainder,
            ))
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADER_RE.match(line):
            continue  # a deeper sub-heading inside FINDINGS — structural, not prose.
        if stripped.lower() in _NONE_SENTINELS:
            continue
        leftover_prose = True
    if findings:
        # Mixed tagged + untagged-prose -> surface the seat raw so nothing drops.
        return ([], False) if leftover_prose else (findings, True)
    stripped = body.strip().lower()
    # explicitly empty / "none" -> a valid zero-finding compliant result.
    if stripped in _NONE_SENTINELS:
        return [], True
    # section present but prose (no tagged lines, not empty) -> NOT parsed.
    return [], False


def parse_seat(result: ProviderResult) -> SeatParse:
    """Parse one seat's review into a ``SeatParse`` for the GROUPED digest (never raises).

    GROUPING-ONLY repair projection: when the seat carries a ``repaired_output``
    (a `repair-seat` reformat that PARSED), the grouped digest's findings/verdict/
    criteria are extracted from that repaired text — but the seat's original
    ``output`` on disk is untouched, and ``render.render_block``/``render_panel``
    (the faithful ``--full``/RAW record) still render the ORIGINAL words. So a
    repair can only ever refine THIS seat's grouped rows; it can never remove or
    alter the recoverable original. When ``repaired_output`` is None (the common
    case, and BEFORE any repair) this parses the original ``output`` exactly as
    before."""
    parse = SeatParse(seat=result.name)
    if render.is_skipped(result):
        return parse  # skipped seats contribute nothing, never findings_parsed.
    repaired = getattr(result, "repaired_output", None)
    text = (repaired if repaired is not None else result.output) or ""
    sections = _sections(text)

    vbody = sections.get("VERDICT")
    if vbody is not None:
        m = _VERDICT_LINE_RE.search(vbody) or _VERDICT_RE.search(vbody)
        if m:
            parse.verdict = m.group(1).upper()

    cbody = sections.get("CONFIDENCE")
    if cbody is not None:
        m = _CONFIDENCE_RE.search(cbody)
        if m:
            parse.confidence = m.group(1).lower()

    crbody = sections.get("CRITERIA")
    if crbody is not None:
        for line in crbody.split("\n"):
            m = _CRITERIA_RE.match(line)
            if m:
                name = m.group("name").strip()
                parse.criteria[name] = m.group("verdict").upper()

    fbody = sections.get("FINDINGS")
    if fbody is None:
        parse.findings_parsed = False
    else:
        parse.findings, parse.findings_parsed = _parse_findings_section(fbody, result.name)
    return parse


# =============================================================================
# Grouping predicate: pure, deterministic, no model call
# =============================================================================

_STOPWORDS = frozenset(
    "the a an is to of in and or for it this that be as on".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_path(raw: str | None, repo_root: str | None = None) -> list[str] | None:
    """Repo-relative, lowercased SEGMENT LIST (or None when no path).

    Strips a known absolute repo-root prefix (``repo_root``, default
    ``CLAUDE_PROJECT_DIR``/cwd) to make the path repo-relative, lowercases, and
    splits on ``/`` dropping empty/``.`` segments. A bare basename -> a 1-segment
    list. The eventual ``path_compatible`` suffix test makes stripping optional
    for correctness, but it keeps the matrix/digest paths tidy."""
    if raw is None:
        return None
    p = raw.strip().replace("\\", "/")
    if repo_root is None:
        repo_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = (repo_root or "").replace("\\", "/").rstrip("/")
    if root:
        low_p, low_root = p.lower(), root.lower()
        if low_p == low_root:
            p = ""
        elif low_p.startswith(low_root + "/"):
            p = p[len(root) + 1:]
    segs = [s for s in p.lower().split("/") if s and s != "."]
    return segs or None


def path_compatible(a: list[str] | None, b: list[str] | None) -> bool:
    """True when two normalized paths may name the SAME file.

    Both None -> True (file-less findings compare on description alone); exactly
    one None -> False (don't merge a file-specific finding with a file-less one);
    both present -> True IFF one segment-list is a SUFFIX of the other."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return long[len(long) - len(short):] == short


def line_compatible(a: int | None, b: int | None) -> bool:
    """Symmetric ±10-line window; an absent line is a wildcard."""
    if a is None or b is None:
        return True
    return abs(a - b) <= 10


def desc_tokens(desc: str) -> frozenset[str]:
    """Lowercased ``[a-z0-9]+`` tokens minus the fixed stopword set."""
    return frozenset(t for t in _TOKEN_RE.findall(desc.lower()) if t not in _STOPWORDS)


def jaccard(a: str, b: str) -> float:
    ta, tb = desc_tokens(a), desc_tokens(b)
    if not ta and not tb:
        return 1.0
    union = ta | tb
    if not union:
        return 1.0
    return len(ta & tb) / len(union)


def compatible(a: Finding, b: Finding, repo_root: str | None = None) -> bool:
    """The combined merge predicate: same severity, path-, line-, and
    description-compatible."""
    if a.severity != b.severity:
        return False
    if not path_compatible(normalize_path(a.raw_path, repo_root),
                           normalize_path(b.raw_path, repo_root)):
        return False
    if not line_compatible(a.line, b.line):
        return False
    return jaccard(a.description, b.description) >= 0.5


_SEVERITY_ORDER = ("BLOCKING", "MINOR")


def group(findings: list[Finding], repo_root: str | None = None) -> list[Group]:
    """HARD-partition by severity, then COMPLETE-LINKAGE cluster within each
    partition. Greedy first-fit is deterministic (not guaranteed
    minimal; over-split is safe, the synthesis LLM merges leftovers)."""
    groups: list[Group] = []
    for sev in _SEVERITY_ORDER:
        clusters: list[list[Finding]] = []
        for f in [x for x in findings if x.severity == sev]:
            for c in clusters:
                if all(compatible(f, m, repo_root) for m in c):
                    c.append(f)
                    break
            else:
                clusters.append([f])
        groups.extend(Group(severity=sev, members=c) for c in clusters)
    return groups


# =============================================================================
# Digest renderer
# =============================================================================

_CANONICAL_CRITERIA = ("Correctness", "Completeness", "Quality", "Safety",
                       "Clarity", "Testability", "Context")


def _criteria_rows(parses: list[SeatParse]) -> list[str]:
    """UNION of emitted criterion names, canonical rubric order, extras appended."""
    emitted: dict[str, str] = {}  # lower -> display name (first-seen casing)
    for p in parses:
        for name in p.criteria:
            emitted.setdefault(name.lower(), name)
    rows: list[str] = []
    used: set[str] = set()
    for canon in _CANONICAL_CRITERIA:
        if canon.lower() in emitted and canon.lower() not in used:
            rows.append(canon)
            used.add(canon.lower())
    for low, disp in emitted.items():
        if low not in used:
            rows.append(disp)
            used.add(low)
    return rows


def _criterion_for(parse: SeatParse, row: str) -> str:
    for name, verdict in parse.criteria.items():
        if name.lower() == row.lower():
            return verdict
    return "?"


def render_digest(
    results: list[ProviderResult],
    repo_root: str | None = None,
    quorum: tuple[int, int] | None = None,
) -> str:
    """Render the grouped panel digest. Falls back to the byte-faithful
    ``render.render_panel`` when NO seat is findings-parsed.

    ``quorum`` is an optional ``(launched, usable)`` pair of quorum facts the
    CALLER computed from the run manifest (this module stays pure: no I/O, no
    manifest read). When given, the digest opens with one PANEL header line
    stating the strict-majority threshold (``launched // 2 + 1``) and whether
    the usable count meets it; a NOT MET header adds the line that an APPROVED
    verdict cannot be recorded. The header prepends in the no-parse fallback
    too (the quorum facts hold regardless of parseability); with ``quorum``
    omitted, output is byte-identical to before the header existed.
    """
    prefix = ""
    if quorum is not None:
        launched, usable = quorum
        threshold = launched // 2 + 1
        hdr = [
            f"PANEL: {launched} launched · {usable} usable · quorum "
            f"{threshold}: {'MET' if usable >= threshold else 'NOT MET'}"
        ]
        if usable < threshold:
            hdr.append("an APPROVED verdict cannot be recorded from this panel")
        prefix = "\n".join(hdr) + "\n\n"

    parses = [parse_seat(r) for r in results]
    parsed_idx = [i for i, p in enumerate(parses) if p.findings_parsed]

    if not parsed_idx:
        # zero findings-parsed -> exactly today's faithful concatenation
        # (behind the quorum header when one was supplied).
        return prefix + render.render_panel(results)

    # A "ran" seat is one that actually executed — a named-but-SKIPPED seat (no
    # result file / no usable block) did NOT run, so it is excluded from the
    # ran-count R and from the RAW/UNPARSED section (it is not "unparsed", it is
    # absent). It still appears in the VERDICTS roster below as "(skipped)".
    ran_idx = [i for i, r in enumerate(results) if not render.is_skipped(r)]
    R = len(ran_idx)
    P = len(parsed_idx)
    names = ", ".join(r.name for r in results)
    lines: list[str] = []
    # "seats:" not "seats ran:" — the list is every NAMED seat, skipped included;
    # the VERDICTS roster below is what distinguishes ran from skipped.
    lines.append(f"# PANEL DIGEST — seats: {names}  ({R} ran, {P} findings-parsed)")
    lines.append("")

    # --- VERDICTS roster (every seat that RAN) ---
    lines.append("## VERDICTS  (roster: every seat that ran)")
    n_appr = n_rev = 0
    for r, p in zip(results, parses):
        if p.verdict == "APPROVED":
            n_appr += 1
            vlabel = "APPROVED"
        elif p.verdict == "REVISE":
            n_rev += 1
            vlabel = "REVISE"
        elif render.is_skipped(r):
            vlabel = "(skipped)"
        else:
            vlabel = "(unparsed)"
        conf = f"  ({p.confidence})" if p.confidence else ""
        lines.append(f"- {r.name}  {vlabel}{conf}")
    lines.append(f"Tally: {n_appr} APPROVED / {n_rev} REVISE   (roster = {R} ran)")
    lines.append("")

    # --- CRITERIA MATRIX (union rows, canonical order, ? for missing) ---
    rows = _criteria_rows(parses)
    if rows:
        lines.append("## CRITERIA MATRIX")
        seat_names = [r.name for r in results]
        label_w = max([len("Criterion")] + [len(x) for x in rows])
        col_w = [max(len(sn), 4) for sn in seat_names]
        header = "Criterion".ljust(label_w) + "  " + "  ".join(
            sn.ljust(col_w[i]) for i, sn in enumerate(seat_names))
        lines.append(header.rstrip())
        for row in rows:
            cells = [_criterion_for(p, row).ljust(col_w[i]) for i, p in enumerate(parses)]
            lines.append((row.ljust(label_w) + "  " + "  ".join(cells)).rstrip())
        lines.append("")

    # --- GROUPED FINDINGS (N = findings-parsed seats) ---
    findings: list[Finding] = []
    for i in parsed_idx:
        findings.extend(parses[i].findings)
    groups = group(findings, repo_root)
    lines.append(f"## GROUPED FINDINGS  (each M/N: N = findings-parsed seats = {P})")
    if not groups:
        lines.append("(none — no findings flagged by any findings-parsed seat)")
    else:
        # BLOCKING partition first; within a tier, higher agreement first,
        # singletons last (descending count is stable for ties).
        for sev in _SEVERITY_ORDER:
            tier = [g for g in groups if g.severity == sev]
            tier.sort(key=lambda g: -g.count)
            for g in tier:
                seats = ", ".join(g.seats)
                if g.count == 1:
                    lines.append(f"- ⚠ SINGLETON 1/{P} [{sev}] {g.representative}  ({seats})")
                else:
                    lines.append(f"- {g.count}/{P} [{sev}] {g.representative}  ({seats})")
    lines.append("")

    # --- RAW / UNPARSED SEATS (every RAN, findings_parsed=False seat, verbatim) ---
    # A SKIPPED seat never ran, so it is NOT "unparsed" — it is excluded here
    # (already shown as "(skipped)" in the VERDICTS roster). Only seats that RAN
    # but did not parse render raw.
    lines.append("## RAW / UNPARSED SEATS")
    raw_idx = [
        i for i, (r, p) in enumerate(zip(results, parses))
        if not p.findings_parsed and not render.is_skipped(r)
    ]
    if not raw_idx:
        lines.append(f"(none — all {P} seats findings-parsed)")
    else:
        sep = "\n\n" + ("-" * 72) + "\n\n"
        blocks = [render.render_block(results[i]) for i in raw_idx]
        lines.append(sep.join(blocks))

    return prefix + "\n".join(lines)


def unparsed_seats(results: list[ProviderResult]) -> list[str]:
    """Seat names whose output RAN but did NOT parse into findings — the haiku-
    repair candidates (Part 2). Excludes skipped seats and empty-output seats
    (nothing to reformat)."""
    out: list[str] = []
    for r in results:
        if render.is_skipped(r):
            continue
        if not (r.output or "").strip():
            continue
        if not parse_seat(r).findings_parsed:
            out.append(r.name)
    return out
