defmodule VitrialSync.Db.Url do
  @moduledoc """
  The `DATABASE_URL` parser: the one piece of `sqlalchemy.engine.URL` behaviour
  the pool actually needs.

  The deployed contract (`deploy/compose.acceptance.yml:4` and
  `compose.production.yml:4`) sets `DATABASE_URL` to a SQLAlchemy-style URL, and
  the reference default carries a driver segment —
  `postgresql+asyncpg://vitrial:vitrial@localhost:5432/vitrial`
  (`app/settings.py:8`). epgsql wants host/port/user/password/database and
  nothing else, so this module:

    * accepts the `postgresql` and `postgres` schemes, with or without a
      `+driver` suffix, which is stripped (`+asyncpg`, and any other dialect
      tag SQLAlchemy might carry);
    * maps userinfo, host, port (default 5432) and database path positionally;
    * **fails loud on query parameters.** SQLAlchemy would interpret `?sslmode=
      require` and silently change the connection's security posture; epgsql
      4.8.0 would ignore it. A parameter that one side honours and the other
      drops is a silent contract break, so the parser refuses the whole URL and
      names the parameter. `sslmode` is called out by name because it is the one
      that turns a silently-unencrypted connection into a deployment.

  A malformed URL raises at pool start rather than failing per-checkout. That
  matches pydantic-settings semantics — `app/settings.py` constructs `Settings()`
  at import time, so a bad value in the reference kills the process at boot too.
  """

  defstruct [:scheme, :username, :password, :host, :port, :database]

  @type t :: %__MODULE__{
          scheme: String.t(),
          username: String.t(),
          password: String.t(),
          host: String.t(),
          port: pos_integer(),
          database: String.t()
        }

  @supported_schemes ["postgresql", "postgres"]
  @default_port 5432

  @doc """
  Parse a SQLAlchemy-style PostgreSQL URL.

  Returns `{:ok, url}` or `{:error, message}` where the message names what was
  wrong with the value. Used by `VitrialSync.Db.Config`, which converts the
  error into a boot-time failure naming `DATABASE_URL`.
  """
  @spec parse(String.t()) :: {:ok, t()} | {:error, String.t()}
  def parse(value) when is_binary(value) do
    with {:ok, scheme} <- parse_scheme(value),
         %URI{userinfo: userinfo, host: host, port: port, path: path, query: query} <-
           URI.parse(value),
         :ok <- no_query(query),
         {:ok, {username, password}} <- userinfo(userinfo),
         :ok <- require_host(host),
         {:ok, database} <- database(path) do
      {:ok,
       %__MODULE__{
         scheme: scheme,
         username: username,
         password: password,
         host: host,
         port: port || @default_port,
         database: database
       }}
    end
  end

  def parse(other), do: {:error, "DATABASE_URL must be a string, got: #{inspect(other)}"}

  # The scheme is everything before the first "://", possibly with a "+driver"
  # suffix. URI.parse does not expose it directly, so it is split off by hand:
  # "postgresql+asyncpg" is not a URI scheme URI.parse understands, but the
  # remainder parses fine once the suffix is gone.
  defp parse_scheme(value) do
    case String.split(value, "://", parts: 2) do
      [raw_scheme, _rest] ->
        base = raw_scheme |> String.split("+", parts: 2) |> hd()

        if base in @supported_schemes do
          {:ok, base}
        else
          {:error,
           "DATABASE_URL scheme #{inspect(raw_scheme)} is not supported " <>
             "(expected postgresql or postgres, with or without a +driver suffix)"}
        end

      [_no_scheme] ->
        {:error, "DATABASE_URL has no scheme:// prefix: #{inspect(value)}"}
    end
  end

  # One named parameter, because it is the one whose loss would be silent and
  # security-relevant; every other parameter gets the same treatment with a
  # generic message. There is no allow-list: epgsql 4.8.0 reads none of them.
  defp no_query(nil), do: :ok

  defp no_query(query) do
    params = URI.decode_query(query)

    case Map.fetch(params, "sslmode") do
      {:ok, mode} ->
        {:error,
         "DATABASE_URL carries sslmode=#{inspect(mode)}; epgsql 4.8.0 does not read URL " <>
           "parameters, so the setting would be silently dropped. TLS is not configured " <>
           "for this deployment yet: remove the parameter, or fail this boot and add " <>
           "explicit TLS options to VitrialSync.Db.Conn first"}

      :error ->
        {:error,
         "DATABASE_URL carries query parameters #{inspect(Map.keys(params))} that this " <>
           "parser does not interpret; an honoured-here-dropped-there parameter is a " <>
           "silent contract break, so the URL is refused. Move the setting into " <>
           "VitrialSync.Db.Config/Conn instead"}
    end
  end

  defp userinfo(nil), do: {:ok, {"", ""}}

  defp userinfo(raw) do
    case String.split(raw, ":", parts: 2) do
      [username] ->
        {:ok, {URI.decode_www_form(username), ""}}

      [username, password] ->
        {:ok, {URI.decode_www_form(username), URI.decode_www_form(password)}}
    end
  end

  defp require_host(host) when is_binary(host) and host != "", do: :ok
  defp require_host(host), do: {:error, "DATABASE_URL has no host (got: #{inspect(host)})"}

  defp database("/" <> name) when name != "", do: {:ok, name}

  defp database(path),
    do: {:error, "DATABASE_URL has no database name in its path (got: #{inspect(path)})"}
end
