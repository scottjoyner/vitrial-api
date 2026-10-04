defmodule VitrialWeb.LogFormatTest do
  use ExUnit.Case, async: false

  alias VitrialWeb.LogFormat

  describe "sensitive_key? (app/observability.py:142-149)" do
    test "the compact set matches" do
      for key <- ["Authorization", "x-vitrial-admin-key", "Set-Cookie", "apiKey", "API_KEY"] do
        assert LogFormat.sensitive_key?(key), "#{key} must be sensitive"
      end
    end

    test "suffix rules catch derived names" do
      assert LogFormat.sensitive_key?("authToken")
      assert LogFormat.sensitive_key?("client_secret")
      assert LogFormat.sensitive_key?("X-User-Password")
    end

    test "ordinary keys pass" do
      refute LogFormat.sensitive_key?("routeTemplate")
      refute LogFormat.sensitive_key?("method")
      refute LogFormat.sensitive_key?("statusCode")
    end
  end

  test "redact collapses sensitive values, walks containers, passes scalars" do
    assert LogFormat.redact("secret", "authorization") == "<redacted>"

    assert LogFormat.redact(%{"authorization" => "Bearer x", "method" => "POST"}) == %{
             "authorization" => "<redacted>",
             "method" => "POST"
           }

    assert LogFormat.redact(%{"body" => %{"password" => "x", "n" => 1}}) == %{
             "body" => %{"password" => "<redacted>", "n" => 1}
           }

    # List items recurse WITHOUT a key (app/observability.py:157), so a nested
    # dict keeps its own keys' redaction decisions.
    assert LogFormat.redact([%{"token" => "t"}, 2]) == [%{"token" => "<redacted>"}, 2]
    assert LogFormat.redact("plain", nil) == "plain"
    assert LogFormat.redact(7, "n") == 7
  end

  test "event_map carries the camelCase wire names from metadata" do
    metadata = %{
      request_id: "req-9",
      organization_ref: nil,
      actor_ref: nil,
      membership_ref: nil,
      session_ref: nil,
      device_ref: "sha256:abcd",
      app_version: "1.2.3",
      app_build: "42"
    }

    map =
      LogFormat.event_map(
        "request.completed",
        :info,
        %{
          method: "GET",
          routeTemplate: "/health",
          statusCode: 200,
          durationMs: 1.25
        },
        metadata
      )

    # The wire names, explicitly -- raw metadata keys must never leak.
    assert map["requestID"] == "req-9"
    assert map["deviceRef"] == "sha256:abcd"
    assert map["appVersion"] == "1.2.3"
    assert map["appBuild"] == "42"
    assert map["organizationRef"] == nil
    assert map["event"] == "request.completed"
    assert map["level"] == "info"
    assert map["method"] == "GET"
    assert map["routeTemplate"] == "/health"
    assert map["statusCode"] == 200
    assert map["durationMs"] == 1.25
    assert map["timestamp"] =~ ~r/\+00:00$/
  end

  test "encode emits NDJSON with null for nil, via OTP :json" do
    assert LogFormat.encode(%{"a" => nil, "b" => [1, nil], "c" => "x"}) ==
             ~s({"a":null,"b":[1,null],"c":"x"})
  end

  test "the refused event's line is the reference's shape, including its nulls" do
    line =
      LogFormat.event_map(
        "auth.rate_limit_refused",
        :warning,
        %{routeTemplate: nil, retryAfterSeconds: 3},
        %{}
      )
      |> LogFormat.encode()

    assert line =~ ~s("event":"auth.rate_limit_refused")
    assert line =~ ~s("level":"warning")
    # No correlation ran: requestID and routeTemplate are both null, exactly
    # the measured reference behaviour under the pinned starlette.
    assert line =~ ~s("requestID":null)
    assert line =~ ~s("routeTemplate":null)
    assert line =~ ~s("retryAfterSeconds":3)
  end
end
