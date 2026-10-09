#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
python="${PYTHON:-python3}"
status=0
found=0

while IFS= read -r dir; do
  found=1
  component="$(dirname "$dir")"
  echo "== ${component#"$root"/}"
  "$python" -m unittest discover -s "$dir" -t "$component" || status=1
done < <(find "$root/plugins" "$root/profiles" "$root/templates" -type d -name tests -not -path '*/node_modules/*' 2>/dev/null | sort)

if [ "$found" -eq 0 ]; then
  echo "No component tests found." >&2
  exit 1
fi
exit "$status"
