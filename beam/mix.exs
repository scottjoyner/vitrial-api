defmodule Beam.MixProject do
  use Mix.Project

  # Umbrella root for the Vitrial BEAM services.
  #
  # One lockfile governs every service app, and in a Mix umbrella that is
  # automatic: children carry no lockfile of their own, they read the umbrella
  # root's `beam/mix.lock`. The doctrine here is minimal dependency surface, and
  # one resolved set is far easier to audit for CVEs than eight independent ones.
  #
  # build_path/deps_path/lockfile are therefore left at their Mix defaults, which
  # resolve inside `beam/`. An earlier revision pointed them at the repository
  # root (`../_build`, `../deps`, `../mix.lock`). That puts the whole BEAM build
  # tree and every fetched dependency next to `app/`, `tests/` and `migrations/`,
  # inside a repository whose root .gitignore covers none of them -- so
  # `mix deps.get` leaves an untracked, unreviewable dependency tree in the
  # Python project, and a repo-root search sweeps hex package sources into every
  # result. Same single-lockfile property, contained estate.

  def project do
    [
      app: :beam,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      deps: deps(),
      apps_path: "apps",
      elixirc_paths: ["lib"],
      test_elixirc_paths: ["test"],
      dialyzer: [plt_add_apps: [:mix]]
    ]
  end

  def application do
    [
      extra_applications: [:logger, :crypto, :public_key, :ssl]
    ]
  end

  defp deps do
    []
  end
end
