defmodule VitrialSync.CursorTest do
  @moduledoc """
  The cursor bound, tested from both sides.

  The upper cases exist because Erlang integers are arbitrary-precision: without
  an explicit ceiling this module would happily accept, store and forward a
  value that `SyncChangeLog.sequence` cannot hold, and the failure would surface
  as a driver exception rather than as a client error. The lower cases pin the
  fact that a large-but-reachable cursor is still accepted, so the ceiling cannot
  be quietly tightened into a range that excludes real clients.
  """

  use ExUnit.Case, async: true

  doctest VitrialSync.Cursor

  alias VitrialSync.Cursor

  describe "reachable positions" do
    test "the origin is a valid position" do
      assert Cursor.decode("seq:0") == {:ok, 0}
    end

    test "an ordinary position decodes" do
      assert Cursor.decode("seq:4096") == {:ok, 4096}
    end

    test "the largest BIGINT is still admitted" do
      # Inclusive on purpose. A client sitting at the last sequence of a
      # long-lived change log must still be able to resume; the ceiling rejects
      # positions that do not exist, not large ones.
      assert Cursor.max_sequence() == 9_223_372_036_854_775_807
      assert Cursor.decode("seq:#{Cursor.max_sequence()}") == {:ok, Cursor.max_sequence()}
    end

    test "the declared digit width is the width of the ceiling" do
      assert Cursor.max_digits() == length(Integer.to_string(Cursor.max_sequence()))
    end
  end

  describe "unreachable positions" do
    test "one past the ceiling is refused" do
      assert {:error, rejected} = Cursor.decode("seq:9223372036854775808")
      assert rejected.reason == :above_max_sequence
    end

    test "a 20-digit value is refused on width before it is parsed" do
      # Width first, value second. A 20-digit string is refused even when the
      # value it denotes would be in range after leading zeros, so the parse
      # cost is bounded by the length of a real cursor.
      assert {:error, rejected} = Cursor.decode("seq:99999999999999999999")
      assert rejected.reason == :too_wide
    end

    test "a padded in-range value is still refused, not normalised" do
      # Silently stripping leading zeros would let a caller send a cursor whose
      # text does not match any position it claims.
      assert {:error, %{reason: :too_wide}} = Cursor.decode("seq:" <> String.duplicate("0", 40) <> "1")
    end
  end

  describe "malformed cursors" do
    test "a negative sequence is not a position" do
      assert {:error, %{reason: :not_a_decimal_integer}} = Cursor.decode("seq:-1")
    end

    test "a decimal point is not an integer" do
      assert {:error, %{reason: :not_a_decimal_integer}} = Cursor.decode("seq:1.0")
    end

    test "hexadecimal is not decimal" do
      assert {:error, %{reason: :not_a_decimal_integer}} = Cursor.decode("seq:0x10")
    end

    test "an empty body is not a position" do
      assert {:error, %{reason: :not_a_decimal_integer}} = Cursor.decode("seq:")
    end

    test "the wrong tag is refused, and the original string is preserved for the log" do
      assert {:error, rejected} = Cursor.decode("sequence:1")
      assert rejected.reason == :missing_prefix
      assert rejected.cursor == "sequence:1"
    end

    test "a bare sequence with no tag is refused" do
      assert {:error, %{reason: :missing_prefix}} = Cursor.decode("1")
    end

    test "trailing whitespace does not survive as a position" do
      assert {:error, %{reason: :not_a_decimal_integer}} = Cursor.decode("seq:1 ")
    end

    test "injection-shaped input is refused on the decimal check" do
      # The digit check runs before any parse, so this never reaches Integer.parse.
      assert {:error, %{reason: :not_a_decimal_integer}} = Cursor.decode("seq:1 OR 1=1")
    end
  end

  describe "decode_optional/1" do
    test "an absent cursor means position zero, not an error" do
      # A freshly paired device legitimately holds nothing. Reporting that as a
      # malformed cursor would fail the one request that is certainly valid.
      assert Cursor.decode_optional(nil) == {:ok, 0}
    end

    test "a present cursor is decoded, and a bad one still fails" do
      assert Cursor.decode_optional("seq:12") == {:ok, 12}
      assert {:error, %{reason: :not_a_decimal_integer}} = Cursor.decode_optional("seq:-1")
    end
  end

  describe "round trip" do
    test "every value this library encodes decodes back to itself" do
      for position <- [0, 1, 42, 65_535, 1_000_000, Cursor.max_sequence()] do
        assert position |> Cursor.encode() |> Cursor.decode() == {:ok, position}
      end
    end
  end
end