#!/usr/bin/env bash
#
# Post-commit hook: Auto-bump versions based on conventional commits
#
# Install: ln -sf ../../scripts/post-commit-version-bump.sh .git/hooks/post-commit
#
# This script runs after each commit and:
# - Skips if the commit is already a version bump
# - Skips on a detached HEAD or any in-progress rebase/merge/cherry-pick
# - Analyzes commits since last bump to determine bump type
# - Bumps marketplace version if marketplace-level files changed
# - Bumps plugin versions only for plugins with changes
#
# Hardening (R4): NO `set -e`. Version reads happen BEFORE any file is touched;
# a failure there aborts with a byte-identical tree. Every json file is
# byte-snapshotted before rewrite, and ANY failure after the first rewrite
# un-stages the paths (`git reset`) and restores each file from its snapshot —
# so a failed bump leaves neither a mutated working tree NOR a dirty index.
# Version read goes through python3/json (argv-only, UTF-8) so it never assumes a
# single "version" key; the WRITE is a surgical, format-preserving substitution
# of only that one version value — so the first hardened bump is a clean 1-line
# diff (no reformat of hand-authored inline arrays, and it locates
# marketplace.json's nested metadata.version, which a top-level ["version"] can't).
#

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info() { echo -e "${BLUE}[version-bump]${NC} $1"; }
success() { echo -e "${GREEN}[version-bump]${NC} $1"; }
warn() { echo -e "${YELLOW}[version-bump]${NC} $1"; }
error() { echo -e "${RED}[version-bump]${NC} $1"; }

# --- Git-state guards (skip cleanly, never partial-mutate) -------------------

# Detached HEAD → no branch to advance; skip.
if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
    exit 0
fi

# In-progress rebase / cherry-pick / merge → the post-commit fires per replayed
# commit; a bump mid-op would corrupt the sequence. Skip until the op finishes.
_git_dir=$(git rev-parse --git-dir 2>/dev/null || echo ".git")
if [ -d "$_git_dir/rebase-merge" ] || [ -d "$_git_dir/rebase-apply" ] \
   || [ -f "$_git_dir/CHERRY_PICK_HEAD" ] || [ -f "$_git_dir/MERGE_HEAD" ]; then
    exit 0
fi

# python3 is REQUIRED for the safe read/write path; without it, do nothing
# rather than fall back to a fragile grep/sed mutation.
if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 not found — skipping version bump (never partial-mutate)"
    exit 0
fi

# Skip if this commit is already a version bump (prevents infinite loop)
LAST_MSG=$(git log -1 --format="%s")
if [[ "$LAST_MSG" == "chore: bump version"* ]]; then
    exit 0
fi

# Skip if commit message contains [skip version] or [no bump]
if [[ "$LAST_MSG" == *"[skip version]"* ]] || [[ "$LAST_MSG" == *"[no bump]"* ]]; then
    info "Skipping version bump ([skip version] or [no bump] in commit message)"
    exit 0
fi

# --- python3 JSON read/write helpers (argv-only, format-stable) --------------
#
# json.load is used to READ the current version robustly (kills the grep
# single-key assumption). The WRITE is a surgical, format-PRESERVING regex
# substitution of exactly that one version value — NOT a full json.dump, which
# would reformat hand-authored inline arrays (crew/plugin.json's `keywords`) and
# cannot even locate marketplace.json's version (it lives at metadata.version,
# not top-level). Only the version line changes; every other byte is preserved.

# Read the version from a json file (top-level "version", else metadata.version).
# Prints the version, or exits 1 if neither is present.
read_version() {
    local file="$1"
    python3 -c 'import json,sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
v = d.get("version")
if v is None:
    v = d.get("metadata", {}).get("version") if isinstance(d.get("metadata"), dict) else None
if v is None:
    sys.exit(1)
print(v)' "$file"
}

