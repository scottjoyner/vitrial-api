defmodule VitrialSync.Db.CodecTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Db.Codec

  # A RowDescription field, the shape Pgconn decodes off the wire and hands to
  # the codec. Only name and type_oid participate in decoding.
  defp field(name, type) do
    %{name: Atom.to_string(name), type_oid: Codec.type_oid(type), format: 0}
  end

  describe "encode_param/1 — the bind boundary" do
    test "nil becomes the wire NULL, unconditionally" do
      # nil is the only NULL spelling this transport accepts; a bind that
      # missed normalization would be a garbage TEXT cell on the wire, so this
      # is the choke point where it is decided instead.
      assert Codec.encode_param(nil) == nil
    end

    test "scalars pass through as themselves" do
      assert Codec.encode_param("text") == "text"
      assert Codec.encode_param(42) == 42
      assert Codec.encode_param(-7) == -7
      assert Codec.encode_param(1.5) == 1.5
      assert Codec.encode_param(true) == true
      assert Codec.encode_param(false) == false
    end

    test "a UTC DateTime becomes UTC wire text, microseconds carried" do
      datetime = DateTime.from_naive!(~N[2026-01-01 12:34:56.500], "Etc/UTC")

      assert Codec.encode_param(datetime) == "2026-01-01 12:34:56.500000+00"
    end

    test "a non-UTC DateTime converts to UTC before encoding" do
      {:ok, utc, _offset} = DateTime.from_iso8601("2026-01-01T12:00:00+02:00")

      assert Codec.encode_param(utc) == "2026-01-01 10:00:00.000000+00"
    end

    test "a NaiveDateTime is accepted AS UTC — the documented reading, not a guess" do
      assert Codec.encode_param(~N[2026-01-01 12:00:00]) == "2026-01-01 12:00:00.000000+00"
    end

    test "a non-UTC zone converts through its carried offsets — the wire is UTC text either way" do
      # The UTC-only zone database cannot resolve "America/New_York", but a
      # DateTime that carries its own utc_offset needs no zone data to shift:
      # shift_zone to Etc/UTC is pure arithmetic, and the verified behavior is
      # the correct UTC instant, not a raise.
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

      assert Codec.encode_param(datetime) == "2026-01-01 17:00:00.000000+00"
    end

    test "maps and lists become JSON bytes" do
      assert Codec.encode_param(%{"a" => 1}) == ~s({"a":1})
      assert Codec.encode_param([1, 2, 3]) == "[1,2,3]"
    end

    test "anything else raises — a caller-visible error beats a garbage bind" do
      # Atoms, tuples, and every struct kind have no TEXT rendering this layer
      # is willing to invent; they are refused here rather than half-encoded.
      assert_raise ArgumentError, ~r/cannot bind/, fn -> Codec.encode_param(:some_atom) end
      assert_raise ArgumentError, ~r/cannot bind/, fn -> Codec.encode_param({1, 2}) end

      assert_raise ArgumentError, ~r/cannot bind/, fn ->
        Codec.encode_param(Date.new!(2026, 1, 1))
      end

      assert_raise ArgumentError, ~r/cannot bind/, fn ->
        Codec.encode_param(%VitrialSync.Canonical.Organization{authorization_revision: 1})
      end
    end
  end

  describe "decode_cell/2 — TEXT format in, estate terms out" do
    test "the SQL NULL becomes nil for every type" do
      assert Codec.decode_cell(nil, field(:n, :int8)) == nil
      assert Codec.decode_cell(nil, field(:n, :timestamptz)) == nil
      assert Codec.decode_cell(nil, field(:n, :jsonb)) == nil
      assert Codec.decode_cell(nil, field(:n, :text)) == nil
    end

    test "timestamptz text decodes to a UTC DateTime, honoring the carried offset" do
      # UTC-pinned session (the estate's posture): offset +00.
      assert Codec.decode_cell("2026-01-01 12:00:00+00", field(:t, :timestamptz)) ==
               DateTime.from_naive!(~N[2026-01-01 12:00:00], "Etc/UTC")

      # Fractions arrive trailing-zero-trimmed from the server; microseconds survive.
      assert Codec.decode_cell("2026-01-01 12:00:00.5+00", field(:t, :timestamptz)).microsecond ==
               {500_000, 6}

      # A session that LOST the UTC pin still decodes correctly: -05 shifts to UTC.
      assert Codec.decode_cell("2026-01-01 07:00:00-05", field(:t, :timestamptz)) ==
               DateTime.from_naive!(~N[2026-01-01 12:00:00], "Etc/UTC")

      # Half-hour offsets are a real zone shape (Asia/Kolkata +05:30).
      assert Codec.decode_cell("2026-01-01 17:30:00+05:30", field(:t, :timestamptz)) ==
               DateTime.from_naive!(~N[2026-01-01 12:00:00], "Etc/UTC")
    end

    test "timestamp (no offset) reads as UTC" do
      assert Codec.decode_cell("2026-01-01 12:00:00", field(:t, :timestamp)) ==
               DateTime.from_naive!(~N[2026-01-01 12:00:00], "Etc/UTC")
    end

    test "undecodable timestamp text fails loud with the text in the message" do
      assert_raise ArgumentError, ~r/undecodable timestamp text/, fn ->
        Codec.decode_cell("not a timestamp", field(:t, :timestamptz))
      end
    end

    test "json and jsonb text decode through VitrialSync.JSON" do
      assert Codec.decode_cell(~s({"a": [1, 2], "b": null}), field(:p, :jsonb)) == %{
               "a" => [1, 2],
               "b" => nil
             }

      assert Codec.decode_cell(~s([true, "x"]), field(:p, :json)) == [true, "x"]

      # A JSON null CELL TEXT decodes to nil as DATA, not as the SQL NULL.
      assert Codec.decode_cell("null", field(:p, :jsonb)) == nil
    end

    test "numeric decodes to the raw binary, per the doctrine's numeric answer" do
      assert Codec.decode_cell("1.25", field(:n, :numeric)) == "1.25"
    end

    test "integer and boolean text decode to terms" do
      assert Codec.decode_cell("42", field(:n, :int8)) == 42
      assert Codec.decode_cell("7", field(:n, :int4)) == 7
      assert Codec.decode_cell("1", field(:n, :int2)) == 1
      assert Codec.decode_cell("t", field(:b, :bool)) == true
      assert Codec.decode_cell("f", field(:b, :bool)) == false

      assert_raise ArgumentError, ~r/undecodable boolean/, fn ->
        Codec.decode_cell("yes", field(:b, :bool))
      end
    end

    test "float text decodes; text-family and unknown types pass through raw" do
      assert Codec.decode_cell("1.5", field(:f, :float8)) == 1.5
      assert Codec.decode_cell("plain", field(:t, :varchar)) == "plain"
      assert Codec.decode_cell("c1", field(:t, :uuid)) == "c1"

      # Unknown type OIDs pass through raw rather than being corrupted by a
      # guess; a field map without a type_oid is a caller bug and raises.
      assert Codec.decode_cell("raw", field(:t, :varchar) |> Map.put(:type_oid, 999_999)) ==
               "raw"

      assert_raise ArgumentError, ~r/type_oid/, fn ->
        Codec.decode_cell("raw", %{name: "t"})
      end
    end
  end

  describe "decode_row/2" do
    # The RowDescription Pgconn hands back for the entity upsert's read-back,
    # and the matching TEXT-format row: NULL is the list's nil, timestamps are
    # server text. Literal — a module attribute cannot call this module's own
    # helpers — with the catalog OIDs spelled out (varchar 1043, int8 20,
    # json 114, timestamptz 1184).
    @fields [
      %{name: "organization_id", type_oid: 1043, format: 0},
      %{name: "server_revision", type_oid: 20, format: 0},
      %{name: "payload_json", type_oid: 114, format: 0},
      %{name: "updated_at", type_oid: 1184, format: 0},
      %{name: "deleted_at", type_oid: 1184, format: 0}
    ]

    @cells ["org-1", "7", ~s({"k":"v"}), "2026-01-02 03:04:05+00", nil]

    test "decodes to a plain map keyed by column-name atoms, carrying every column" do
      row = Codec.decode_row(@fields, @cells)

      assert row == %{
               organization_id: "org-1",
               server_revision: 7,
               payload_json: %{"k" => "v"},
               updated_at: DateTime.from_naive!(~N[2026-01-02 03:04:05], "Etc/UTC"),
               deleted_at: nil
             }
    end

    test "a cell/field count mismatch raises instead of silently mis-reading" do
      assert_raise ArgumentError, ~r/mismatch/, fn ->
        Codec.decode_row(@fields, Enum.drop(@cells, 1))
      end
    end
  end

  describe "the wire-encodability guarantee over encoder outputs" do
    # One missed normalization is a garbage TEXT cell or a server-side type
    # rejection, invisible to the compiler — so this is a pass over ALL encoder
    # outputs, not a spot check: every parameter in every statement a null-heavy
    # Effects batch produces must be nil (the wire NULL) or a scalars/binary the
    # transport can put on the wire verbatim — never a struct, atom, or tuple.
    test "a null-heavy Effects batch emits only wire-encodable binds" do
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
          assert is_nil(param) or is_integer(param) or is_float(param) or is_boolean(param) or
                   is_binary(param),
                 "un-encodable bind leaked into: #{inspect(sql)} (#{inspect(param)})"

          refute is_struct(param), "struct bind leaked into: #{inspect(sql)} (#{inspect(param)})"
        end)
      end)
    end
  end
end
