defmodule VitrialWeb.Router do
  @moduledoc """
  The hand-written route table and its matcher.

  All 23 method+path clauses of the reference surface, in declaration order:

    * app/main.py:153,158,185,201,206,214,228 (health, ready, metrics, v1
      version/auth-me/sync, evidence-blobs, internal admin)
    * app/auth_session_routes.py:25,39,64 (pair, logout, pairing-grants)
    * app/reference_routes.py:29,41,54,67,83 (reference-data, under the
      router prefix /api/v1/reference-data)
    * app/sync_v2_routes.py:21,26,40 (v2, under the router prefix /api/v2)

  Matching follows starlette's router semantics, because that is what the
  FastAPI reference runs on:

    * routes are tried in declaration order; the FIRST full match wins;
    * a path match whose method differs is remembered as the first PARTIAL
      match, scanning continues, and the partial becomes a 405 only if no full
      match exists anywhere (starlette/routing.py:695-707);
    * a GET route also answers HEAD -- starlette adds HEAD to the method set of
      every GET route (starlette/routing.py:240), and cowboy suppresses the
      body of a HEAD reply on its own;
    * with no match at all, the trailing slash is flipped (added or stripped)
      and the table is retried; a hit there is a 307 redirect, which is
      starlette's `redirect_slashes` default;
    * nothing matching still means 404 `{"detail":"Not Found"}` and a known
      path with a wrong method means 405 `{"detail":"Method Not Allowed"}` with
      an Allow header -- both are the reference's own bodies
      (starlette/exceptions.py:10 falls back to http.client.responses).

  Path parameters ({documentID}, {sessionID}, {publicationID}) are captured
  verbatim, one segment each. The declared constraints (main.py:60
  documentID min_length=1; main.py:383 sessionID 1..128; reference_routes.py:73
  publicationID 1..256) ride on the table as `params` metadata: on the
  reference they are enforced by pydantic INSIDE the endpoint as 422s, which is
  request validation -- business logic, deliberately not on the boundary yet.
  A segment that cannot exist (an empty one) never matches, which is the same
  routing-level effect min_length=1 has on the reference.

  ## The wave's single invented behaviour

  Every clause whose engine is not landed yet is answered by ONE uniform
  response from VitrialWeb.Handlers: 501 with the fixed JSON body
  `{"detail":"not implemented"}`. The reference never emits this; it exists so
  the route is observable end to end (middleware stages see it, metrics label
  it by template) without anyone mistaking it for parity. It is replaced
  engine by engine by later waves. Only /health and /api/v1/version are fully
  servable at this round; /ready and /metrics stay unwired on purpose --
  drain state and the metrics registry are business surface, not boundary.
  """

  # Compiled once at boot. The rows live in VitrialWeb.Router.Table because a
  # module attribute cannot call its own module's functions.
  @routes VitrialWeb.Router.Table.build()

  @doc "The table, exposed for tests and later contract tooling."
  def routes, do: @routes

  @doc """
  Match a method and path. Returns `{:ok, route, bindings}`, or the reason the
  request is not routable: `{:method_not_allowed, route, allow}`, `:not_found`,
  or `{:redirect, location}`.
  """
  def match(method, path) do
    segments = split_path(path)

    case scan(@routes, method, segments, nil) do
      {:ok, route, bindings} -> {:ok, route, bindings}
      {:method_not_allowed, route, allow} -> {:method_not_allowed, route, allow}
      :not_found -> maybe_redirect(method, path, segments)
    end
  end

  defp scan([], _method, _segments, partial), do: partial || :not_found

  defp scan([route | rest], method, segments, partial) do
    case match_segments(route.segments, segments) do
      {:ok, bindings} ->
        if method_allowed?(route, method) do
          {:ok, route, bindings}
        else
          # Starlette keeps only the FIRST partial match (routing.py:699).
          scan(rest, method, segments, partial || {:method_not_allowed, route, allow(route)})
        end

      :no_match ->
        scan(rest, method, segments, partial)
    end
  end

  defp method_allowed?(route, "HEAD"), do: route.method == "HEAD" or route.method == "GET"
  defp method_allowed?(route, method), do: route.method == method

  # starlette/routing.py:275 -- Allow is the route's full method set, and a GET
  # route's set contains HEAD.
  defp allow(route) do
    if route.method == "GET", do: "GET, HEAD", else: route.method
  end

  defp match_segments(route_segments, segments) do
    match_segments(route_segments, segments, [])
  end

  defp match_segments([], [], bindings), do: {:ok, :maps.from_list(bindings)}

  defp match_segments([seg | rest_a], [seg | rest_b], bindings),
    do: match_segments(rest_a, rest_b, bindings)

  defp match_segments([{:param, name} | rest_a], [value | rest_b], bindings)
       when byte_size(value) > 0 do
    # A path parameter never captures an empty segment -- starlette's str
    # convertor is [^/]+ -- which is also the routing-level effect of the
    # reference's min_length=1 on these parameters.
    match_segments(rest_a, rest_b, [{name, value} | bindings])
  end

  defp match_segments(_, _, _), do: :no_match

  defp maybe_redirect(_method, path, _segments) when path == "/", do: :not_found

  defp maybe_redirect(method, path, _segments) do
    # starlette's redirect_slashes default: with no route match, try the path
    # with the trailing slash flipped either way; a hit there is a 307. The
    # Location the handler builds keeps the original query string.
    flipped =
      if String.ends_with?(path, "/") do
        String.replace_suffix(path, "/", "")
      else
        path <> "/"
      end

    case scan(@routes, method, split_path(flipped), nil) do
      {:ok, _route, _bindings} -> {:redirect, flipped}
      _ -> :not_found
    end
  end

  # Unlike a trimmed split, a TRAILING empty segment is kept: "/health/" is
  # two segments and must not full-match "/health" -- starlette's route regex
  # is anchored (^/health$) and redirects the slash instead.
  defp split_path(path) do
    case String.split(path, "/") do
      ["" | rest] -> rest
      other -> other
    end
  end
end
