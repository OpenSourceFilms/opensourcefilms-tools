#!/usr/bin/env bash

require_var() {
  local v="$1"
  if [ -z "${!v-}" ]; then
    echo "FATAL: required variable not set: $v" >&2
    return 2
  fi
}

run_step() {
  local name="$1"; shift
  echo "=== $name ==="

  local errexit_was_set=0
  case "$-" in *e*) errexit_was_set=1 ;; esac

  set +e
  "$@"
  local rc=$?
  if [ "$errexit_was_set" -eq 1 ]; then
    : # disabled: was "set -e"
  fi

  if [ "$rc" -ne 0 ]; then
    echo "WARN: step failed ($name) rc=$rc, continuing" >&2
  fi
  return "$rc"
}
