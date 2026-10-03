#!/usr/bin/env bash
# The BEAM gate, exactly as CI runs it, in one command a machine can branch on.
#
# CI splits this across two workflows: `.github/workflows/beam.yml` runs format +
# compile + test on the self-hosted runner, and the `BEAM umbrella` job in
# `ci.yml` runs the same three steps. This script is that triple, so a caller
# does not have to reconstruct it from three workflow files and drift from them.
#
# It runs in a container because the BEAM toolchain is not on this host and
# should not be: the pinned image is the same one both workflows use, so a
# green run here means the same thing a green CI run means. `:z` relabels for
# SELinux; without it the bind mount is unreadable inside the container.
#
# ## The image, and why it is not just the pinned one
#
# `beam/Dockerfile.gate` builds the pinned hexpm/elixir image PLUS python3 and
# pydantic. The differential harness under `test/support/` AST-lifts the Python
# reference out of `app/` and executes it; in the bare pinned image there is no
# python3, so the harness skips itself and `mix test` reports all-green with the
# oracle skipped. A gate that skips its own oracle cannot fail, which is the
# defect class this repository keeps closing. The image adds the ability to
# execute the oracle and relaxes nothing.
#
# Set BEAM_IMAGE to override. Set BEAM_BASE_IMAGE_ONLY=1 to force the bare pinned
# image — useful for proving the fallback path, and it will report the harness as
# SKIPPED rather than silently pretending the oracle ran.
#
# ## The mount
#
# The repo ROOT is mounted, not just `beam/`, because the oracle reads `app/`
# from outside the umbrella. Mounting only `beam/` makes `repo_root()` resolve to
# `/` and the harness skips for a reason that looks like a code problem.
#
# Exit codes are the three steps' own, first failure wins:
#   0  format clean, compiled with no warnings, all tests passed
#   1  `mix format --check-formatted` found unformatted sources
#   2  compilation failed, or emitted a warning under --warnings-as-errors
#   3  the test suite failed
#   4  the container could not run at all (no image, no mount)
#   5  dependencies could not be resolved (offline host, first dependency)
#
# `mix compile --warnings-as-errors` is the only thing in the estate that finds
# an unreachable clause inside a reachable module. The module-level orphan
# guard cannot see one, so this step is not redundant with it.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEAM="$REPO/beam"
GATE_IMAGE="server-api-beam-gate:local"
BASE_IMAGE="docker.io/hexpm/elixir:1.20.4-erlang-29.1.1-debian-bookworm-20260918-slim"

# BEAM_BASE_IMAGE_ONLY=1 forces the bare pinned image. It has to be applied HERE,
# to the value IMAGE is chosen from -- reading it only inside the build block
# (which is where it used to be read) leaves IMAGE already pointing at the gate
# image, so the variable silently did nothing on any host where the gate image
# existed. That is a documented knob that does not work, which is worse than no
# knob: it is how you convince yourself the fallback path is exercised.
if [ "${BEAM_BASE_IMAGE_ONLY:-0}" = "1" ]; then
  IMAGE="$BASE_IMAGE"
elif [ -n "${BEAM_IMAGE:-}" ]; then
  IMAGE="$BEAM_IMAGE"
else
  IMAGE="$GATE_IMAGE"
fi

if [ ! -d "$BEAM" ]; then
  echo "beam gate: $BEAM does not exist" >&2
  exit 4
fi

# Build the gate image on demand. This is a cached layer after the first build,
# so the common case is a no-op; the fallback exists because an offline host
# should still get format + compile + test rather than nothing.
if [ "$IMAGE" = "$GATE_IMAGE" ] && ! podman image exists "$GATE_IMAGE" 2>/dev/null; then
  echo "beam gate: building $GATE_IMAGE (first run only)" >&2
  if ! podman build -q -t "$GATE_IMAGE" -f "$BEAM/Dockerfile.gate" "$REPO" >/dev/null 2>&1; then
    echo "beam gate: could not build the gate image; falling back to $BASE_IMAGE." >&2
    echo "beam gate: the differential harness will report itself SKIPPED." >&2
    IMAGE="$BASE_IMAGE"
  fi
fi

if ! podman image exists "$IMAGE" 2>/dev/null; then
  echo "beam gate: image $IMAGE is not present locally; run: podman pull $IMAGE" >&2
  exit 4
fi

# -v "$REPO":/w — the repo root, so `app/` is reachable from inside.
# `mix deps.get` and `mix local.hex` are NOT optional decoration. The gate ran
# clean for the whole zero-dependency era and then broke the moment the first hex
# package was admitted:
#
#   Mix requires the Hex package manager to fetch dependencies
#   ** (Mix) Could not find an SCM for dependency :epgsql
#
# ci.yml already did these three lines (`mix local.hex`, `mix local.rebar`,
# `mix deps.get`); the local gate did not, so the two drifted exactly where drift
# costs the most — the gate that is supposed to BE the check CI runs.
#
# And they must all run in the SAME container as the compile. Each step used to be
# its own `podman run`, so `mix local.hex` installed Hex into a throwaway layer
# that the next container could not see: `deps.get` succeeded, then `compile` in a
# fresh container asked for Hex again and failed. One invocation, one container.
run_gate() {
  podman run --rm \
    -v "$REPO":/w:z \
    -w /w/beam \
    "$IMAGE" \
    bash -c "$1"
}

set +e
run_gate '
  set -euo pipefail
  mix local.hex --force --if-missing
  mix local.rebar --force --if-missing
  mix deps.get
  echo "--- beam gate: format ---"
  mix format --check-formatted
  echo "--- beam gate: compile --warnings-as-errors ---"
  mix compile --warnings-as-errors
  echo "--- beam gate: test ---"
  mix test
' 2>&1 | tee /tmp/beam-gate.log
GATE_EXIT=${PIPESTATUS[0]}
set -e

# Stage attribution from the echoed markers, so a failure names the step rather
# than just an exit code. The markers are the only thing identifying the stage.
if [ "$GATE_EXIT" -ne 0 ]; then
  if grep -q 'beam gate: test' /tmp/beam-gate.log; then
    echo "beam gate: FAILED at test (exit 3)" >&2
    exit 3
  elif grep -q 'beam gate: compile' /tmp/beam-gate.log; then
    echo "beam gate: FAILED at compile (exit 2)" >&2
    exit 2
  elif grep -q 'beam gate: format' /tmp/beam-gate.log; then
    echo "beam gate: FAILED at format (exit 1)" >&2
    exit 1
  fi
  echo "beam gate: FAILED before format — dependency resolution (exit 5)" >&2
  echo "beam gate: if this is an offline host, the gate cannot fetch new" >&2
  echo "beam gate: dependencies. Populate beam/deps on a networked host, or" >&2
  echo "beam gate: point BEAM_IMAGE at an image that already carries them." >&2
  exit 5
fi

echo "beam gate: PASS"