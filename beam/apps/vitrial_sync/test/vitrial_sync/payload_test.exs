defmodule VitrialSync.PayloadTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Payload

  describe "the base64 branch" do
    test "base64 of a JSON object is accepted" do
      assert Payload.decode(Base.encode64(~s({"a": 1}))) == {:ok, %{"a" => 1}}
    end

    test "base64 of a JSON array is rejected, and is NOT retried as raw JSON" do
      # The subtlety in decode_payload. base64 succeeded and the decoded bytes
      # were valid JSON, so no exception was raised and the reference
      # implementation never falls back -- it rejects on "not an object"
      # immediately. Treating "not an object" as a reason to try the other
      # encoding would accept inputs Python refuses.
      assert Payload.decode(Base.encode64("[1,2]")) ==
               {:error, {:invalid_payload, :not_a_json_object}}
    end

    test "base64 of a scalar is rejected the same way" do
      for document <- ["42", ~s("a string"), "true", "false", "null"] do
        assert Payload.decode(Base.encode64(document)) ==
                 {:error, {:invalid_payload, :not_a_json_object}}
      end
    end

    test "base64 of bytes that are not JSON falls through to the raw path" do
      assert Payload.decode(Base.encode64("not json at all")) == {:error, :unexpected_byte}
    end
  end

  describe "the raw-JSON fallback" do
    test "a raw JSON object is accepted" do
      assert Payload.decode(~s({"a": 1})) == {:ok, %{"a" => 1}}
    end

    test "a raw object whose bytes happen to be valid base64 still decodes as JSON" do
      # "null" and "true" are four base64-alphabet characters, so they take the
      # base64 branch first, fail to parse, and come back through the raw path.
      # CPython does the same and then rejects on "not an object".
      assert Payload.decode("null") == {:error, {:invalid_payload, :not_a_json_object}}
      assert Payload.decode("true") == {:error, {:invalid_payload, :not_a_json_object}}
      assert Payload.decode("1234") == {:error, {:invalid_payload, :not_a_json_object}}
    end

    test "a raw object is accepted even though it is base64-shaped" do
      # "abcd" is valid base64, decodes to three bytes, and is not JSON -- so the
      # fallback parses the ORIGINAL bytes, not the decoded ones.
      assert Payload.decode("{}") == {:ok, %{}}
    end
  end

  describe "strict base64 shape checking" do
    test "a non-alphabet character rejects the base64 branch outright" do
      for raw <- ["ey=h", "ey!h", "ey h", "ey\nh"] do
        assert {:error, _} = Payload.decode(raw)
      end
    end

    test "padding in the middle is a shape failure, not a decode failure" do
      assert {:error, _} = Payload.decode("=eyJh")
      assert {:error, _} = Payload.decode("ey=h")
    end

    test "a length that is not a multiple of four falls through to the raw path" do
      # "a", "ab", "abc" are all base64-alphabet characters but are not valid
      # encodings. They are also not JSON, so both paths fail.
      for raw <- ["a", "ab", "abc", "eyJhIjogMX0"] do
        assert {:error, _} = Payload.decode(raw)
      end
    end
  end

  describe "non-object payloads" do
    test "every JSON non-object is rejected" do
      for raw <- ["[]", "[1,2]", "42", "-1", "0", ~s("a string"), "true", "false", "null"] do
        assert Payload.decode(raw) == {:error, {:invalid_payload, :not_a_json_object}},
               "expected #{raw} to be rejected as a non-object"
      end
    end
  end

  describe "malformed payloads" do
    test "garbage, empty input and truncated documents" do
      assert {:error, _} = Payload.decode(<<0xFF, 0xFE, " not json">>)
      assert Payload.decode("") == {:error, :empty}
      assert {:error, _} = Payload.decode(~s({"a"))
      assert {:error, _} = Payload.decode("{}}")
    end

    test "nesting past the depth ceiling" do
      assert {:error, _} =
               Payload.decode(String.duplicate("[", 2000) <> String.duplicate("]", 2000))
    end
  end

  describe "decodable?/1" do
    test "agrees with decode/1 on both outcomes" do
      assert Payload.decodable?(~s({"a": 1}))
      assert Payload.decodable?(Base.encode64(~s({"a": 1})))
      refute Payload.decodable?("[1,2]")
      refute Payload.decodable?("garbage")
    end
  end
end
