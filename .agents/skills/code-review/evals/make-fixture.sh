#!/usr/bin/env bash
set -euo pipefail

dir=${1:?usage: make-fixture.sh <new-dir>}
mkdir "$dir"
findings="$(cd "$dir/.." && pwd)/$(basename "$dir")-earlier-findings.json"
cd "$dir"
git init -q -b main
git config user.name fixture
git config user.email fixture@example.com

mkdir src
cat > src/list.js <<'JS'
export function last(items) {
  return items[items.length];
}
JS
git add . && git commit -qm "chore: list helpers"
git tag base

git switch -qc stale
echo "export const unused = 1;" > src/stale.js
git add . && git commit -qm "chore: stale side commit"
git switch -q main

git switch -qc feature
cat >> src/list.js <<'JS'

export function first(items) {
  return items[0];
}
JS
for name in orders invoices refunds; do
  cat > "src/$name.js" <<JS
export function sum_$name(rows) {
  let total = 0;
  for (let i = 0; i <= rows.length; i++) total += rows[i].amount;
  return total;
}
JS
done
cat > src/discount.js <<'JS'
export function applyDiscount(price, code) {
  let pct = 0;
  if (code = "HALF") pct = 50;
  return price - (price * pct) / 100;
}
JS
cat > src/greet.js <<'JS'
export function greet(user) {
  return "Hello, " + user.name.toUpperCase();
}
JS
cat > spec.md <<'MD'
# Totals

- Add `first(items)` to `src/list.js`.
- Add sum helpers for orders, invoices, and refunds.
- `applyDiscount(price, code)` takes 50% off for code `HALF` only.
- `greet(user)` returns `Hello, guest` when `user` is missing.
- Add `formatCents(n)` that formats cents as a dollar string.
MD
git add . && git commit -qm "feat: totals, discount, and greeting"
git tag r1

cat > src/greet.js <<'JS'
export function greet(user) {
  if (!user) return "Hello, guest";
  return "Hello, " + user.name.toUpperCase();
}
JS
cat > src/format.js <<'JS'
export function formatCents(n) {
  return (n / 100).toFixed(2);
}
JS
git add . && git commit -qm "fix: greet a missing user and add formatCents"

cat > "$findings" <<'JSON'
[
  {"id": "E1", "axis": "Bugs", "severity": "Important", "location": "src/discount.js:3", "text": "`if (code = \"HALF\")` assigns instead of compares, so every code gets 50% off.", "state": "open"},
  {"id": "E2", "axis": "Bugs", "severity": "Important", "location": "src/greet.js:2", "text": "`greet()` throws when `user` is undefined; the spec asks for `Hello, guest`.", "state": "open"},
  {"id": "E3", "axis": "Bugs", "severity": "Important", "location": "src/orders.js:3, src/invoices.js:3, src/refunds.js:3", "text": "Loop bound `i <= rows.length` reads one past the end.", "state": "answered"}
]
JSON
