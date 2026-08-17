#!/usr/bin/env bash
#
# Installs the pre-commit hook that blocks committing health data (plan §5).
# Symlinks rather than copies, so the hook tracks the repo.

set -euo pipefail

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "Not inside a git repository." >&2
    exit 1
}

hooks_dir=$(git rev-parse --git-path hooks)
mkdir -p "$hooks_dir"

target="$root/scripts/pre-commit"
link="$hooks_dir/pre-commit"

if [ -e "$link" ] && [ ! -L "$link" ]; then
    echo "A pre-commit hook already exists at $link and is not a symlink." >&2
    echo "Move it aside, or add: bash \"$target\"" >&2
    exit 1
fi

chmod +x "$target"
ln -sf "$target" "$link"

echo "Installed pre-commit hook -> $link"
echo
echo "It blocks staging Apple Health exports, generated indexes, records and"
echo "notes folders, consent records, and stray PDFs. Synthetic fixtures under"
echo "tests/fixtures/ are allowed."
echo
echo "Bypass a single commit with --no-verify."
