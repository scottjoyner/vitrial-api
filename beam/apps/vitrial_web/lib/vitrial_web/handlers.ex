defmodule VitrialWeb.Handlers do
  @moduledoc """
  The boundary's answers.

  Boundary only, per beam/README.md and beam/apps/vitrial_web/README.md: this
  module projects configuration (`/health`, `/api/v1/version`) and says one
  uniform thing for every clause whose engine has not landed yet (501, see
  VitrialWeb.Router's moduledoc). No sync, ownership, auth or evidence decision
  lives here. The 404/405/500/307 shapes are the reference's own (starlette),
  not inventions.
  """

  @type reply :: %{
          required(:status) => pos_integer(),
          required(:headers) => %{binary() => binary()},
          required(:body) => binary()
        }

  @doc """
  Serve a matched route. Two clauses project configuration; every other action
  is the uniform not-wired response. `bindings` carries the path parameters
  (unused until an engine lands -- no boundary code reads a documentID).
  """
  def serve({:serve, :health}, _bindings, _ctx), do: health()
  def serve({:serve, :version}, _bindings, _ctx), do: version()
  def serve(:not_wired, _bindings, _ctx), do: not_wired()

  @doc "GET /health -- app/schemas.py:37-38, the whole response is {\"status\":\"ok\"}."
  def health do
    json(200, ~s({"status":"ok"}))
  end

  @doc """
  GET /api/v1/version -- app/schemas.py:46-48. `serviceVersion` is the
  configured value (default "0.3.0", app/settings.py:10); `apiVersion` is
  pinned "v1" by the schema.
  """
  def version do
    service_version = Application.get_env(:vitrial_web, :service_version, "0.3.0")
    json(200, ~s({"apiVersion":"v1","serviceVersion":"#{service_version}"}))
  end

  @doc """
  The wave's single invented behaviour: every routed clause whose engine is not
  landed yet gets this one response. Uniform so a differential test can never
  mistake it for parity. Documented in VitrialWeb.Router's moduledoc.
  """
  def not_wired do
    json(501, ~s({"detail":"not implemented"}))
  end

  @doc "Unmatched path -- starlette/exceptions.py:10 falls back to the HTTP phrase."
  def not_found do
    json(404, ~s({"detail":"Not Found"}))
  end

  @doc """
  Known path, wrong method -- the reference's 405 carries the Allow header
  (starlette/routing.py:275) over the same phrase body.
  """
  def method_not_allowed(allow) do
    json(405, ~s({"detail":"Method Not Allowed"}), %{"allow" => allow})
  end

  @doc """
  Trailing-slash redirect, starlette's `redirect_slashes` default: a 307 whose
  Location is the absolute URL with the slash flipped and the original query
  string kept. VitrialWeb.Handler builds that URL; the body is empty.
  """
  def redirect(location) do
    %{status: 307, headers: %{"location" => location}, body: ""}
  end

  @doc """
  The unhandled-exception answer: starlette's ServerErrorMiddleware, a
  plain-text 500. Deliberately NOT JSON -- the JSON 500s the reference does
  emit come from handlers that caught something specific, which is business
  surface.
  """
  def internal_server_error do
    %{
      status: 500,
      headers: %{"content-type" => "text/plain; charset=utf-8"},
      body: "Internal Server Error"
    }
  end

  defp json(status, body, headers \\ %{}) do
    %{status: status, headers: Map.put(headers, "content-type", "application/json"), body: body}
  end
end
