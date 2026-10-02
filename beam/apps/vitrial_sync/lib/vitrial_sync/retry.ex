defmodule VitrialSync.Retry do
  @moduledoc """
  Which PostgreSQL aborts are worth replaying, and how long to wait first.

  Reproduces the policy in `app/transient_retry.py` and the delay arithmetic at
  `app/transient_retry.py:145-146`.

  ## The classification is an allow-list, not a deny-list

  Three SQLSTATEs are transient: `40001` serialization_failure, `40P01`
  deadlock_detected, `55P03` lock_not_available. Everything else is permanent.

  That direction is the design. The source is explicit about why:

  > an unknown SQLSTATE is treated as permanent, because retrying a permanent
  > failure converts a fast, clear error into a slow, misleading one

  A deny-list would make every SQLSTATE added by a future PostgreSQL release
  retryable by default, which is the exact failure the allow-list exists to
  prevent. It also means this module has to be revisited when the driver
  changes, and that is a deliberate cost.

  ## `23505` is the one that looks like a bug

  `unique_violation` is not transient, and the reason is that on this path it is
  usually the idempotency constraint *working* -- a concurrent push of the same
  `clientMutationID` lost the race, and the application-level handler turns that
  into a replay acknowledgement. A generic retry would re-run the batch to
  collide again.

  ## `57014` is opt-in, and the push does not opt in

  A server-side `statement_timeout` arrives as `57014`. It is transient in the
  sense that the query stopped, but retrying it by default multiplies load
  exactly when the database is already struggling. `include_timeout: true` is
  available for callers whose work is cheap to re-run; the push does not use it.

  ## The backoff is short on purpose

  50 ms then 100 ms, capped at 500 ms, three attempts. A serialization failure is
  usually resolved by being next in the queue, so a long backoff mostly adds
  latency to whichever push is about to win anyway. This is a correctness aid for
  a race, not a load-shedding mechanism, and it must not grow into a retry storm
  under contention.
  """

  @serialization_failure "40001"
  @deadlock_detected "40P01"
  @lock_not_available "55P03"
  @statement_timeout "57014"

  @default_attempts 3
  @default_base_delay_ms 50
  @default_max_delay_ms 500

  @transient MapSet.new([@serialization_failure, @deadlock_detected, @lock_not_available])

  @type sqlstate :: String.t()

  @doc "Number of attempts before the original database error is re-raised."
  @spec default_attempts() :: pos_integer()
  def default_attempts, do: @default_attempts

  @doc """
  Extract a SQLSTATE from a driver error.

  Both `sqlstate` and `pgcode` are consulted, in that order, because asyncpg
  exposes the latter and a future driver may expose only the former. Returning
  `nil` -- rather than guessing -- is what makes the allow-list fail closed: an
  error with no recognizable SQLSTATE is permanent.
  """
  @spec sqlstate_of(Exception.t() | term()) :: sqlstate() | nil
  def sqlstate_of(%{__exception__: true} = error) do
    original = Map.get(error, :orig) || Map.get(error, :original) || error

    with nil <- fetch_code(original, :sqlstate),
         nil <- fetch_code(original, :pgcode) do
      nil
    end
  end

  def sqlstate_of(_other), do: nil

  defp fetch_code(source, key) do
    case Map.get(source, key) do
      code when is_binary(code) and code != "" -> code
      _ -> nil
    end
  end

  @doc """
  Whether a SQLSTATE is worth replaying.

  `include_timeout: true` opts `57014` in; the push path does not.

      iex> VitrialSync.Retry.transient?("40001")
      true

      iex> VitrialSync.Retry.transient?("23505")
      false
  """
  @spec transient?(sqlstate() | nil, keyword()) :: boolean()
  def transient?(sqlstate, opts \\ [])

  def transient?(nil, _opts), do: false

  def transient?(@statement_timeout, opts), do: Keyword.get(opts, :include_timeout, false)

  def transient?(sqlstate, _opts) when is_binary(sqlstate),
    do: MapSet.member?(@transient, sqlstate)

  @doc """
  The delay before retry number `attempt`, in milliseconds.

  `attempt` is 1-based: the delay before the *first* retry is
  `base_delay_ms * 2^0`. Attempt numbers at or below zero are treated as 1
  rather than raising, because a retry loop that has miscounted should sleep the
  shortest useful interval, not crash on the way to recovering from a database
  abort.

      iex> VitrialSync.Retry.delay_ms(1)
      50

      iex> VitrialSync.Retry.delay_ms(2)
      100

      iex> VitrialSync.Retry.delay_ms(30)
      500
  """
  @spec delay_ms(pos_integer(), keyword()) :: non_neg_integer()
  def delay_ms(attempt, opts \\ []) when is_integer(attempt) do
    base = Keyword.get(opts, :base_delay_ms, @default_base_delay_ms)
    max = Keyword.get(opts, :max_delay_ms, @default_max_delay_ms)
    exponent = if attempt < 1, do: 0, else: attempt - 1

    min(base * Integer.pow(2, exponent), max)
  end

  @doc """
  Whether the push should stop retrying and surface the original error.

  On exhaustion the *original* database error is re-raised rather than a wrapper,
  so the SQLSTATE and the real cause stay visible to the operator. This predicate
  is the loop's terminal condition.
  """
  @spec exhausted?(pos_integer(), keyword()) :: boolean()
  def exhausted?(attempt, opts \\ []) when is_integer(attempt),
    do: attempt >= Keyword.get(opts, :attempts, @default_attempts)
end
