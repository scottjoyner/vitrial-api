defmodule VitrialWeb.EndpointTest do
  # Integration tests against the REAL listener: `test.exs` binds port 0, the
  # port actually bound is read back from ranch, and every request here goes
  # through the whole pipeline -- metrics, size guard, limiter, correlation,
  # router -- exactly as a client drives it. These are the measured reference
  # invariants the unit suites cannot see end to end.
  use ExUnit.Case, async: false

  import ExUnit.CaptureLog

  alias VitrialWeb.{Listener, Metrics, RateLimit}

  @uuid4_regex ~r/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
  @canonical "12345678-1234-5678-1234-567812345678"

  # --- helpers ----------------------------------------------------------------

  defp port, do: Listener.port()

  # A raw HTTP/1.1 client: one connection per request, `connection: close`, the
  # response read to EOF. Every assertion below sees the exact wire bytes.
  defp request(method, path, headers \\ [], body \\ nil) do
    body = body || ""
    host = "127.0.0.1:#{port()}"

    header_lines =
      (headers ++ [{"host", host}, {"content-length", Integer.to_string(byte_size(body))}])
      |> Enum.map(fn {k, v} -> "#{k}: #{v}" end)
      |> Enum.join("\r\n")

    payload = "#{method} #{path} HTTP/1.1\r\n#{header_lines}\r\nconnection: close\r\n\r\n" <> body

    {:ok, sock} =
      :gen_tcp.connect(~c"127.0.0.1", port(), [:binary, active: false, buffer: 65_536])

    :ok = :gen_tcp.send(sock, payload)
    response = drain(sock)
    :gen_tcp.close(sock)

    parse_response(response)
  end

  defp drain(sock, acc \\ []) do
    case :gen_tcp.recv(sock, 0, 2_000) do
      {:ok, data} -> drain(sock, [data | acc])
      {:error, _reason} -> acc |> Enum.reverse() |> IO.iodata_to_binary()
    end
  end

  defp parse_response(response) do
    [head, body] = :binary.split(response, "\r\n\r\n")
    [status_line | header_lines] = String.split(head, "\r\n")

    headers =
      header_lines
      |> Enum.map(fn line -> :binary.split(line, ": ") end)
      |> Map.new(fn
        [k, v] -> {String.downcase(k), v}
        [k] -> {String.downcase(k), ""}
      end)

    status =
      status_line
      |> String.split(" ")
      |> Enum.at(1)
      |> String.to_integer()

    {status, headers, body}
  end

  defp post_pair(body), do: request("POST", "/api/v1/auth/pair", [], body)

  # --- the two servable routes --------------------------------------------------

  test "GET /health is exactly {\"status\":\"ok\"}" do
    {status, headers, body} = request("GET", "/health")

    assert status == 200
    assert body == ~s({"status":"ok"})
    assert headers["content-type"] == "application/json"
    assert headers["x-request-id"] =~ @uuid4_regex
  end

  test "GET /api/v1/version is exactly the configured v1 version response" do
    {status, _headers, body} = request("GET", "/api/v1/version")

    assert status == 200
    assert body == ~s({"apiVersion":"v1","serviceVersion":"0.3.0"})
  end

  test "HEAD /health is a GET-shaped 200 with no body (cowboy suppresses it)" do
    {status, headers, body} = request("HEAD", "/health")

    assert status == 200
    assert body == ""
    assert headers["content-type"] == "application/json"
  end

  # --- routing boundaries ---------------------------------------------------------

  test "unmatched path is 404 with the reference's body, and still echoes a request id" do
    {status, headers, body} = request("GET", "/definitely-not-a-route")

    assert status == 404
    assert body == ~s({"detail":"Not Found"})
    assert headers["x-request-id"] =~ @uuid4_regex
  end

  test "known path with a wrong method is 405 with Allow and the reference's body" do
    {status, headers, body} = request("DELETE", "/health")

    assert status == 405
    assert body == ~s({"detail":"Method Not Allowed"})
    assert headers["allow"] == "GET, HEAD"
    assert headers["x-request-id"] =~ @uuid4_regex
  end

  test "trailing slash is a 307 to the absolute flipped URL" do
    {status, headers, body} = request("GET", "/health/")

    assert status == 307
    assert headers["location"] == "http://127.0.0.1:#{port()}/health"
    assert body == ""
  end

  test "every unwired clause answers the ONE uniform 501" do
    # Every clause of the 23-row table except the two servable ones, each driven
    # with its DECLARED method -- a wrong method would answer 405, not 501.
    for {method, path} <- [
          {"GET", "/ready"},
          {"GET", "/metrics"},
          {"GET", "/api/v1/auth/me"},
          {"POST", "/api/v1/auth/pair"},
          {"POST", "/api/v1/auth/logout"},
          {"POST", "/api/v1/sync/push"},
          {"GET", "/api/v1/sync/pull"},
          {"HEAD", "/api/v1/sync/evidence-blobs/doc-1"},
          {"PUT", "/api/v1/sync/evidence-blobs/doc-1"},
          {"GET", "/api/v1/sync/evidence-blobs/doc-1"},
          {"POST", "/internal/admin/v1/bootstrap"},
          {"POST", "/internal/admin/v1/pairing-grants"},
          {"POST", "/internal/admin/v1/sessions/s-1/revoke"},
          {"GET", "/api/v1/reference-data/manifest"},
          {"GET", "/api/v1/reference-data/current"},
          {"GET", "/api/v1/reference-data/publications"},
          {"GET", "/api/v1/reference-data/publications/pub-1"},
          {"POST", "/api/v1/reference-data/publications"},
          {"GET", "/api/v2/version"},
          {"POST", "/api/v2/sync/push"},
          {"GET", "/api/v2/sync/pull"}
        ] do
      {status, _headers, body} = request(method, path)

      assert status == 501,
             "#{method} #{path} answered #{status}; must answer the uniform not-wired response"

      # Cowboy suppresses the body of a HEAD response; the status and the
      # content-length header still carry the shape.
      unless method == "HEAD" do
        assert body == ~s({"detail":"not implemented"}),
               "#{method} #{path} must keep the fixed body"
      end
    end
  end

  # --- the request-id contract ------------------------------------------------------

  test "X-Request-ID echoes the normalized form for every uuid.UUID-acceptable shape" do
    cases = [
      {@canonical, @canonical},
      {String.upcase(@canonical), @canonical},
      {String.replace(@canonical, "-", ""), @canonical},
      {"{#{@canonical}}", @canonical},
      {"urn:uuid:" <> @canonical, @canonical}
    ]

    for {sent, expected} <- cases do
      {_status, headers, _body} = request("GET", "/health", [{"x-request-id", sent}])
      assert headers["x-request-id"] == expected, "sent #{inspect(sent)}"
    end
  end

  test "garbage in the header yields a fresh uuid4, never an echo of the garbage" do
    {_status, headers, _body} = request("GET", "/health", [{"x-request-id", "garbage ;; value"}])

    assert headers["x-request-id"] =~ @uuid4_regex
  end

  # --- the size guard (413: the declared arm and the counted arm) -------------------

  @pair_limit 16 * 1024

  # The reference's own suite drives the "body that lies about its length" case
  # at the ASGI receive layer (tests/test_request_size.py:160-178), where the
  # receive channel genuinely delivers every streamed byte. Over real HTTP/1.1
  # the FRAMING owns a content-length: a lying header makes the extra bytes
  # another request's problem, in both implementations. The wire-drivable
  # counted arm is chunked transfer with no content-length at all -- which is
  # also the only way the summing-across-chunks rule (:202-224) can fire.

  test "413, counted-streaming arm: chunked body with no content-length, chunks that each fit" do
    # Mirrors test_chunked_body_with_no_content_length_is_cut_off: three chunks,
    # each under the limit, summing past it. Only counting across chunks catches
    # this.
    {status, headers, resp_body} =
      chunked_pair_request([
        String.duplicate("x", 8000),
        String.duplicate("x", 8000),
        String.duplicate("x", 8000)
      ])

    assert status == 413
    assert resp_body == ~s({"detail":"request body too large"})
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert headers["x-vitrial-max-body-bytes"] == Integer.to_string(@pair_limit)
    # Pre-correlation refusal: no request id, by measured reference behaviour.
    assert headers["x-request-id"] == nil
  end

  test "413, counted-streaming arm: a single chunk over the limit is caught one byte past it" do
    {status, _headers, resp_body} = chunked_pair_request([String.duplicate("x", 50_000)])

    assert status == 413
    assert resp_body == ~s({"detail":"request body too large"})
  end

  defp chunked_pair_request(chunks) do
    chunked =
      Enum.map_join(chunks, fn chunk ->
        "#{Integer.to_string(byte_size(chunk), 16)}\r\n#{chunk}\r\n"
      end) <> "0\r\n\r\n"

    {:ok, sock} =
      :gen_tcp.connect(~c"127.0.0.1", port(), [:binary, active: false, buffer: 65_536])

    :ok =
      :gen_tcp.send(
        sock,
        "POST /api/v1/auth/pair HTTP/1.1\r\nhost: 127.0.0.1\r\ntransfer-encoding: chunked\r\nconnection: close\r\n\r\n" <>
          chunked
      )

    response = drain(sock)
    :gen_tcp.close(sock)
    parse_response(response)
  end

  # --- the rate limiter (429 ladder) -------------------------------------------------

  test "the 429 ladder: allowed through the limit, refused with the measured shape and event" do
    RateLimit.reset()

    log =
      capture_log(fn ->
        result =
          Enum.reduce_while(1..40, :none, fn i, _ ->
            case post_pair("{}") do
              {429, headers, body} -> {:halt, {i, headers, body}}
              _ -> {:cont, :none}
            end
          end)

        send(self(), {:ladder, result})
      end)

    {refusal_index, headers, body} =
      receive do
        {:ladder, {i, headers, body}} -> {i, headers, body}
        {:ladder, :none} -> flunk("never refused within 40 requests")
      end

    assert refusal_index <= 31, "first refusal at #{refusal_index}, default limit is 30"

    # The measured header set: retry-after + cache-control + content-type +
    # content-length, with NO X-Request-ID.
    assert headers["retry-after"] =~ ~r/^[1-9][0-9]*$/
    assert headers["cache-control"] == "no-store"
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == "30"
    assert headers["x-request-id"] == nil
    assert body == ~s({"detail":"too many requests"})

    # The refused event: requestID null and routeTemplate null (routing never
    # ran), the behaviour the reference's own docstring contradicts.
    assert log =~ ~s("event":"auth.rate_limit_refused")
    assert log =~ ~s("requestID":null)
    assert log =~ ~s("routeTemplate":null)
  after
    RateLimit.reset()
  end

  test "throttling is scoped to the pairing path: /health is never limited" do
    RateLimit.reset()

    results =
      for _ <- 1..35 do
        request("GET", "/health")
      end

    assert Enum.all?(results, fn {status, _, _} -> status == 200 end)
  after
    RateLimit.reset()
  end

  test "a limiter fault fails OPEN: bypassed log event, request proceeds to the router" do
    RateLimit.reset()

    log =
      capture_log(fn ->
        :ok = Supervisor.terminate_child(VitrialWeb.Supervisor, RateLimit)

        {status, headers, _body} = post_pair("{}")

        assert status == 501, "fail-open must reach the router, not refuse"
        assert headers["x-request-id"] =~ @uuid4_regex
      end)

    assert log =~ ~s("event":"auth.rate_limit_bypassed")
  after
    case Supervisor.restart_child(VitrialWeb.Supervisor, RateLimit) do
      {:ok, _pid} -> :ok
      {:error, :already_present} -> :ok
      other -> flunk("could not restore the limiter child: #{inspect(other)}")
    end
  end

  # --- correlation events on the wire ---------------------------------------------

  test "request.started and request.completed carry the template and status" do
    RateLimit.reset()

    # config/test.exs holds the primary level at :warning; CaptureLog's :level
    # option does NOT override the primary level (it filters the capture), so
    # the :info events must be re-enabled the real way, and restored.
    previous_level = Logger.level()

    try do
      log =
        capture_log(fn ->
          Logger.configure(level: :info)

          {status, headers, _body} = request("GET", "/health")
          assert status == 200
          send(self(), {:rid, headers["x-request-id"]})
        end)

      rid =
        receive do
          {:rid, rid} -> rid
        end

      assert log =~ ~s("event":"request.started")
      assert log =~ ~s("event":"request.completed")
      assert log =~ ~s("routeTemplate":"/health")
      assert log =~ ~s("statusCode":200)
      assert log =~ ~s("requestID":"#{rid}")
    after
      Logger.configure(level: previous_level)
    end
  end

  # --- the metrics stage ------------------------------------------------------------

  test "metrics label by matched template, with one shared unmatched bucket" do
    RateLimit.reset()

    # The registry accumulates across the whole run, but this test must not
    # depend on test ORDER for its buckets: it produces its own 413 and 429.
    assert {413, _, _} =
             chunked_pair_request([
               String.duplicate("x", 8000),
               String.duplicate("x", 8000),
               String.duplicate("x", 8000)
             ])

    capture_log(fn ->
      Enum.reduce_while(1..40, :none, fn _i, _ ->
        case post_pair("{}") do
          {429, _, _} -> {:halt, :refused}
          _ -> {:cont, :none}
        end
      end)
    end)

    request("GET", "/health")
    request("GET", "/nope")
    request("DELETE", "/health")
    post_pair("{}")

    snapshot = Metrics.snapshot()

    health = snapshot[{"/health", "GET"}]
    assert health.classes["2xx"] >= 1
    assert health.duration_count >= 1
    assert health.duration_total_ms >= 0.0

    # 404, 413 and 429 all happen before routing matched anything.
    unmatched_get = snapshot[{"unmatched", "GET"}]
    assert unmatched_get.classes["4xx"] >= 1

    unmatched_post = snapshot[{"unmatched", "POST"}]
    assert unmatched_post.statuses[413] >= 1
    assert unmatched_post.statuses[429] >= 1

    # A 405 keeps the template of the partial match (starlette stamps
    # scope["route"] with it).
    not_allowed = snapshot[{"/health", "DELETE"}]
    assert not_allowed.statuses[405] >= 1

    # A wired-or-not routed request labels by TEMPLATE, never by raw path.
    wired = snapshot[{"/api/v1/auth/pair", "POST"}]
    assert wired.statuses[501] >= 1
  after
    RateLimit.reset()
  end
end
