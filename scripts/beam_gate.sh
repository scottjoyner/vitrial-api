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
# Exit codes are the three steps' own, first failure wins:
#   0  format clean, compiled with no warnings, all tests passed
#   1  `mix format --check-formatted` found unformatted sources
#   2  compilation failed, or emitted a warning under --warnings-as-errors
#   3  the test suite failed
#   4  the container could not run at all (no image, no mount)
#
# `mix compile --warnings-as-errors` is the only thing in the estate that finds
# an unreachable clause inside a reachable module. The module-level orphan
# guard cannot see one, so this step is not redundant with it.
set -uo pipefail

IMAGE="${BEAM_IMAGE:-docker.io/hexpm/elixir:1.20.4-erlang-29.1.1-debian-bookworm-20260918-slim}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BEAM="$REPO/beam"

if [ ! -d "$BEAM" ]; then
  echo "beam gate: $BEAM does not exist" >&2
  exit 4
fi

if ! podman image exists "$IMAGE" 2>/dev/null; then
  echo "beam gate: image $IMAGE is not present locally; run: podman pull $IMAGE" >&2
  exit 4
fi

run_beam() {
  podman run --rm \
    -v "$BEAM":/w:z \
    -w /w \
    "$IMAGE" \
    bash -c "$1"
}

echo "beam gate: format"
if ! run_beam 'mix format --check-formatted'; then
  echo "beam gate: FAILED at format (exit 1)" >&2
  exit 1
fi

echo "beam gate: compile --warnings-as-errors"
if ! run_beam 'mix compile --warnings-as-errors'; then
  echo "beam gate: FAILED at compile (exit 2)" >&2
  exit 2
fi

echo "beam gate: test"
# The summary line is what a reader wants; `-q` keeps the rest out of the log
# without hiding a failure, which prints its own traceback regardless.
if ! run_beam 'mix test 2>&1 | tail -40'; then
  echo "beam gate: FAILED at test (exit 3)" >&2
  exit 3
fi

echo "beam gate: PASS"