# Rewrite ONLY the single version value in place, preserving all other bytes.
# argv[1]=path, argv[2]=new version. Exits 1 if the version key can't be located
# or the exact-value substitution doesn't match exactly once.
write_version() {
    local file="$1"
    local newver="$2"
    python3 -c 'import json,re,sys
path, newver = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    text = f.read()
d = json.loads(text)
cur = d.get("version")
if cur is None:
    cur = d.get("metadata", {}).get("version") if isinstance(d.get("metadata"), dict) else None
if cur is None:
    sys.exit(1)
pat = re.compile(r"(\"version\"\s*:\s*\")" + re.escape(cur) + r"(\")")
new_text, n = pat.subn(r"\g<1>" + newver + r"\g<2>", text, count=1)
if n != 1:
    sys.exit(1)
with open(path, "w", encoding="utf-8") as f:
    f.write(new_text)' "$file" "$newver"
}

# --- Baseline (subject-anchored, not body-matched) ---------------------------

# Get the last version bump commit. Anchors on the SUBJECT line so a commit whose
# BODY merely mentions "chore: bump version" can't poison the baseline.
get_last_bump_commit() {
    local commit
    commit=$(git log --format="%H%x09%s" 2>/dev/null \
        | awk -F'\t' '$2 ~ /^chore: bump version/ {print $1; exit}')
    if [ -z "$commit" ]; then
        # No previous bump, use first commit or HEAD~50
        commit=$(git rev-list --max-parents=0 HEAD 2>/dev/null || git rev-parse HEAD~50 2>/dev/null || echo "HEAD~20")
    fi
    echo "$commit"
}

# Determine bump type from commits.
# The two major triggers live in DIFFERENT parts of a conventional commit, so
# they are matched against different scopes:
#   - `type!:` is a SUBJECT-line form → matched against %s (the subject only).
#     Matching it over the full body would let a pasted diff/changelog line like
#     `foo!: bar` false-trigger a major bump.
#   - `BREAKING CHANGE:` is a FOOTER form → matched against %B (the full body),
#     where the footer legitimately lives.
# feat (minor) is likewise a subject-line marker → matched against %s.
get_bump_type() {
    local last_bump="$1"
    local subjects bodies
    subjects=$(git log "$last_bump"..HEAD --format="%s" 2>/dev/null || git log -20 --format="%s")
    bodies=$(git log "$last_bump"..HEAD --format="%B" 2>/dev/null || git log -20 --format="%B")

    # Major: a `type!:` marker on a SUBJECT line, OR a `BREAKING CHANGE:` footer
    # anywhere in the body. The BREAKING CHANGE arm is ANCHORED to the
    # conventional-commits footer shape (`^BREAKING[ -]CHANGE:` — line start,
    # both the space and hyphen spellings, colon REQUIRED); grep scans the
    # multi-line %B per-line, so `^` matches a footer line. This stops a body
    # that merely mentions or negates the phrase ("this is NOT a BREAKING
    # CHANGE", a pasted changelog line), or a body line shaped like `foo!: bar`,
    # from forcing a false MAJOR bump.
    if echo "$subjects" | grep -qE '^[a-z]+(\([^)]+\))?!:' \
       || echo "$bodies" | grep -qE '^BREAKING[ -]CHANGE:'; then
        echo "major"
    # Check for features (minor) — subject-line marker only.
    elif echo "$subjects" | grep -qE '^feat(\([^)]+\))?:'; then
        echo "minor"
    else
        echo "patch"
    fi
}

# Bump a semver version
bump_version() {
    local current="$1"
    local bump_type="$2"

    local major minor patch
    major=$(echo "$current" | cut -d. -f1)
    minor=$(echo "$current" | cut -d. -f2)
    patch=$(echo "$current" | cut -d. -f3)

    case "$bump_type" in
        major)
            major=$((major + 1))
            minor=0
            patch=0
            ;;
        minor)
            minor=$((minor + 1))
            patch=0
            ;;
        patch)
            patch=$((patch + 1))
            ;;
    esac

    echo "$major.$minor.$patch"
}

