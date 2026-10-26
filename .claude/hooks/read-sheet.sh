#!/bin/sh
# Decode the sheet once, before any consumer interprets its lines. PowerShell
# 5.1 writes UTF-16LE; other writers may add a UTF-8 BOM or CRLF line endings.
set -eu
sheet=$1
[ -f "$sheet" ] && [ -r "$sheet" ] || exit 1
lead=$(od -An -tx1 -N2 "$sheet" | tr -d ' ')
if [ "$lead" = fffe ] || [ "${lead#??}" = 00 ]; then
  text=$(iconv -f UTF-16LE -t UTF-8 "$sheet")
elif iconv -f UTF-8 -t UTF-8 "$sheet" >/dev/null 2>&1; then
  text=$(cat "$sheet")
else
  text=$(iconv -f ISO-8859-1 -t UTF-8 "$sheet")
fi
bom=$(printf '\357\273\277')
cr=$(printf '\r')
printf '%s\n' "$text" | sed -e "1s/^$bom//" -e "s/$cr\$//"
