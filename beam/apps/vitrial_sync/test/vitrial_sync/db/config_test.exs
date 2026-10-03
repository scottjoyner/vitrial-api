defmodule VitrialSync.Db.ConfigTest do
  # The functions under test read the process environment; parallel tests would
  # race each other's System.put_env calls.
  use ExUnit.Case, async: false

  alias VitrialSync.Db.Config

  @env_names [
    "DATABASE_URL",
    "DATABASE_NULL_POOL",
    "DATABASE_POOL_SIZE",
    "DATABASE_MAX_OVERFLOW",
    "DATABASE_POOL_TIMEOUT_SECONDS",
    "DATABASE_POOL_RECYCLE_SECONDS",
    "DATABASE_STATEMENT_TIMEOUT_MS"
  ]

  setup do
    # Snap and clear the whole surface: every test states the env it assumes.
    snap = Map.new(@env_names, &{&1, System.get_env(&1)})
    Enum.each(@env_names, &System.delete_env/1)

    on_exit(fn ->
      Enum.each(snap, fn
        {name, nil} -> System.delete_env(name)
        {name, value} -> System.put_env(name, value)
      end)
    end)

    :ok
  end

  describe "defaults" do
    test "match app/settings.py:8-9 and :26-40" do
      config = Config.load()

      # url: the reference default minus the +asyncpg segment.
      assert %{host: "localhost", port: 5432, database: "vitrial", username: "vitrial"} =
               config.url

      assert config.null_pool == false
      assert config.pool_size == 10
      assert config.max_overflow == 5
      # 10.0 s and 300 s, in milliseconds here.
      assert config.pool_timeout_ms == 10_000
      assert config.recycle_ms == 300_000
      assert config.statement_timeout_ms == 15_000
    end
  end

  describe "the environment, read at load" do
    test "every deployed name is read (the compose files' surface)" do
      System.put_env("DATABASE_URL", "postgresql+asyncpg://u:p@cfg-host:5433/cfgdb")
      System.put_env("DATABASE_NULL_POOL", "true")
      System.put_env("DATABASE_POOL_SIZE", "3")
      System.put_env("DATABASE_MAX_OVERFLOW", "0")
      System.put_env("DATABASE_POOL_TIMEOUT_SECONDS", "2.5")
      System.put_env("DATABASE_POOL_RECYCLE_SECONDS", "60")
      System.put_env("DATABASE_STATEMENT_TIMEOUT_MS", "4000")

      config = Config.load()

      assert %{host: "cfg-host", port: 5433, database: "cfgdb"} = config.url
      assert config.null_pool == true
      assert config.pool_size == 3
      assert config.max_overflow == 0
      assert config.pool_timeout_ms == 2_500
      assert config.recycle_ms == 60_000
      assert config.statement_timeout_ms == 4_000
    end

    test "boolean parsing accepts pydantic's vocabulary and refuses the rest" do
      System.put_env("DATABASE_NULL_POOL", "TRUE")
      assert Config.load().null_pool == true

      System.put_env("DATABASE_NULL_POOL", "off")
      assert Config.load().null_pool == false

      System.put_env("DATABASE_NULL_POOL", "flase")

      assert_raise ArgumentError, ~r/DATABASE_NULL_POOL must be a boolean/, fn ->
        Config.load()
      end
    end

    test "integer parsing refuses garbage by variable name" do
      System.put_env("DATABASE_POOL_SIZE", "banana")

      assert_raise ArgumentError, ~r/DATABASE_POOL_SIZE must be an integer/, fn ->
        Config.load()
      end
    end

    test "float parsing takes the timeout as seconds, fractions included" do
      System.put_env("DATABASE_POOL_TIMEOUT_SECONDS", "0.25")
      assert Config.load().pool_timeout_ms == 250
    end

    test "a bad DATABASE_URL fails at load, naming the problem" do
      System.put_env("DATABASE_URL", "postgresql://u:p@h/vitrial?sslmode=require")

      assert_raise ArgumentError, ~r/sslmode/, fn ->
        Config.load()
      end
    end
  end

  describe "validation" do
    test "a pool size below 1 is refused at boot, not discovered as a cascading outage" do
      assert_raise ArgumentError, ~r/DATABASE_POOL_SIZE must be at least 1/, fn ->
        Config.load(pool_size: 0)
      end
    end

    test "a negative overflow is refused" do
      assert_raise ArgumentError, ~r/DATABASE_MAX_OVERFLOW must be at least 0/, fn ->
        Config.load(max_overflow: -1)
      end
    end

    test "a non-positive statement timeout is refused" do
      assert_raise ArgumentError, ~r/DATABASE_STATEMENT_TIMEOUT_MS must be positive/, fn ->
        Config.load(statement_timeout_ms: 0)
      end
    end
  end

  describe "overrides" do
    test "win over both environment and defaults, without touching the env" do
      System.put_env("DATABASE_POOL_SIZE", "9")
      config = Config.load(pool_size: 2, url: "postgresql://x:y@z:1/q")

      assert config.pool_size == 2
      assert config.url.host == "z"
      # Untouched surface still reads the environment.
      assert config.max_overflow == 5
    end
  end
end
