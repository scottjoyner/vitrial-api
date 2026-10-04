defmodule VitrialWeb.Listener do
  @moduledoc """
  The clear-HTTP cowboy listener, started from the live configuration.

  `config :vitrial_web, VitrialWeb.Endpoint, http: [...]` is read HERE and
  passed to :cowboy.start_clear/3 -- the config block's comment promises the
  bind happens at the listener, not in the request path. `test.exs` sets
  `port: 0`, so tests read the actually-bound port back through `port/0`.

  Two decisions worth stating:

  * **The ref is a fixed, named term** (`VitrialWeb.HTTPListener`): the
    supervisor child id, the :ranch ref and `port/0` must all name the same
    listener, and an anonymous or derived ref would make the tests race
    whatever the supervisor happened to call it.
  * **The shutdown is explicitly non-brutal** (VitrialWeb.Application's
    moduledoc: drain before drop). cowboy's generated child spec carries
    ranch's default; it is overridden here with a timed value so a shutdown
    lets in-flight requests finish and only then drops the socket -- never
    :brutal_kill, never infinity.

  `request_body_max_bytes` from the same config block is consumed as cowboy's
  `max_skip_body_length`: how much of an UNREAD body the listener drains to
  keep a keep-alive connection alive. The per-path admission table that
  actually refuses oversized requests is VitrialWeb.RequestSize; the reference
  deliberately leaves unlisted paths unlimited (app/request_size.py:62-64),
  which is why this value is a draining bound, not a rejection rule.
  """

  @ref VitrialWeb.HTTPListener

  # Drain before drop: a timed, non-brutal shutdown for the listener's
  # connection tree.
  @shutdown_ms 30_000

  def child_spec(_arg) do
    # cowboy:start_clear/3 starts the listener IMMEDIATELY and returns a pid --
    # no spec-returning API exists on the cowboy side. The spec comes from
    # ranch itself: the same transport (:ranch_tcp) and protocol module
    # (:cowboy_clear) start_clear would use, with the same connection_type
    # start_clear stamps in. The one normalization skipped is cowboy's
    # dynamic-buffer seeding, a throughput tuning cowboy_http re-derives on
    # its own; behaviour is identical.
    spec =
      :ranch.child_spec(
        @ref,
        :ranch_tcp,
        http_transport_opts(),
        :cowboy_clear,
        %{
          connection_type: :supervisor,
          env: %{
            dispatch:
              :cowboy_router.compile([
                {:_, [{:_, VitrialWeb.Handler, []}]}
              ])
          },
          max_skip_body_length: request_body_max_bytes()
        }
      )

    # ranch's generated spec carries no shutdown key at all (Supervisor then
    # applies its default); put the explicit timed drain rather than wrapping
    # it, so the supervision tree names one listener.
    Map.put(spec, :shutdown, @shutdown_ms)
  end

  @doc "The port the listener actually bound (config asked for 0 in tests)."
  def port do
    :ranch.get_port(@ref)
  end

  defp http_transport_opts do
    :vitrial_web
    |> Application.get_env(VitrialWeb.Endpoint, [])
    |> Keyword.fetch!(:http)
  end

  defp request_body_max_bytes do
    :vitrial_web
    |> Application.get_env(VitrialWeb.Endpoint, [])
    |> Keyword.get(:request_body_max_bytes, 4 * 1024 * 1024)
  end
end
