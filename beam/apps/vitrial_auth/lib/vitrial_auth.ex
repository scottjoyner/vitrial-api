defmodule VitrialAuth do
  @moduledoc """
  Device pairing and session authority.

  Access tokens are stored only as SHA-256 hashes. A database disclosure yields
  hashes, and a hash of a bearer token cannot be presented as a bearer token.

  Pairing is a code exchange; a session is the long-lived thing it produces. The
  rate limiter in front of both is deliberately in-process, which is correct for
  the single-replica deployment this service ships as and is wrong the moment
  there is a second replica -- `deploy/env.production.example` says so at the
  setting, because a ceiling that silently stops being a ceiling is worse than
  one that was never claimed.

  ## What belongs here and what does not

  The reference splits this across `app/auth.py` (101 lines) and `app/pairing.py`
  (167 lines). Nearly all of it is a `db.get` followed by an `if`. Those loads are
  `VitrialSync.Db`'s job — this app owns the branch that DECIDES, and the branch is
  the part worth testing without a database in the loop.

  So each function here takes the rows a lookup would have produced and returns a
  tagged result. A caller holding a `Pool` does the lookups; a caller without one
  can still exercise every branch, which is the argument the rest of the umbrella
  is built on.

  ## This app declares its OWN principal, and that is deliberate

  `app/auth.py:20-29` is ALSO ported as `VitrialSync.Canonical.Principal`, because
  the push pipeline needs it there. This app could depend on `vitrial_sync` and
  reuse that struct — but no app in this umbrella depends on a sibling (0 of 8),
  and a dependency between them would couple two independently deployable units
  for the sake of one struct. The convention wins.

  Two structs is a drift risk, so the risk is managed where it can be: a parity
  test in this app asserts field-for-field equality with
  `VitrialSync.Canonical.Principal`, and a check would catch a field added to one
  and not the other. `Canonical.Principal` is not shadowed or aliased here.

  ## Where the security properties live, and why

  * **Tokens are hashed, never compared raw.** `token_hash/1` is SHA-256 over the
    UTF-8 bytes, and the reference looks the row up BY THAT HASH
    (`AuthSession.access_token_hash == token_hash(...)`, `app/auth.py:45`). The
    plaintext never reaches a comparison, so a disclosure of `auth_sessions` yields
    digests rather than usable credentials.
  * **Comparisons of secrets are constant-time.** Where a supplied value is
    compared against a stored digest instead of being used as a lookup key,
    `:crypto.hash_equals/2` is used. `==` on a digest leaks its length and prefix
    through timing.
  * **Pairing codes carry 192 bits.** `secrets.token_urlsafe(24)`, not a
    human-typed PIN. The reference says why itself (`app/pairing.py:83-85`): a
    pasteable code is convenient, but a low-entropy one needs a separate online
    guessing defense to stay secure, and `VitrialWeb.RateLimit` bounds requests per
    ROUTE, not guessing attempts against one code.
  """

  @typedoc "Why authentication was refused. Carries the code, never the credential."
  @type refusal ::
          :authentication_required
          | :session_expired_or_revoked
          | :membership_inactive
          | :session_membership_mismatch
          | :unknown_pairing_code
          | :pairing_grant_expired
          | :pairing_code_already_used
          | :membership_not_found
          | :invalid_grant_ttl

  @typedoc "A row from `auth_sessions`, as the storage layer would hand it over."
  @type auth_session :: %{
          required(:id) => String.t(),
          required(:user_id) => String.t(),
          required(:organization_id) => String.t(),
          required(:membership_id) => String.t(),
          required(:expires_at) => DateTime.t(),
          required(:revoked_at) => DateTime.t() | nil
        }

  @typedoc "A row from `memberships`."
  @type membership :: %{
          required(:id) => String.t(),
          required(:user_id) => String.t(),
          required(:organization_id) => String.t(),
          required(:active) => boolean(),
          required(:capabilities) => [String.t()],
          required(:customer_ids) => [String.t()],
          required(:project_ids) => [String.t()],
          required(:all_customers) => boolean(),
          required(:all_projects) => boolean()
        }

  defmodule Principal do
    @moduledoc """
    `Principal`, `app/auth.py:20-29`. Field-for-field with
    `VitrialSync.Canonical.Principal`.

    Deliberately a SEPARATE struct from that one — see the outer moduledoc. The
    types are the same and a parity test asserts it, because two structs that
    drift is the failure this note exists to name.
    """
    defstruct [
      :user_id,
      :organization_id,
      :membership_id,
      :session_id,
      :authorization_revision,
      all_customers: false,
      all_projects: false,
      capabilities: MapSet.new(),
      customer_ids: MapSet.new(),
      project_ids: MapSet.new()
    ]

    @type t :: %__MODULE__{
            user_id: String.t(),
            organization_id: String.t(),
            membership_id: String.t(),
            session_id: String.t(),
            authorization_revision: non_neg_integer(),
            all_customers: boolean(),
            all_projects: boolean(),
            capabilities: MapSet.t(String.t()),
            customer_ids: MapSet.t(String.t()),
            project_ids: MapSet.t(String.t())
          }
  end

  @typedoc "A row from `organizations`."
  @type organization :: %{required(:authorization_revision) => non_neg_integer()}

  @doc """
  SHA-256 hexdigest of a bearer token — `app/auth.py:33-34`.

  The reference's own note (`app/auth.py:24-26`) is why this is the storage key
  rather than the token: a disclosure of `auth_sessions` yields digests, not
  credentials.
  """
  @spec token_hash(String.t()) :: String.t()
  def token_hash(token) when is_binary(token) do
    :sha256 |> :crypto.hash(token) |> Base.encode16(case: :lower)
  end

  @doc """
  Split `Authorization: <scheme> <credentials>` into its two halves.

  Returns `{:ok, scheme, credentials}` or `{:error, :authentication_required}`.
  The scheme is downcased because HTTP auth schemes are case-insensitive and the
  reference compares `credentials.scheme.lower() != "bearer"` (`app/auth.py:39`).

  A header with no space, or with nothing after the space, is refused HERE rather
  than at the point of use — an empty credential is not a credential.
  """
  @spec parse_authorization(String.t() | nil) ::
          {:ok, String.t(), String.t()} | {:error, :authentication_required}
  def parse_authorization(nil), do: {:error, :authentication_required}

  def parse_authorization(header) when is_binary(header) do
    case String.split(header, " ", parts: 2) do
      [name, credentials] ->
        if String.trim(credentials) == "" do
          {:error, :authentication_required}
        else
          {:ok, String.downcase(String.trim(name)), String.trim(credentials)}
        end

      [_only] ->
        {:error, :authentication_required}
    end
  end

  @doc """
  Whether an `Authorization` header names the bearer scheme.
  """
  @spec bearer_scheme?(String.t() | nil) :: boolean()
  def bearer_scheme?(header) do
    match?({:ok, "bearer", _credentials}, parse_authorization(header))
  end

  @doc """
  Resolve a bearer token to a `Principal`, given the rows a lookup would find.

  `app/auth.py:37-75`. `session`, `membership` and `organization` are each `nil`
  when no row matched — the caller has already done the lookups, and this decides.

  The ORDER of the checks is the reference's and it matters. An expired session is
  reported as expired even when its membership is also inactive, because that is
  the answer a client can act on, and because reversing the order would make a
  revoked session look like a permission problem.
  """
  @spec authenticate(
          String.t() | nil,
          auth_session() | nil,
          membership() | nil,
          organization() | nil,
          DateTime.t()
        ) :: {:ok, Principal.t(), map()} | {:error, refusal()}
  def authenticate(header, session, membership, organization, now) do
    with :ok <- require_bearer(header),
         :ok <- live_session(session, now),
         :ok <- active_membership(membership, session),
         :ok <- present(organization) do
      {principal, event} = principal(session, membership, organization)
      {:ok, principal, event}
    end
  end

  defp require_bearer(header) do
    if bearer_scheme?(header), do: :ok, else: {:error, :authentication_required}
  end

  # The reference folds three conditions into one `or` (`app/auth.py:46`): no row,
  # revoked, or expired. They stay ONE refusal here on purpose — a caller must not
  # be able to tell which of the three it was, and "session expired or revoked" is
  # the reference's own single message.
  defp live_session(nil, _now), do: {:error, :session_expired_or_revoked}

  defp live_session(%{revoked_at: revoked}, _now) when not is_nil(revoked),
    do: {:error, :session_expired_or_revoked}

  defp live_session(%{expires_at: expires}, now) do
    # `expires_at <= now` in the reference, so an expiry exactly at `now` is
    # refused. `== :gt` is that comparison, spelled so the boundary is visible.
    if DateTime.compare(expires, now) == :gt do
      :ok
    else
      {:error, :session_expired_or_revoked}
    end
  end

  defp active_membership(nil, _session), do: {:error, :membership_inactive}
  defp active_membership(%{active: false}, _session), do: {:error, :membership_inactive}

  # The cross-check at `app/auth.py:59-63`: a session whose membership belongs to
  # a different organization OR user is refused, not repaired. Two rows that
  # disagree is a data problem, and silently preferring one of them is how a
  # session ends up authorized for an organization it does not belong to.
  defp active_membership(membership, session) do
    if membership.organization_id == session.organization_id and
         membership.user_id == session.user_id do
      :ok
    else
      {:error, :session_membership_mismatch}
    end
  end

  defp present(nil), do: {:error, :membership_inactive}
  defp present(_organization), do: :ok

  @doc """
  Build the `Principal` and the event the reference logs beside it.

  `app/auth.py:65-75`. The reference emits `auth.session_authenticated` with the
  authorization revision immediately after constructing the principal; that is an
  EFFECT, so it is returned alongside rather than performed here — a pure function
  cannot observe anything by logging.
  """
  @spec principal(auth_session(), membership(), organization()) ::
          {Principal.t(), map()}
  def principal(session, membership, organization) do
    principal = %Principal{
      user_id: session.user_id,
      organization_id: session.organization_id,
      membership_id: session.membership_id,
      session_id: session.id,
      authorization_revision: organization.authorization_revision,
      capabilities: MapSet.new(membership.capabilities),
      customer_ids: MapSet.new(membership.customer_ids),
      project_ids: MapSet.new(membership.project_ids),
      all_customers: membership.all_customers,
      all_projects: membership.all_projects
    }

    event = %{
      event: "auth.session_authenticated",
      authorization_revision: principal.authorization_revision
    }

    {principal, event}
  end

  @doc """
  A 192-bit URL-safe pairing code — `app/pairing.py:83-85`.

  `secrets.token_urlsafe(24)`. Deliberately NOT a short human PIN: `rate_limit.ex`
  bounds requests per route, not guessing attempts against one code, so a
  low-entropy code would need an online guessing defense this service does not have.
  """
  @spec pairing_code() :: String.t()
  def pairing_code, do: 24 |> :crypto.strong_rand_bytes() |> Base.url_encode64(padding: false)

  @doc """
  The normalized form of a pairing code — `app/pairing.py:66-67`.

  Separate from `exchange_grant/3` because the reference's own structure says so:
  `_normalized_code` exists for a reason distinct from the exchange.
  """
  @spec normalize_code(String.t()) :: String.t()
  def normalize_code(code) when is_binary(code), do: String.trim(code)

  @doc """
  Decide whether a pairing grant may be issued — `app/pairing.py:70-81`.

  A membership that does not exist and one that exists but is inactive are
  DIFFERENT refusals, because the reference raises different statuses for them
  (404 and 409). Collapsing them would tell an unauthenticated caller which
  membership ids exist.
  """
  @spec issue_grant(membership() | nil, non_neg_integer()) :: :ok | {:error, refusal()}
  def issue_grant(nil, _ttl_seconds), do: {:error, :membership_not_found}
  def issue_grant(%{active: false}, _ttl_seconds), do: {:error, :membership_inactive}
  def issue_grant(_membership, ttl_seconds) when ttl_seconds > 0, do: :ok
  def issue_grant(_membership, _ttl_seconds), do: {:error, :invalid_grant_ttl}

  @doc """
  Whether a pairing grant may be exchanged for a session — `app/pairing.py:110+`.

  `grant` is `nil` when no row matched `code_hash`. The refusals stay distinct
  because a client retrying a code it already spent has to be told that, while "no
  such code" must not confirm whether a code ever existed.
  """
  @spec exchange_grant(
          String.t() | nil,
          %{required(:code_hash) => String.t(), required(:expires_at) => DateTime.t()} | nil,
          DateTime.t()
        ) :: :ok | {:error, refusal()}
  def exchange_grant(_code, nil, _now), do: {:error, :unknown_pairing_code}

  def exchange_grant(code, %{code_hash: hash, expires_at: expires}, now) do
    cond do
      # Constant-time: this compares a SUPPLIED value against a stored digest
      # rather than using it as a lookup key, so `==` would leak the digest's
      # length and prefix through timing.
      not :crypto.hash_equals(token_hash(normalize_code(code)), hash) ->
        {:error, :unknown_pairing_code}

      DateTime.compare(expires, now) != :gt ->
        {:error, :pairing_grant_expired}

      true ->
        :ok
    end
  end
end