# Check if file has substantive changes (not just version bump)
has_substantive_changes() {
    local file="$1"
    local last_bump="$2"

    # Get diff excluding version lines
    local diff
    diff=$(git diff "$last_bump"..HEAD -- "$file" 2>/dev/null | grep '^[+-]' | grep -v '^[+-]\{3\}' | grep -v '"version"' || true)

    [ -n "$diff" ]
}

# Check if directory has changes
has_directory_changes() {
    local dir="$1"
    local last_bump="$2"

    local changes
    changes=$(git diff --name-only "$last_bump"..HEAD -- "$dir" 2>/dev/null || echo "")

    # Filter out version-only changes to plugin.json
    if [ -n "$changes" ]; then
        if echo "$changes" | grep -q 'plugin.json'; then
            local plugin_json="$dir/.claude-plugin/plugin.json"
            if [ -f "$plugin_json" ] && ! has_substantive_changes "$plugin_json" "$last_bump"; then
                changes=$(echo "$changes" | grep -v 'plugin.json' || true)
            fi
        fi
    fi

    [ -n "$changes" ]
}

# --- Snapshot-restore rollback infrastructure --------------------------------
#
# SNAP_PATHS / SNAP_FILES are parallel arrays: SNAP_FILES[i] is the json path
# that was snapshotted to the temp file SNAP_PATHS[i] (its exact pre-rewrite
# bytes). A failure after ANY rewrite restores every snapshot AND un-stages the
# tracked paths, then aborts — leaving the working tree AND index untouched.
SNAP_PATHS=()
SNAP_FILES=()

cleanup_snapshots() {
    local snap
    for snap in "${SNAP_PATHS[@]}"; do
        [ -n "$snap" ] && rm -f "$snap"
    done
}
trap cleanup_snapshots EXIT

snapshot_file() {
    local file="$1"
    local snap
    snap=$(mktemp) || return 1
    cat "$file" > "$snap" || { rm -f "$snap"; return 1; }
    SNAP_PATHS+=("$snap")
    SNAP_FILES+=("$file")
}

