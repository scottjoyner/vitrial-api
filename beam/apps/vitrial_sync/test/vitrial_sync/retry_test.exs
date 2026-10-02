defmodule VitrialSync.RetryTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Retry

  describe "transient?/2" do
    test "the three SQLSTATEs that mean 'replay me'" do
      assert Retry.transient?("40001")
      assert Retry.transient?("40P01")
      assert Retry.transient?("55P03")
    end

    test "23505 is deliberately permanent -- the idempotency constraint working" do
      # A concurrent push of the same clientMutationID lost the race. The
      # application-level handler turns that into a replay acknowledgement, and a
      # generic retry would re-run the batch to collide again.
      refute Retry.transient?("23505")
    end

    test "57014 is permanent unless the caller opts in, and the push does not" do
      refute Retry.transient?("57014")
      assert Retry.transient?("57014", include_timeout: true)
    end

    test "an unknown SQLSTATE is permanent, so a new PostgreSQL release cannot start retrying by accident" do
      for state <- ["XX000", "42P01", "08006", "", "4000", "400011"] do
        refute Retry.transient?(state), "expected #{inspect(state)} to be permanent"
      end
    end

    test "no SQLSTATE at all is permanent, which is what makes the allow-list fail closed" do
      refute Retry.transient?(nil)
    end
  end

  describe "sqlstate_of/1" do
    defmodule FakeError do
      defexception [:orig]

      @impl true
      def message(%__MODULE__{orig: orig}), do: "fake db error #{inspect(orig)}"
    end

    test "reads sqlstate from the original driver error" do
      assert Retry.sqlstate_of(%FakeError{orig: %{sqlstate: "40001"}}) == "40001"
    end

    test "falls back to pgcode, which is what asyncpg exposes" do
      assert Retry.sqlstate_of(%FakeError{orig: %{pgcode: "40P01"}}) == "40P01"
    end

    test "prefers sqlstate when both are present" do
      error = %FakeError{orig: %{sqlstate: "40001", pgcode: "99999"}}
      assert Retry.sqlstate_of(error) == "40001"
    end

    test "returns nil rather than guessing when neither is present" do
      assert Retry.sqlstate_of(%FakeError{orig: %{}}) == nil
      assert Retry.sqlstate_of(%FakeError{orig: %{sqlstate: ""}}) == nil
    end

    test "a non-exception term has no SQLSTATE" do
      assert Retry.sqlstate_of(:not_an_error) == nil
      assert Retry.sqlstate_of(%{orig: %{sqlstate: "40001"}}) == nil
    end
  end

  describe "delay_ms/2" do
    test "the default schedule is 50ms then 100ms" do
      assert Retry.delay_ms(1) == 50
      assert Retry.delay_ms(2) == 100
    end

    test "it is capped, so a long contention run does not become a latency floor" do
      assert Retry.delay_ms(3) == 200
      assert Retry.delay_ms(4) == 400
      assert Retry.delay_ms(5) == 500
      assert Retry.delay_ms(30) == 500
    end

    test "a miscounted attempt sleeps the shortest useful interval rather than raising" do
      # A retry loop that has lost count should still recover from a database
      # abort, not crash on the way there.
      assert Retry.delay_ms(0) == 50
      assert Retry.delay_ms(-5) == 50
    end

    test "the base and cap are overridable" do
      assert Retry.delay_ms(1, base_delay_ms: 10) == 10
      assert Retry.delay_ms(2, base_delay_ms: 10) == 20
      assert Retry.delay_ms(9, base_delay_ms: 10, max_delay_ms: 25) == 25
    end
  end

  describe "exhausted?/2" do
    test "three attempts by default" do
      refute Retry.exhausted?(1)
      refute Retry.exhausted?(2)
      assert Retry.exhausted?(3)
      assert Retry.exhausted?(4)
    end

    test "the attempt count is overridable" do
      assert Retry.exhausted?(1, attempts: 1)
      refute Retry.exhausted?(1, attempts: 5)
    end
  end
end
