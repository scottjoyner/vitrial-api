defmodule VitrialAuthTest do
  use ExUnit.Case, async: true

  alias VitrialAuth
  alias VitrialAuth.Principal

  @now ~U[2026-10-04 12:00:00Z]
  @later ~U[2026-10-04 13:00:00Z]
  @earlier ~U[2026-10-04 11:00:00Z]

  defp session(overrides \\ %{}) do
    Map.merge(
      %{
        id: "sess-1",
        user_id: "user-1",
        organization_id: "org-1",
        membership_id: "mem-1",
        expires_at: @later,
        revoked_at: nil
      },
      overrides
    )
  end

  defp membership(overrides \\ %{}) do
    Map.merge(
      %{
        id: "mem-1",
        user_id: "user-1",
        organization_id: "org-1",
        active: true,
        capabilities: ["sync"],
        customer_ids: ["c1"],
        project_ids: ["p1"],
        all_customers: false,
        all_projects: false
      },
      overrides
    )
  end

  defp organization, do: %{authorization_revision: 4}

  describe "token_hash/1" do
    test "is the lowercase SHA-256 hexdigest of the UTF-8 bytes" do
      # The digest is the STORAGE KEY, not a display value: the reference looks
      # AuthSession rows up by it (app/auth.py:45), so it has to be byte-exact.
      assert VitrialAuth.token_hash("abc") ==
               "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

      assert VitrialAuth.token_hash("") ==
               "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    end

    test "hashes UTF-8, so a non-ASCII token is not truncated" do
      expected = :sha256 |> :crypto.hash(<<195, 169>>) |> Base.encode16(case: :lower)
      assert VitrialAuth.token_hash("é") == expected
    end

    test "is not reversible by hashing again" do
      once = VitrialAuth.token_hash("secret-token")
      refute once == VitrialAuth.token_hash(once)
    end
  end

  describe "parse_authorization/1" do
    test "splits scheme from credentials" do
      assert {:ok, "bearer", "abc123"} == VitrialAuth.parse_authorization("Bearer abc123")
    end

    test "downcases the scheme, because HTTP auth schemes are case-insensitive" do
      for header <- ["bearer t", "Bearer t", "BEARER t", "BeArEr t"] do
        assert {:ok, "bearer", "t"} == VitrialAuth.parse_authorization(header)
      end
    end

    test "keeps spaces inside the credential" do
      # A token is not a token if it is truncated at the first space — splitting
      # ONCE and keeping the remainder is what makes an opaque credential opaque.
      assert {:ok, "bearer", "a b c"} == VitrialAuth.parse_authorization("Bearer a b c")
    end

    test "refuses a header with no credential" do
      assert {:error, :authentication_required} == VitrialAuth.parse_authorization("Bearer")
      assert {:error, :authentication_required} == VitrialAuth.parse_authorization("Bearer ")

      assert {:error, :authentication_required} ==
               VitrialAuth.parse_authorization("Bearer    ")
    end

    test "refuses nil, because absent is not an empty credential" do
      assert {:error, :authentication_required} == VitrialAuth.parse_authorization(nil)
      refute VitrialAuth.bearer_scheme?(nil)
    end

    test "keeps a non-bearer scheme parseable but not authenticating" do
      refute VitrialAuth.bearer_scheme?("Basic dXNlcjpwYXNz")

      assert {:ok, "basic", "dXNlcjpwYXNz"} ==
               VitrialAuth.parse_authorization("Basic dXNlcjpwYXNz")
    end
  end

  describe "authenticate/5 — the happy path" do
    test "resolves a live session to a Principal and the event beside it" do
      assert {:ok, principal, event} =
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(),
                 membership(),
                 organization(),
                 @now
               )

      assert %Principal{
               user_id: "user-1",
               organization_id: "org-1",
               membership_id: "mem-1",
               session_id: "sess-1",
               authorization_revision: 4,
               capabilities: capabilities,
               customer_ids: customer_ids,
               project_ids: project_ids
             } = principal

      # MapSet, not List: `frozenset` on the Python side, and `Authority`/`Scope`
      # already assume MapSet. A List would compile and then misbehave.
      assert capabilities == MapSet.new(["sync"])
      assert customer_ids == MapSet.new(["c1"])
      assert project_ids == MapSet.new(["p1"])

      # The event is RETURNED, not emitted: a pure function cannot observe
      # anything by logging, and the reference logs right after building the
      # principal (app/auth.py:73-74).
      assert event == %{event: "auth.session_authenticated", authorization_revision: 4}
    end
  end

  describe "authenticate/5 — refusals, in the reference's order" do
    test "no header is authentication_required" do
      assert {:error, :authentication_required} ==
               VitrialAuth.authenticate(nil, session(), membership(), organization(), @now)
    end

    test "a wrong scheme is authentication_required, not a session failure" do
      assert {:error, :authentication_required} ==
               VitrialAuth.authenticate("Basic x", session(), membership(), organization(), @now)
    end

    test "no session row is session_expired_or_revoked" do
      assert {:error, :session_expired_or_revoked} ==
               VitrialAuth.authenticate("Bearer tok", nil, membership(), organization(), @now)
    end

    test "a revoked session is refused even though it has not expired" do
      assert {:error, :session_expired_or_revoked} ==
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(%{revoked_at: @earlier}),
                 membership(),
                 organization(),
                 @now
               )
    end

    test "an expired session is refused, and the boundary is exclusive" do
      # `expires_at <= now` in the reference, so expiring EXACTLY at now is
      # refused; one microsecond later is not.
      assert {:error, :session_expired_or_revoked} ==
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(%{expires_at: @now}),
                 membership(),
                 organization(),
                 @now
               )

      assert {:ok, _, _} =
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(%{expires_at: DateTime.add(@now, 1, :microsecond)}),
                 membership(),
                 organization(),
                 @now
               )
    end

    test "revoked and expired are ONE refusal, not two" do
      # The caller must not be able to tell which it was, and "session expired or
      # revoked" is the reference's own single message.
      revoked =
        VitrialAuth.authenticate(
          "Bearer t",
          session(%{revoked_at: @earlier}),
          membership(),
          organization(),
          @now
        )

      expired =
        VitrialAuth.authenticate(
          "Bearer t",
          session(%{expires_at: @earlier}),
          membership(),
          organization(),
          @now
        )

      assert revoked == expired
    end

    test "an inactive membership is refused even with a live session" do
      assert {:error, :membership_inactive} ==
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(),
                 membership(%{active: false}),
                 organization(),
                 @now
               )
    end

    test "a missing membership is refused" do
      assert {:error, :membership_inactive} ==
               VitrialAuth.authenticate("Bearer tok", session(), nil, organization(), @now)
    end

    test "a session pointing at another organization's membership is refused" do
      # Two rows that disagree is a DATA problem. Repairing it — preferring either
      # row — is how a session ends up authorized for an organization it does not
      # belong to, which is the whole authorization boundary.
      assert {:error, :session_membership_mismatch} ==
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(),
                 membership(%{organization_id: "org-OTHER"}),
                 organization(),
                 @now
               )
    end

    test "a session pointing at another user's membership is refused" do
      assert {:error, :session_membership_mismatch} ==
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(),
                 membership(%{user_id: "user-OTHER"}),
                 organization(),
                 @now
               )
    end

    test "expiry is reported BEFORE membership, because that is the actionable answer" do
      # Reversing these would make a revoked session look like a permission
      # problem, which sends an operator to the wrong table.
      assert {:error, :session_expired_or_revoked} ==
               VitrialAuth.authenticate(
                 "Bearer tok",
                 session(%{expires_at: @earlier}),
                 membership(%{active: false}),
                 organization(),
                 @now
               )
    end
  end

  describe "pairing_code/0" do
    test "carries 192 bits, not a human-typed PIN" do
      code = VitrialAuth.pairing_code()
      # 24 bytes at 6 bits per base64 char, unpadded.
      assert byte_size(code) == 32
      assert {:ok, decoded} = Base.url_decode64(code, padding: false)
      assert byte_size(decoded) == 24
    end

    test "is different every call — strong_rand_bytes, not a seeded PRNG" do
      codes = for _ <- 1..50, do: VitrialAuth.pairing_code()
      assert length(Enum.uniq(codes)) == 50
    end
  end

  describe "issue_grant/2" do
    test "an active membership may be granted" do
      assert :ok == VitrialAuth.issue_grant(membership(), 300)
    end

    test "a missing membership is NOT the same refusal as an inactive one" do
      # The reference raises 404 and 409 respectively (app/pairing.py:72-77).
      # Collapsing them would tell an unauthenticated caller which membership ids
      # are real.
      assert {:error, :membership_not_found} == VitrialAuth.issue_grant(nil, 300)

      assert {:error, :membership_inactive} ==
               VitrialAuth.issue_grant(membership(%{active: false}), 300)
    end

    test "a non-positive TTL is refused rather than silently defaulted" do
      assert {:error, :invalid_grant_ttl} == VitrialAuth.issue_grant(membership(), 0)
      assert {:error, :invalid_grant_ttl} == VitrialAuth.issue_grant(membership(), -1)
    end
  end

  describe "exchange_grant/3" do
    defp grant(code, overrides \\ %{}) do
      Map.merge(%{code_hash: VitrialAuth.token_hash(code), expires_at: @later}, overrides)
    end

    test "a live grant matching its code may be exchanged" do
      assert :ok == VitrialAuth.exchange_grant("the-code", grant("the-code"), @now)
    end

    test "the SUPPLIED code is normalized before hashing, as app/pairing.py:66-67 does" do
      # Only the supplied side. `_normalized_code` trims what the caller typed;
      # the stored `code_hash` was computed from an already-normalized code when
      # the grant was ISSUED, so normalizing it again here would be a second
      # rule the reference does not have — and a grant for "the-code" must not
      # match "  the-code  " merely because the stored digest contains spaces.
      assert :ok == VitrialAuth.exchange_grant("  the-code  ", grant("the-code"), @now)

      assert {:error, :unknown_pairing_code} ==
               VitrialAuth.exchange_grant("the-code", grant("  the-code  "), @now)
    end

    test "a missing grant is unknown_pairing_code" do
      assert {:error, :unknown_pairing_code} == VitrialAuth.exchange_grant("x", nil, @now)
    end

    test "a wrong code is unknown_pairing_code, and says nothing about the grant" do
      assert {:error, :unknown_pairing_code} ==
               VitrialAuth.exchange_grant("wrong", grant("right"), @now)
    end

    test "an expired grant is refused even when the code is right" do
      assert {:error, :pairing_grant_expired} ==
               VitrialAuth.exchange_grant("c", grant("c", %{expires_at: @earlier}), @now)
    end

    test "a wrong code is refused whatever the grant's own expiry" do
      # The code is checked BEFORE the expiry so that a wrong code and an expired
      # grant are not the same answer — otherwise an attacker learns the code was
      # right from the difference.
      assert {:error, :unknown_pairing_code} ==
               VitrialAuth.exchange_grant("wrong", grant("right", %{expires_at: @earlier}), @now)
    end
  end

  describe "the Principal struct" do
    test "carries every field app/auth.py:20-29 declares" do
      # The shape is pinned here rather than against `VitrialSync.Canonical.Principal`
      # because no app in this umbrella depends on a sibling (0 of 8), which is
      # why there are two Principal structs at all. Reaching the other one from
      # this app is not possible, and pretending otherwise would need a dependency
      # that couples two deployable units for the sake of one struct.
      #
      # So the contract is the REFERENCE: these are the fields app/auth.py:20-29
      # declares, in the order it declares them. A field added to
      # `VitrialSync.Canonical.Principal` without being added here fails the
      # comparison below in THAT app's test, which is the one place both are
      # visible.
      assert Principal.__struct__() |> Map.from_struct() |> Map.keys() |> Enum.sort() ==
               [
                 :all_customers,
                 :all_projects,
                 :authorization_revision,
                 :capabilities,
                 :customer_ids,
                 :membership_id,
                 :organization_id,
                 :project_ids,
                 :session_id,
                 :user_id
               ]
    end

    test "defaults to empty collections, not nil" do
      # `frozenset()` on the Python side. nil would compile and then fail at the
      # first membership test that does `MapSet.union/2` on it.
      principal = Principal.__struct__()

      assert principal.capabilities == MapSet.new()
      assert principal.customer_ids == MapSet.new()
      assert principal.project_ids == MapSet.new()
      assert principal.all_customers == false
      assert principal.all_projects == false
      assert is_nil(principal.user_id)
    end
  end
end
