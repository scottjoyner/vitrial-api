defmodule VitrialWeb.CorrelationTest do
  use ExUnit.Case, async: false

  alias VitrialWeb.Correlation

  @uuid4_regex ~r/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

  # app/observability.py:76-85 against CPython's uuid.UUID acceptance. The
  # canonical value used throughout is 12345678-1234-5678-1234-567812345678.
  @canonical "12345678-1234-5678-1234-567812345678"

  describe "request_id_for_header: the uuid.UUID parity matrix" do
    test "canonical form re-emits itself" do
      assert Correlation.request_id_for_header(@canonical) == @canonical
    end

    test "uppercase is normalized" do
      assert Correlation.request_id_for_header(String.upcase(@canonical)) == @canonical
    end

    test "hyphenless form is hyphenated" do
      assert Correlation.request_id_for_header(String.replace(@canonical, "-", "")) ==
               @canonical
    end

    test "braced form is unbraced" do
      assert Correlation.request_id_for_header("{#{@canonical}}") == @canonical
    end

    test "urn:uuid: form is stripped" do
      assert Correlation.request_id_for_header("urn:uuid:" <> @canonical) == @canonical
    end

    test "hyphens anywhere are stripped, like CPython's replace('-', '')" do
      assert Correlation.request_id_for_header("12-345678-12-34-5678-1234-567812345678") ==
               @canonical
    end

    test "garbage, empty and nil get a fresh uuid4" do
      for garbage <- [
            "not-a-uuid",
            "",
            nil,
            "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
            @canonical <> "x"
          ] do
        fresh = Correlation.request_id_for_header(garbage)
        assert fresh =~ @uuid4_regex, "garbage #{inspect(garbage)} must yield a fresh uuid4"
      end
    end

    test "a fresh uuid4 carries version 4 and the RFC 4122 variant" do
      for _ <- 1..100 do
        assert Correlation.uuid4() =~ @uuid4_regex
      end
    end
  end

  describe "correlation_ref (app/observability.py:68-73)" do
    test "sha256, first 16 hex, prefixed" do
      # hashlib.sha256(b"device-1").hexdigest()[:16]
      assert Correlation.correlation_ref("device-1") ==
               "sha256:" <> binary_part_of_sha256("device-1")
    end

    test "stable and non-reversible-looking" do
      assert Correlation.correlation_ref("abc") == Correlation.correlation_ref("abc")
      refute Correlation.correlation_ref("abc") =~ "abc"
    end

    test "empty and nil give nil" do
      assert Correlation.correlation_ref("") == nil
      assert Correlation.correlation_ref(nil) == nil
    end
  end

  describe "release_value (app/observability.py:88-97)" do
    test "accepts stripped operational labels" do
      assert Correlation.release_value("1.2.3") == "1.2.3"
      assert Correlation.release_value("  build-9  ") == "build-9"
      assert Correlation.release_value("1.0.0+beta_2") == "1.0.0+beta_2"
    end

    test "rejects free-form log fields" do
      assert Correlation.release_value("has space") == nil
      assert Correlation.release_value("semi;colon") == nil
      assert Correlation.release_value(String.duplicate("a", 65)) == nil
      assert Correlation.release_value("") == nil
      assert Correlation.release_value(nil) == nil
    end

    test "accepts exactly 64 bytes" do
      value = String.duplicate("a", 64)
      assert Correlation.release_value(value) == value
    end
  end

  describe "the metadata lifecycle" do
    test "begin binds, bind fills, end clears" do
      Correlation.begin("req-1")
      Correlation.bind(device_id: "device-1", app_version: "1.0", app_build: nil)

      metadata = :logger.get_process_metadata()
      assert metadata[:request_id] == "req-1"
      assert metadata[:device_ref] == Correlation.correlation_ref("device-1")
      assert metadata[:app_version] == "1.0"
      assert metadata[:app_build] == nil
      # unset principal refs are bound nil, like the contextvar defaults
      assert metadata[:organization_ref] == nil

      # a blank device id strips to nothing -> nil ref (observability.py:130)
      Correlation.bind(device_id: "   ", app_version: " bad version ", app_build: "2.0")
      metadata = :logger.get_process_metadata()
      assert metadata[:device_ref] == nil
      assert metadata[:app_version] == nil
      assert metadata[:app_build] == "2.0"

      Correlation.end_request()
      assert :logger.get_process_metadata() == %{}
    end
  end

  test "elapsed_ms rounds to 2 places like main.py:89" do
    value = Correlation.elapsed_ms(System.monotonic_time(:microsecond))
    assert is_float(value)
    assert value == Float.round(value, 2)
  end

  test "error_type is the exception name, like type(exc).__name__" do
    assert Correlation.error_type(%RuntimeError{message: "x"}) == "RuntimeError"
    assert Correlation.error_type(%FunctionClauseError{}) == "FunctionClauseError"
    assert Correlation.error_type({:exit, :noproc}) == "exit"
  end

  defp binary_part_of_sha256(value) do
    <<digest::binary-size(8), _::binary>> = :crypto.hash(:sha256, value)
    Base.encode16(digest, case: :lower)
  end
end
