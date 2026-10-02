# server-api

Provenance: this tree was migrated here from `vitrial-api`.

## Where it came from

`vitrial-api` is a repository in this environment whose README reads *"Dedicated
backend for the Vitrial 0.2.0 Connected Operations client."* The Vitrial client
family (`vitrialuminios-del-valle-react`, `-unity`, `-ios`) lives on the
`astrix-group-accounts` GitHub org, and this repository — `server-api` — is that
org's server-side repository. It is the natural home for the backend, so the code
was moved here with its history intact rather than re-typed.

The migration is a **seed**, not a rewrite. Nothing in the Python or Elixir source
was edited to make it fit the new repository.

## History

`main` was seeded from `vitrial-source/main` at `0b89421` and then merged with
this repository's own `origin/main` (`3fc5533`), which carried a parallel line of
CI hardening that the source repository had not seen. The merge is recorded as
`0d39d44`; its only conflict was in `Makefile`, where both lines had added a `ubs`
target. The resolution keeps the longer comment from `origin/main` and keeps the
`branches` target that only existed on the source line.

So `main` carries both: the CI/security work this repository already had, and the
migration work (BEAM sync engine verticals 1–3, the stable rejection-code
vocabulary, the UBS gate and branch-staleness audit) that came across.

## Remotes

| Remote | Points at | Why it is here |
| --- | --- | --- |
| `origin` | `astrix-group-accounts/server-api` | this repository |
| `vitrial-source` | `../vitrial-api` (local path) | the source tree, kept so the two can be diffed |
| `fork` | `nezter/vitrial-api` | carried over from the source clone; the local BEAM CI runner lives there |
| `up` | `scottjoyner/vitrial-api` | carried over from the source clone; upstream of the fork |

`fork` and `up` name the *vitrial-api* repository, not this one. They are retained
deliberately: `beam.yml` gates the BEAM build on the `nezter/vitrial-api` fork,
which is the only one of the two that has a runner registered. Removing them would
break the documented way to run the BEAM lane. Renaming them to say what they are
is a reasonable follow-up, but it is a judgement call about which repository the
fork relationship should point at, so it has not been made unilaterally.

## Not pushed

`main` is ahead of `origin/main` by the migration commits. Nothing has been pushed
to `astrix-group-accounts/server-api`. Publishing to the organisation's repository
is someone else's call, so it is left to them.

## What this repository is

- **Python** at the root: the reference implementation, and the dual-run oracle.
  It stays. It is not being deleted as part of a port.
- **Elixir/OTP** under `beam/`: a mix umbrella. `beam/apps/vitrial_sync` currently
  covers the sync contract — cursors, canonical JSON, request fingerprinting,
  timestamp normalisation, strict JSON decoding, payload decoding, record
  validation, entity ordering, push admission, retry classification, and pull
  page assembly with exact wire sizing.

The umbrella has eight apps. Only `vitrial_sync` has real content. The other
seven — `vitrial_auth`, `vitrial_delivery`, `vitrial_evidence`,
`vitrial_lifecycle`, `vitrial_ownership`, `vitrial_reference` and
`vitrial_web` — are the Mix scaffold: a `mix.exs`, a `.formatter.exs`, a
`test_helper.exs` and a module whose body is a `@moduledoc` describing what the
app is *going* to do. They compile and they assert nothing. That is the one
thing here that contradicts the standing rule that nothing lands as a skeleton,
and it came across with the migration rather than being created by it. Two
options, both someone else's call: build them out, or drop the seven from the
umbrella until there is code for them. `vitrial_web` is not purely inert — it has
endpoint configuration in `beam/config/`.

The BEAM port is differential against the Python, not derived from it: the
harnesses execute the real Python (its functions AST-lifted and run, or its real
Pydantic serializers) and compare digests, sizes and decisions. The contract
specification both sides answer to is in `contracts/` and
`/mnt/ssd-relief/Projects/vitrial-phase0/BEHAVIOUR-SPEC.md`.

## Running the checks

Locally:

    make check      # compileall, contract validation, guard prefixes, unit tests
    make ubs        # UBS delta gate against the committed baseline
    make branches   # content-based branch-staleness audit

The BEAM suite runs in a container, not on the host — see `AGENTS.md` for the
exact invocation, and note that a local `mix` on the PATH may be a guard wrapper
that exits 0 without doing anything. Always check that the umbrella's apps
actually exist after a scaffolding step.
