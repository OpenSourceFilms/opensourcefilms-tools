#!/usr/bin/env bash
set -euo pipefail

EXPECTED_REPO_ROOT="/workspace/opensourcefilms-tools"
EXPECTED_LAUNCHER="bin/media_watch_launch_detached.sh"

fail() {
  echo "PREFLIGHT_FAIL: $1" >&2
  exit 2
}

pass() {
  echo "PREFLIGHT_OK: $1"
}

echo "=== PREFLIGHT: context ==="
pwd

[[ "$(pwd)" == "$EXPECTED_REPO_ROOT" ]] || fail "Wrong working directory. Expected ${EXPECTED_REPO_ROOT}"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "Not inside a git repo"

BRANCH="$(git branch --show-current || true)"
[[ -n "${BRANCH}" ]] || fail "No current branch detected (detached HEAD?)"
pass "branch=${BRANCH}"

if git remote get-url origin >/dev/null 2>&1; then
  ORIGIN_URL="$(git remote get-url origin)"
  pass "origin=${ORIGIN_URL}"
else
  echo "PREFLIGHT_WARN: origin remote missing"
fi

[[ -f "${EXPECTED_LAUNCHER}" ]] || fail "Missing ${EXPECTED_LAUNCHER}"

bash -n "${EXPECTED_LAUNCHER}" || fail "Launcher syntax invalid"

if grep -nE '(^|[[:space:]])set -e([[:space:]]|$)|(^|[[:space:]])set -u([[:space:]]|$)|NOHUP_OUT|nohup_out|media_watch\.nohup' "${EXPECTED_LAUNCHER}" >/dev/null; then
  fail "Forbidden patterns present in launcher"
fi

ESAC_COUNT="$(awk 'BEGIN{n=0} {if($0~/^esac$/) n++} END{print n}' "${EXPECTED_LAUNCHER}")"
[[ "${ESAC_COUNT}" == "1" ]] || fail "Expected exactly one esac, got ${ESAC_COUNT}"

NOHUP_COUNT="$(grep -cE '^[[:space:]]*nohup[[:space:]]' "${EXPECTED_LAUNCHER}")"
[[ "${NOHUP_COUNT}" == "1" ]] || fail "Expected exactly one nohup launch, got ${NOHUP_COUNT}"

grep -n -- '--log "$LOGFILE"' "${EXPECTED_LAUNCHER}" >/dev/null || fail "Missing canonical --log \"\$LOGFILE\" usage"
grep -nE 'set -o pipefail|PIDFILE=|LOGFILE=' "${EXPECTED_LAUNCHER}" >/dev/null || fail "Missing required launcher control lines"

pass "launcher invariants OK"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "PREFLIGHT_INFO: working tree has uncommitted changes"
else
  pass "working tree clean"
fi

echo "HEAD=$(git rev-parse HEAD)"
echo "LAUNCHER_SHA=$(git hash-object "${EXPECTED_LAUNCHER}")"

pass "preflight complete"
