# Worktree Protection Hook (Optional)

This optional hook prevents AI agents from editing files directly in the base repository, enforcing the worktree workflow.

## Quick Setup (AI-Assisted)

Ask an AI agent: "Set up the worktree protection hook for me" and provide these commands to run:

```bash
# 1. Create hook script
mkdir -p ~/.claude/hooks
cat > ~/.claude/hooks/require-worktree.sh << 'HOOKEOF'
#!/bin/bash
BASE_REPO="$HOME/dev/issue-orchestrator"
FLAG_FILE="$BASE_REPO/.claude-allow-direct-edit-$PPID"
[[ -f "$FLAG_FILE" ]] && exit 0
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
if [[ -n "$FILE_PATH" ]]; then
    FILE_DIR=$(dirname "$FILE_PATH")
    REPO_ROOT=$(cd "$FILE_DIR" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)
else
    REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
fi
if [[ "$REPO_ROOT" == "$BASE_REPO" ]]; then
    echo "STOP: Base repo. Create worktree or run: claude-direct-on" >&2
    exit 2
fi
exit 0
HOOKEOF
chmod +x ~/.claude/hooks/require-worktree.sh

# 2. Create toggle scripts
mkdir -p ~/bin
cat > ~/bin/claude-direct-on << 'EOF'
#!/bin/bash
touch "$HOME/dev/issue-orchestrator/.claude-allow-direct-edit-$PPID"
echo "Direct edit enabled for PID $PPID"
EOF
cat > ~/bin/claude-direct-off << 'EOF'
#!/bin/bash
rm -f "$HOME/dev/issue-orchestrator/.claude-allow-direct-edit-$PPID"
echo "Direct edit disabled"
EOF
chmod +x ~/bin/claude-direct-on ~/bin/claude-direct-off

# 3. Add ~/bin to PATH (if not already)
grep -q 'HOME/bin' ~/.zshrc || echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc

# 4. Add to local gitignore
cat >> .git/info/exclude << 'EOF'
.claude/settings.local.json
.claude-allow-direct-edit
.claude-allow-direct-edit-*
EOF

# 5. Create settings.local.json (merges with project hooks)
cat > .claude/settings.local.json << EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/require-worktree.sh"}]
      },
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": ".claude/hooks/block-no-verify.sh"}]
      }
    ],
    "Stop": [
      {
        "hooks": [{"type": "command", "command": "test -f .agent-done-marker || echo 'WARNING: Session ending without coding-done/reviewer-done!'", "timeout": 5}]
      }
    ]
  }
}
EOF

echo "Done! Restart Claude for hooks to take effect."
```

After setup, use `!claude-direct-on` to temporarily allow direct edits when needed.

## Why Use This

The project requires work to be done in git worktrees, not the base repo. Without enforcement, agents may forget and edit the base repo directly. This hook:

- Blocks Edit/Write operations in the base repo
- Provides clear instructions to create a worktree
- Allows per-session bypass when explicitly needed
- Supports multiple concurrent Claude sessions

## Setup

### 1. Create the Hook Script

Create `~/.claude/hooks/require-worktree.sh`:

```bash
#!/bin/bash
# Blocks edits in issue-orchestrator base repo unless enabled for this session
# Enable:  claude-direct-on
# Disable: claude-direct-off

BASE_REPO="$HOME/dev/issue-orchestrator"  # Adjust to your path
FLAG_FILE="$BASE_REPO/.claude-allow-direct-edit-$PPID"

# Check if flag file exists for this session's PID
[[ -f "$FLAG_FILE" ]] && exit 0

# Read JSON input to get the file path being edited
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')

# If we got a file path, check if it's in the base repo (not a worktree)
if [[ -n "$FILE_PATH" ]]; then
    FILE_DIR=$(dirname "$FILE_PATH")
    REPO_ROOT=$(cd "$FILE_DIR" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)
else
    # Fallback to cwd if no file path
    REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
fi

if [[ "$REPO_ROOT" == "$BASE_REPO" ]]; then
    echo "STOP: You are in the base issue-orchestrator repository." >&2
    echo "" >&2
    echo "Create a worktree:" >&2
    echo "  make -C \"$BASE_REPO\" worktree-create BRANCH=BRANCH" >&2
    echo "" >&2
    echo "Or enable direct edit for this session:" >&2
    echo "  claude-direct-on" >&2
    exit 2  # Exit code 2 blocks the tool call
fi

exit 0
```

Make it executable:

```bash
chmod +x ~/.claude/hooks/require-worktree.sh
```

### 2. Create Toggle Scripts

Create `~/bin/claude-direct-on`:

```bash
#!/bin/bash
# Enable direct edit in issue-orchestrator for current Claude session
FLAG_FILE="$HOME/dev/issue-orchestrator/.claude-allow-direct-edit-$PPID"
touch "$FLAG_FILE"
echo "Direct edit enabled for Claude PID $PPID"
```

Create `~/bin/claude-direct-off`:

```bash
#!/bin/bash
# Disable direct edit in issue-orchestrator for current Claude session
FLAG_FILE="$HOME/dev/issue-orchestrator/.claude-allow-direct-edit-$PPID"
rm -f "$FLAG_FILE"
echo "Direct edit disabled for Claude PID $PPID"
```

Make them executable and ensure `~/bin` is in your PATH:

```bash
chmod +x ~/bin/claude-direct-on ~/bin/claude-direct-off
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc  # or ~/.bashrc
```

### 3. Configure Claude Code

Create `.claude/settings.local.json` in the issue-orchestrator repo (this file is gitignored):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/YOURNAME/.claude/hooks/require-worktree.sh"
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/block-no-verify.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "test -f .agent-done-marker || echo '⚠️  WARNING: Session ending without coding-done/reviewer-done! Run: coding-done completed/blocked/needs_human'",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Replace `/Users/YOURNAME/` with your actual home directory path.

### 4. Add Flag Files to Local Gitignore

Add to `.git/info/exclude` (local, not committed):

```
.claude/settings.local.json
.claude-allow-direct-edit
.claude-allow-direct-edit-*
```

## Usage

### Quick Reference

| Command | Effect |
|---------|--------|
| `!claude-direct-on` | Enable direct edit for this session |
| `!claude-direct-off` | Disable direct edit for this session |

### Normal Workflow

1. Start Claude in issue-orchestrator
2. Agent tries to edit → Hook blocks with instructions
3. Create a worktree and work there

### When Direct Edit is Needed

1. Run `!claude-direct-on` in Claude
2. Agent can now edit (this session only)
3. Run `!claude-direct-off` when done, or just end the session

> **Reminder:** The `!` prefix runs shell commands from within Claude. So `!claude-direct-on` executes the toggle script.

### Multiple Sessions

Each session gets its own flag file based on Claude's PID. Multiple sessions can have direct edit enabled independently without collision.

## How It Works

- **Exit code 2** blocks the tool call (exit code 1 does not)
- **stderr** is shown to the agent as the error message
- **PPID** is Claude Code's process ID, unique per session
- Hook parses **stdin JSON** to get the file path being edited
- Worktrees have different git roots, so edits there are allowed

## Troubleshooting

**Hook not blocking:**
- Verify `settings.local.json` exists and has correct path
- Check hook script is executable
- Restart Claude (hooks load at session start)

**Can't edit in worktree:**
- The hook checks the file's git root, not cwd
- Ensure worktree was created properly (`git worktree list`)

**Toggle scripts not working:**
- Ensure `~/bin` is in your PATH
- Run with full path: `!~/bin/claude-direct-on`
