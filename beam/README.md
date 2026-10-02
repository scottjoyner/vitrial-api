# Vitrial BEAM services

A BEAM/Erlang reimplementation of the Vitrial Connected Operations API, living
alongside the FastAPI service in the repository root. The Python service is
retained unmodified as the **reference implementation and parity oracle** — it is
not being retired by this tree.

## Why this exists

The Python service is 7,884 lines of application code with 11,487 lines of tests
guarding it. It is well run, but it is single-process CPython: one core, no
in-process memory ceiling, and GC pauses on the tail. This tree moves the
request-serving and state-machine work onto the BEAM while keeping the same
observable contract.

The acceptance criterion is **behavioural parity**, measured against the Python
implementation. See `../vitrial-phase0/BEHAVIOUR-SPEC.md` for the rule set both
sides are checked against.

## Layout

Eight independently supervised applications under `apps/`:

| App | Owns |
|---|---|
| `vitrial_sync` | V1 + V2 sync engine, revision guard, idempotency, transient retry |
| `vitrial_ownership` | 13 entity types, canonical parent chain, capability enforcement |
| `vitrial_delivery` | 8-state delivery machine, plan freezing, append-only history |
| `vitrial_lifecycle` | 62-state quotation closure validator, blockers, provenance |
| `vitrial_evidence` | Streamed SHA-256 verification, S3 multipart staging, GC queue |
| `vitrial_auth` | Device pairing, sessions, admin boundary |
| `vitrial_reference` | Versioned reference-data publications |
| `vitrial_web` | HTTP boundary only — no business logic |

No application holds mutable state belonging to another, and each has its own
supervisor. That is isolation of *ownership*, not of *availability*: these eight
apps ship as one release, and in `:prod` `start_permanent: true` means a crash in
any of them takes the release down rather than looping. Recovery is the container
restart policy, which is a blunt instrument compared with a supervisor -- and it
is the right one here, because a partially-alive sync engine serving requests is a
worse outcome than one that is plainly down.

## Dependency doctrine

Erlang/OTP and the Elixir standard library are the baseline. External packages
are adopted only where a native rewrite is genuinely worse, and every adoption
carries a written verdict in `../vitrial-phase0/DEPENDENCY-DOCTRINE.md`.

**One resolved dependency set and one lockfile govern all eight applications**,
at `beam/mix.lock`. In a Mix umbrella that is automatic: each app's `mix.exs`
redirects `build_path`, `deps_path`, `config_path` and `lockfile` to the umbrella
root, and children carry no lockfile of their own. Eight independent dependency
trees would be eight independent CVE surfaces to audit, and eight
`mix deps.get` results to reconcile when one transitive package is yanked.

Everything the build produces stays inside `beam/`. The root `.gitignore` of the
Python repository covers none of it, so redirecting there would leave an
untracked dependency tree in a repository whose searches would then sweep hex
package sources into every result.

## Building

The build runs in CI, not on a workstation. The `beam` job in
`../.github/workflows/ci.yml` compiles, format-checks and tests the umbrella
inside a pinned container, and fails if `beam/mix.lock` moved during the build —
a dependency set that floats is the CVE surface this layout exists to collapse.

```bash
# Only meaningful for reading or iterating locally. The result that counts is
# the `beam` job's.
cd beam
mix compile --warnings-as-errors
mix format --check-formatted
mix test
```

The container is `hexpm/elixir:1.20.4-erlang-29.1.1-debian-bookworm-20260918-slim`
— Elixir 1.20.4 on OTP 29.1.1, which is three OTP majors ahead of the estate's
`ci/beam-compat:v1` image (OTP 27.3.4.2). This code is not permitted to compile
against the older one; `mix.exs` requires `elixir: "~> 1.19"` and the image is
the newest release on that constraint.

> **Note on `mix` on this machine.** `mix` on `PATH` resolves to
> `/home/nez/.local/bin/mix`, a wrapper around `.ci_only_guard` that blocks
> `mix compile` and then exits 0 — so a scaffold command reports success while
> having created nothing. Use `/home/nez/elixir/bin/mix` until that guard knows
> about this repository, and `ls` the tree afterwards rather than trusting the
> exit code.

## Latency budgets

Per-operation budgets and the measured envelopes behind them are in
`../vitrial-phase0/VERSIONS-AND-BUDGETS.md`. Targets are stated per layer
because a single end-to-end figure cannot be met or defended.

## Relationship to native-shared

`native-shared` was read as a **reference for design decisions only**. No code
is copied and no dependency is taken. Its BEAM packages are reimplemented here
from the decision, not from the source, per project doctrine.
