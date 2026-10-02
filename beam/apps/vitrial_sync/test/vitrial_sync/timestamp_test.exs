defmodule VitrialSync.TimestampTest do
  @moduledoc """
  The rendering the fingerprint depends on, pinned to Python's `isoformat()`.

  Every expectation here was checked against `datetime.isoformat()` output rather
  than against what Elixir's own `DateTime.to_iso8601/1` produces, because those
  two disagree on the offset spelling and the disagreement is the bug. If this
  module is ever "simplified" to `DateTime.to_iso8601/1`, these tests fail --
  which is the intended outcome, because that simplification changes every
  fingerprint in the system.
  """

  use ExUnit.Case, async: true

  alias VitrialSync.Timestamp

  describe "UTC" do
    test "Z renders as +00:00, never as Z" do
      # Python: datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
      # Elixir:  DateTime.to_iso8601(~U[2026-01-01 00:00:00Z])
      # These differ, and the fingerprint hashes this text.
      assert Timestamp.normalize("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00+00:00"
    end

    test "an explicit +00:00 renders identically" do
      assert Timestamp.normalize("2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"
    end
  end

  describe "fractional seconds" do
    test "zero microseconds are omitted entirely" do
      assert Timestamp.normalize("2026-01-01T00:00:00.000000Z") == "2026-01-01T00:00:00+00:00"
      # Spelled-out zero is the same instant as no fraction at all, and must
      # render identically. This is the case that catches matching the PRECISION
      # instead of the VALUE: a parsed ".000000" carries {0, 6}, and a test on
      # the second element emits ".000000" where isoformat() omits it.
      assert Timestamp.normalize("2026-01-01T00:00:00.000000Z") ==
               Timestamp.normalize("2026-01-01T00:00:00Z")
    end

    test "non-zero microseconds are rendered as exactly six digits" do
      assert Timestamp.normalize("2026-12-31T23:59:59.5Z") == "2026-12-31T23:59:59.500000+00:00"

      assert Timestamp.normalize("2026-12-31T23:59:59.5Z") ==
               Timestamp.normalize("2026-12-31T23:59:59.500000Z")
    end

    test "a short fractional part is padded, not truncated to a different value" do
      assert Timestamp.normalize("2026-01-01T00:00:00.1Z") == "2026-01-01T00:00:00.100000+00:00"
    end
  end

  describe "offsets" do
    test "a negative offset is converted to UTC and rendered as +00:00" do
      # The datetime Python holds after parsing is already UTC, so isoformat()
      # renders the converted wall time, not the offset the client wrote.
      assert Timestamp.normalize("2026-03-04T05:06:07-05:00") == "2026-03-04T10:06:07+00:00"
    end

    test "a positive offset is converted the same way, crossing midnight backwards" do
      # 05:06:07 at +05:30 is 23:36:07 on the *previous* day. Getting this
      # backwards -- by clamping rather than borrowing -- would produce a
      # timestamp that never existed, and a fingerprint of it would match nothing.
      assert Timestamp.normalize("2026-03-04T05:06:07+05:30") == "2026-03-03T23:36:07+00:00"
    end

    test "the same instant in three spellings is one timestamp" do
      # A client varying its offset style between attempts must not produce two
      # different mutations.
      rendered =
        ["2026-03-04T10:06:07Z", "2026-03-04T10:06:07+00:00", "2026-03-04T05:06:07-05:00"]
        |> Enum.map(&Timestamp.normalize/1)
        |> Enum.uniq()

      assert rendered == ["2026-03-04T10:06:07+00:00"]
    end
  end

  describe "rejection" do
    test "a non-timestamp raises rather than hashing something arbitrary" do
      # The alternative -- hashing the raw string -- would give two spellings of
      # the same instant two fingerprints, silently.
      assert_raise ArgumentError, ~r/not an ISO-8601 timestamp/, fn ->
        Timestamp.normalize("not-a-timestamp")
      end

      assert_raise ArgumentError, fn -> Timestamp.normalize("2026-13-45T99:99:99Z") end
    end
  end
end
