defmodule VitrialSync.CanonicalJSONValuesTest do
  @moduledoc """
  The value space beyond the fingerprint's six keys.

  `VitrialSync.CanonicalJSON` started life encoding only what
  `app/idempotency.py` hashes: a flat map of strings, non-negative integers and
  nulls. `VitrialSync.Page` reuses it for stored entity payloads, which come out
  of a PostgreSQL `JSONB` column and therefore contain arrays, booleans, floats
  and negatives. These tests pin the widened behaviour against what CPython's
  `json.dumps(..., separators=(",", ":"), sort_keys=True)` actually produces.
  """

  use ExUnit.Case, async: true

  alias VitrialSync.CanonicalJSON

  describe "booleans and null" do
    test "booleans are literals, never 1 and 0" do
      # A digest that silently rendered `true` as `1` would collide with a
      # payload that genuinely contained the integer.
      assert CanonicalJSON.encode(%{"a" => true}) == ~s({"a":true})
      assert CanonicalJSON.encode(%{"a" => false}) == ~s({"a":false})
      assert CanonicalJSON.encode(%{"a" => nil}) == ~s({"a":null})
    end

    test "a boolean and an integer are distinguishable" do
      refute CanonicalJSON.encode(%{"a" => true}) == CanonicalJSON.encode(%{"a" => 1})
      refute CanonicalJSON.encode(%{"a" => false}) == CanonicalJSON.encode(%{"a" => 0})
    end
  end

  describe "integers" do
    test "negatives are literals, not escaped" do
      assert CanonicalJSON.encode(%{"a" => -42}) == ~s({"a":-42})
    end

    test "arbitrary precision is preserved" do
      big = 123_456_789_012_345_678_901_234_567_890
      assert CanonicalJSON.encode(%{"a" => big}) == ~s({"a":#{big}})
    end

    test "zero is 0, not null" do
      assert CanonicalJSON.encode(%{"a" => 0}) == ~s({"a":0})
    end
  end

  describe "floats" do
    # Expected values are CPython's `repr`, which is what `json.dumps` emits.
    test "an integral float keeps its .0" do
      assert CanonicalJSON.encode(%{"a" => 1.0}) == ~s({"a":1.0})
      assert CanonicalJSON.encode(%{"a" => 100.0}) == ~s({"a":100.0})
      assert CanonicalJSON.encode(%{"a" => -0.0}) == ~s({"a":-0.0})
    end

    test "a fractional float uses the shortest round-trip form" do
      assert CanonicalJSON.encode(%{"a" => 1.5}) == ~s({"a":1.5})
      assert CanonicalJSON.encode(%{"a" => 0.1}) == ~s({"a":0.1})
      assert CanonicalJSON.encode(%{"a" => 3.141592653589793}) == ~s({"a":3.141592653589793})

      assert CanonicalJSON.encode(%{"a" => 123_456_789.123_456_79}) ==
               ~s({"a":123456789.12345679})
    end

    test "scientific notation matches Python, not Erlang" do
      # Erlang's [:short] gives 1.0e22 / 1.0e-5. Python's repr gives 1e+22 /
      # 1e-05: no fractional zero in the mantissa, signed exponent, padded to
      # two digits.
      assert CanonicalJSON.encode(%{"a" => 1.0e22}) == ~s({"a":1e+22})
      assert CanonicalJSON.encode(%{"a" => 1.0e-5}) == ~s({"a":1e-05})
      assert CanonicalJSON.encode(%{"a" => 1.0e-7}) == ~s({"a":1e-07})
      assert CanonicalJSON.encode(%{"a" => 1.0e100}) == ~s({"a":1e+100})
      assert CanonicalJSON.encode(%{"a" => 1.0e16}) == ~s({"a":1e+16})
    end

    test "the fixed/scientific threshold matches Python's" do
      assert CanonicalJSON.encode(%{"a" => 1.0e-4}) == ~s({"a":0.0001})
      assert CanonicalJSON.encode(%{"a" => 1.0e-5}) == ~s({"a":1e-05})
    end

    test "extreme magnitudes still match" do
      assert CanonicalJSON.encode(%{"a" => 1.7976931348623157e308}) ==
               ~s({"a":1.7976931348623157e+308})

      assert CanonicalJSON.encode(%{"a" => 5.0e-324}) == ~s({"a":5e-324})
    end
  end

  describe "arrays" do
    test "an empty array" do
      assert CanonicalJSON.encode(%{"a" => []}) == ~s({"a":[]})
    end

    test "a flat array of mixed types" do
      assert CanonicalJSON.encode(%{"a" => [1, "two", true, nil, 3.5]}) ==
               ~s({"a":[1,"two",true,null,3.5]})
    end

    test "a nested array is encoded in place" do
      assert CanonicalJSON.encode(%{"a" => [1, [2, [3]]]}) == ~s({"a":[1,[2,[3]]]})
    end

    test "an array of objects sorts the keys of each object" do
      assert CanonicalJSON.encode(%{"a" => [%{"b" => 1, "c" => 2}]}) == ~s({"a":[{"b":1,"c":2}]})
    end
  end

  describe "nesting" do
    test "objects inside arrays inside objects" do
      value = %{"z" => [%{"b" => 1, "a" => %{"y" => 2, "x" => [3, 4]}}]}
      assert CanonicalJSON.encode(value) == ~s({"z":[{"a":{"x":[3,4],"y":2},"b":1}]})
    end
  end

  describe "escaping still applies at any depth" do
    test "a string inside an array is escaped" do
      assert CanonicalJSON.encode(%{"a" => ["a\"b"]}) == ~s({"a":["a\\"b"]})
    end

    test "non-ASCII inside an array still becomes \\uXXXX, because this is the canonical escaper" do
      # Deliberately the OPPOSITE of VitrialSync.Page's wire escaper, which keeps
      # non-ASCII raw. Both are correct; they encode for different consumers.
      assert CanonicalJSON.encode(%{"a" => ["café"]}) == ~s({"a":["caf\\u00e9"]})
    end
  end

  describe "Unsupported" do
    test "a term that is not JSON raises rather than being coerced" do
      assert_raise CanonicalJSON.Unsupported, fn -> CanonicalJSON.encode(%{"a" => {:ok, 1}}) end
      assert_raise CanonicalJSON.Unsupported, fn -> CanonicalJSON.encode(%{"a" => :atom}) end
      assert_raise CanonicalJSON.Unsupported, fn -> CanonicalJSON.encode(%{"a" => self()}) end
    end

    test "the error names the path to the offending value" do
      error =
        assert_raise CanonicalJSON.Unsupported, fn ->
          CanonicalJSON.encode(%{"a" => [1, {:bad}]})
        end

      assert error.path == ["a", 1]
    end
  end

  describe "the fingerprint path is unchanged" do
    test "the six-key material still encodes exactly as before" do
      # Regression guard for widening the value space: the fingerprint's golden
      # digests depend on these keys and nothing else.
      assert CanonicalJSON.encode(%{
               "entityType" => "item",
               "entityID" => "i1",
               "baseServerRevision" => nil,
               "updatedAt" => "2026-10-02T00:00:00+00:00",
               "deletedAt" => nil,
               "payload" => "e30="
             }) ==
               ~s({"baseServerRevision":null,"deletedAt":null,"entityID":"i1","entityType":"item","payload":"e30=","updatedAt":"2026-10-02T00:00:00+00:00"})
    end
  end
end
