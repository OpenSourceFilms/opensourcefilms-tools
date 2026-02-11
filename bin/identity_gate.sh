#!/usr/bin/env bash
set -o pipefail

fail() { echo "IDENTITY_FAIL: $1" >&2; exit 2; }
ok()   { echo "IDENTITY_OK: $1"; }

EXPECTED_ROOT="/workspace/pipeline"
EXPECTED_BRANCH="main"
EXPECTED_REMOTE="https://github.com/OpenSourceFilms/opensourcefilms-tools.git"
EXPECTED_MOUNT_SUBSTR="mfs#euro-2.runpod.net:9421[/networkvolumes/cmkgi43hcx]"

pwd_now="$(pwd)"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
remote="$(git remote get-url origin 2>/dev/null || true)"
mount_src="$(findmnt -T /workspace -no SOURCE 2>/dev/null || true)"

[ -d /workspace ] || fail "/workspace missing"
[ -n "$mount_src" ] || fail "cannot resolve /workspace mount source"
echo "$mount_src" | grep -F "$EXPECTED_MOUNT_SUBSTR" >/dev/null || fail "unexpected /workspace mount source: $mount_src"

[ "$repo_root" = "$EXPECTED_ROOT" ] || fail "wrong repo root: $repo_root (expected $EXPECTED_ROOT)"
[ "$branch" = "$EXPECTED_BRANCH" ] || fail "wrong branch: $branch (expected $EXPECTED_BRANCH)"
[ "$remote" = "$EXPECTED_REMOTE" ] || fail "wrong origin: $remote (expected $EXPECTED_REMOTE)"

ok "pwd=$pwd_now"
ok "repo_root=$repo_root"
ok "branch=$branch"
ok "origin=$remote"
ok "mount_source=$mount_src"
