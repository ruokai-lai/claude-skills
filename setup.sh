#!/usr/bin/env bash
# Set up my skills on this computer.
#   own skills      -> symlinked from ./skills, so `git pull` updates them and edits land in this repo
#   external skills -> fetched from the repos listed in external-skills.txt (copied, pinned to a ref)
#
# Usage: ./setup.sh [--target claude|copilot] [--dry-run]
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGET=claude
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$TARGET" in
  claude) DEST="$HOME/.claude/skills" ;;
  copilot) DEST="$HOME/.copilot/skills" ;;
  *) echo "--target must be claude or copilot" >&2; exit 2 ;;
esac
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/claude-skills"

run() { if [ "$DRY" = 1 ]; then echo "  would: $*"; else "$@"; fi; }

echo "Installing into $DEST"
run mkdir -p "$DEST"

echo "Own skills:"
for dir in "$ROOT"/skills/*/; do
  [ -f "$dir/SKILL.md" ] || continue
  name="$(basename "$dir")"
  link="$DEST/$name"
  if [ -L "$link" ]; then
    run ln -sfn "${dir%/}" "$link"; echo "  linked   $name"
  elif [ -e "$link" ]; then
    echo "  SKIPPED  $name: $link already exists and is not a symlink (move it into skills/ or delete it)"
  else
    run ln -s "${dir%/}" "$link"; echo "  linked   $name"
  fi
done

echo "External skills:"
# Format per line: name|owner/repo|path-in-repo|ref   (blank lines and # comments are ignored)
while IFS='|' read -r name repo path ref; do
  case "$name" in ''|\#*) continue ;; esac
  link="$DEST/$name"
  if [ -L "$link" ]; then
    echo "  SKIPPED  $name: $link is a symlink (an own skill with the same name?)"; continue
  fi
  if [ "$DRY" = 1 ]; then echo "  would fetch $name from $repo ($path @ $ref)"; continue; fi
  clone="$CACHE/${repo//\//__}"
  if [ ! -d "$clone/.git" ]; then
    mkdir -p "$CACHE"
    git clone --quiet "https://github.com/$repo.git" "$clone"
  fi
  git -C "$clone" fetch --quiet origin
  git -C "$clone" checkout --quiet --detach "$ref" 2>/dev/null || git -C "$clone" checkout --quiet --detach "origin/$ref"
  if [ ! -f "$clone/$path/SKILL.md" ]; then
    echo "  FAILED   $name: no SKILL.md at $repo/$path @ $ref"; continue
  fi
  rm -rf "$link"
  cp -R "$clone/$path" "$link"
  echo "  fetched  $name ($repo @ $ref)"
done < "$ROOT/external-skills.txt"

echo "Done. Restart Claude Code (or open a new session) to pick up new skills."
