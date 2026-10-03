defmodule VitrialSync.ParityTest do
  @moduledoc """
  The differential suite: this estate against the Python reference, executed.

  Every input comes out of `parity_oracle.py`'s own JSON document. Nothing here
  retypes a case. That is not tidiness -- a prototype that listed the cases in
  Elixir produced nineteen false mismatches from the retyping alone, and every
  one of them looked like a real parity bug until the two lists were diffed.

  Three claims, in the order `ARCHITECTURE.md` §2.1 states them:

    * **admission** -- for each record, `{status, reason, result_revision}` from
      `VitrialSync.Admission.decide/2` against the same three fields from the
      reference's own `_apply_push_once`
    * **page** -- `VitrialSync.Page`'s wire sizing against the byte length of
      real `SyncBatch.model_dump_json()` output, plus the three ceilings against
      the constants read out of `app/schemas.py` and `app/sync_service.py`
    * **payload** -- `VitrialSync.Payload.decode/1`'s accept/reject and
      `VitrialSync.CanonicalJSON.encode/1`'s bytes against the reference's

  ## The seven things that are NOT identical, and why that is a test

  `VitrialSync.JSON` documents two places where it is deliberately narrower than
  CPython's `json.loads`: non-finite numbers (including a finite literal that
  overflows to infinity) and unpaired surrogates. The oracle classifies every
  decoded value by exactly those two rules, from the value the *reference*
  produced -- never from what this side did with it. The test therefore asserts:

    * a case the oracle did not narrow: this side must agree, on the verdict and
      on the canonical bytes
    * a case the oracle did narrow: this side must reject it, and the count of
      those must be greater than zero

  A blanket `assert cases == cases` would either be permanently red or get
  "fixed" by relaxing the assertion. The enumerated set is the only shape that
  cannot do either.

  The error TEXT is never compared. `decode_payload` embeds CPython's own json
  module message (`Expecting value: line 1 column 1`), which no other
  implementation is going to reproduce verbatim.

  ## Skipping

  The skip is wired in the module body, not in `setup_all`. A `@tag skip` inside
  an `unless` skips the first test and still runs `setup_all`, which then raises
  -- `mix test` reports `2 invalid, 1 skipped` with a stack trace. `@moduletag`
  skips the whole module, `setup_all` never runs, and `mix test` exits 0 on an
  all-skipped module.

  The notice is written at module-body time rather than from `setup_all`,
  because ExUnit captures output written from `setup_all` and it never reaches
  the terminal.
  """
  use ExUnit.Case, async: false

  alias VitrialSync.{Admission, CanonicalJSON, Page, Payload, Record}
  alias VitrialSync.PythonReference

  if reason = PythonReference.unavailable_reason() do
    IO.write(:user, "[parity] differential suite SKIPPED: #{reason}\n")
    @moduletag skip: reason
  end

  # The figures `beam/ARCHITECTURE.md` §2.1 quotes. They are asserted here
  # against what the oracle actually emitted, so a corpus change that the
  # document does not follow fails the build instead of quietly making the
  # document a transcription of an older measurement. This is the ONLY place the
  # counts appear twice; nothing else in the suite hardcodes a number.
  @documented %{
    admission_cases: 44,
    page_cases: 117,
    payload_cases: 169,
    payload_accepted: 55,
    payload_narrowed: 13,
    payload_narrowed_nonfinite: 7,
    payload_narrowed_surrogate: 6
  }

  setup_all do
    document = PythonReference.load!()

    {:ok,
     admission: index_by_name(document["admission"]),
     page: index_by_name(document["page"]),
     payload: index_by_name(document["payload"]),
     limits: document["limits"]}
  end

  describe "admission decisions" do
    test "every case agrees with the reference's steps 1-3, and step 4 with decode/1", %{
      admission: cases
    } do
      assert map_size(cases) == @documented.admission_cases

      Enum.each(cases, fn {name, row} ->
        records = Enum.map(row["records"], &record!/1)
        outcomes = row["outcomes"]
        step4 = row["step4"]

        Enum.zip([records, row["worlds"], outcomes, step4])
        |> Enum.with_index()
        |> Enum.each(fn {{record, world, expected, decode}, index} ->
          outcome = Admission.decide(record, context!(world))

          case expected do
            nil ->
              # No event: the reference passed steps 1-3 and fell off the end of
              # the sliced loop into step 4, which is `Payload.decode/1`'s. The
              # oracle reports that verdict separately, as `step4`, precisely
              # because the slice stops there -- so this is the assertion that
              # `decide/2`'s step 4 IS the reference's step 4.
              case Payload.decode(record.payload) do
                {:ok, _value} ->
                  assert outcome.status == :accepted, name
                  assert outcome.reason == "committed", name

                {:error, _reason} ->
                  assert outcome.status == :rejected, name
                  assert outcome.reason == "invalid_mutation", name
              end

            event ->
              assert {to_string(outcome.status), outcome.reason, outcome.result_revision} ==
                       {event["status"], event["reason"], event["result_revision"]},
                     "#{name} (record #{index})"
          end

          assert_payload_agrees(name, record, decode)
        end)
      end)
    end

    test "the corpus reaches every reason the steps 1-3 slice can emit", %{admission: cases} do
      emitted =
        cases
        |> Enum.flat_map(fn {_name, row} -> Enum.map(row["outcomes"], & &1["reason"]) end)
        |> Enum.reject(&is_nil/1)
        |> Enum.uniq()
        |> Enum.sort()

      assert emitted == [
               "idempotent_replay",
               "missing_client_mutation_id",
               "mutation_id_collision",
               "rejected_mutation_id_reuse",
               "stale_revision"
             ]
    end
  end

  describe "pull page wire sizes" do
    test "Page's sizing matches SyncBatch.model_dump_json(), case by case", %{page: cases} do
      assert map_size(cases) == @documented.page_cases

      Enum.each(cases, fn {name, row} ->
        cond do
          row["page_error"] != nil ->
            assert_pulls_apart_on_non_utf8_payload(name, row)

          row["accepted_records"] != nil ->
            # `Page.assemble/2` rebuilds `id` as "server:<sequence>", so it
            # cannot produce the capped `id` this case needs. The oracle hands
            # over the records it accepted and the byte count it measured.
            records = Enum.map(row["accepted_records"], &record!/1)
            assert length(records) == row["included"], name
            assert Page.batch_byte_size(records, row["max_sequence"]) == row["batch_bytes"], name

          true ->
            assembled = Page.assemble(row["cursor_sequence"], candidates!(row))
            assert length(assembled.records) == row["included"], name
            assert assembled.cursor_sequence == row["max_sequence"], name
            assert assembled.stopped_at != nil == row["truncated"], name

            assert Page.batch_byte_size(assembled.records, assembled.cursor_sequence) ==
                     row["batch_bytes"],
                   name
        end
      end)
    end

    test "all three ceilings are reached by at least one case", %{page: cases} do
      # Each ceiling, in the order `pull_page_accepts` applies them.
      assert cases["count_ceiling"]["included"] == Page.max_records()
      assert cases["count_ceiling"]["truncated"]

      assert cases["payload_ceiling"]["truncated"]
      assert cases["payload_ceiling"]["included"] == 1

      response = cases["response_ceiling"]

      # The third ceiling is load-bearing: the accepted prefix is inside
      # MAX_SYNC_PULL_RESPONSE_BYTES and one more record is not, while the
      # aggregate payload ceiling still has room.
      assert response["truncated"]
      assert response["batch_bytes"] <= 2_250_000
      assert response["batch_bytes"] > 2_100_000
    end

    test "the ceilings Page exports are the ones the reference declares", %{limits: limits} do
      assert Page.max_records() == limits["max_records"]
      assert Page.max_batch_payload_bytes() == limits["max_batch_payload_bytes"]
      assert Page.max_response_bytes() == limits["max_response_bytes"]
      assert Page.max_scan_changes() == limits["max_scan_changes"]
      assert Record.max_records() == limits["max_records"]
      assert Record.max_batch_payload_bytes() == limits["max_batch_payload_bytes"]
    end
  end

  describe "payload decode" do
    test "every case agrees, except the two documented narrowings", %{payload: cases} do
      assert map_size(cases) == @documented.payload_cases

      accepted = Enum.count(cases, fn {_n, row} -> row["accepted"] end)
      assert accepted == @documented.payload_accepted

      narrowed =
        Enum.map(cases, fn {name, row} -> {name, row["narrowing"]} end)
        |> Enum.reject(fn {_name, class} -> is_nil(class) end)

      assert length(narrowed) == @documented.payload_narrowed

      assert Enum.count(narrowed, fn {_n, c} -> c == "nonfinite" end) ==
               @documented.payload_narrowed_nonfinite

      assert Enum.count(narrowed, fn {_n, c} -> c == "surrogate" end) ==
               @documented.payload_narrowed_surrogate

      Enum.each(cases, fn {name, row} ->
        raw = Base.decode64!(row["input_b64"])
        {:ok, record} = Record.new(%{valid_record_fields() | payload: raw})

        assert_payload_agrees(name, record, %{
          "accepted" => row["accepted"],
          "narrowing" => row["narrowing"],
          "canonical" => row["canonical"]
        })
      end)
    end
  end

  # -- helpers ---------------------------------------------------------------

  # The whole narrowing contract in one place, so the admission block and the
  # payload block cannot drift apart in how they read the oracle's `narrowing`.
  defp assert_payload_agrees(name, record, decode) do
    case Payload.decode(record.payload) do
      {:ok, value} ->
        cond do
          not decode["accepted"] ->
            flunk("#{name}: the reference rejected this payload and this side accepted it")

          decode["narrowing"] != nil ->
            flunk(
              "#{name}: this side accepted a value the oracle narrowed as " <>
                inspect(decode["narrowing"])
            )

          true ->
            assert CanonicalJSON.encode(value) == decode["canonical"], name
        end

      {:error, _reason} ->
        assert decode["accepted"] == false or decode["narrowing"] != nil,
               "#{name}: this side rejected a payload the reference accepted outright"
    end
  end

  # app/schemas.py:76 types `payload` as `bytes` with no UTF-8 check, so a
  # record carrying b"\xff\xfe" VALIDATES. `pull_page_accepts` then calls
  # `model_dump_json()` on it (app/sync_service.py:84) and Pydantic raises
  # `PydanticSerializationError`, which the pull loop does not catch -- it
  # escapes as an unhandled 500.
  #
  # This side cannot express the case at all, and it does not fail loudly
  # either: `escaped_byte_size/1` walks `<<char::utf8 <- binary>>`, an invalid
  # sequence simply matches nothing, and the record is sized as though the
  # payload were two empty bytes. That divergence is pinned here so it cannot
  # change unnoticed in either direction.
  defp assert_pulls_apart_on_non_utf8_payload(name, row) do
    assert name == "non_utf8_payload"
    assert row["page_error"] =~ "invalid utf-8"

    [change | _] = row["changes"]

    {:ok, record} =
      Record.new(%{
        valid_record_fields()
        | payload: Base.decode16!(change["raw_payload_hex"], case: :lower)
      })

    assert Page.record_byte_size(record) ==
             Page.record_byte_size(%{record | payload: ""}),
           "a non-UTF-8 payload must size as though it were empty -- that is the defect"
  end

  defp valid_record_fields do
    %{
      id: "r1",
      entity_type: "customer",
      entity_id: "c1",
      updated_at: "2026-01-01T00:00:00Z",
      payload: "{}",
      base_server_revision: nil,
      server_revision: nil,
      client_mutation_id: "m1",
      deleted_at: nil
    }
  end

  defp record!(attrs) do
    {:ok, record} =
      Record.new(%{
        id: attrs["id"],
        entity_type: attrs["entity_type"],
        entity_id: attrs["entity_id"],
        updated_at: attrs["updated_at"],
        # base64, always. `json.dumps` raises TypeError on bytes, and text
        # transport would let a non-UTF-8 payload round-trip differently on each
        # estate -- a false PASS, which is worse than a false failure.
        payload: Base.decode64!(attrs["payload_b64"]),
        base_server_revision: attrs["base_server_revision"],
        server_revision: attrs["server_revision"],
        client_mutation_id: attrs["client_mutation_id"],
        deleted_at: attrs["deleted_at"]
      })

    record
  end

  defp context!(world) do
    %{
      # `Admission.prior()` is a map with ATOM keys, and `same_request?/3`
      # pattern-matches on `prior.entity_type`. The oracle emits string keys,
      # so the rename happens exactly once, here.
      prior: prior!(world["prior"]),
      prior_fingerprint: world["prior_fingerprint"],
      entity_exists: world["entity_exists"],
      current_server_revision: world["current_server_revision"]
    }
  end

  defp prior!(nil), do: nil

  defp prior!(prior) do
    %{
      entity_type: prior["entity_type"],
      entity_id: prior["entity_id"],
      base_server_revision: prior["base_server_revision"],
      result_status: prior["result_status"],
      result_server_revision: prior["result_server_revision"]
    }
  end

  defp candidates!(row) do
    Enum.map(row["changes"], fn change ->
      %Page.Candidate{
        sequence: change["sequence"],
        entity_type: change["entity_type"],
        entity_id: change["entity_id"],
        payload_json: change["payload_json"],
        server_revision: change["server_revision"],
        client_mutation_id: change["client_mutation_id"],
        updated_at: change["updated_at"],
        deleted_at: change["deleted_at"],
        visible?: true,
        entity_present?: true
      }
    end)
  end

  defp index_by_name(rows) when is_list(rows) do
    Map.new(rows, fn row -> {row["name"], row} end)
  end
end
