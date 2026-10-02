defmodule VitrialSync.PageTest do
  use ExUnit.Case, async: true

  alias VitrialSync.{CanonicalJSON, Page, Record}
  alias VitrialSync.Page.Candidate

  @ts "2026-10-02T12:00:00Z"

  defp candidate(overrides) do
    struct!(
      %Candidate{
        sequence: 1,
        entity_type: "item",
        entity_id: "i1",
        payload_json: %{},
        server_revision: 3,
        client_mutation_id: "m1",
        updated_at: @ts
      },
      Map.new(overrides)
    )
  end

  defp assemble(start, candidates), do: Page.assemble(start, candidates)

  describe "wire sizing" do
    test "an empty batch is exactly the framing Pydantic emits" do
      # {"deviceID":"server","cursor":"seq:0","records":[]} == 51 bytes
      assert Page.batch_byte_size([], 0) == 51
    end

    test "one baseline record is 191 bytes, which is what SyncRecord serializes to" do
      record = %Record{
        id: "server:1",
        entity_type: "item",
        entity_id: "i1",
        updated_at: @ts,
        payload: Base.encode64("{}"),
        base_server_revision: nil,
        server_revision: 3,
        client_mutation_id: "m1",
        deleted_at: nil
      }

      assert Page.record_byte_size(record) == 191
      assert Page.batch_byte_size([record], 1) == 242
    end

    test "the cursor's own width moves the total" do
      record = %Record{
        id: "r",
        entity_type: "item",
        entity_id: "i",
        updated_at: @ts,
        payload: "x"
      }

      assert Page.batch_byte_size([record], 0) <
               Page.batch_byte_size([record], 1_000_000_000_000_000_000)
    end

    test "records are framed with commas, not concatenated" do
      record = %Record{
        id: "r",
        entity_type: "item",
        entity_id: "i",
        updated_at: @ts,
        payload: "x"
      }

      one = Page.record_byte_size(record)

      assert Page.batch_byte_size([record, record], 1) ==
               Page.batch_byte_size([record], 1) + one + 1
    end
  end

  describe "wire escaping" do
    defp sized(text) do
      Page.record_byte_size(%Record{
        id: "r",
        entity_type: "item",
        entity_id: text,
        updated_at: @ts,
        payload: "x"
      })
    end

    test "raw UTF-8, not \\uXXXX" do
      # Pydantic emits raw UTF-8. Escaping it would over-count by four bytes per
      # character and decide the page-fit question on content rather than size.
      assert sized("é") == sized("ab")
      assert sized("€") == sized("abc")
      assert sized("😀") == sized("abcd")
    end

    test "the two-character escapes" do
      # Each is 3 source characters becoming 4 escaped bytes, so +2 over a
      # 2-character string -- one for the extra character, one for the escape.
      for text <- [~s(a"b), "a\\b", "a\tb", "a\nb", "a\rb", "a\bb", "a\fb"] do
        assert sized(text) == sized("ab") + 2, "escape wrong for #{inspect(text)}"
      end
    end

    test "the solidus is not escaped" do
      assert sized("a/b") == sized("ab") + 1
    end

    test "other control characters become \\u00XX" do
      # 1 byte becomes the 6 bytes of \u0001: +5 over the same-length string.
      assert sized("a" <> <<0x01>> <> "b") == sized("abc") + 5
    end

    test "DEL is passed through raw, which is where this differs from the canonical escaper" do
      # json.dumps(ensure_ascii=True) escapes DEL to a 6-byte \u007f; Pydantic
      # does not. Applying the canonical rule here over-counts by five bytes.
      assert sized("a" <> <<0x7F>> <> "b") == sized("abc")
    end

    test "the two escapers genuinely disagree on DEL and non-ASCII" do
      # The whole reason there are two of them.
      assert CanonicalJSON.encode(%{"a" => "x" <> <<0x7F>> <> "y"}) == "{\"a\":\"x\\u007fy\"}"
      assert CanonicalJSON.encode(%{"a" => "é"}) == "{\"a\":\"\\u00e9\"}"
    end
  end

  describe "the cursor discipline" do
    test "an invisible change is consumed" do
      result = assemble(0, [candidate(sequence: 5, visible?: false)])

      assert result.records == []
      assert result.cursor_sequence == 5
    end

    test "a change whose entity is gone is consumed" do
      result = assemble(0, [candidate(sequence: 7, entity_present?: false)])

      assert result.records == []
      assert result.cursor_sequence == 7
    end

    test "consuming an invisible change does not skip a visible one after it" do
      result =
        assemble(0, [
          candidate(sequence: 1),
          candidate(sequence: 2, visible?: false),
          candidate(sequence: 3)
        ])

      assert length(result.records) == 2
      assert result.cursor_sequence == 3
    end

    test "a fully invisible run advances the cursor past every one of them" do
      # Leaving an undeliverable change unconsumed would stall every later pull
      # at the same position forever.
      result = assemble(0, Enum.map(1..10, fn n -> candidate(sequence: n, visible?: false) end))

      assert result.records == []
      assert result.cursor_sequence == 10
    end

    test "the cursor starts at the scan start when nothing is consumed" do
      assert assemble(42, []).cursor_sequence == 42
    end

    test "the cursor never moves backwards" do
      result = assemble(100, [candidate(sequence: 5)])
      assert result.cursor_sequence == 100
    end
  end

  describe "record synthesis" do
    test "a delivered record carries the server-assigned id" do
      assert [%Record{id: "server:9"}] = assemble(0, [candidate(sequence: 9)]).records
    end

    test "the payload is canonical JSON, then base64" do
      assert [%Record{payload: payload}] =
               assemble(0, [candidate(sequence: 1, payload_json: %{"b" => 1, "a" => 2})]).records

      assert payload == Base.encode64(~s({"a":2,"b":1}))
    end

    test "a null payload column is an empty object, not a failure" do
      assert [%Record{payload: payload}] =
               assemble(0, [candidate(sequence: 1, payload_json: nil)]).records

      assert payload == Base.encode64("{}")
    end

    test "a pull record never carries a base server revision" do
      assert [%Record{base_server_revision: nil}] = assemble(0, [candidate(sequence: 1)]).records
    end
  end

  describe "ceilings" do
    test "the constants match app/schemas.py and app/sync_service.py" do
      assert Page.max_records() == 200
      assert Page.max_batch_payload_bytes() == 2_100_000
      assert Page.max_response_bytes() == 2_250_000
      assert Page.max_scan_changes() == 500
    end

    test "the record-count ceiling stops the page and leaves the change unconsumed" do
      candidates =
        Enum.map(1..201, fn n ->
          candidate(sequence: n, payload_json: %{"n" => n}, entity_id: "i#{n}")
        end)

      result = assemble(0, candidates)

      assert length(result.records) == 200
      # The 201st is deliverable but does not fit, so it is NOT consumed.
      assert result.stopped_at == 201
      assert result.cursor_sequence == 200
    end

    test "the aggregate payload ceiling stops the page before the count ceiling does" do
      big = String.duplicate("a", 1_500_000)
      result = assemble(0, [candidate(sequence: 1, payload_json: %{"d" => big})])

      # One record at the ceiling is admitted. There is nothing after it, so
      # the loop runs out rather than stopping -- stopped_at stays nil.
      assert length(result.records) == 1
      assert result.cursor_sequence == 1
      assert result.stopped_at == nil

      two =
        assemble(0, [
          candidate(sequence: 1, payload_json: %{"d" => String.duplicate("a", 1_100_000)}),
          candidate(sequence: 2, payload_json: %{"d" => String.duplicate("b", 1_500_000)})
        ])

      assert length(two.records) == 1
      assert two.cursor_sequence == 1
      assert two.stopped_at == 2
    end

    test "an empty batch is always admitted" do
      assert assemble(0, []).records == []
    end
  end

  describe "stopping" do
    test "when a change does not fit, later smaller changes are not considered either" do
      # Skipping a full change would leave later smaller ones deliverable while
      # the cursor had already advanced past the one the client never received.
      big = %{"d" => String.duplicate("a", 1_400_000)}

      result =
        assemble(0, [
          candidate(sequence: 1, payload_json: big),
          candidate(sequence: 2, payload_json: big),
          candidate(sequence: 3, payload_json: %{"d" => "tiny"})
        ])

      assert length(result.records) == 1
      assert result.stopped_at == 2
      assert result.cursor_sequence == 1
    end
  end
end
