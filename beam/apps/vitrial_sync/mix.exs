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
      extra_applications: [:logger, :epgsql],
      mod: {VitrialSync.Application, []}
    ]
  end

  defp deps do
    [
      # First admission, wave 5: the PostgreSQL wire protocol. The specific need is
      # `VitrialSync.Db` — a hand-written connection pool plus row codecs over 14
      # sync-path tables. OTP provides nothing for the wire protocol (no `postgres`
      # application in OTP 27/29 — doctrine §1.1), and hand-rolling v3 framing +
      # SCRAM auth is 3,000-5,000 security-sensitive lines, past any "rewrite
      # natively" bar (doctrine §5.3). Verdict: ADOPT, per
      # `../vitrial-phase0/DEPENDENCY-DOCTRINE.md §4.2` (there is no
      # `beam/DEPENDENCY-DOCTRINE.md`; the doctrine lives one directory over).
      #
      # The caveat, restated verbatim from §3.1/§5.2 so it cannot be lost in this
      # file: epgsql's last hex release is 2025-06-06, ~16 months stale as of the
      # 2026-10-02 ruling, and it carries 47 open issues, while the repository was
      # pushed 2026-08-16 — active code, simply not cut. Accepted knowingly.
      # Doctrine §5.2's own yield-rule ("if epgsql is 6+ months stale at
      # implementation time, the rule yields to postgrex") therefore remains
      # triggered and unresolved in the doctrine text; the ask, §4.2 and
      # ARCHITECTURE.md §Wave-5 all resolve to ADOPT-epgsql, and this boundary is
      # what keeps that decision revisitable: postgrex remains the one-module
      # switch (§4.2/§5.2) if the query surface ever outgrows hand-written SQL,
      # because nothing above `VitrialSync.Db` sees an epgsql type. postgrex
      # (0.22.4, 2026-08-07) is REJECTED here along with ecto/ecto_sql and
      # db_connection — this service's writes are hand-rolled state machines, and
      # `db_connection` buys a second pool under the one we own.
      {:epgsql, "4.8.0"}
    ]
  end
end
