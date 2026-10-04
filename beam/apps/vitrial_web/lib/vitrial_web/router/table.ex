defmodule VitrialWeb.Router.Table do
  @moduledoc """
  The route table's constructor.

  A separate module for one mechanical reason: module attributes cannot call
  functions of their own module, and the table is compiled once at boot into
  `VitrialWeb.Router` via `@routes Table.build()` -- a remote call, which
  attributes CAN evaluate. All of this app's route knowledge lives behind
  these 23 rows; nothing else in the umbrella enumerates the surface.
  """

  @doc "The 23 clauses of the reference surface, in declaration order."
  def build do
    [
      route("GET", "/health", {:serve, :health}),
      route("GET", "/ready", :not_wired),
      route("GET", "/metrics", :not_wired),
      route("GET", "/api/v1/version", {:serve, :version}),
      route("GET", "/api/v1/auth/me", :not_wired),
      route("POST", "/api/v1/sync/push", :not_wired),
      route("GET", "/api/v1/sync/pull", :not_wired),
      # main.py:60 -- DocumentID = Path(min_length=1)
      route("HEAD", "/api/v1/sync/evidence-blobs/{documentID}", :not_wired,
        params: %{"documentID" => %{min_length: 1}}
      ),
      route("PUT", "/api/v1/sync/evidence-blobs/{documentID}", :not_wired,
        params: %{"documentID" => %{min_length: 1}}
      ),
      route("GET", "/api/v1/sync/evidence-blobs/{documentID}", :not_wired,
        params: %{"documentID" => %{min_length: 1}}
      ),
      route("POST", "/internal/admin/v1/bootstrap", :not_wired),
      # main.py:383 -- sessionID min_length=1, max_length=128
      route("POST", "/internal/admin/v1/sessions/{sessionID}/revoke", :not_wired,
        params: %{"sessionID" => %{min_length: 1, max_length: 128}}
      ),
      route("POST", "/api/v1/auth/pair", :not_wired),
      route("POST", "/api/v1/auth/logout", :not_wired),
      route("POST", "/internal/admin/v1/pairing-grants", :not_wired),
      route("GET", "/api/v1/reference-data/manifest", :not_wired),
      route("GET", "/api/v1/reference-data/current", :not_wired),
      route("GET", "/api/v1/reference-data/publications", :not_wired),
      # reference_routes.py:73 -- publicationID min_length=1, max_length=256
      route("GET", "/api/v1/reference-data/publications/{publicationID}", :not_wired,
        params: %{"publicationID" => %{min_length: 1, max_length: 256}}
      ),
      route("POST", "/api/v1/reference-data/publications", :not_wired),
      route("GET", "/api/v2/version", :not_wired),
      route("POST", "/api/v2/sync/push", :not_wired),
      route("GET", "/api/v2/sync/pull", :not_wired)
    ]
  end

  defp route(method, path, action, opts \\ []) do
    %{
      method: method,
      path: path,
      action: action,
      segments: compile(path),
      params: Keyword.get(opts, :params, %{})
    }
  end

  defp compile(path) do
    path
    |> String.split("/", trim: true)
    |> Enum.map(fn
      "{" <> rest -> {:param, String.trim_trailing(rest, "}")}
      segment -> segment
    end)
  end
end
