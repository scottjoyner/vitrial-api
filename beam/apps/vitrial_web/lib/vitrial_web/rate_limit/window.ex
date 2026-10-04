defmodule VitrialWeb.RateLimit.Window do
  @moduledoc """
  Fixed-capacity sliding window over per-client request timestamps -- the pure
  core of app/rate_limit.py:125-174.

  Deliberately not a token bucket: a token bucket allows a burst equal to its
  full capacity, which is the wrong shape for bounding rejected-auth traffic
  (app/rate_limit.py:126-130). Pure over a plain map of key -> [timestamps],
  so the eviction rules can be tested against synthetic clocks instead of
  sleeps.

  The eviction order is load-bearing (:163-174): keys with no hits inside the
  window are dropped FIRST, and only then the oldest key if the table is still
  full -- a host that has gone quiet is the cheapest thing to forget, so a
  long-idle deployment does not pin its whole capacity on stale entries before
  expiry ever gets a chance.
  """

  @enforce_keys [:limit, :window, :max_clients]
  defstruct [:limit, :window, :max_clients, hits: %{}]

  @opaque t :: %__MODULE__{}

  @spec new(pos_integer(), float(), pos_integer()) :: t()
  def new(limit, window_seconds, max_clients \\ 10_000) do
    %__MODULE__{limit: limit, window: window_seconds, max_clients: max_clients}
  end

  @doc """
  Return `{allowed, retry_after_seconds, state}` (:139-161).

  `retry_after_seconds` is 0.0 when allowed; on refusal it is the time until
  the OLDEST hit leaves the window, so a client that backs off is admitted as
  soon as it is actually safe rather than after a fixed penalty. A refused
  request still leaves its key (and its pruned bucket) in the table, exactly
  like the reference mutates its deque in place. `now` is a monotonic-second
  float, the unit `time.monotonic()` uses.
  """
  @spec allow(t(), binary(), float()) :: {boolean(), float(), t()}
  def allow(%__MODULE__{} = state, key, now) do
    cutoff = now - state.window

    {bucket, hits} =
      case Map.get(state.hits, key) do
        nil ->
          hits =
            if map_size(state.hits) >= state.max_clients do
              evict(state.hits, cutoff, state.max_clients)
            else
              state.hits
            end

          {[], Map.put(hits, key, [])}

        bucket ->
          {bucket, state.hits}
      end

    bucket = prune(bucket, cutoff)
    hits = Map.put(hits, key, bucket)

    if length(bucket) >= state.limit do
      retry_after = max(0.0, hd(bucket) + state.window - now)
      {false, retry_after, %{state | hits: hits}}
    else
      bucket = bucket ++ [now]
      {true, 0.0, %{state | hits: Map.put(hits, key, bucket)}}
    end
  end

  @doc "app/rate_limit.py:176-178."
  @spec reset(t()) :: t()
  def reset(%__MODULE__{} = state), do: %{state | hits: %{}}

  defp keep?(bucket, cutoff) do
    case bucket do
      [] -> false
      hits -> List.last(hits) > cutoff
    end
  end

  defp prune(bucket, cutoff) do
    case bucket do
      [oldest | rest] when oldest <= cutoff -> prune(rest, cutoff)
      _ -> bucket
    end
  end

  defp evict(hits, cutoff, max_clients) do
    # Expire first: keys with no hits, or whose newest hit is outside the
    # window (:169-171).
    hits =
      for {key, bucket} <- hits,
          keep?(bucket, cutoff),
          into: %{},
          do: {key, bucket}

    if map_size(hits) < max_clients do
      hits
    else
      # Then the oldest if still full (:172-174). An empty bucket counts as
      # 0.0, the reference's `if item[1] else 0.0`.
      {oldest_key, _} = Enum.min_by(hits, fn {_key, bucket} -> List.last(bucket, 0.0) end)

      Map.delete(hits, oldest_key)
    end
  end
end
