defmodule VitrialSync.Db.Config do
  @moduledoc """
  The database environment surface, read once at pool start.

  This is `app/settings.py`'s `database_*` block (`:8-9`, `:26-40`) under the
  exact names the two compose files set
  (`deploy/compose.acceptance.yml:4-10`, `deploy/compose.production.yml:4-10`).
  The names are a deployed contract, not a naming preference: an operator tunes
  `DATABASE_MAX_OVERFLOW` today and this estate has to hear them tomorrow.

  Semantics are pydantic-settings': every variable is read from the process
  environment **when the pool starts**, with the reference's default when unset.
  Reading at start (not at `config.exs` compile time) is what makes the pool a
  normal supervision child — the same image boots into the gate (no database,
  no env), into acceptance and into production with nothing recompiled. A value
  that does not parse fails right here, at boot, naming the variable — a bad
  `DATABASE_POOL_SIZE` must not surface later as a checkout failure three
  requests deep.
  """

  alias VitrialSync.Db.Url

  defstruct [
    :url,
    :null_pool,
    :pool_size,
    :max_overflow,
    :pool_timeout_ms,
    :recycle_ms,
    :statement_timeout_ms
  ]

  @type t :: %__MODULE__{
          url: Url.t(),
          null_pool: boolean(),
          pool_size: pos_integer(),
          max_overflow: non_neg_integer(),
          pool_timeout_ms: pos_integer(),
          recycle_ms: non_neg_integer(),
          statement_timeout_ms: pos_integer()
        }

  @doc "Reference defaults, `app/settings.py:8-9` and `:26-40`."
  @spec defaults() :: keyword()
  def defaults do
    [
      url: "postgresql://vitrial:vitrial@localhost:5432/vitrial",
      null_pool: false,
      pool_size: 10,
      max_overflow: 5,
      pool_timeout_seconds: 10.0,
      recycle_seconds: 300,
      statement_timeout_ms: 15_000
    ]
  end

  @doc """
  Read the environment into a `t`.

  `overrides` lets tests and the `VitrialSync.Application` child spec pin values
  explicitly; anything not overridden comes from the environment, and anything
  the environment does not set falls back to `defaults/0`.
  """
  @spec load(keyword()) :: t()
  def load(overrides \\ []) do
    base = defaults()
    over = Map.new(overrides)

    %__MODULE__{
      url: override_or_env(over, :url, "DATABASE_URL", base[:url]) |> parse_url(),
      null_pool:
        override_or_env(over, :null_pool, "DATABASE_NULL_POOL", base[:null_pool])
        |> parse_bool("DATABASE_NULL_POOL"),
      pool_size:
        override_or_env(over, :pool_size, "DATABASE_POOL_SIZE", base[:pool_size])
        |> parse_int("DATABASE_POOL_SIZE"),
      max_overflow:
        override_or_env(over, :max_overflow, "DATABASE_MAX_OVERFLOW", base[:max_overflow])
        |> parse_int("DATABASE_MAX_OVERFLOW"),
      pool_timeout_ms:
        override_or_env(
          over,
          :pool_timeout_seconds,
          "DATABASE_POOL_TIMEOUT_SECONDS",
          base[:pool_timeout_seconds]
        )
        |> parse_float("DATABASE_POOL_TIMEOUT_SECONDS")
        |> Kernel.*(1000)
        |> trunc(),
      recycle_ms:
        override_or_env(
          over,
          :recycle_seconds,
          "DATABASE_POOL_RECYCLE_SECONDS",
          base[:recycle_seconds]
        )
        |> parse_int("DATABASE_POOL_RECYCLE_SECONDS")
        |> Kernel.*(1000),
      statement_timeout_ms:
        override_or_env(
          over,
          :statement_timeout_ms,
          "DATABASE_STATEMENT_TIMEOUT_MS",
          base[:statement_timeout_ms]
        )
        |> parse_int("DATABASE_STATEMENT_TIMEOUT_MS")
    }
    |> validate()
  end

  # Reads at start, like pydantic-settings. NOT compile time: see the moduledoc.
  defp override_or_env(over, key, env, default) do
    case Map.fetch(over, key) do
      {:ok, value} -> value
      :error -> System.get_env(env) || default
    end
  end

  defp parse_url(value) do
    case Url.parse(value) do
      {:ok, url} -> url
      {:error, message} -> raise ArgumentError, message
    end
  end

  # pydantic parses booleans from "true"/"false"/"1"/"0"/"yes"/"no"/"on"/"off",
  # case-insensitive. That is the accepted vocabulary here too; anything else is
  # a boot failure, not a falsy default — DATABASE_NULL_POOL=false is what the
  # compose files set, and DATABASE_NULL_POOL=flase must not quietly become
  # "pooling on".
  defp parse_bool(value, env) when is_boolean(value), do: value

  defp parse_bool(value, env) when is_binary(value) do
    case String.downcase(String.trim(value)) do
      t when t in ["true", "1", "yes", "on"] -> true
      f when f in ["false", "0", "no", "off"] -> false
      other -> raise ArgumentError, "#{env} must be a boolean, got: #{inspect(other)}"
    end
  end

  defp parse_bool(value, env),
    do: raise(ArgumentError, "#{env} must be a boolean, got: #{inspect(value)}")

  defp parse_int(value, env) when is_integer(value), do: value

  defp parse_int(value, env) when is_binary(value) do
    case Integer.parse(String.trim(value)) do
      {int, ""} -> int
      _ -> raise ArgumentError, "#{env} must be an integer, got: #{inspect(value)}"
    end
  end

  defp parse_int(value, env),
    do: raise(ArgumentError, "#{env} must be an integer, got: #{inspect(value)}")

  defp parse_float(value, env) when is_integer(value), do: value * 1.0
  defp parse_float(value, env) when is_float(value), do: value

  defp parse_float(value, env) when is_binary(value) do
    case Float.parse(String.trim(value)) do
      {float, ""} -> float
      _ -> raise ArgumentError, "#{env} must be a number, got: #{inspect(value)}"
    end
  end

  defp parse_float(value, env),
    do: raise(ArgumentError, "#{env} must be a number, got: #{inspect(value)}")

  # Bound the same values the reference leaves to the operator's judgement. A
  # zero-or-negative pool size would make every checkout queue for the full
  # timeout before failing, which is the cascading-outage shape
  # app/settings.py:14-25 exists to prevent — refuse it at boot instead.
  defp validate(config) do
    unless config.pool_size >= 1 do
      raise ArgumentError, "DATABASE_POOL_SIZE must be at least 1, got: #{config.pool_size}"
    end

    unless config.max_overflow >= 0 do
      raise ArgumentError, "DATABASE_MAX_OVERFLOW must be at least 0, got: #{config.max_overflow}"
    end

    unless config.pool_timeout_ms >= 1 do
      raise ArgumentError,
            "DATABASE_POOL_TIMEOUT_SECONDS must be positive, got: #{config.pool_timeout_ms / 1000}"
    end

    unless config.recycle_ms >= 0 do
      raise ArgumentError,
            "DATABASE_POOL_RECYCLE_SECONDS must be at least 0, got: #{config.recycle_ms / 1000}"
    end

    unless config.statement_timeout_ms >= 1 do
      raise ArgumentError,
            "DATABASE_STATEMENT_TIMEOUT_MS must be positive, got: #{config.statement_timeout_ms}"
    end

    config
  end
end
