defmodule VitrialSync.CanonicalJSONTest do
  @moduledoc """
  The encoder's rules, tested directly rather than only through the fingerprint.

  Every escaping rule here exists to match one specific behaviour of Python's
  `json.dumps(..., ensure_ascii=True)`. The rules are short enough to state
  exhaustively, which is the point: this module is only trustworthy if it is
  boring, and a canonical encoder that is "mostly right" produces digests that
  are "mostly equal", which is the same as useless.
  """

  use ExUnit.Case, async: true

  alias VitrialSync.CanonicalJSON

  describe "structure" do
    test "an empty object is {}" do
      assert CanonicalJSON.encode(%{}) == "{}"
    end

    test "keys are emitted in sorted order regardless of map order" do
      assert CanonicalJSON.encode(%{"b" => 1, "a" => 2, "c" => 3}) ==
               ~s({"a":2,"b":1,"c":3})
    end

    test "sorting is by code point, so uppercase sorts before lowercase" do
      assert CanonicalJSON.encode(%{"b" => 1, "A" => 2}) == ~s({"A":2,"b":1})
    end

    test "there is no insignificant whitespace" do
      encoded = CanonicalJSON.encode(%{"a" => 1, "b" => "x"})

      refute encoded =~ " "
      refute encoded =~ "\n"
    end
  end

  describe "values" do
    test "an integer is a bare decimal literal" do
      assert CanonicalJSON.encode(%{"n" => 0}) == ~s({"n":0})
      assert CanonicalJSON.encode(%{"n" => 12_345}) == ~s({"n":12345})
    end

    test "nil is null" do
      assert CanonicalJSON.encode(%{"n" => nil}) == ~s({"n":null})
    end

    test "a string is quoted" do
      assert CanonicalJSON.encode(%{"s" => "hi"}) == ~s({"s":"hi"})
    end

    test "an empty string is still quoted" do
      assert CanonicalJSON.encode(%{"s" => ""}) == ~s({"s":""})
    end

    test "a value that is not JSON at all raises rather than being coerced" do
      # Coercing a term here would silently produce a digest that differs from
      # Python's without anyone noticing until a replay is wrongly rejected.
      # Failing loudly is the cheap outcome.
      #
      # Floats, negative integers, nested maps and arrays used to be in this
      # list; they are legitimate JSON and `VitrialSync.Page` encodes them for
      # stored entity payloads. See CanonicalJSONValuesTest.
      assert_raise CanonicalJSON.Unsupported, fn -> CanonicalJSON.encode(%{"n" => {:a, 1}}) end
      assert_raise CanonicalJSON.Unsupported, fn -> CanonicalJSON.encode(%{"n" => :atom}) end
      assert_raise CanonicalJSON.Unsupported, fn -> CanonicalJSON.encode(%{"n" => self()}) end
    end

    test "the error names the offending key" do
      assert_raise CanonicalJSON.Unsupported, ~r/entityID/, fn ->
        CanonicalJSON.encode(%{"entityID" => {:a, 1}})
      end
    end
  end

  describe "escaping" do
    test "double quote and backslash are escaped" do
      assert CanonicalJSON.encode(%{"s" => ~s(a"b\\c)}) == ~s({"s":"a\\"b\\\\c"})
    end

    test "Python's short escapes are used, not \\u00XX" do
      assert CanonicalJSON.encode(%{"s" => "\b\f\n\r\t"}) == ~s({"s":"\\b\\f\\n\\r\\t"})
    end

    test "other control characters become \\u00XX" do
      assert CanonicalJSON.encode(%{"s" => <<0x00, 0x1F>>}) == ~s({"s":"\\u0000\\u001f"})
    end

    test "DEL is escaped, because ensure_ascii covers everything above 0x7F" do
      # 0x7F is not a control character in the C0 sense, so a naive guard
      # (`char < 0x20`) lets it through. Python escapes it. This is the exact
      # boundary where such a guard produces a wrong-but-plausible digest.
      assert CanonicalJSON.encode(%{"s" => <<0x7F>>}) == ~s({"s":"\\u007f"})
    end

    test "non-ASCII becomes lowercase \\uXXXX with four digits" do
      assert CanonicalJSON.encode(%{"s" => "é"}) == ~s({"s":"\\u00e9"})
      assert CanonicalJSON.encode(%{"s" => "中"}) == ~s({"s":"\\u4e2d"})
    end

    test "a code point above the BMP is escaped as a surrogate pair" do
      # Python emits two \\uXXXX escapes. Elixir's <<char::utf8>> walks code
      # points, so an emoji arrives here as one value and must be re-expanded,
      # or the digest silently differs for exactly the inputs most likely to be
      # human-authored.
      assert CanonicalJSON.encode(%{"s" => "😀"}) == ~s({"s":"\\ud83d\\ude00"})
    end

    test "the solidus is NOT escaped" do
      assert CanonicalJSON.encode(%{"s" => "a/b"}) == ~s({"s":"a/b"})
    end

    test "every key in the short-escape table is an integer" do
      # Regression. The quote and backslash entries were once written as the
      # strings `"\""` and `"\\"` while the rest were character literals, so the
      # table held mixed key types. `escape_char/1` is called with an integer --
      # a character off a binary -- and `Map.fetch/2` never matches a string key,
      # so both escapes were SILENTLY SKIPPED. Nothing raised; every string
      # containing a quote or a backslash just fingerprinted differently from
      # Python. The character literal `?"` cannot be used to avoid this: Elixir
      # lexes it as the opening of a string literal, which is a parse error at
      # the next `?:` on the line, reported at a column nowhere near the cause.
      for {key, escaped} <- Map.to_list(CanonicalJSON.short_escapes()) do
        assert is_integer(key), "short-escape table key #{inspect(key)} is not an integer"
        assert CanonicalJSON.encode(%{"s" => <<key>>}) == ~s({"s":"#{escaped}"})
      end
    end

    test "escaping applies to keys as well as values" do
      assert CanonicalJSON.encode(%{"a\"b" => 1}) == ~s({"a\\"b":1})
    end
  end

  describe "digest/1 and sha256_hex/1" do
    test "digest of an object equals sha256 of its encoding" do
      object = %{"a" => 1, "b" => "x"}

      assert CanonicalJSON.digest(object) ==
               CanonicalJSON.sha256_hex(CanonicalJSON.encode(object))
    end

    test "the digest is lowercase hex of the right width" do
      digest = CanonicalJSON.digest(%{"a" => 1})

      assert String.length(digest) == 64
      assert digest == String.downcase(digest)
      assert Regex.match?(~r/\A[0-9a-f]{64}\z/, digest)
    end

    test "sha256_hex of the empty binary is the well-known empty digest" do
      assert CanonicalJSON.sha256_hex(<<>>) ==
               "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    end
  end
end
