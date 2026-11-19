#!/usr/bin/env bash
# Install the odoo-impact agent skills.
#   scripts/install-skills.sh                       -> ~/.agents/skills (or ~/.claude/skills if that is where skills live)
#   scripts/install-skills.sh --target DIR          -> DIR (e.g. <project>/.agents/skills or <project>/.claude/skills)
#   scripts/install-skills.sh --link                -> symlink instead of copy (follows `git pull` of this clone)
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/.agents/skills"
TARGET=""
MODE=copy
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --link) MODE=link; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
if [ -z "$TARGET" ]; then
  if [ -d "$HOME/.agents/skills" ]; then TARGET="$HOME/.agents/skills"
  elif [ -d "$HOME/.claude/skills" ]; then TARGET="$HOME/.claude/skills"
  else TARGET="$HOME/.agents/skills"; fi
fi
mkdir -p "$TARGET"
for skill in "$SRC"/*/; do
  name="$(basename "$skill")"
  dest="$TARGET/$name"
  if [ -e "$dest" ] || [ -L "$dest" ]; then rm -rf "$dest"; fi
  if [ "$MODE" = link ]; then ln -s "${skill%/}" "$dest"; else cp -R "${skill%/}" "$dest"; fi
  echo "installed $name -> $dest"
done
# Claude Code reads ~/.claude/skills: make the skills visible there too when it is a separate folder
if [ "$TARGET" = "$HOME/.agents/skills" ] && [ -d "$HOME/.claude/skills" ] && [ ! -L "$HOME/.claude/skills" ]; then
  for skill in "$SRC"/*/; do
    name="$(basename "$skill")"
    [ -e "$HOME/.claude/skills/$name" ] || ln -s "$TARGET/$name" "$HOME/.claude/skills/$name"
  done
  echo "linked into ~/.claude/skills"
fi
echo "Restart the coding agent so it loads the skills."
