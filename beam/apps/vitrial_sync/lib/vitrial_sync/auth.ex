defmodule VitrialSync.Auth do
  @moduledoc """
  Resolving a bearer token to a `VitrialAuth.Principal`.

  ## Why this module exists at all

  It is three lookups and one call, and it could have lived in either app. It
  lives here because both alternatives invert something:

  * In `VitrialSync.Db.Reads`, the engine's database layer would depend on the
    authentication app — the engine would acquire a dependency on a component that
    exists to serve it, and the layering stops pointing the way the doctrine says.
  * In `VitrialAuth`, that app would depend on the engine to reach the reads, and
    `vitrial_auth` would drag the whole umbrella in behind it — for four
    functions, none of which need the engine.

  Above both is the only place that depends on both, which is what a seam is.

  ## The one place a Principal is minted

  `VitrialAuth.authenticate/5` owns every decision — expiry, revocation, membership
  currency, the session/membership cross-check. This module owns the three
  lookups and nothing else, and it returns the principal the auth module built
  rather than constructing one. A caller that needs a principal comes through
  here; there is no second path to one.

  ## Why a token is looked up BY ITS HASH

  `app/auth.py:45` selects `AuthSession` rows by `access_token_hash`, and
  `app/auth.py:24-26` says why: storing only the digest means a disclosure of
  `auth_sessions` yields hashes, and a hash of a bearer token cannot be presented
  as a bearer token. The plaintext token goes into `:crypto.hash(:sha256, ...)`
  and never reaches a comparison or a log.

  ## `now` is a parameter

  Not read here. The expiry boundary is `expires_at <= now`
  (`app/auth.py:46`), so a caller testing that edge has to be able to place `now`
  exactly on it — which is why `VitrialAuth.authenticate/5` takes it too.
  """

  alias VitrialAuth
  alias VitrialSync.Db.Reads

  @typedoc "A `VitrialSync.Db.Pool` pid or registered name."
  @type pool :: pid() | atom()

  # Why the POOL and not a connection: every caller has a pool (the application
  # owns one) and no caller has a connection, because a connection is only valid
  # inside `Pool.with_conn/2`. The first version took a `conn` and every call
  # died with
  #
  #     ** (FunctionClauseError) no function clause matching in
  #        VitrialSync.Db.Pgconn.query/3
  #        def query(conn, sql, params) when is_pid(conn) and is_binary(sql)
  #
  # because a registered pool name is an ATOM, not a pid — and the error named
  # the wrong function, three frames from the wrong assumption.

  @doc """
  Resolve an `Authorization` header to `{principal, event}`, or `{:error, reason}`.

  `reason` is `VitrialAuth`'s own vocabulary — `:authentication_required`,
  `:session_expired_or_revoked`, `:membership_inactive`,
  `:session_membership_mismatch` — because those are the answers a client can act
  on, and re-spelling them here would be a second translation table to keep
  honest. A database failure is `{:error, {:load, reason}}`, which is a different
  shape precisely because it is not the client's fault.
  """
  @spec authenticate(pool(), String.t() | nil, DateTime.t()) ::
          {:ok, VitrialAuth.Principal.t(), map()} | {:error, atom() | {:load, term()}}
  def authenticate(pool, authorization_header, now \\ DateTime.utc_now()) do
    VitrialSync.Db.with_conn(pool, fn conn ->
      with {:ok, token} <- bearer_token(authorization_header),
           {:ok, session} <- fetch(conn, &Reads.auth_session(&1, hash(token))),
           {:ok, membership} <- fetch_related(conn, session, &Reads.membership/2),
           {:ok, organization} <-
             fetch(conn, &Reads.organization(&1, session.organization_id)) do
        VitrialAuth.authenticate(authorization_header, session, membership, organization, now)
      end
    end)
  end

  # The digest of the SUPPLIED token. `token_hash/1` is `VitrialAuth`'s, so the
  # storage key and the lookup key cannot drift apart — a second implementation
  # here would be a second answer to "what is the storage key for this token",
  # and the two disagreeing is a silent 401 for every client.
  defp hash(token), do: VitrialAuth.token_hash(token)

  # `Bearer <token>`, `app/auth.py:39`. The scheme comparison belongs to
  # `VitrialAuth`, so this only extracts; a header that is not bearer-shaped never
  # reaches the digest computation at all.
  defp bearer_token(header) when is_binary(header) do
    case VitrialAuth.parse_authorization(header) do
      {:ok, "bearer", token} -> {:ok, token}
      _ -> {:error, :authentication_required}
    end
  end

  defp bearer_token(_header), do: {:error, :authentication_required}

  # `fetch/2` wraps a read so a DATABASE failure is distinguishable from a MISSING
  # row. Both are `{:ok, nil}`-shaped otherwise, and collapsing them would turn a
  # dropped connection into "your session expired" — which sends a client to
  # re-authenticate over a problem that is ours.
  defp fetch(conn, read) do
    case read.(conn) do
      {:ok, nil} -> {:error, :authentication_required}
      {:ok, row} -> {:ok, row}
      {:error, reason} -> {:error, {:load, reason}}
    end
  end

  # The membership read is keyed on the SESSION, so a session that does not exist
  # never reaches it — there is no `membership_id` to look up, and asking for one
  # would be a query against a value known to be absent.
  #
  # A missing membership is `:membership_inactive` rather than
  # `:authentication_required` on purpose. The two are the same refusal at the
  # boundary (403) but they mean different things to a client, and collapsing them
  # would hide which one happened.
  defp fetch_related(conn, session, read) do
    case read.(conn, session.membership_id) do
      {:ok, nil} -> {:error, :membership_inactive}
      {:ok, row} -> {:ok, row}
      {:error, reason} -> {:error, {:load, reason}}
    end
  end
end
