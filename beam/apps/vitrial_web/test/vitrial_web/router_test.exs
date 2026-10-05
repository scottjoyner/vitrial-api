defmodule VitrialWeb.RouterTest do
  use ExUnit.Case, async: false

  alias VitrialWeb.Router

  # The measured route surface: 23 clauses across four reference files
  # (12 in app/main.py, 3 in app/auth_session_routes.py, 5 in
  # app/reference_routes.py, 3 in app/sync_v2_routes.py).
  @surface [
    {"GET", "/health"},
    {"GET", "/ready"},
    {"GET", "/metrics"},
    {"GET", "/api/v1/version"},
    {"GET", "/api/v1/auth/me"},
    {"POST", "/api/v1/sync/push"},
    {"GET", "/api/v1/sync/pull"},
    {"HEAD", "/api/v1/sync/evidence-blobs/{documentID}"},
    {"PUT", "/api/v1/sync/evidence-blobs/{documentID}"},
    {"GET", "/api/v1/sync/evidence-blobs/{documentID}"},
    {"POST", "/internal/admin/v1/bootstrap"},
    {"POST", "/internal/admin/v1/sessions/{sessionID}/revoke"},
    {"POST", "/api/v1/auth/pair"},
    {"POST", "/api/v1/auth/logout"},
    {"POST", "/internal/admin/v1/pairing-grants"},
    {"GET", "/api/v1/reference-data/manifest"},
    {"GET", "/api/v1/reference-data/current"},
    {"GET", "/api/v1/reference-data/publications"},
    {"GET", "/api/v1/reference-data/publications/{publicationID}"},
    {"POST", "/api/v1/reference-data/publications"},
    {"GET", "/api/v2/version"},
    {"POST", "/api/v2/sync/push"},
    {"GET", "/api/v2/sync/pull"}
  ]

  test "the table carries all 23 clauses, in declaration order" do
    assert Enum.map(Router.routes(), &{&1.method, &1.path}) == @surface
  end

  test "every declared (method, path) clause matches, param routes capture their binding" do
    for {method, path} <- @surface do
      concrete = String.replace(path, ~r/\{[^}]+\}/, "x")
      assert {:ok, route, bindings} = Router.match(method, concrete)
      assert route.path == path

      if path =~ "{", do: assert(map_size(bindings) == 1)
    end
  end

  test "a GET route also answers HEAD (starlette/routing.py:240)" do
    assert {:ok, _route, _} = Router.match("HEAD", "/health")
  end

  test "known path with a wrong method is the first partial match, with its Allow set" do
    assert {:method_not_allowed, route, "GET, HEAD"} = Router.match("DELETE", "/health")
    assert route.path == "/health"

    assert {:method_not_allowed, route, "POST"} = Router.match("GET", "/api/v1/auth/pair")
    assert route.path == "/api/v1/auth/pair"
  end

  test "a full match elsewhere wins over the partial (starlette scans past partials)" do
    # /api/v1/reference-data/publications matches GET fully; the param route is
    # declared after it and must not shadow it.
    assert {:ok, route, %{"publicationID" => "pub-9"}} =
             Router.match("GET", "/api/v1/reference-data/publications/pub-9")

    assert route.path == "/api/v1/reference-data/publications/{publicationID}"
  end

  test "unknown path is :not_found" do
    assert :not_found = Router.match("GET", "/nope")
    assert :not_found = Router.match("GET", "/api/v3/version")
    assert :not_found = Router.match("GET", "/")
  end

  test "trailing slash flips to a 307 redirect when the flipped path exists" do
    assert {:redirect, "/health"} = Router.match("GET", "/health/")
    assert {:redirect, "/api/v2/version"} = Router.match("GET", "/api/v2/version/")
  end

  test "param constraints ride on the table as metadata for the later 422 wave" do
    routes = Map.new(Router.routes(), &{&1.path, &1})

    assert routes["/api/v1/sync/evidence-blobs/{documentID}"].params == %{
             "documentID" => %{min_length: 1}
           }

    assert routes["/internal/admin/v1/sessions/{sessionID}/revoke"].params == %{
             "sessionID" => %{min_length: 1, max_length: 128}
           }

    assert routes["/api/v1/reference-data/publications/{publicationID}"].params == %{
             "publicationID" => %{min_length: 1, max_length: 256}
           }
  end

  test "the wave's uniformity property: no route is silently half-wired" do
    actions = Router.routes() |> Enum.map(& &1.action) |> Enum.frequencies()

    # Three servable clauses now: health, version, and the v2 sync push, which
    # reached `VitrialSync.PushService` and `VitrialSync.Auth` and is the first
    # route that answers from an engine rather than for itself. The count was 21
    # not-wired and is now 20.
    assert actions[{:serve, :health}] == 1
    assert actions[{:serve, :version}] == 1
    assert actions[{:serve, :sync_push}] == 1

    # The property this test exists for is not a NUMBER — it is that no route is
    # routed and then left unanswered. Asserting the exact not-wired count would
    # make every honest wiring a test failure, which is how a test that guards a
    # property becomes a test that guards a moment.
    assert Map.has_key?(actions, :not_wired)

    # v1 push stays not-wired deliberately: the reference registers the handler
    # twice, but only after confirming v1's `deviceID` requirement, and that is
    # not something to guess at.
    assert actions[:not_wired] == Router.routes() |> length() |> Kernel.-(3)
  end
end
