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
      # `vitrial_sync` -> `vitrial_auth`, ONE WAY, and the direction is the whole
      # point.
      #
      # `VitrialSync.Auth` resolves a bearer token to a `VitrialAuth.Principal`, so
      # the engine needs the authentication app's decisions. The reverse edge does
      # not exist and must not: `vitrial_auth` references `vitrial_sync` only in
      # comments, so adding this here is acyclic. Adding it to `vitrial_auth`
      # instead would be a cycle, and would make the app whose job is deciding
      # about tokens depend on the whole sync engine to do it.
      #
      # This is the FIRST sibling dependency in the umbrella (it was 0 of 8). Like
      # `vitrial_web`'s, it is `in_umbrella` rather than a versioned hex dep: a
      # path inside one deployment unit is not a published package, and a version
      # number on it would misdescribe the release process.
      {:vitrial_auth, in_umbrella: true}
      # Empty by design, and now empty BY ACHIEVEMENT rather than by deferral.
      #
      # This list once carried epgsql, admitted for wave 5 (the PostgreSQL wire
      # protocol). It was removed in favour of VitrialSync.Db.Pgconn — a
      # hand-written version 3 frontend/backend client over OTP's `:gen_tcp`.
      # The measurement that made the swap cheap: the whole coupling was three
      # functions (`:epgsql.connect/1`, `:epgsql.squery/2`, `:epgsql.close/1`),
      # all three inside one module. Codec, Rows, Url, Config, Writes and Pool
      # were PostgreSQL knowledge wearing a driver's name and are unchanged.
      #
      # What the native client gains over the driver is not fewer lines, it is
      # control the driver would not give: statement_timeout and
      # idle_in_transaction_session_timeout go in the STARTUP PACKET, because
      # epgsql 4.8.0 has no startup-parameter hook
      # (deps/epgsql/src/commands/epgsql_cmd_connect.erl:59-78) and could only
      # apply them as post-connect SETs — which means a connection handed out
      # without its statement timeout, the exact pool-exhaustion failure
      # app/settings.py:36-40 exists to prevent.
      #
      # The doctrine is to stay native: OTP ships no `postgres` application, so
      # the protocol has to be reached somehow, and reaching it through a driver
      # inherits every CVE in that driver's tree.
    ]
  end
end