# Restore every snapshotted file from its byte-snapshot (NOT git checkout, so an
# unrelated pre-existing unstaged edit in these files is preserved) AND un-stage
# the three json paths so a failed bump leaves nothing staged.
rollback() {
    local i
    for ((i = 0; i < ${#SNAP_FILES[@]}; i++)); do
        cat "${SNAP_PATHS[$i]}" > "${SNAP_FILES[$i]}"
    done
    git reset -q HEAD -- \
        .claude-plugin/marketplace.json \
        plugins/crew/.claude-plugin/plugin.json \
        plugins/sk/.claude-plugin/plugin.json 2>/dev/null || true
}

fail() {
    error "$1"
    rollback
    cleanup_snapshots
    exit 1
}

# --- Main logic --------------------------------------------------------------
main() {
    info "Checking for version bumps..."

    local last_bump
    last_bump=$(get_last_bump_commit)
    info "Last version bump: ${last_bump:0:7}"

    local bump_type
    bump_type=$(get_bump_type "$last_bump")
    info "Bump type from commits: $bump_type"

    # Decide WHICH files bump, and READ all current versions FIRST — any read
    # failure aborts before a single byte is written (byte-identical tree).
    local do_marketplace=false do_crew=false do_sk=false
    local mp_current="" mp_new="" crew_current="" crew_new="" sk_current="" sk_new=""
    local mp_json=".claude-plugin/marketplace.json"
    local crew_json="plugins/crew/.claude-plugin/plugin.json"
    local sk_json="plugins/sk/.claude-plugin/plugin.json"

    # marketplace: substantive marketplace.json change OR README/docs change
    if has_substantive_changes "$mp_json" "$last_bump" \
       || git diff --name-only "$last_bump"..HEAD -- 'README.md' 'docs/' 2>/dev/null | grep -q .; then
        do_marketplace=true
        mp_current=$(read_version "$mp_json") || fail "cannot read version from $mp_json"
        mp_new=$(bump_version "$mp_current" "$bump_type")
    else
        info "No marketplace-level changes"
    fi

    if has_directory_changes "plugins/crew" "$last_bump"; then
        do_crew=true
        crew_current=$(read_version "$crew_json") || fail "cannot read version from $crew_json"
        crew_new=$(bump_version "$crew_current" "$bump_type")
    else
        info "No crew plugin changes"
    fi

    if has_directory_changes "plugins/sk" "$last_bump"; then
        do_sk=true
        sk_current=$(read_version "$sk_json") || fail "cannot read version from $sk_json"
        sk_new=$(bump_version "$sk_current" "$bump_type")
    else
        info "No sk plugin changes"
    fi

    if [ "$do_marketplace" != true ] && [ "$do_crew" != true ] && [ "$do_sk" != true ]; then
        info "No version changes needed"
        return 0
    fi

    # Snapshot every file we are about to rewrite BEFORE touching any of them.
    local commit_parts=""
    if [ "$do_marketplace" = true ] && [ "$mp_current" != "$mp_new" ]; then
        snapshot_file "$mp_json" || fail "cannot snapshot $mp_json"
    fi
    if [ "$do_crew" = true ] && [ "$crew_current" != "$crew_new" ]; then
        snapshot_file "$crew_json" || fail "cannot snapshot $crew_json"
    fi
    if [ "$do_sk" = true ] && [ "$sk_current" != "$sk_new" ]; then
        snapshot_file "$sk_json" || fail "cannot snapshot $sk_json"
    fi

    # Apply the rewrites; ANY failure rolls every snapshot back + un-stages.
    # bumped_paths collects ONLY the files actually rewritten, so `git add` can't
    # accidentally stage an unrelated unstaged edit in a NON-bumped json file.
    local bumped_paths=()
    if [ "$do_marketplace" = true ] && [ "$mp_current" != "$mp_new" ]; then
        write_version "$mp_json" "$mp_new" || fail "cannot write version to $mp_json"
        success "Marketplace: $mp_current -> $mp_new"
        commit_parts="marketplace $mp_current -> $mp_new"
        bumped_paths+=("$mp_json")
    fi
    if [ "$do_crew" = true ] && [ "$crew_current" != "$crew_new" ]; then
        write_version "$crew_json" "$crew_new" || fail "cannot write version to $crew_json"
        success "crew: $crew_current -> $crew_new"
        if [ -n "$commit_parts" ]; then
            commit_parts="$commit_parts, crew $crew_current -> $crew_new"
        else
            commit_parts="crew $crew_current -> $crew_new"
        fi
        bumped_paths+=("$crew_json")
    fi
    if [ "$do_sk" = true ] && [ "$sk_current" != "$sk_new" ]; then
        write_version "$sk_json" "$sk_new" || fail "cannot write version to $sk_json"
        success "sk: $sk_current -> $sk_new"
        if [ -n "$commit_parts" ]; then
            commit_parts="$commit_parts, sk $sk_current -> $sk_new"
        else
            commit_parts="sk $sk_current -> $sk_new"
        fi
        bumped_paths+=("$sk_json")
    fi

    if [ -z "$commit_parts" ] || [ ${#bumped_paths[@]} -eq 0 ]; then
        info "No version changes needed"
        return 0
    fi

    # Stage + commit. A failure at either step rolls back (restores bytes AND
    # un-stages) so nothing is left half-applied.
    git add "${bumped_paths[@]}" || fail "git add failed"
    if git diff --cached --quiet; then
        info "No staged version changes to commit"
        return 0
    fi
    git commit --no-verify -m "chore: bump version ($commit_parts)" \
        || fail "git commit failed"
    success "Version bump committed! Ready to push."
}

main "$@"
