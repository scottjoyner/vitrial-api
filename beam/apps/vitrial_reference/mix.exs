defmodule VitrialReference.MixProject do
  use Mix.Project

  # Reference data and its published manifests, resolved from canonical server state.
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
      app: :vitrial_reference,
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
      extra_applications: [:logger],
      mod: {VitrialReference.Application, []}
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
