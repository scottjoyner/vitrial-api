defmodule VitrialSync.Cursor do
  @moduledoc """
  Pull cursors: the only thing a client is allowed to use to say "I have seen
  everything up to here".

  A cursor is the wire form of `SyncChangeLog.sequence`, which is a `BIGINT`. The
  domain is therefore `{0, 2^63 - 1}`, and anything outside it is not a position
  that exists -- it is a client that sent a string the server cannot interpret.

  ## Why the bound is explicit here

  Erlang integers are arbitrary-precision. Nothing in the language stops this
  module from accepting `seq:99999999999999999999999` and handing it onward, and
  the value would survive every arithmetic step in the BEAM without complaint.
  It becomes an error three layers down, at the driver, as a value-out-of-range
  against a `bigint` column -- a driver exception escaping as a 500 for what is
  unambiguously a malformed client string.

  So the ceiling is a decision made here, on purpose, with the reason attached,
  rather than a limit the storage layer happens to impose. Native bignums are an
  advantage everywhere else; here they are exactly the hazard.

  ## Parsing is also a length bound

  A 20-digit digit string is rejected on length before it is parsed. That is not
  only a micro-optimisation: `String.to_integer/1` on an unbounded string is work
  proportional to the string, and the cursor arrives from the network before any
  authentication-independent ceiling has been applied to it.
  """

  @prefix "seq:"
  @max_sequence 2**63 - 1
  @max_digits 19

  @typedoc "A decoded cursor position: a change-log sequence in `[0, 2^63 - 1]`."
  @opaque t :: non_neg_integer()

  @typedoc "Why a cursor string is not a position."
  @type reject_reason ::
          :missing_prefix
          | :not_a_decimal_integer
          | :negative
          | :too_wide
          | :above_max_sequence

  defmodule Rejected do
    @moduledoc """
    A cursor string that does not name a reachable change-log position.

    Every one of these is a client error, and the route layer answers all of them
    with the same 400: distinguishing them on the wire would tell a caller which
    part of its own string it got wrong, which is of no use to it and is free
    information for anyone probing.
    """
    defexception [:cursor, :reason]

    @type t :: %__MODULE__{cursor: String.t(), reason: VitrialSync.Cursor.reject_reason()}

    @impl true
    def message(%__MODULE__{cursor: cursor, reason: reason}) do
      "invalid cursor #{inspect(cursor)}: #{reason}"
    end
  end

  @doc "The `BIGINT` ceiling for `SyncChangeLog.sequence`."
  @spec max_sequence() :: pos_integer()
  def max_sequence, do: @max_sequence

  @doc "Digit width of `#{@max_sequence}` -- the longest decodable cursor body."
  @spec max_digits() :: pos_integer()
  def max_digits, do: @max_digits

  @doc """
  Decode a cursor string.

  Returns `{:ok, position}`, or `{:error, %Rejected{}}`.

      iex> VitrialSync.Cursor.decode("seq:42")
      {:ok, 42}

      iex> {:error, rejected} = VitrialSync.Cursor.decode("seq:99999999999999999999")
      iex> rejected.reason
      :too_wide
  """
  @spec decode(String.t()) :: {:ok, t()} | {:error, Rejected.t()}
  def decode("seq:" <> digits) when is_binary(digits) do
    cond do
      digits == "" ->
        {:error, reject(digits, :not_a_decimal_integer)}

      not all_decimal?(digits) ->
        {:error, reject(digits, :not_a_decimal_integer)}

      String.length(digits) > @max_digits ->
        {:error, reject(digits, :too_wide)}

      true ->
        case Integer.parse(digits) do
          {position, ""} when position > @max_sequence ->
            {:error, reject(digits, :above_max_sequence)}

          {position, ""} ->
            {:ok, position}

          _ ->
            {:error, reject(digits, :not_a_decimal_integer)}
        end
    end
  end

  def decode(cursor), do: {:error, %Rejected{cursor: cursor, reason: :missing_prefix}}

  @doc """
  Decode a cursor that is allowed to be absent.

  An absent cursor means "I hold nothing", which is position 0 -- a real request
  from a freshly paired device, not a malformed one. Keeping that case distinct
  from `decode/1`'s rejections is what stops an absent cursor from being reported
  as a client error.

      iex> VitrialSync.Cursor.decode_optional(nil)
      {:ok, 0}
  """
  @spec decode_optional(String.t() | nil) :: {:ok, t()} | {:error, Rejected.t()}
  def decode_optional(nil), do: {:ok, 0}
  def decode_optional(cursor), do: decode(cursor)

  @doc """
  Render a position back to its wire form.

  The inverse of `decode/1`, so that a value this library produced always
  round-trips through `decode/1` unchanged.
  """
  @spec encode(t()) :: String.t()
  def encode(position) when is_integer(position) and position >= 0 and position <= @max_sequence do
    @prefix <> Integer.to_string(position)
  end

  defp reject(body, reason), do: %Rejected{cursor: @prefix <> body, reason: reason}

  defp all_decimal?(<<>>), do: true
  defp all_decimal?(<<char, rest::binary>>) when char >= ?0 and char <= ?9, do: all_decimal?(rest)
  defp all_decimal?(_), do: false
end