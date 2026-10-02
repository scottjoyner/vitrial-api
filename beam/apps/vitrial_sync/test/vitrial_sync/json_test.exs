defmodule VitrialSync.JSONTest do
  use ExUnit.Case, async: true

  alias VitrialSync.JSON

  describe "objects" do
    test "an empty object is an empty map, not nil" do
      assert JSON.decode("{}") == {:ok, %{}}
    end

    test "nested objects and arrays" do
      assert JSON.decode(~s({"a": [1, 2], "b": {"c": null}})) ==
               {:ok, %{"a" => [1, 2], "b" => %{"c" => nil}}}
    end

    test "duplicate keys resolve last-one-wins, matching CPython's dict assignment" do
      assert JSON.decode(~s({"a": 1, "a": 2})) == {:ok, %{"a" => 2}}
    end

    test "a key's position is not observable, so out-of-order keys still compare equal" do
      assert JSON.decode(~s({"z": 1, "a": 2})) == JSON.decode(~s({"a": 2, "z": 1}))
    end
  end

  describe "whitespace" do
    test "is permitted before a value, after a separator, and around a colon" do
      assert JSON.decode("  \t\r\n {\n\t\"a\" :\t 1 \r\n} \n") == {:ok, %{"a" => 1}}
    end

    test "is permitted between array elements" do
      assert JSON.decode("[ 1 , 2 ,\n3 ]") == {:ok, [1, 2, 3]}
    end
  end

  describe "strings" do
    test "the two-character escapes" do
      assert JSON.decode(~S("\"\\\/")) == {:ok, "\"\\/"}
    end

    test "the single-character escapes, in the order CPython emits them" do
      assert JSON.decode(~S("\b\f\n\r\t")) == {:ok, "\b\f\n\r\t"}
    end

    test "unicode escapes" do
      assert JSON.decode(~S("\u0041")) == {:ok, "A"}
      assert JSON.decode(~S("\u00e9")) == {:ok, "é"}
      assert JSON.decode(~S("\u20ac")) == {:ok, "€"}
    end

    test "a surrogate pair combines into one codepoint" do
      assert JSON.decode(~S("\ud83d\ude00")) == {:ok, "😀"}
    end

    test "a literal multibyte character matches its escape" do
      assert JSON.decode(~s("😀")) == JSON.decode(~S("\ud83d\ude00"))
    end

    test "an escaped NUL is a value, not a terminator" do
      assert JSON.decode(~S("\u0000")) == {:ok, <<0>>}
    end

    test "a raw control character is a syntax error" do
      assert JSON.decode(<<0x22, 0x01, 0x22>>) == {:error, :control_character_in_string}
    end
  end

  describe "numbers" do
    test "an integer literal stays an integer" do
      assert JSON.decode("42") == {:ok, 42}
      assert JSON.decode("-42") == {:ok, -42}
    end

    test "an arbitrary-precision integer is not truncated" do
      assert JSON.decode("123456789012345678901234567890") ==
               {:ok, 123_456_789_012_345_678_901_234_567_890}
    end

    test "a fraction or exponent makes it a float" do
      assert JSON.decode("1.5") == {:ok, 1.5}
      assert JSON.decode("1e2") == {:ok, 100.0}
      assert JSON.decode("1E2") == {:ok, 100.0}
      assert JSON.decode("1e+2") == {:ok, 100.0}
      assert JSON.decode("1e-2") == {:ok, 0.01}
      assert JSON.decode("-0.0") == {:ok, -0.0}
    end

    test "the integer/float split follows the literal's shape, not its value" do
      # `1e2` is 100 either way, but it is a float in JSON and has to stay one:
      # a revision that arrives as 100.0 where the other estate sees 100 is a
      # digest mismatch waiting to happen.
      assert JSON.decode("1e2") == {:ok, 100.0}
      assert is_float(elem(JSON.decode("1e2"), 1))
      assert is_integer(elem(JSON.decode("100"), 1))
    end

    test "malformed numbers are rejected, including the ones a permissive scanner accepts" do
      for literal <- ["01", "00", "1.", ".1", "+1", "1e", "1.2.3", "-", "1e+"] do
        assert {:error, reason} = JSON.decode(literal), "expected #{literal} to be rejected"
        assert reason in [:invalid_number, :unexpected_byte, :unexpected_end, :trailing_data]
      end
    end
  end

  describe "literals" do
    test "true, false and null" do
      assert JSON.decode("true") == {:ok, true}
      assert JSON.decode("false") == {:ok, false}
      assert JSON.decode("null") == {:ok, nil}
    end

    test "a literal prefix is not a literal" do
      assert JSON.decode("truely") == {:error, :trailing_data}
      assert JSON.decode("nullx") == {:error, :trailing_data}
    end

    test "NaN and Infinity are rejected, narrowing CPython's default" do
      assert JSON.decode("NaN") == {:error, :unexpected_byte}
      assert JSON.decode("Infinity") == {:error, :unexpected_byte}
      assert JSON.decode("-Infinity") == {:error, :invalid_number}
      assert JSON.decode(~s({"a": NaN})) == {:error, :unexpected_byte}
    end

    test "a finite literal that overflows to infinity is rejected too" do
      # CPython yields float('inf') here. Erlang's float conversion refuses to
      # represent it, and PostgreSQL JSONB would reject it downstream anyway.
      assert JSON.decode(~s({"a": 1e999})) == {:error, :invalid_number}
    end
  end

  describe "byte-order marks" do
    test "a UTF-8 BOM is stripped" do
      assert JSON.decode(<<0xEF, 0xBB, 0xBF, 0x7B, 0x7D>>) == {:ok, %{}}
    end

    test "UTF-16 little-endian, little-endian BOM" do
      body =
        <<0x7B, 0x00, 0x22, 0x00, 0x61, 0x00, 0x22, 0x00, 0x3A, 0x00, 0x31, 0x00, 0x7D, 0x00>>

      assert JSON.decode(<<0xFF, 0xFE>> <> body) == {:ok, %{"a" => 1}}
    end

    test "UTF-16 big-endian, big-endian BOM" do
      body =
        <<0x00, 0x7B, 0x00, 0x22, 0x00, 0x61, 0x00, 0x22, 0x00, 0x3A, 0x00, 0x31, 0x00, 0x7D>>

      assert JSON.decode(<<0xFE, 0xFF>> <> body) == {:ok, %{"a" => 1}}
    end

    test "a UTF-32 mark is not mistaken for a UTF-16 one" do
      # The four-byte UTF-32 little-endian BOM begins with the same two bytes as
      # the UTF-16 one. Testing the shorter mark first truncates the document.
      body =
        <<0x7B, 0, 0, 0, 0x22, 0, 0, 0, 0x61, 0, 0, 0, 0x22, 0, 0, 0, 0x3A, 0, 0, 0, 0x31, 0, 0,
          0, 0x7D, 0, 0, 0>>

      assert JSON.decode(<<0xFF, 0xFE, 0x00, 0x00>> <> body) == {:ok, %{"a" => 1}}
    end

    test "a BOM promising an encoding the bytes do not honour is an error, not a prefix" do
      assert JSON.decode(<<0xFF, 0xFE, " not json">>) == {:error, :invalid_encoding}
    end
  end

  describe "rejection" do
    test "empty and whitespace-only input" do
      assert JSON.decode("") == {:error, :empty}
      assert JSON.decode("   \n\t") == {:error, :empty}
    end

    test "trailing content" do
      assert JSON.decode("{} {}") == {:error, :trailing_data}
      assert JSON.decode("{}x") == {:error, :trailing_data}
    end

    test "truncated documents" do
      assert JSON.decode(~s({"a")) == {:error, :unexpected_end}
      assert JSON.decode(~s({"a":1)) == {:error, :unexpected_end}
      assert JSON.decode("[1,") == {:error, :unexpected_end}
      assert JSON.decode(~s('{"a": "unterminated)) == {:error, :unexpected_end}
    end

    test "a trailing comma is not permitted" do
      assert JSON.decode(~s({"a":1,})) == {:error, :unexpected_byte}
      assert JSON.decode("[1,]") == {:error, :unexpected_byte}
    end

    test "single quotes are not JSON" do
      assert JSON.decode("{'a':1}") == {:error, :unexpected_byte}
    end

    test "unpaired surrogates are rejected, narrowing CPython" do
      for literal <- [~S("\ud800"), ~S("\udc00"), ~S("\ud800x"), ~S("\ud800\ud800")] do
        assert JSON.decode(literal) == {:error, :unpaired_surrogate}
      end
    end

    test "an invalid escape" do
      assert JSON.decode(~S("\q")) == {:error, :invalid_escape}
      assert JSON.decode(~S("\x41")) == {:error, :invalid_escape}
      assert JSON.decode(~S("\u00")) == {:error, :invalid_unicode_escape}
      assert JSON.decode(~S("\uZZZZ")) == {:error, :invalid_unicode_escape}
    end

    test "nesting past the depth ceiling is an error rather than a stack overflow" do
      deep = String.duplicate("[", 1500) <> String.duplicate("]", 1500)
      assert JSON.decode(deep) == {:error, :too_deep}
    end

    test "nesting within the ceiling still decodes" do
      assert {:ok, nested} = JSON.decode(String.duplicate("[", 50) <> String.duplicate("]", 50))
      assert is_list(nested)
    end
  end

  describe "decode_object/1" do
    test "accepts an object" do
      assert JSON.decode_object(~s({"a": 1})) == {:ok, %{"a" => 1}}
    end

    test "refuses a non-object" do
      assert JSON.decode_object("[1,2]") == {:error, :unexpected_byte}
      assert JSON.decode_object("42") == {:error, :unexpected_byte}
    end
  end
end
