defmodule VitrialWeb.RateLimitTest do
  use ExUnit.Case, async: false

  alias VitrialWeb.RateLimit
  alias VitrialWeb.RateLimit.Window

  # --- the pure window (app/rate_limit.py:125-174) ---------------------------
  # The state is immutable, so every test threads the returned window forward.

  test "admits up to the limit, then refuses with time until the OLDEST hit leaves" do
    window = Window.new(3, 60.0)

    {allowed?, retry_after, window} = Window.allow(window, "c", 100.0)
    assert {allowed?, retry_after} == {true, 0.0}

    {allowed?, retry_after, window} = Window.allow(window, "c", 101.0)
    assert {allowed?, retry_after} == {true, 0.0}

    {allowed?, retry_after, window} = Window.allow(window, "c", 102.0)
    assert {allowed?, retry_after} == {true, 0.0}

    # oldest is 100.0; waiting until 100 + 60 - 103 = 57.0 more seconds.
    {allowed?, retry_after, _window} = Window.allow(window, "c", 103.0)
    assert {allowed?, retry_after} == {false, 57.0}
  end

  test "a refused request does not consume a slot" do
    window = Window.new(2, 60.0)

    {allowed?, _, window} = Window.allow(window, "c", 0.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "c", 1.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "c", 2.0)
    refute allowed?
    {allowed?, _, _} = Window.allow(window, "c", 3.0)
    refute allowed?
  end

  test "hits leave the window and free slots again" do
    window = Window.new(2, 60.0)

    {allowed?, _, window} = Window.allow(window, "c", 0.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "c", 1.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "c", 2.0)
    refute allowed?

    # cutoff = 61 - 60 = 1.0, so the t=0.0 hit is pruned first, then the t=1.0
    # hit at the boundary is pruned too (<= cutoff): bucket empty, admitted.
    {allowed?, _, _} = Window.allow(window, "c", 61.0)
    assert allowed?
  end

  test "clients are independent" do
    window = Window.new(1, 60.0)

    {allowed?, _, window} = Window.allow(window, "a", 0.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "b", 0.0)
    assert allowed?
    {allowed?, _, _} = Window.allow(window, "a", 1.0)
    refute allowed?
  end

  test "eviction expires quiet keys BEFORE dropping the recency-oldest" do
    # app/rate_limit.py:163-174, both arms:
    window = Window.new(1, 60.0, 2)

    {allowed?, _, window} = Window.allow(window, "old", 0.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "fresh", 50.0)
    assert allowed?

    # Table full; "old"'s hit is outside the window at t=61 (cutoff 1.0), so
    # EXPIRY makes room and "fresh" is never the eviction victim.
    {allowed?, _, _} = Window.allow(window, "newcomer", 61.0)
    assert allowed?
  end

  test "eviction falls back to the oldest key when nothing has expired" do
    window = Window.new(1, 60.0, 2)

    {allowed?, _, window} = Window.allow(window, "first", 10.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "second", 20.0)
    assert allowed?

    # Both inside the window at t=21: nothing to expire, so the oldest
    # ("first") is dropped to make room.
    {allowed?, _, window} = Window.allow(window, "third", 21.0)
    assert allowed?

    # "second"'s bucket is untouched by the eviction and still refuses with the
    # time until its own hit leaves the window.
    {allowed?, retry_after, _} = Window.allow(window, "second", 22.0)
    assert {allowed?, retry_after} == {false, 58.0}
  end

  test "reset clears every bucket (app/rate_limit.py:176-178)" do
    window = Window.new(1, 60.0)
    {allowed?, _, window} = Window.allow(window, "c", 0.0)
    assert allowed?
    {allowed?, _, window} = Window.allow(window, "c", 1.0)
    refute allowed?

    {allowed?, _, _} = window |> Window.reset() |> Window.allow("c", 2.0)
    assert allowed?
  end

  # --- keys and scope (app/rate_limit.py:64-66, :103-122) --------------------

  test "the throttled scope is the pairing path prefix only" do
    assert RateLimit.path_throttled?("/api/v1/auth/pair")
    assert RateLimit.path_throttled?("/api/v1/auth/pair/")
    assert RateLimit.path_throttled?("/api/v1/auth/pair-bulk")
    refute RateLimit.path_throttled?("/api/v1/auth/logout")
    refute RateLimit.path_throttled?("/health")
  end

  test "the client key prefers the left-most XFF entry, then the peer" do
    assert RateLimit.client_key(%{"x-forwarded-for" => "1.2.3.4, 5.6.7.8"}, {9, 9, 9, 9}) ==
             "xff:1.2.3.4"

    assert RateLimit.client_key(%{"x-forwarded-for" => "1.2.3.4"}, {9, 9, 9, 9}) == "xff:1.2.3.4"
    assert RateLimit.client_key(%{"x-forwarded-for" => ""}, {1, 2, 3, 4}) == "peer:1.2.3.4"
    assert RateLimit.client_key(%{"x-forwarded-for" => "  "}, {1, 2, 3, 4}) == "peer:1.2.3.4"
    assert RateLimit.client_key(%{}, {1, 2, 3, 4}) == "peer:1.2.3.4"
    assert RateLimit.client_key(%{}, nil) == "peer:unknown"
  end

  test "limiter configuration reads the environment, ignoring non-positive values" do
    System.put_env("VITRIAL_AUTH_RATE_LIMIT", "7")
    System.put_env("VITRIAL_AUTH_RATE_WINDOW_SECONDS", "1.5")
    assert RateLimit.config() == {7, 1.5}

    System.put_env("VITRIAL_AUTH_RATE_LIMIT", "abc")
    System.put_env("VITRIAL_AUTH_RATE_WINDOW_SECONDS", "0")
    assert RateLimit.config() == {30, 60.0}
  after
    System.delete_env("VITRIAL_AUTH_RATE_LIMIT")
    System.delete_env("VITRIAL_AUTH_RATE_WINDOW_SECONDS")
  end

  # --- the supervised limiter -------------------------------------------------

  test "the running limiter honours the configured limit against an explicit clock" do
    RateLimit.reset()
    {limit, _window} = RateLimit.config()

    results = for _ <- 1..limit, do: RateLimit.allow("test-key", 1_000.0)
    assert Enum.all?(results, fn {allowed?, _} -> allowed? end)

    {allowed?, retry_after} = RateLimit.allow("test-key", 1_001.0)
    refute allowed?
    assert retry_after == 59.0
  after
    RateLimit.reset()
  end
end
