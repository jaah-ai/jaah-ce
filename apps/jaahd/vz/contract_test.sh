#!/bin/bash
# The jaah-vz contract a macOS CI runner can prove without booting a guest (GitHub's macOS runners cannot
# nest VMs): probe's pinned shape, the bad-spec exits, the unsupported exit, and the entitlement probe.
# Usage: contract_test.sh <path to the signed jaah-vz>
set -euo pipefail
vz="$1"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }
pass() { echo "ok    $*"; }

# One JSON object line on stdin matching the shape named by $1.
check_json() {
  python3 -I -c '
import json, sys
shape, text = sys.argv[1], sys.stdin.read()
assert text.endswith("\n") and text.count("\n") == 1, f"not exactly one line: {text!r}"
obj = json.loads(text)
if shape == "probe":
    assert set(obj) == {"usable", "error"} and isinstance(obj["usable"], bool), obj
    assert obj["error"] in (None, "unsupported", "entitlement"), obj
    assert obj["usable"] == (obj["error"] is None), obj
else:
    assert set(obj) == {"error", "detail"} and obj["error"] == shape, obj
print(json.dumps(obj))
' "$1"
}

# expect_run <name> <exit> <error code> <spec path or ->: run, then check the exit and the one stderr line.
expect_run() {
  local name="$1" want="$2" code="$3" spec="$4" got=0
  "$vz" run --spec "$spec" >"$work/out" 2>"$work/err" || got=$?
  if [ "$got" != "$want" ]; then fail "$name: exit $got, want $want ($(cat "$work/err"))"; return; fi
  if [ -s "$work/out" ]; then fail "$name: stdout not empty: $(cat "$work/out")"; return; fi
  if ! line="$(check_json "$code" <"$work/err")"; then fail "$name: stderr is not one {error: $code} line"; return; fi
  pass "$name: exit $got, $line"
}

got=0
"$vz" probe >"$work/probe" 2>"$work/probe.err" || got=$?
if [ "$got" != 0 ]; then fail "probe: exit $got"; fi
if probe="$(check_json probe <"$work/probe")"; then pass "probe: exit 0, $probe"; else fail "probe: bad output"; fi

vmdir="$work/vm1"
mkdir -p "$vmdir/share"
: >"$vmdir/root.raw"
: >"$vmdir/seed.raw"
write_spec() {   # write_spec <tap JSON value>
  cat >"$vmdir/spec.json" <<JSON
{"vm_id": "vm1", "vcpus": 2, "ram_mib": 2048, "disk_path": "$vmdir/root.raw", "seed_path": "$vmdir/seed.raw",
 "tap": $1, "mac": null, "egress": "vsock", "shares": [{"tag": "worktree", "host_path": "$vmdir/share", "ro": false}],
 "vsock_ports": [1024, 1025, 15001, 15053], "pane_log": "$vmdir/pane.log"}
JSON
}

echo '{"vm_id": ' >"$work/malformed.json"
expect_run "malformed spec" 64 spec "$work/malformed.json"
expect_run "missing spec" 64 spec "$work/absent/spec.json"
write_spec '"jvm0"'
expect_run "non-null tap" 64 spec "$vmdir/spec.json"
write_spec null
case "$probe" in
  *'"error": "unsupported"'*) expect_run "unsupported Mac" 69 unsupported "$vmdir/spec.json" ;;
  *) echo "skip  unsupported exit: this runner reports $probe" ;;
esac

got=0
"$vz" bogus >/dev/null 2>"$work/err" || got=$?
if [ "$got" = 64 ] && check_json usage <"$work/err" >/dev/null; then pass "usage: exit 64"; else fail "usage: exit $got"; fi

# The same binary re-signed WITHOUT its entitlement must say so, never claim usable.
cp "$vz" "$work/jaah-vz-bare"
codesign --force --sign - "$work/jaah-vz-bare" 2>/dev/null
bare="$("$work/jaah-vz-bare" probe)"
if [ "$bare" = '{"error":"entitlement","usable":false}' ]; then pass "unentitled probe: $bare"; else fail "unentitled probe: $bare"; fi

[ "$failures" = 0 ] || { echo "$failures contract failure(s)" >&2; exit 1; }
echo "jaah-vz contract: all checks passed"
