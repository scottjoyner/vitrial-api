defmodule VitrialSync.Db.PoolTest do
  # These are the NO-SERVER behaviours: boot, config accounting, drain, health
  # shapes, and the checkout trap. Everything that needs a live PostgreSQL is in
  # LiveDbTest and skips (with its own banner) when none is reachable.
  use ExUnit.Case, async: false

  alias VitrialSync.Db.{Config, Pool}

  # Nothing listens here; connects refuse in microseconds, which is what makes
  # these tests fast without a database.
  @unreachable "postgresql://vitrial:vitrial@127.0.0.1:1/vitrial"

  defp tiny_pool(opts \\ []) do
    name = Module.concat([PoolTest, String.to_atom("pool_#{System.unique_integer([:positive])}")])

    config =
      Config.load(
        Keyword.merge(
          [
            url: @unreachable,
            pool_size: 2,
            max_overflow: 1,
            pool_timeout_seconds: 1,
            recycle_seconds: 300,
            statement_timeout_ms: 15_000
          ],
          opts
        )
      )

    start_supervised!({Pool, name: name, config: config, shutdown: 100})
    name
  end

  describe "the application child" do
    test "the umbrella boots the pool with the app, and it stays at zero connections" do
      # The permanent guard against an eager warm-up: the pool is a child of
      # VitrialSync.Application, Mix starts it under `mix test`, and it must sit
      # there holding NOTHING until a checkout happens.
      #
      # It used to assert `stats.total == 0` against the APPLICATION pool, which
      # held only because the gate had no PostgreSQL at all. Giving the BEAM job a
      # `postgres:` service (the live suite needs one) made `live_db_test.exs`
      # check connections out of that same application pool, and because both
      # files are `async: false` the ordering decided the answer: this guard
      # passed or failed depending on whether the live suite ran first. Asserting
      # on a shared pool's current contents was never what this test was about —
      # it is about what STARTUP does, not what any other test did before it.
      #
      # So it starts its OWN pool and asserts that is untouched, which is the
      # property stated in Pool's moduledoc and is immune to ordering.
      name = tiny_pool()

      stats = GenServer.call(name, :stats, 5_000)
      assert stats.total == 0
      assert stats.idle == 0
      assert stats.checked_out == 0
      assert stats.waiters == 0
      assert stats.draining? == false

      # And the application child itself is alive — the guard that motivated the
      # original assertion, kept without depending on its connection count.
      pid = GenServer.whereis(VitrialSync.Db.Pool)
      assert is_pid(pid) and Process.alive?(pid)
    end

    test "the running app's checkout surfaces a connect failure as an error, not a crash" do
      # Whatever this host's state, the app pool must answer with an error tuple.
      # (The gate has no server at the default URL; a dev host may have one —
      # either answer is a working pool.)
      result = Pool.with_conn(VitrialSync.Db.Pool, fn conn -> {:ok, conn} end)
      assert match?({:ok, _}, result) or match?({:error, _}, result)
    end
  end

  describe "boot without a database" do
    test "init performs no I/O: the pool starts alive, holding zero connections" do
      pool = tiny_pool()
      pid = GenServer.whereis(pool)

      assert is_pid(pid) and Process.alive?(pid)

      stats = GenServer.call(pool, :stats, 5_000)
      assert stats.total == 0
      assert stats.idle == 0
    end

    test "checkout with an unreachable server yields {:error, reason}" do
      pool = tiny_pool(pool_size: 1, max_overflow: 0)

      assert {:error, reason} = Pool.checkout(pool)
      assert reason != :pool_timeout
      # The pool outlived the failed connect.
      assert Process.alive?(GenServer.whereis(pool))
    end

    test "with_conn with an unreachable server yields {:error, reason} and the pool survives" do
      pool = tiny_pool()

      assert {:error, _reason} = Pool.with_conn(pool, fn _conn -> flunk("never granted") end)
      assert Process.alive?(GenServer.whereis(pool))
      assert GenServer.call(pool, :stats, 5_000).total == 0
    end
  end

  describe "drain before any connection exists" do
    test "drain/2 answers {:ok, 0} and refuses later checkouts without touching the server" do
      pool = tiny_pool()

      assert {:ok, 0} = Pool.drain(pool, 0)

      assert {:error, :draining} =
               Pool.with_conn(pool, fn _ -> flunk("granted while draining") end)

      stats = GenServer.call(pool, :stats, 5_000)
      assert stats.draining? == true
      assert stats.total == 0
    end

    test "drain is idempotent" do
      pool = tiny_pool()
      assert {:ok, 0} = Pool.drain(pool, 0)
      assert {:ok, 0} = Pool.drain(pool, 0)
      assert {:error, :draining} = Pool.checkout(pool)
    end
  end

  describe "health with nothing parked" do
    test "reports the empty shape and :no_idle_connection" do
      pool = tiny_pool()

      health = Pool.health(pool)

      assert health.total == 0
      assert health.idle == 0
      assert health.checked_out == 0
      assert health.overflow == 0
      assert health.waiters == 0
      assert health.null_pool? == false
      assert health.ping == :no_idle_connection
    end

    test "null-pool mode reports itself" do
      pool = tiny_pool(null_pool: true)
      assert Pool.health(pool).null_pool? == true
    end
  end

  describe "the checkout error path" do
    test "checkout/2 returns an error tuple — never a raised exit into the caller" do
      # A refused connect fails fast here; the pool's answer is a plain
      # {:error, reason}. (The {:error, :pool_timeout} translation twin lives in
      # LiveDbTest: exhausting capacity needs a real connection to hold a slot.)
      pool = tiny_pool()

      assert {:error, _reason} = Pool.checkout(pool, pool_timeout_ms: 50)
      assert {:error, _reason} = Pool.checkout(pool)
    end
  end
end
