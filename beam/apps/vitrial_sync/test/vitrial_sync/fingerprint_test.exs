defmodule VitrialSync.FingerprintTest do
  @moduledoc """
  Parity with `app/idempotency.py`, pinned to observed output.

  Every expectation in the `reference vectors` block was produced by running the
  Python implementation, not by reading it. A digest that drifts from those values
  is not a refactor of this module: it is the two services disagreeing about
  whether a replay is legitimate, which surfaces only once a client that pushed
  through both is rejected by one of them.

  Regenerate with the recipe in the module below rather than by hand -- a
  hand-computed SHA-256 is a test that agrees with whatever the author believed.
  """

  use ExUnit.Case, async: true

  alias VitrialSync.Fingerprint

  defp mutation(attrs) do
    Map.merge(
      %{
        entity_type: "item",
        entity_id: "item-1",
        base_server_revision: nil,
        updated_at: "2026-01-01T00:00:00Z",
        deleted_at: nil,
        payload: <<>>
      },
      attrs
    )
  end

  describe "reference vectors" do
    test "an empty payload at the origin" do
      assert Fingerprint.of(mutation(%{})) ==
               "9a9a562aed54f3f1b2916ebd874e5014f13f39bc6563984c1211a1f949b4b933"
    end

    test "a revision-zero create with a JSON payload" do
      m =
        mutation(%{
          entity_type: "customer",
          entity_id: "customer-0001",
          base_server_revision: 0,
          payload: ~s({"a":1})
        })

      assert Fingerprint.of(m) ==
               "7b5c07b4a322fd2a52bae7fbb15019a5487f92531ae2ea785ccdb3d9c2d124e4"
    end

    test "a delete: same instant in updatedAt and deletedAt, all 256 byte values" do
      # Every byte value, so the payload cannot be valid UTF-8. That is the case
      # that catches an encoder walking bytes instead of code points, and the case
      # where the digest has to come from Python rather than from a re-reading of
      # it -- the golden value below was produced by app/idempotency.py.
      m =
        mutation(%{
          entity_id: "item-2",
          base_server_revision: 7,
          updated_at: "2026-06-15T12:30:45Z",
          deleted_at: "2026-06-15T12:30:45Z",
          payload: Enum.into(0..255, <<>>, fn byte -> <<byte>> end)
        })

      assert byte_size(m.payload) == 256

      assert Fingerprint.of(m) ==
               "33dcf2089a8bb2a8a0aac4fd8397269d788ddb05e948953005c50ac2bbe21cf8"
    end

    test "sub-second precision survives as exactly six fractional digits" do
      m =
        mutation(%{
          entity_type: "delivery_execution",
          entity_id: "de-9",
          base_server_revision: 12_345,
          updated_at: "2026-12-31T23:59:59.500000Z",
          payload: <<0xFF, 0xFE>>
        })

      assert Fingerprint.of(m) ==
               "a98e9f54564bd8f1a613ef3871a99ec081718ff2f96c85122e658b186053e6b7"
    end

    test "quotes, backslashes and control characters in an identifier are escaped" do
      m =
        mutation(%{
          entity_id: ~s(quote"and\\slash),
          base_server_revision: 1,
          updated_at: "2026-03-04T05:06:07Z",
          payload: <<0x00, 0x1F, ?\n, ?\t>>
        })

      assert Fingerprint.of(m) ==
               "be27f78890819f19fa1c8cbdd4f613d75f30123b4a2dc9f58e914999ffb09065"
    end

    test "non-ASCII is escaped as \\uXXXX, matching ensure_ascii" do
      m =
        mutation(%{
          entity_id: "unicode-é中文",
          base_server_revision: 1,
          updated_at: "2026-03-04T05:06:07+00:00",
          payload: "x"
        })

      assert Fingerprint.of(m) ==
               "140fa96ceed24d5e5482cbf02f2b08fe4354f68e9b648168662fbe56bb666a07"
    end

    test "a non-UTC offset normalises to the same instant" do
      # 05:06:07 at -05:00 IS 10:06:07 UTC. Python's isoformat() renders the
      # datetime it parsed, which is UTC -- so the offset style a client uses must
      # not change the fingerprint, or the same instant looks like two mutations.
      m =
        mutation(%{
          entity_id: "offset",
          base_server_revision: 1,
          updated_at: "2026-03-04T05:06:07-05:00",
          payload: "x"
        })

      assert Fingerprint.of(m) ==
               "8a9c98f71b7e66f58a3775cd3b7751fc77a631ce147683f77be38f0d6b9d0298"
    end
  end

  describe "canonical encoding" do
    test "keys are sorted, so map construction order cannot change the result" do
      # Two maps with identical content built in different orders. Without sorting
      # these would fingerprint differently and a legitimate replay would be
      # rejected as a collision -- and the rejection would look like a client bug.
      assert Fingerprint.canonical_bytes(mutation(%{entity_id: "a", base_server_revision: 1})) ==
               Fingerprint.canonical_bytes(mutation(%{base_server_revision: 1, entity_id: "a"}))
    end

    test "the encoding is exactly the six fields, in sorted order" do
      assert Fingerprint.canonical_bytes(mutation(%{})) ==
               ~s({"baseServerRevision":null,"deletedAt":null,"entityID":"item-1",) <>
                 ~s("entityType":"item","payload":"","updatedAt":"2026-01-01T00:00:00+00:00"})
    end
  end

  describe "what counts as the same mutation" do
    test "identical input is the same mutation" do
      assert Fingerprint.same_mutation?(mutation(%{}), mutation(%{}))
    end

    test "a rebased revision is a different intent, even with an identical payload" do
      # The case a payload-only fingerprint gets wrong: the client sent the same
      # bytes, but on the belief it was building on revision 3 rather than 4.
      # Collapsing those two would treat a rebase as a replay.
      refute Fingerprint.same_mutation?(
               mutation(%{base_server_revision: 3}),
               mutation(%{base_server_revision: 4})
             )
    end

    test "one changed payload byte is a different mutation" do
      refute Fingerprint.same_mutation?(mutation(%{payload: "a"}), mutation(%{payload: "b"}))
    end

    test "a retargeted entity is a different mutation" do
      refute Fingerprint.same_mutation?(
               mutation(%{entity_id: "item-1"}),
               mutation(%{entity_id: "item-2"})
             )
    end

    test "the same instant expressed in two offsets is the same mutation" do
      assert Fingerprint.same_mutation?(
               mutation(%{updated_at: "2026-03-04T10:06:07Z"}),
               mutation(%{updated_at: "2026-03-04T05:06:07-05:00"})
             )
    end

    test "a tombstone and a live write are different mutations" do
      refute Fingerprint.same_mutation?(
               mutation(%{deleted_at: "2026-01-01T00:00:00Z"}),
               mutation(%{deleted_at: nil})
             )
    end
  end

  # ---------------------------------------------------------------------------
  # How the reference vectors above were produced, and how to regenerate them.
  # A hand-computed SHA-256 is a test that agrees with whatever the author
  # believed; these came from running the Python implementation.
  #
  #   cd <repo root>
  #   python3 - <<'PY'
  #   import hashlib, json
  #   from datetime import datetime, timezone
  #
  #   def iso(s):
  #       return datetime.fromiso8601(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
  #
  #   material = {
  #       "entityType": "item",
  #       "entityID": "item-1",
  #       "baseServerRevision": None,
  #       "updatedAt": iso("2026-01-01T00:00:00Z"),
  #       "deletedAt": None,
  #       "payload": "",
  #   }
  #   canonical = json.dumps(material, separators=(",", ":"), sort_keys=True).encode()
  #   print(canonical.decode())
  #   print(hashlib.sha256(canonical).hexdigest())
  #   PY
  # ---------------------------------------------------------------------------
end
