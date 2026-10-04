defmodule VitrialWeb.Handler do
  @moduledoc """
  The one cowboy handler, and the whole request pipeline.

  ## The middleware mechanism, decided on the record

  The reference (app/main.py:391-407) assembles the request path from four ASGI
  middleware, outermost -> innermost: Metrics, RequestSize, RateLimit,
  Correlation, then the router. That order is load-bearing (each stage's
  observable behaviour depends on sitting outside the next). ASGI middleware can
  be passive about it because ASGI gives every middleware the `send` channel --
  the outermost stage sees the final status on the way OUT. **cowboy 2.x has no
  outbound hook**: `cowboy_middleware`'s `execute/2` runs once, on the way in,
  and a stage that runs before the handler can never observe the status the
  handler eventually replies with. Porting the four classes as four
  `cowboy_middleware` modules would compile and silently never record anything.

  So this port does not use cowboy's middleware chain at all. It is the shape
  DEPENDENCY-DOCTRINE.md §4.4 prescribes -- one route table plus one dispatch
  handler -- with the four stages as ordinary modules sequenced as function
  calls inside THIS handler, and a single response-sending helper,
  `VitrialWeb.Reply`, that stands in for ASGI's `send` wrapper:

      Metrics        -> the monotonic start time is taken at handler entry;
                        the recording happens inside Reply.send, the one point
                        every response leaves through
      RequestSize    -> VitrialWeb.RequestSize.admit/1; reads and buffers the
                        body itself (cowboy handlers own body reads) and passes
                        it on through the context
      RateLimit      -> VitrialWeb.RateLimit.admit/1; runs BEFORE correlation,
                        so a 429 carries no X-Request-ID, logs its refused
                        event with requestID null, and emits no started/
                        completed events
      Correlation    -> VitrialWeb.Correlation; binds logger metadata, emits
                        request.started/completed/failed, echoes X-Request-ID
      dispatch       -> VitrialWeb.Router.match/2 + VitrialWeb.Handlers.serve/3

  Sequencing anything in a different order changes what the wire and the logs
  look like; the measured invariants this preserves are written on the stages.

  ## Error surface

  The reference's outer wrappers (starlette ServerErrorMiddleware +
  ExceptionTelemetryMiddleware) turn an unhandled exception into a 500
  plain-text "Internal Server Error", with request.failed logged by the
  correlation middleware before the response goes out. The rescue/catch here is
  that wrapper. Stages that run before correlation have no request id bound, so
  their 413/429 responses are shaped without one -- exactly as the reference's
  stack order produces.
  """

  alias VitrialWeb.{Correlation, Events, Handlers, RateLimit, Reply, Router, RequestSize}

  # No @behaviour: cowboy 2.x declares none for plain handlers (the compiler
  # confirms -- `:cowboy_clear is not a behaviour`), so the init/2 + terminate/3
  # pair is the convention itself, kept visible here.
  def init(req, _opts) do
    ctx = %{
      req: req,
      method: :cowboy_req.method(req),
      path: :cowboy_req.path(req),
      started_monotonic: System.monotonic_time(:microsecond),
      route_template: nil,
      request_id: nil,
      correlated_at: nil,
      body: nil
    }

    # A def-level rescue would not see `ctx`; the explicit try is the outermost
    # reference wrapper (ServerErrorMiddleware), and it needs the context to
    # know whether correlation had begun when the fault hit. Every path threads
    # the (possibly advanced) request back -- cowboy's init/2 must return
    # {ok, Req, State}, and returning anything else exits the request process
    # with {:try_clause, whatever}.
    ctx =
      try do
        run(ctx)
      rescue
        e -> failed(ctx, e)
      catch
        # GenServer calls to the metrics/limiter processes surface as exits,
        # and an exit escaping the pipeline is the same "the request did not
        # complete normally" fact as a raise.
        kind, e -> failed(ctx, {kind, e})
      end

    {:ok, ctx.req, nil}
  end

  def terminate(_reason, _req, _state), do: :ok

  # Stage order: RequestSize -> RateLimit -> Correlation -> dispatch, with the
  # metrics clock started at `init/2` (outermost) and recorded in Reply.
  defp run(ctx) do
    case RequestSize.admit(ctx) do
      {:reject, reply} ->
        replied(ctx, reply)

      {:ok, ctx} ->
        case RateLimit.admit(ctx) do
          {:reject, reply} -> replied(ctx, reply)
          {:bypass, ctx} -> serve_correlated(ctx)
          {:ok, ctx} -> serve_correlated(ctx)
        end
    end
  end

  defp replied(ctx, reply) do
    {_status, req} = Reply.send(ctx, reply)
    %{ctx | req: req}
  end

  defp serve_correlated(ctx) do
    request_id = Correlation.request_id_for_header(header(ctx, "x-request-id"))
    ctx = %{ctx | request_id: request_id, correlated_at: System.monotonic_time(:microsecond)}

    Correlation.begin(request_id)

    Correlation.bind(
      device_id: header(ctx, "x-vitrial-device-id"),
      app_version: header(ctx, "x-vitrial-app-version"),
      app_build: header(ctx, "x-vitrial-app-build")
    )

    Events.emit("request.started", :info, %{method: ctx.method})

    try do
      dispatch(ctx)
    rescue
      e ->
        Events.emit("request.failed", :error, %{
          routeTemplate: ctx.route_template,
          durationMs: Correlation.elapsed_ms(ctx.correlated_at),
          errorType: Correlation.error_type(e)
        })

        failed(ctx, e)
    after
      Correlation.end_request()
    end
  end

  defp dispatch(ctx) do
    case Router.match(ctx.method, ctx.path) do
      {:ok, route, bindings} ->
        ctx = %{ctx | route_template: route.path}
        complete(ctx, Handlers.serve(route.action, bindings, ctx))

      {:method_not_allowed, route, allow} ->
        # Starlette keeps the first partial match and stamps scope["route"] with
        # it (starlette/routing.py:699,707), so a 405 carries the template into
        # the metrics label rather than falling into the unmatched bucket.
        ctx = %{ctx | route_template: route.path}
        complete(ctx, Handlers.method_not_allowed(allow))

      :not_found ->
        complete(ctx, Handlers.not_found())

      {:redirect, path} ->
        # starlette's 307 keeps the original query string; only the slash is
        # flipped. Its Location is the absolute URL (scheme://host), built the
        # way starlette's URL(scope) builds it: clear-HTTP scheme, Host header,
        # then the path and query.
        host = :cowboy_req.header("host", ctx.req) || "localhost"

        location =
          case :cowboy_req.qs(ctx.req) do
            "" -> "http://" <> host <> path
            qs -> "http://" <> host <> path <> "?" <> qs
          end

        complete(ctx, Handlers.redirect(location))
    end
  end

  defp complete(ctx, reply) do
    {status, req} = Reply.send(ctx, reply)

    Events.emit("request.completed", :info, %{
      method: ctx.method,
      routeTemplate: ctx.route_template,
      statusCode: status,
      durationMs: Correlation.elapsed_ms(ctx.correlated_at)
    })

    %{ctx | req: req}
  end

  defp failed(ctx, e) do
    # A fault in the stages OUTSIDE correlation (size guard, limiter) never ran
    # the correlation middleware in the reference, so it logs no
    # request.failed event -- the outermost wrapper just answers 500. Emit the
    # event only when correlation had actually begun; its durationMs measures
    # from the correlation start (main.py:77,89), not from handler entry.
    if ctx.correlated_at do
      Events.emit("request.failed", :error, %{
        routeTemplate: ctx.route_template,
        durationMs: Correlation.elapsed_ms(ctx.correlated_at),
        errorType: Correlation.error_type(e)
      })
    end

    {_status, req} = Reply.send(ctx, Handlers.internal_server_error())
    %{ctx | req: req}
  end

  defp header(ctx, name), do: :cowboy_req.header(name, ctx.req)
end
