defmodule VitrialWeb.Reply do
  @moduledoc """
  The single point every response leaves through.

  This module is the cowboy stand-in for the ASGI `send` wrapper the reference
  hangs its outermost observation on (app/metrics_middleware.py:64-67): the
  status is only knowable HERE, because cowboy has no outbound middleware hook
  (the mechanism decision is written up in VitrialWeb.Handler's moduledoc).

  Two things happen on the way out, in this order:

  1. The X-Request-ID echo -- `X-Request-ID` is set on every response that
     passed through correlation (main.py:94). Responses refused BEFORE
     correlation begins (413 from the size guard, 429 from the rate limiter)
     carry no request id, which is a measured reference behaviour, not an
     oversight.
  2. The metrics recording -- route label from the matched template only (the
     single "unmatched" bucket otherwise), status class, status and duration
     over the whole request. Recording is best-effort and swallowed: an
     exception escaping into the reply path would replace the real outcome of
     the request with an instrumentation failure (app/metrics_middleware.py:80-96).
  """

  alias VitrialWeb.Metrics

  @unmatched Metrics.unmatched_route()

  @doc """
  Send `reply` for `ctx`. Returns `{status, req}` once the response is on the
  wire -- the status for the caller's request.completed event, the req because
  cowboy's init/2 contract hands one back ({ok, Req, State}).
  """
  def send(ctx, reply) do
    headers = maybe_request_id(reply.headers, ctx.request_id)

    req = :cowboy_req.reply(reply.status, headers, reply.body, ctx.req)

    record_metrics(ctx, reply.status)

    {reply.status, req}
  end

  defp maybe_request_id(headers, nil), do: headers

  defp maybe_request_id(headers, request_id),
    do: Map.put(headers, "x-request-id", request_id)

  defp record_metrics(ctx, status) do
    now = System.monotonic_time(:microsecond)
    duration_ms = (now - ctx.started_monotonic) / 1_000

    try do
      Metrics.record(route_label(ctx.route_template), ctx.method, status, duration_ms)
    rescue
      _ -> :ok
    catch
      # A down or overloaded registry process surfaces as an exit; a broken
      # counter degrades to "no metric", never to "no response".
      _, _ -> :ok
    end
  end

  defp route_label(nil), do: @unmatched
  defp route_label(template), do: template
end
