defmodule VitrialSync.Db.CodecTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Db.Codec

  describe "encode_param/1 — the bind boundary" do
    test "nil becomes :null, unconditionally — the rule that keeps connections alive" do
      # Binding a literal nil raises {:integer_overflow, :int8, nil} inside
      # epgsql_codec_integer and KILLS the connection; this is the choke point
      # that makes that unreachable from the encoders.
      assert Codec.encode_param(nil) == :null
    end

    test "already-normalized :null passes through" do
      assert Codec.encode_param(:null) == :null
    end

    test "scalars pass through as themselves" do
      assert Codec.encode_param("text") == "text"
      assert Codec.encode_param(42) == 42
      assert Codec.encode_param(-7) == -7
      assert Codec.encode_param(1.5) == 1.5
      assert Codec.encode_param(true) == true
      assert Codec.encode_param(false) == false
    end

    test "a UTC DateTime becomes a naive erl tuple — never a struct" do
      # A struct bound directly hits function_clause in epgsql_idatetime:timestamp2i/1
      # and kills the connection; the codec converts before that can happen.
      datetime = DateTime.from_naive!(~N[2026-01-01 12:34:56.500], "Etc/UTC")

      assert Codec.encode_param(datetime) == {{2026, 1, 1}, {12, 34, 56}}
    end

    test "a non-UTC DateTime converts to UTC before encoding" do
      {:ok, utc, _offset} = DateTime.from_iso8601("2026-01-01T12:00:00+02:00")

      assert Codec.encode_param(utc) == {{2026, 1, 1}, {10, 0, 0}}
    end

    test "a non-UTC zone without a zone database fails loud, not wrong" do
      datetime = %DateTime{
        year: 2026,
        month: 1,
        day: 1,
        hour: 12,
        minute: 0,
        second: 0,
        microsecond: {0, 0},
        time_zone: "America/New_York",
        zone_abbr: "EST",
        utc_offset: -18_000,
        std_offset: 0
      }

      assert_raise ArgumentError, ~r/pins UTC on the wire/, fn ->
        Codec.encode_param(datetime)
      end
    end

    test "maps and lists become JSON bytes" do
      assert Codec.encode_param(%{"a" => 1}) == ~s({"a":1})
      assert Codec.encode_param([1, 2, 3]) == "[1,2,3]"
    end

    test "anything else raises — a caller-visible error beats a dead connection" do
      # Atoms other than :null, tuples, and every struct kind would each reach
      # an epgsql function_clause in production; here they are refused.
      assert_raise ArgumentError, ~r/cannot bind/, fn -> Codec.encode_param(:some_atom) end
      assert_raise ArgumentError, ~r/cannot bind/, fn -> Codec.encode_param({1, 2}) end

      assert_raise ArgumentError, ~r/cannot bind/, fn ->
        Codec.encode_param(~N[2026-01-01 00:00:00])
      end

      assert_raise ArgumentError, ~r/cannot bind/, fn ->
        Codec.encode_param(Date.new!(2026, 1, 1))
      end
    end
  end

  describe "decode_cell/2" do
    test ":null becomes nil for every type" do
      assert Codec.decode_cell(:null, :int8) == nil
      assert Codec.decode_cell(:null, :timestamptz) == nil
      assert Codec.decode_cell(:null, :jsonb) == nil
      assert Codec.decode_cell(:null, :text) == nil
    end

    test "timestamptz erl tuple decodes to a UTC DateTime, microseconds carried" do
      # epgsql_idatetime:i2timestamp/1 yields {{y,m,d},{h,m,s}} with a FLOAT
      # second — pure epoch arithmetic, no timezone input (the corrected fact).
      assert Codec.decode_cell({{2026, 1, 1}, {12, 0, 0.0}}, :timestamptz) ==
               DateTime.from_naive!(~N[2026-01-01 12:00:00], "Etc/UTC")

      assert Codec.decode_cell({{2026, 1, 1}, {12, 0, 0.5}}, :timestamptz).microsecond ==
               {500_000, 6}
    end

    test "json and jsonb binaries decode through VitrialSync.JSON" do
      assert Codec.decode_cell(~s({"a": [1, 2], "b": null}), :jsonb) == %{
               "a" => [1, 2],
               "b" => nil
             }

      assert Codec.decode_cell(~s([true, "x"]), :json) == [true, "x"]
    end

    test "numeric decodes to the raw binary, per the doctrine's numeric answer" do
      assert Codec.decode_cell("1.25", :numeric) == "1.25"
    end

    test "scalars pass through unchanged" do
      assert Codec.decode_cell(42, :int8) == 42
      assert Codec.decode_cell(true, :bool) == true
      assert Codec.decode_cell("plain", :varchar) == "plain"
      # Unknown types pass through raw rather than being corrupted by a guess.
      assert Codec.decode_cell("raw", :weird_type) == "raw"
    end
  end

  describe "decode_row/2" do
    # The column description shape epgsql hands back: #epgsql_column{} records
    # — {column, name, type, oid, size, modifier, format, table_oid, attr}.
    @columns [
      {:column, "organization_id", :varchar, 0, 128, -1, 1, 0, 0},
      {:column, "server_revision", :int8, 0, 8, -1, 1, 0, 0},
      {:column, "payload_json", :json, 0, -1, -1, 1, 0, 0},
      {:column, "updated_at", :timestamptz, 0, 8, -1, 1, 0, 0},
      {:column, "deleted_at", :timestamptz, 0, 8, -1, 1, 0, 0}
    ]

    @row {"org-1", 7, ~s({"k":"v"}), {{2026, 1, 2}, {3, 4, 5.0}}, :null}

    test "decodes to a plain map keyed by column-name atoms, carrying every column" do
      row = Codec.decode_row(@columns, @row)

      assert row == %{
               organization_id: "org-1",
               server_revision: 7,
               payload_json: %{"k" => "v"},
               updated_at: DateTime.from_naive!(~N[2026-01-02 03:04:05], "Etc/UTC"),
               deleted_at: nil
             }
    end
  end

  describe "the no-nil guarantee over encoder outputs" do
    # One missed normalization is a killed production connection triggered by
    # data, invisible to the compiler — so this is a pass over ALL encoder
    # outputs, not a spot check: every parameter in every statement a null-heavy
    # Effects batch produces must be non-nil and non-struct.
    test "a null-heavy Effects batch emits zero nil binds and zero struct binds" do
      effects = %VitrialSync.Batch.Effects{
        mutation_rows: [
          %{
            organization_id: "o",
            client_mutation_id: "m1",
            submitted_by_user_id: "u",
            membership_id: "ms",
            session_id: "s",
            authorization_revision: 1,
            device_id: "d",
            entity_type: "evidence",
            entity_id: "e1",
            base_server_revision: nil,
            result_server_revision: nil,
            result_status: "rejected"
          }
        ],
        fingerprint_rows: [
          %{organization_id: "o", client_mutation_id: "m1", request_fingerprint: "f"}
        ],
        entity_upserts: [
          %{
            organization_id: "o",
            entity_type: "evidence",
            entity_id: "e1",
            server_revision: 1,
            payload: nil,
            updated_at: "2026-01-01T00:00:00Z",
            deleted_at: "2026-01-01T00:00:00Z"
          }
        ],
        changelog_rows: [
          %{
            organization_id: "o",
            entity_type: "evidence",
            entity_id: "e1",
            server_revision: 1,
            client_mutation_id: nil,
            operation: "delete"
          }
        ],
        membership_grants: [%{customer_ids: [], project_ids: ["p1"]}],
        evidence_gc_rows: [
          %{
            document_id: "e1",
            storage_provider: "s3",
            object_key: "k",
            reason: "metadata_tombstone",
            grace_seconds: 0
          }
        ],
        organization_authorization_revision: 1
      }

      statements = VitrialSync.Db.Writes.effects(effects, "o", "ms", DateTime.utc_now())

      refute statements == []

      Enum.each(statements, fn {sql, params} ->
        assert is_binary(sql)

        Enum.each(params, fn param ->
          refute is_nil(param), "nil bind leaked into: #{inspect(sql)}"
          refute is_struct(param), "struct bind leaked into: #{inspect(sql)} (#{inspect(param)})"
        end)
      end)
    end
  end
end
