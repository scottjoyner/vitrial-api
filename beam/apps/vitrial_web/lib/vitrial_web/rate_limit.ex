defmodule VitrialWeb.RateLimit do
  @moduledoc """
  Bounded request rate for the unauthenticated auth surface (port of
  app/rate_limit.py plus the gate at app/main.py:107-150).

  * In-process, per-client sliding window -- see `VitrialWeb.RateLimit.Window`.
    Correct for the single-instance deployment; behind more than one instance
    the effective ceiling is limit x instances, and a shared store is
    deliberately not faked here (app/rate_limit.py:36-39).
  * Scoped to `/api/v1/auth/pair` only (:64-66): throttling authenticated sync
    traffic by client address would penalise everyone behind a shared egress.
  * A limiter FAULT FAILS OPEN (:main.py:120-133): an internal error in the
    limiter must not become an outage on the sign-in path. A down registry
    process logs `auth.rate_limit_bypassed` and the request proceeds.
  * The 429 body is fixed and carries no credential-derived information
    (:117-119): `{"detail":"too many requests"}` with only `Retry-After` and
    `Cache-Control: no-store` beside the transport headers.

  This stage sits between the size guard and correlation in the pipeline
  (VitrialWeb.Handler), which reproduces the measured reference behaviour of a
  throttled request: NO X-Request-ID on the response, and a refused event whose
  `requestID` and `routeTemplate` are both null -- null template because
  routing never ran (the port gets this for free only because the limiter sits
  before the router; filling it from the path table would silently diverge).
  The prose at main.py:110-112 claims the opposite ("a throttled request still
  gets a request id and a completed-event log line"); under the pinned
  starlette the runtime contradicts its own docstring -- add_middleware's
  insert(0) plus the reversed wrap put correlation INSIDE the limiter -- and
  the behaviour, not the prose, is the oracle.
  """

  use GenServer

  alias VitrialWeb.Events
  alias VitrialWeb.RateLimit.Window

  @default_limit 30
  @default_window_seconds 60.0

  # Paths on the unauthenticated auth surface (app/rate_limit.py:64-66).
  @throttled_path_prefixes ["/api/v1/auth/pair"]

  defstruct [:limit, :window, :state]

  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @doc """
  The limiter's resolved configuration -- `VITRIAL_AUTH_RATE_LIMIT` (default
  30) and `VITRIAL_AUTH_RATE_WINDOW_SECONDS` (default 60.0), both ignored
  unless a positive number (app/rate_limit.py:95-100).
  """
  def config do
    limit = positive_int(System.get_env("VITRIAL_AUTH_RATE_LIMIT"), @default_limit)

    window =
      positive_float(System.get_env("VITRIAL_AUTH_RATE_WINDOW_SECONDS"), @default_window_seconds)

    {limit, window}
  end

  @doc "app/rate_limit.py:206-207."
  def path_throttled?(path) do
    Enum.any?(@throttled_path_prefixes, &String.starts_with?(path, &1))
  end

  @doc """
  Identify the client for throttling purposes (app/rate_limit.py:103-122):
  the left-most `X-Forwarded-For` entry when the deployment's proxy provides
  one, else the peer address. Only as trustworthy as the deployment -- see the
  reference's own caveat.
  """
  def client_key(headers, peer_ip) do
    case headers["x-forwarded-for"] do
      forwarded when is_binary(forwarded) and forwarded != "" ->
        case String.split(forwarded, ",", parts: 2) do
          [first | _] ->
            first = String.trim(first)
            if first != "", do: "xff:" <> first, else: peer_key(peer_ip)

          [] ->
            peer_key(peer_ip)
        end

      _ ->
        peer_key(peer_ip)
    end
  end

  @doc """
  The pipeline stage. `{:ok, ctx}` to proceed, `{:bypass, ctx}` on a limiter
  fault (fail-open), `{:reject, reply}` with the fixed 429.
  """
  def admit(ctx) do
    if path_throttled?(ctx.path) do
      key = client_key(:cowboy_req.headers(ctx.req), peer_ip(ctx))

      try do
        case GenServer.call(__MODULE__, {:allow, key}, 1_000) do
          {true, _retry_after} ->
            {:ok, ctx}

          {false, retry_after} ->
            retry_after_seconds = max(1, trunc(retry_after + 0.999))

            Events.emit("auth.rate_limit_refused", :warning, %{
              routeTemplate: ctx.route_template,
              retryAfterSeconds: retry_after_seconds
            })

            {:reject, refused(retry_after_seconds)}
        end
      rescue
        e -> bypass(ctx, e)
      catch
        _, e -> bypass(ctx, e)
      end
    else
      {:ok, ctx}
    end
  end

  @doc "Drive the limiter directly (tests, and the stage above)."
  def allow(key, now \\ nil) do
    GenServer.call(__MODULE__, {:allow, key, now})
  end

  @doc "Empty every bucket (app/rate_limit.py:176-178, reset_limiter_for_tests)."
  def reset do
    GenServer.call(__MODULE__, :reset)
  end

  @impl true
  def init(_opts) do
    {limit, window_seconds} = config()

    {:ok,
     %__MODULE__{limit: limit, window: window_seconds, state: Window.new(limit, window_seconds)}}
  end

  @impl true
  def handle_call({:allow, key}, _from, limiter) do
    now = :erlang.monotonic_time(:microsecond) / 1_000_000
    allow_and_reply(key, now, limiter)
  end

  def handle_call({:allow, key, now}, _from, limiter) when is_float(now) or is_integer(now) do
    allow_and_reply(key, now * 1.0, limiter)
  end

  def handle_call(:reset, _from, limiter) do
    {:reply, :ok, %{limiter | state: Window.reset(limiter.state)}}
  end

  defp allow_and_reply(key, now, limiter) do
    {allowed, retry_after, state} = Window.allow(limiter.state, key, now)
    {:reply, {allowed, retry_after}, %{limiter | state: state}}
  end

  defp bypass(ctx, e) do
    Events.emit("auth.rate_limit_bypassed", :error, %{
      errorType: VitrialWeb.Correlation.error_type(e)
    })

    {:bypass, ctx}
  end

  # main.py:143-150. No X-Request-ID: this reply is built before correlation
  # begins, and Reply only stamps the header from the context's request id.
  defp refused(retry_after_seconds) do
    %{
      status: 429,
      headers: %{
        "content-type" => "application/json",
        "retry-after" => Integer.to_string(retry_after_seconds),
        "cache-control" => "no-store"
      },
      body: ~s({"detail":"too many requests"})
    }
  end

  defp peer_key(nil), do: "peer:unknown"
  defp peer_key(peer_ip), do: "peer:" <> (:inet.ntoa(peer_ip) |> IO.iodata_to_binary())

  defp peer_ip(ctx) do
    {ip, _port} = :cowboy_req.peer(ctx.req)
    ip
  end

  defp positive_int(nil, fallback), do: fallback

  defp positive_int(raw, fallback) do
    case Integer.parse(raw) do
      {value, ""} -> if value > 0, do: value, else: fallback
      _ -> fallback
    end
  end

  defp positive_float(nil, fallback), do: fallback

  defp positive_float(raw, fallback) do
    case Float.parse(raw) do
      {value, ""} -> if value > 0, do: value, else: fallback
      _ -> fallback
    end
  end
end
