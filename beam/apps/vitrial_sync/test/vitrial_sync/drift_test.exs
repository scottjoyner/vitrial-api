defmodule VitrialSync.DriftTest do
  @moduledoc """
  The drift guard: is the BEAM port still a port, or has it become a fork?

  Everything else in this suite is differential, and a differential suite is
  only as good as the reference it measures against. `ParityTest` answers "do
  the two estates agree today"; this answers "are we still reading the same
  document", which is the question that matters the day the reference changes
  and nobody re-runs the port.

  ## Why this is a SEPARATE module from `ParityTest`

  `ParityTest` skips itself whenever `python3` or pydantic is missing, because
  it cannot execute the reference without them. This module needs a FILE READER
  and nothing else -- no interpreter, no pydantic, no SQLAlchemy.

  Gating the drift guard off by the same condition that disables the
  differential suite is the one arrangement that makes both worthless: every lane
  that runs on every push -- `.github/workflows/ci.yml`'s `BEAM umbrella` job in
  the pinned image, and `scripts/beam_gate.sh` running the same image -- has no
  `python3` at all, so a drift guard behind that skip would never execute
  anywhere it matters. It runs whenever `app/` is readable, and skips only when
  `app/` is not, which is the one condition under which it genuinely cannot.

  That is a real limitation and it is recorded rather than papered over. In the
  gate container `beam/` is mounted at `/w` and the reference tree is not
  present, so this module skips there and says so; of the three BEAM lanes,
  `.github/workflows/beam.yml` checks out the whole repository and is the only
  one that can currently run either half. The notice is printed at module-body
  time, because ExUnit captures output written from `setup_all` and it would
  never reach the terminal.

  ## What it checks, and why each one

  A drift guard that asserts "the file is non-empty" catches nothing. Each
  assertion below is one the BEAM port encodes a decision about, and each is
  written so that changing it in the reference FAILS here:

    * the three replay tokens, in source order -- `mutation_id_collision`,
      `idempotent_replay`, `rejected_mutation_id_reuse`. Collapsing three into
      two is the refactor this whole module exists to catch, and it is a
      plausible one: the third arm looks redundant to a reader who has not
      chased an operator reading a false `idempotent_replay`.
    * `accepted_replay = same_request and prior.result_status == "accepted"` --
      the `result_status` half is what makes "previously rejected" different
      from "previously accepted", and it is the half that is easy to drop while
      refactoring the surrounding ternary.
    * the revision guard's two arms. Both must be `or`-ed, not `and`-ed: a
      record with no stored entity that carries a `baseServerRevision` is a
      create attempt wearing an update's clothes.
    * `if not record.clientMutationID:` -- falsy, so `""` is rejected too, not
      only an absent field.
    * `same_request`'s two arms -- the fingerprint comparison and the weaker
      entityType/entityID/baseServerRevision fallback.
    * the three page ceilings, in the order `pull_page_accepts` applies them.
      Reordering them is a behaviour change, not a style change.
    * the serialized-batch measurement itself, so `Page`'s byte accounting
      cannot lose its reference without this noticing.
    * `"InvalidMutation": "invalid_mutation"` in `app/rejection_codes.py`, and
      the whole `RejectionCode` vocabulary compared against
      `VitrialSync.RejectionCode.codes/0` -- so two closed vocabularies cannot
      drift apart one token at a time.
    * `outcomes[original_index]` -- the index key the parity harness maps an
      outcome back to the record that produced it by.

  ## Comments and docstrings are stripped first

  Every assertion is about CODE. A construct that survives only as a comment, or
  only as a line of the prose explaining it, must not satisfy an assertion here,
  and `strip/1` is what makes that true. It is a small scanner rather than a
  `String.replace("#", "")`, because a naive strip would also remove the `#`
  inside a string literal and quietly change what the remaining text means.
  """
  use ExUnit.Case, async: false

  alias VitrialSync.{Admission, PythonReference, PythonSource, RejectionCode}

  if not PythonReference.reference_tree?() do
    reason = "python reference tree app/ not reachable from #{PythonReference.repo_root()}"

    IO.write(:user, "[drift] reference-source guard SKIPPED: #{reason}\n")
    @moduletag skip: reason
  end

  setup_all do
    {:ok,
     sync: PythonSource.read(PythonReference.reference_file("sync_service.py")),
     codes: PythonSource.read(PythonReference.reference_file("rejection_codes.py"))}
  end

  describe "the reference's replay reasons" do
    test "are three, in source order, inside _apply_push_once", %{sync: sync} do
      push = PythonSource.function_body(sync, "async def _apply_push_once")

      assert_in_order(push, [
        "reason = \"mutation_id_collision\"",
        "reason = \"idempotent_replay\"",
        "reason = \"rejected_mutation_id_reuse\""
      ])
    end

    test "the accepted replay still reads result_status", %{sync: sync} do
      push = PythonSource.function_body(sync, "async def _apply_push_once")

      assert_contains(
        push,
        "accepted_replay = same_request and prior.result_status == \"accepted\""
      )
    end

    test "the prior row is fetched by clientMutationID, and its fingerprint beside it", %{
      sync: sync
    } do
      push = PythonSource.function_body(sync, "async def _apply_push_once")

      assert_contains(push, "SyncMutation.organization_id == principal.organization_id")
      assert_contains(push, "SyncMutation.client_mutation_id == record.clientMutationID")

      assert_contains(
        push,
        "prior_fingerprint.request_fingerprint == request_fingerprint(record)"
      )

      # `db.get` keyed on (organization_id, client_mutation_id) -- the composite
      # primary key of the fingerprint table, and a LookupError written any other
      # way round.
      assert_contains(push, "SyncMutationFingerprint")
      assert_contains(push, "principal.organization_id, record.clientMutationID")
    end
  end

  describe "the reference's optimistic-concurrency guard" do
    test "keeps both arms, or-ed", %{sync: sync} do
      push = PythonSource.function_body(sync, "async def _apply_push_once")

      assert_contains(
        push,
        "(current is not None and record.baseServerRevision != current_revision)\n" <>
          "or (current is None and record.baseServerRevision is not None)"
      )
    end

    test "the missing-id step is a falsy test, not an is-None test", %{sync: sync} do
      push = PythonSource.function_body(sync, "async def _apply_push_once")

      assert_contains(push, "if not record.clientMutationID:")
    end

    test "outcomes stay keyed on the original batch index", %{sync: sync} do
      push = PythonSource.function_body(sync, "async def _apply_push_once")

      assert_contains(push, "outcomes[original_index]")
      assert_contains(push, "indexed_records.sort(")
    end
  end

  describe "the reference's page-fit predicate" do
    test "applies the count, payload and response ceilings in that order", %{sync: sync} do
      accepts = PythonSource.function_body(sync, "def pull_page_accepts")

      assert_in_order(accepts, [
        "len(records) >= MAX_SYNC_RECORDS",
        "MAX_SYNC_V1_BATCH_PAYLOAD_BYTES",
        "MAX_SYNC_PULL_RESPONSE_BYTES"
      ])
    end

    test "measures the serialized batch rather than estimating it", %{sync: sync} do
      accepts = PythonSource.function_body(sync, "def pull_page_accepts")

      assert_contains(accepts, "candidate.model_dump_json().encode(\"utf-8\")")
    end
  end

  describe "the shared rejection vocabulary" do
    test "still maps InvalidMutation to invalid_mutation", %{codes: codes} do
      assert_contains(codes, "\"InvalidMutation\": \"invalid_mutation\"")
    end

    test "VitrialSync's vocabulary is the reference's vocabulary", %{codes: codes} do
      assert RejectionCode.codes() ==
               PythonSource.literal_members(codes, "RejectionCode = Literal[")
    end

    test "every reason Admission can emit is a token the reference declares", %{codes: codes} do
      vocabulary = MapSet.new(PythonSource.literal_members(codes, "RejectionCode = Literal["))

      # `replay_reason/2` is the three-way branch the drift guard above is really
      # about; the rest are the tokens the other steps return. Both halves of the
      # comparison matter: a token this side invents is a client that gets a
      # reason no metric has ever aggregated, and a token the reference drops is
      # a client that gets one nothing else can produce.
      replay =
        Enum.uniq(
          for(
            same <- [true, false],
            accepted <- [true, false],
            do: Admission.replay_reason(same, accepted)
          )
        )

      assert Enum.sort(replay) == [
               "idempotent_replay",
               "mutation_id_collision",
               "rejected_mutation_id_reuse"
             ]

      assert Enum.all?(replay, &MapSet.member?(vocabulary, &1))

      assert Enum.all?(
               [
                 "missing_client_mutation_id",
                 "stale_revision",
                 "invalid_mutation",
                 "committed"
               ],
               &MapSet.member?(vocabulary, &1)
             )
    end
  end

  describe "the scanner the assertions above rest on" do
    test "removes a comment but keeps a hash inside a string literal" do
      # The `#` on the first line is not a comment. A naive strip-to-end-of-line
      # destroys it, and every assertion above that touches a string depends on
      # it surviving.
      assert PythonSource.strip("a = \"# not a comment\"\n# a comment\nb = 1\n") ==
               "a = \"# not a comment\"\n\nb = 1\n"
    end

    test "removes a docstring and leaves the definition below it findable" do
      source =
        PythonSource.strip("class C:\n    \"\"\"prose about x\n    more prose\"\"\"\n    x = 1\n")

      refute source =~ "prose about x"
      assert source =~ "x = 1"
    end

    test "removes an apostrophe in prose without swallowing the rest of the file" do
      assert PythonSource.strip("# it doesn't matter\nx = 1\ny = 2\n") == "x = 1\ny = 2\n"
    end

    test "treats a single-quoted literal as a string, not as prose" do
      assert PythonSource.strip("a = 'item_audit_event'\n") == "a = 'item_audit_event'\n"
    end

    test "an escaped quote does not end the string" do
      # Python source: a = "a \" b"  -- the backslash-quote is content.
      assert PythonSource.strip("a = \"a \\\" b\"\n") == "a = \"a \\\" b\"\n"
    end

    test "a comment in the reference is really gone from the stripped text", %{sync: sync} do
      refute String.contains?(sync, "Upper bound on changes scanned for one pull")
      assert String.contains?(sync, "MAX_SYNC_PULL_SCAN_CHANGES")
    end
  end

  defp assert_contains(source, fragment) do
    assert String.contains?(source, fragment),
           "the reference no longer contains #{inspect(fragment)}"
  end

  defp assert_in_order(source, fragments) do
    positions =
      Enum.map(fragments, fn fragment ->
        case :binary.match(source, fragment) do
          {position, _length} ->
            position

          :nomatch ->
            flunk("the reference no longer contains #{inspect(fragment)}")
        end
      end)

    assert positions == Enum.sort(positions),
           "expected #{inspect(fragments)} in that order, found them at #{inspect(positions)}"
  end
end
