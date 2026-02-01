#!/usr/bin/env bash
#
# Post-commit hook: Auto-bump versions based on conventional commits
#
# Install: ln -sf ../../scripts/post-commit-version-bump.sh .git/hooks/post-commit
#
# This script runs after each commit and:
# - Skips if the commit is already a version bump
# - Analyzes commits since last bump to determine bump type
# - Bumps marketplace version if marketplace-level files changed
# - Bumps plugin versions only for plugins with changes
#

set -e

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

# Get the last version bump commit
get_last_bump_commit() {
    local commit
    commit=$(git log --oneline --grep="chore: bump version" -1 --format="%H" 2>/dev/null || echo "")
    if [ -z "$commit" ]; then
        # No previous bump, use first commit or HEAD~50
        commit=$(git rev-list --max-parents=0 HEAD 2>/dev/null || git rev-parse HEAD~50 2>/dev/null || echo "HEAD~20")
    fi
    echo "$commit"
}

# Determine bump type from commits
get_bump_type() {
    local last_bump="$1"
    local commits
    commits=$(git log --oneline "$last_bump"..HEAD --format="%s" 2>/dev/null || git log --oneline -20 --format="%s")

    # Check for breaking changes (major)
    if echo "$commits" | grep -qE '^[a-z]+(\([^)]+\))?!:|BREAKING CHANGE'; then
        echo "major"
    # Check for features (minor)
    elif echo "$commits" | grep -qE '^feat(\([^)]+\))?:'; then
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

# Main logic
main() {
    info "Checking for version bumps..."

    local last_bump
    last_bump=$(get_last_bump_commit)
    info "Last version bump: ${last_bump:0:7}"

    local bump_type
    bump_type=$(get_bump_type "$last_bump")
    info "Bump type from commits: $bump_type"

    local bumped_marketplace=false
    local bumped_crew=false
    local bumped_sk=false
    local commit_parts=""

    # Check marketplace-level changes
    local marketplace_changed=false
    if has_substantive_changes ".claude-plugin/marketplace.json" "$last_bump"; then
        marketplace_changed=true
    elif git diff --name-only "$last_bump"..HEAD -- 'README.md' 'docs/' 2>/dev/null | grep -q .; then
        marketplace_changed=true
    fi

    if [ "$marketplace_changed" = true ]; then
        local current new
        current=$(grep -o '"version": *"[^"]*"' .claude-plugin/marketplace.json | grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+')
        new=$(bump_version "$current" "$bump_type")

        if [ "$current" != "$new" ]; then
            sed -i.bak "s/\"version\": *\"$current\"/\"version\": \"$new\"/" .claude-plugin/marketplace.json
            rm -f .claude-plugin/marketplace.json.bak
            success "Marketplace: $current -> $new"
            bumped_marketplace=true
            commit_parts="marketplace $current -> $new"
        fi
    else
        info "No marketplace-level changes"
    fi

    # Check crew plugin
    if has_directory_changes "plugins/crew" "$last_bump"; then
        local plugin_json="plugins/crew/.claude-plugin/plugin.json"
        local current new
        current=$(grep -o '"version": *"[^"]*"' "$plugin_json" | grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+')
        new=$(bump_version "$current" "$bump_type")

        if [ "$current" != "$new" ]; then
            sed -i.bak "s/\"version\": *\"$current\"/\"version\": \"$new\"/" "$plugin_json"
            rm -f "$plugin_json.bak"
            success "crew: $current -> $new"
            bumped_crew=true
            if [ -n "$commit_parts" ]; then
                commit_parts="$commit_parts, crew $current -> $new"
            else
                commit_parts="crew $current -> $new"
            fi
        fi
    else
        info "No crew plugin changes"
    fi

    # Check sk plugin
    if has_directory_changes "plugins/sk" "$last_bump"; then
        local plugin_json="plugins/sk/.claude-plugin/plugin.json"
        local current new
        current=$(grep -o '"version": *"[^"]*"' "$plugin_json" | grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+')
        new=$(bump_version "$current" "$bump_type")

        if [ "$current" != "$new" ]; then
            sed -i.bak "s/\"version\": *\"$current\"/\"version\": \"$new\"/" "$plugin_json"
            rm -f "$plugin_json.bak"
            success "sk: $current -> $new"
            bumped_sk=true
            if [ -n "$commit_parts" ]; then
                commit_parts="$commit_parts, sk $current -> $new"
            else
                commit_parts="sk $current -> $new"
            fi
        fi
    else
        info "No sk plugin changes"
    fi

    # Commit if anything was bumped
    if [ "$bumped_marketplace" = true ] || [ "$bumped_crew" = true ] || [ "$bumped_sk" = true ]; then
        git add .claude-plugin/marketplace.json
        git add plugins/crew/.claude-plugin/plugin.json
        git add plugins/sk/.claude-plugin/plugin.json

        # Only commit if there are staged changes
        if ! git diff --cached --quiet; then
            git commit --no-verify -m "chore: bump version ($commit_parts)"
            success "Version bump committed! Ready to push."
        fi
    else
        info "No version changes needed"
    fi
}

main "$@"
