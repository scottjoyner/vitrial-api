defmodule VitrialSync.MixProject do
  use Mix.Project

  # Sync engine: bounded push/pull pages, cursor discipline, clientMutationID idempotency.
  #
  # The four path keys below are what make this app a member of the umbrella rather
  # than a project that happens to sit under apps/. They redirect _build, deps,
  # config and mix.lock to the umbrella root, which is why there is exactly one
  # resolved dependency set and one lockfile for all eight apps instead of eight
  # independently auditable ones. `mix new` writes them when the parent is an
  # umbrella; they are repeated here because dropping them silently reverts this
  # app to a private _build and a private mix.lock.
  #
  # `elixirc_paths` is the fifth path key, and for the same kind of reason. It is
  # the only thing that puts `test/support/` on the compile path, which is where
  # `VitrialSync.PythonReference` lives -- the wrapper that runs the Python
  # parity oracle from `parity_oracle.py`. Without it that module is not
  # compiled and the differential suite fails with
  # `VitrialSync.PythonReference.load!/0 is undefined (module
  # VitrialSync.PythonReference is not available)`, which reads like a typo
  # rather than a missing project setting and lands the whole suite as a file
  # nothing executes. `mix format --check-formatted` already covers
  # `test/support/`, so format is not affected either way.

  def project do
    [
      app: :vitrial_sync,
      version: "0.1.0",
      elixir: "~> 1.19",
      start_permanent: Mix.env() == :prod,
      build_path: "../../_build",
      config_path: "../../config/config.exs",
      deps_path: "../../deps",
      lockfile: "../../mix.lock",
      elixirc_paths: elixirc_paths(Mix.env()),
      deps: deps()
    ]
  end

  # `test/support` is test-only. Compiling it into :dev or :prod would ship the
  # oracle's `System.cmd` plumbing in a release, pointing at a reference tree
  # that is not there.
  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  def application do
    [
      extra_applications: [:logger],
      mod: {VitrialSync.Application, []}
    ]
  end

  defp deps do
    [
      # Empty by design. Dependencies are admitted one at a time, with the specific
      # need written down, so that the exposure window of any third-party code and
      # its transitive CVE surface is a decision on the record rather than a side
      # effect of scaffolding.
    ]
  end
end
