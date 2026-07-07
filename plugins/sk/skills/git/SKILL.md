---
name: git
description: Use for ANY git or version-control task. Triggers: commit, stash, rebase, squash, cherry-pick, merge, push, pull, branch, diff, blame, log, reset, conflict resolution, or commit messages — including committing staged changes, writing/improving commit messages, squashing/reordering commits, rebasing onto main, managing git stashes (list/apply/pop/drop), cherry-picking specific commits, searching git history, resolving merge conflicts, or "commit this"/"stash that". Do NOT use for CI/CD pipeline configuration, Docker, code refactoring, or general development tasks unless they specifically involve a git operation.
---

# Git Skill

Expert guidance for git operations: commits, rebasing, history search, and conflict resolution.

## Core Principle: Logical Cohesion

**One commit = one logical change.**

A "logical change" is something that:
- Could be reverted independently without breaking things
- Has a single, clear purpose
- Makes sense as a unit in `git log`

### When to Split

| Signal | Action |
|--------|--------|
| Different concerns (feature vs refactor vs fix) | Split |
| Unrelated files that just happened to change together | Split |
| "This commit does X and also Y" | Split |
| Changes that could be reverted separately | Split |

### When NOT to Split

| Signal | Keep Together |
|--------|---------------|
| Component + its tests | One commit |
| Feature + its documentation | One commit |
| Rename + all references updated | One commit |
| Tightly coupled changes that would break if separated | One commit |

**File count doesn't matter.** A 10-file rename is one commit. A 2-file change touching unrelated concerns is two commits.

## Style Detection

Before committing, detect the repo's conventions:

```bash
git log -20 --pretty=format:"%s"
```

Look for:
- **Conventional commits**: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`
- **Capitalization**: "Add feature" vs "add feature"
- **Tense**: "Add" vs "Added" vs "Adds"

**Match the existing style.** Don't impose conventions on a repo that doesn't use them.

## Commit Message Guidelines

### Preferred Format (Conventional Commits)

```
type(scope): description
```

**Components:**
- **type**: `feat`, `fix`, `chore`, `refactor`, `docs`, `test`
- **scope**: optional, affected area (e.g., `auth`, `api`, `ui`)
- **description**: imperative tense, lowercase, concise

**Rules:**
- Imperative tense: "add", "fix", "remove" (not "added", "fixed", "removed")
- Lowercase after the colon
- 50 chars max for the subject line
- No period at the end

### Good Messages

```
feat(auth): add OAuth2 login flow
```

```
fix(api): improve query efficiency for group membership

* Update membership queries
* Add group_membership indexes
```

```
fix: prevent duplicate form submissions

The submit button wasn't disabled after click, allowing
users to accidentally submit multiple times.
```

```
chore(deps): update dependencies
```

### Bad Messages

```
# Too vague
fix: bug fix
update: updates
misc: various changes

# Wrong tense
fix: fixed the bug
feat: added new feature

# Just describing the diff (we can see that)
update: change variable name from x to count

# No context
fix: null check
```

### When to Include Body

- **Simple changes**: No body needed
- **Squashed PRs**: List intermediate commits as bullet points
- **Complex changes**: Explain the "why" not the "what"

## Rebase Workflow

```bash
# Rebase onto updated main
git fetch origin
git rebase origin/main
```

**Note:** Claude Code cannot run interactive rebase (`git rebase -i`) since it requires an interactive editor. For interactive rebasing, squashing, or reordering commits, suggest the user run it themselves:

```
# Tell the user:
# "You'll want to run this yourself since it needs an interactive editor:"
# git rebase -i HEAD~N
```

Common non-interactive alternatives:
```bash
# Squash last N commits into one (non-interactive)
git reset --soft HEAD~N
git commit -m "combined commit message"

# Fixup a specific commit (auto-squash)
git commit --fixup=<sha>
git rebase --autosquash origin/main
```

Operations in interactive rebase (for user reference):
- `pick` - keep commit as-is
- `reword` - change commit message
- `squash` - combine with previous commit
- `fixup` - combine with previous, discard message
- `drop` - remove commit entirely

## History Search

| Goal | Command |
|------|---------|
| When was "X" added? | `git log -S "X" --oneline` |
| Commits changing "X" | `git log -G "X" --oneline` |
| Who wrote line N? | `git blame -L N,N file` |
| Changes to a file | `git log --oneline -- path/to/file` |
| Find when bug started | `git bisect start && git bisect bad && git bisect good <known-good>` |
| Show commit details | `git show <sha>` |

## Safety Rules

### Never Do

- `git push --force` to shared branches (main, master, develop)
- Rebase commits that others have based work on
- Force push without `--force-with-lease`

### Always Do

```bash
# Use force-with-lease instead of force
git push --force-with-lease

# Stash before rebasing
git stash
git rebase origin/main
git stash pop

# Check status before committing
git status
git diff --staged
```

## Conflict Resolution

When conflicts occur:

1. **Understand both sides** - read the conflict markers carefully
2. **Check the intent** - `git log --merge` shows commits causing conflict
3. **Resolve thoughtfully** - don't just pick one side blindly
4. **Test after resolving** - make sure it still works
5. **Continue the operation** - `git rebase --continue` or `git merge --continue`

```bash
# See what's conflicting
git status

# After resolving
git add <resolved-files>
git rebase --continue
```

## Commit Workflow

```bash
# 1. See what changed
git status
git diff

# 2. Stage selectively (not just `git add .`)
git add -p  # interactive staging

# 3. Review staged changes
git diff --staged

# 4. Commit with good message
git commit

# 5. Verify
git log -1
git show
```
