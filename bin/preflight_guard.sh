#!/usr/bin/env bash
set -o pipefail

EXPECTED_LAUNCHER="bin/media_watch_launch_detached.sh"

fail() {
  echo "PREFLIGHT_FAIL: $1" >&2
  exit 2
}

warn() {
  echo "PREFLIGHT_WARN: $1" >&2
}

info() {
  echo "PREFLIGHT_INFO: $1"
}

pass() {
  echo "PREFLIGHT_OK: $1"
}

echo "=== PREFLIGHT: context ==="
pwd

# Resolve repo root dynamically (contract requirement)
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "${REPO_ROOT}" ] || fail "Not inside a git repo (cannot resolve repo root)"
cd "${REPO_ROOT}" || fail "Cannot cd to repo root: ${REPO_ROOT}"
pass "repo_root=${REPO_ROOT}"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "Not inside a git work tree"

BRANCH="$(git branch --show-current 2>/dev/null || true)"
[ -n "${BRANCH}" ] || fail "No current branch detected (detached HEAD?)"
pass "branch=${BRANCH}"

if git remote get-url origin >/dev/null 2>&1; then
  ORIGIN_URL="$(git remote get-url origin)"
  pass "origin=${ORIGIN_URL}"
else
  warn "origin remote missing"
fi

[ -f "${EXPECTED_LAUNCHER}" ] || fail "Missing ${EXPECTED_LAUNCHER}"

bash -n "${EXPECTED_LAUNCHER}" || fail "Launcher syntax invalid"

# Forbidden launcher patterns
if grep -nE '(^|[[:space:]])set -e([[:space:]]|$)|(^|[[:space:]])set -u([[:space:]]|$)|NOHUP_OUT|nohup_out|media_watch\.nohup' "${EXPECTED_LAUNCHER}" >/dev/null; then
  fail "Forbidden patterns present in launcher"
fi

# Exactly one nohup launch line
NOHUP_COUNT="$(grep -cE '^[[:space:]]*nohup[[:space:]]' "${EXPECTED_LAUNCHER}")"
[ "${NOHUP_COUNT}" = "1" ] || fail "Expected exactly one nohup launch, got ${NOHUP_COUNT}"

# Required launcher control tokens
grep -n -- '--log "$LOGFILE"' "${EXPECTED_LAUNCHER}" >/dev/null || fail "Missing canonical --log \"\$LOGFILE\" usage"
grep -nE 'set -o pipefail|PIDFILE=|LOGFILE=' "${EXPECTED_LAUNCHER}" >/dev/null || fail "Missing required launcher control lines"

pass "launcher invariants OK"

if [ -n "$(git status --porcelain)" ]; then
  info "working tree has uncommitted changes"
else
  pass "working tree clean"
fi

echo "HEAD=$(git rev-parse HEAD)"
echo "LAUNCHER_SHA=$(git hash-object "${EXPECTED_LAUNCHER}")"

pass "preflight complete"
