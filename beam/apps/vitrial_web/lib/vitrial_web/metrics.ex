defmodule VitrialWeb.Metrics do
  @moduledoc """
  The counters behind the outermost instrumentation stage (port of
  app/metrics_middleware.py:80-96 against app/metrics.py's registry).

  Three counters per (route label, method), exactly the labels the reference
  records: `requests_total` by status CLASS, `request_status_total` by exact
  status, and the duration accumulator over the full request. The `/metrics`
  exposition endpoint itself is NOT part of this wave -- it is gated on the
  admin key and rendered from the registry (main.py:185-198), which is
  business surface. The registry exists so the recording half is real and
  testable, not because anything renders it yet.

  ## The cardinality rule

  The route label comes from the MATCHED TEMPLATE only, never the raw path:
  `/api/v1/sync/evidence-blobs/{documentID}` must not mint one time series per
  uploaded document (app/metrics_middleware.py:7-17). A request that failed
  before routing matched anything -- 413s from the size guard, 429s from the
  limiter, 404s -- collapses into the single shared "unmatched" bucket. This
  falls out of the pipeline order: the context only carries a template once
  VitrialWeb.Router has matched, and the pre-routing stages reply before that.
  """

  use GenServer

  @unmatched_route "unmatched"

  @doc "The one shared label for requests routing never matched (:37-39)."
  def unmatched_route, do: @unmatched_route

  @doc "app/metrics_middleware.py:48-49 -- `f\"{status // 100}xx\"`."
  def status_class(status) when is_integer(status) and status >= 100 do
    "#{div(status, 100)}xx"
  end

  def start_link(opts) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @doc """
  Record one observed response. Called only from VitrialWeb.Reply, which wraps
  this in a best-effort try -- instrumentation must never change the response
  (:80-96).
  """
  def record(route_label, method, status, duration_ms) do
    GenServer.call(__MODULE__, {:record, route_label, method, status, duration_ms})
  end

  @doc """
  A read-shaped snapshot of the counters, keyed
  `%{label => %{method => %{classes:, statuses:, duration_count:, duration_total_ms:, duration_max_ms:}}}`.
  Tests read this; a later wave's exposition will too.
  """
  def snapshot do
    GenServer.call(__MODULE__, :snapshot)
  end

  @impl true
  def init(_opts), do: {:ok, %{}}

  @impl true
  def handle_call({:record, label, method, status, duration_ms}, _from, state) do
    duration_ms = Float.round(duration_ms, 2)
    key = {label, method}
    entry = Map.get(state, key, fresh_entry())

    entry = %{
      classes: Map.update(entry.classes, status_class(status), 1, &(&1 + 1)),
      statuses: Map.update(entry.statuses, status, 1, &(&1 + 1)),
      duration_count: entry.duration_count + 1,
      duration_total_ms: entry.duration_total_ms + duration_ms,
      duration_max_ms: max(entry.duration_max_ms, duration_ms)
    }

    {:reply, :ok, Map.put(state, key, entry)}
  end

  @impl true
  def handle_call(:snapshot, _from, state), do: {:reply, state, state}

  defp fresh_entry do
    %{
      classes: %{},
      statuses: %{},
      duration_count: 0,
      duration_total_ms: 0.0,
      duration_max_ms: 0.0
    }
  end
end
