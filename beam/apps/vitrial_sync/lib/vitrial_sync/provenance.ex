defmodule VitrialSync.Provenance do
  @moduledoc """
  The five-connected-provenance check, and the one thing that makes it work.

  Six places in the reference validate the same five fields against the same
  principal:

      {actorID, organizationID, membershipID, sessionID, authorizationRevision}

  `app/lifecycle.py:49-79` (blocker and quotation events), and four copies inside
  the delivery validators -- `app/delivery_execution.py:139-166, 187-233`,
  `app/delivery_production.py:26-57`, `app/delivery_installation.py:24-60`. Each
  copy spells out its own three messages, and each is a different string, so the
  helper here takes the three messages rather than composing them from a label.
  Composing them would be tidier and would be wrong: `"blocker event organization
  is invalid"` and `"delivery production plan organization is invalid"` are the
  reference's words, and a test that asserts on them is asserting on behaviour.

  ## The shape of the check

  Two arms, and the second is the one that matters:

    * **complete** -- all four ids normalize to a non-empty string and
      `authorizationRevision` is a Python `int` (`not bool`) that is `>= 0`. This
      is the arm that runs on *every* event, historical and current.
    * **current actor** -- only when the caller passes `current_actor: true`, and
      only for the single event being appended. All five must equal the
      principal's current values.

  `authorizationRevision` is read from the `Organization` row on every request
  (`app/auth.py:66`), never from the token, which is S-120: bump the revision and
  every in-flight mutation that asserts current provenance fails. That is the
  propagation mechanism for a permission change, and it is why the appended tail
  -- not the whole history -- is the only event checked against the current
  principal.

  ## `true` is a valid revision

  The comparison is Python's `!=`, in which `True == 1`. A payload carrying
  `"authorizationRevision": true` therefore satisfies a current-actor check
  against revision 1. That is SUSPECT-9, and it is reproduced rather than fixed:
  the reference does it, a client may already rely on it, and the job of this
  port is parity. `Value.py_ne?/2` is the comparison, so the behaviour is stated
  in one place and tested there.
  """

  alias VitrialSync.{Canonical, Value}

  @typedoc "The three reference messages for one call site."
  @type messages :: {String.t(), String.t(), String.t()}

  @doc """
  Check one provenance-carrying object.

  Returns `:ok`, or `{:error, message}` with the reference's own message so a
  test can name the rule that fired.

      iex> alias VitrialSync.{Canonical, Provenance}
      iex> principal = %Canonical.Principal{organization_id: "org", user_id: "u1",
      ...>   membership_id: "m1", session_id: "s1", authorization_revision: 7}
      iex> event = %{"actorID" => "u1", "organizationID" => "org", "membershipID" => "m1",
      ...>   "sessionID" => "s1", "authorizationRevision" => 7}
      iex> Provenance.check(event, principal, false,
      ...>   {"incomplete", "bad org", "bad actor"})
      :ok

      iex> alias VitrialSync.{Canonical, Provenance}
      iex> principal = %Canonical.Principal{organization_id: "org", user_id: "u1",
      ...>   membership_id: "m1", session_id: "s1", authorization_revision: 7}
      iex> Provenance.check(%{"actorID" => "u1"}, principal, false,
      ...>   {"incomplete", "bad org", "bad actor"})
      {:error, "incomplete"}
  """
  @spec check(map(), Canonical.Principal.t() | nil, boolean(), messages()) ::
          :ok | {:error, String.t()}
  def check(value, principal, current_actor, {incomplete, bad_org, bad_actor})
      when is_map(value) do
    actor_id = Value.normalized(Map.get(value, "actorID"))
    organization_id = Value.normalized(Map.get(value, "organizationID"))
    membership_id = Value.normalized(Map.get(value, "membershipID"))
    session_id = Value.normalized(Map.get(value, "sessionID"))
    revision = Map.get(value, "authorizationRevision")

    cond do
      is_nil(actor_id) or is_nil(organization_id) or is_nil(membership_id) or
        is_nil(session_id) or not Value.int?(revision) or revision < 0 ->
        {:error, incomplete}

      is_nil(principal) ->
        :ok

      organization_id != principal.organization_id ->
        {:error, bad_org}

      current_actor and
          (actor_id != principal.user_id or membership_id != principal.membership_id or
             session_id != principal.session_id or
             Value.py_ne?(revision, principal.authorization_revision)) ->
        {:error, bad_actor}

      true ->
        :ok
    end
  end

  @doc """
  `_require_current_provenance`, `app/ownership.py:162-173`.

  The other flavour: all five keys must match, and the message names the key that
  did not. No `current_actor` flag and no separate "incomplete" arm -- the first
  key checked is `actorID`, so a payload missing everything reports `actorID`.
  Used by `item_audit_event` payloads, by a `blocker`'s last event inside
  `authorize_record`, and by a `quotation`'s last event.
  """
  @spec require_current(map(), Canonical.Principal.t()) :: :ok | {:error, String.t()}
  def require_current(value, %Canonical.Principal{} = principal) when is_map(value) do
    expected = [
      {"actorID", principal.user_id},
      {"organizationID", principal.organization_id},
      {"membershipID", principal.membership_id},
      {"sessionID", principal.session_id},
      {"authorizationRevision", principal.authorization_revision}
    ]

    Enum.reduce_while(expected, :ok, fn {key, value_expected}, :ok ->
      if Value.py_eq?(Map.get(value, key), value_expected) do
        {:cont, :ok}
      else
        {:halt, {:error, "#{key} does not match authenticated provenance"}}
      end
    end)
  end
end
