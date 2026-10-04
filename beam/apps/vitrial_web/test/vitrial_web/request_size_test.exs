defmodule VitrialWeb.RequestSizeTest do
  use ExUnit.Case, async: false

  alias VitrialWeb.RequestSize

  @mib 1024 * 1024

  # app/request_size.py:77-82, verbatim.
  test "the default table is the reference's, byte for byte" do
    assert RequestSize.default_path_limits() == [
             {"/api/v1/auth/pair", 16 * 1024},
             {"/api/v1/sync/push", 4 * @mib},
             {"/api/v2/sync/push", 4 * @mib},
             {"/internal/admin/v1", 64 * 1024}
           ]
  end

  test "each table entry governs its path family; unlisted paths are unlimited" do
    assert RequestSize.limit_for_path("/api/v1/auth/pair") == 16 * 1024
    assert RequestSize.limit_for_path("/api/v1/sync/push") == 4 * @mib
    assert RequestSize.limit_for_path("/api/v2/sync/push") == 4 * @mib
    assert RequestSize.limit_for_path("/internal/admin/v1/bootstrap") == 64 * 1024
    assert RequestSize.limit_for_path("/internal/admin/v1/sessions/s1/revoke") == 64 * 1024

    # app/request_size.py:62-64: refusing a legitimate request on a path
    # nobody reasoned about is the worse outcome, so unlisted means unlimited.
    assert RequestSize.limit_for_path("/health") == nil
    assert RequestSize.limit_for_path("/api/v1/sync/pull") == nil
    assert RequestSize.limit_for_path("/api/v1/reference-data/manifest") == nil
  end

  test "longest matching prefix wins, regardless of table order" do
    table = [{"/a", 1}, {"/a/b", 2}, {"/a/b/c", 3}]

    assert RequestSize.limit_for_path("/a/anything", table) == 1
    assert RequestSize.limit_for_path("/a/b/anything", table) == 2
    assert RequestSize.limit_for_path("/a/b/c/anything", table) == 3
    assert RequestSize.limit_for_path("/b", table) == nil

    # The reference's startswith semantics (app/request_size.py:118): a bare
    # prefix match does not require a segment boundary. Recorded, not invented.
    assert RequestSize.limit_for_path("/a/bc", table) == 2
  end

  test "declared content-length: unusable is unknown, unknown is not zero" do
    assert RequestSize.declared_length(%{"content-length" => "16384"}) == 16_384
    assert RequestSize.declared_length(%{"content-length" => "0"}) == 0
    assert RequestSize.declared_length(%{"content-length" => "abc"}) == nil
    assert RequestSize.declared_length(%{"content-length" => "12x"}) == nil
    assert RequestSize.declared_length(%{"content-length" => "-1"}) == nil
    assert RequestSize.declared_length(%{}) == nil
  end

  test "per-prefix environment overrides apply, and non-positive or garbage falls back" do
    var = "VITRIAL_MAX_BODY_BYTES_API_V1_AUTH_PAIR"

    System.put_env(var, "999")
    assert RequestSize.limit_for_path("/api/v1/auth/pair") == 999

    System.put_env(var, "not-a-number")
    assert RequestSize.limit_for_path("/api/v1/auth/pair") == 16 * 1024

    System.put_env(var, "0")
    assert RequestSize.limit_for_path("/api/v1/auth/pair") == 16 * 1024

    System.put_env(var, "-5")
    assert RequestSize.limit_for_path("/api/v1/auth/pair") == 16 * 1024
  after
    System.delete_env("VITRIAL_MAX_BODY_BYTES_API_V1_AUTH_PAIR")
  end

  test "the override variable name is generated like app/request_size.py:100-101" do
    # The suffix is not spelled out in the table; it is derived. Assert the
    # derivation for every prefix so the names cannot silently drift.
    for {prefix, _limit} <- RequestSize.default_path_limits() do
      suffix =
        prefix
        |> String.graphemes()
        |> Enum.map(fn ch -> if ch =~ ~r/[a-zA-Z0-9]/, do: ch, else: "_" end)
        |> Enum.join()
        |> String.trim("_")
        |> String.upcase()

      assert System.get_env("VITRIAL_MAX_BODY_BYTES_" <> suffix) == nil,
             "unexpected override set for #{prefix}"
    end
  end
end
