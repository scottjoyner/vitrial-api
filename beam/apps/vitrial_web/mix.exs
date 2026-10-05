defmodule VitrialWeb.MixProject do
  use Mix.Project

  # HTTP boundary: cowboy listeners, routing, body admission and the health surface.
  #
  # The four path keys below are what make this app a member of the umbrella rather
  # than a project that happens to sit under apps/. They redirect _build, deps,
  # config and mix.lock to the umbrella root, which is why there is exactly one
  # resolved dependency set and one lockfile for all eight apps instead of eight
  # independently auditable ones. `mix new` writes them when the parent is an
  # umbrella; they are repeated here because dropping them silently reverts this
  # app to a private _build and a private mix.lock.

  def project do
    [
      app: :vitrial_web,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      build_path: "../../_build",
      config_path: "../../config/config.exs",
      deps_path: "../../deps",
      lockfile: "../../mix.lock",
      deps: deps()
    ]
  end

  def application do
    [
      # :crypto -- UUIDv4 generation and SHA-256 correlation refs
      # (VitrialWeb.Correlation). No hex UUID package: the doctrine sizes the
      # native implementation at six lines over :crypto.strong_rand_bytes/1
      # (DEPENDENCY-DOCTRINE.md §1.1).
      extra_applications: [:logger, :crypto],
      mod: {VitrialWeb.Application, []}
    ]
  end

  defp deps do
    [
      # The boundary reaches the engine. `in_umbrella` rather than a versioned
      # hex dep: a path inside one deployment unit is not a published package,
      # and a version number on it would misdescribe the release process.
      #
      # Direction is fixed by `vitrial_sync`'s own edge: the boundary depends on
      # the engine, never the reverse. `vitrial_web` is the outermost layer in
      # every sense — it owns the socket — and the engine must not know a socket
      # exists.
      {:vitrial_sync, in_umbrella: true},
      # The umbrella's first hex package, admitted 2026-10-03 with the verdict
      # already on the record: DEPENDENCY-DOCTRINE.md §3.1 ADOPTs cowboy 2.19.0
      # (+ mandatory cowlib 2.20.0, ranch 2.3.0 -- zero-dependency siblings) as
      # the HTTP/1.1 edge, and §3.2 REJECTs bandit (8-package closure for 23
      # routes and no WebSocket) and plug (a framework replacing 23 case
      # clauses; its one useful export, Plug.Crypto.secure_compare/2, is already
      # native as crypto:hash_equals/2). The pin is the doctrine's own shape
      # (§3.3): a bare version, which Hex reads as an exact requirement, not a
      # range. mix.lock comes into existence with this entry and is committed.
      {:cowboy, "2.19.0"}
    ]
  end
end
