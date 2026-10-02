defmodule VitrialSync.Payload do
  @moduledoc """
  Decoding a sync record's payload into the object the engine stores.

  Reproduces `decode_payload` (`app/sync_service.py:82-94`) exactly, including
  the part that reads like a mistake.

  ## The double decode is deliberate

      try:
          decoded = base64.b64decode(raw, validate=True)
          value = json.loads(decoded)
      except Exception:
          value = json.loads(raw)

  Two input classes reach this function and the server accepts both: a client
  that base64-encodes its JSON on the way in (the iOS V1 contract), and a client
  that sends raw JSON. Rather than pick one, the reference implementation *tries*
  base64 and falls back. The fallback is load-bearing -- a client that sends
  `{"a": 1}` has bytes that are not valid base64 (the `{` is not in the
  alphabet), so without the fallback every raw-JSON client is rejected.

  ## Why the fallback is not a retry

  The subtlety, and the reason this is a module rather than four lines inline:
  the fallback fires only when the base64 attempt **threw**. If base64 succeeds
  and the result is valid JSON that is not an object -- `[1, 2]`, or `"a"`, or
  `42` -- no exception was raised, so the reference implementation does *not*
  retry as raw JSON. It raises `typed payload must decode to a JSON object`
  immediately.

  A natural-looking refactor that treats "not an object" as a reason to try the
  other encoding would accept `[1, 2]` sent as base64-of-`[1, 2]`... which
  coincidentally still fails, but would accept `"a"` sent base64-encoded where
  Python rejects it outright. Same class of bug as the escaped-key table in
  vertical 1: something that compiles, does not raise, and disagrees.

  ## `validate=True` is not the default

  `Base.decode64/1` in Elixir is lenient -- it ignores characters outside the
  alphabet. `base64.b64decode(raw, validate=True)` is not: CPython first applies
  `^[A-Za-z0-9+/]*={0,2}$` and raises on anything else. Lenient decoding here
  would mean a corrupted payload silently decoding to *something*, and the record
  being stored with content the client never sent.
  """

  alias VitrialSync.JSON

  @typedoc "A decoded payload: always a JSON object, never an array or a scalar."
  @type t :: %{optional(String.t()) => JSON.value()}

  @doc """
  Decode a record payload.

  Returns `{:ok, object}`, or `{:error, reason}` where reason is
  `{:invalid_payload, detail}` for a well-formed base64 string holding valid
  non-object JSON, and a `JSON.error_reason()` otherwise.

      iex> VitrialSync.Payload.decode(~s({"sku": "A-1"}))
      {:ok, %{"sku" => "A-1"}}

      iex> VitrialSync.Payload.decode("eyJhIjogMX0=")
      {:ok, %{"a" => 1}}

      iex> {:error, {:invalid_payload, :not_a_json_object}} = VitrialSync.Payload.decode("WzEsIDJd")
      :ok
  """
  @spec decode(binary()) ::
          {:ok, t()} | {:error, {:invalid_payload, :not_a_json_object} | JSON.error_reason()}
  def decode(raw) when is_binary(raw) do
    case strict_base64(raw) do
      {:ok, decoded} ->
        # No exception, therefore no fallback: a valid non-object here is a
        # rejection, exactly as in the reference implementation.
        case JSON.decode(decoded) do
          {:ok, object} when is_map(object) -> {:ok, object}
          {:ok, _not_an_object} -> {:error, {:invalid_payload, :not_a_json_object}}
          {:error, _reason} -> decode_raw(raw)
        end

      :error ->
        decode_raw(raw)
    end
  end

  @doc """
  Whether the bytes are a payload this server can admit.

  A convenience predicate over `decode/1` for the call sites that only need the
  yes/no. Written as a function rather than left to callers using `match?/2`,
  because `{:error, reason}` and `{:error, {:invalid_payload, detail}}` have
  different shapes and a caller that pattern-matches on the wrong one gets a
  `MatchError` at runtime instead of a rejection.
  """
  @spec decodable?(binary()) :: boolean()
  def decodable?(raw), do: match?({:ok, _}, decode(raw))

  defp decode_raw(raw) do
    case JSON.decode(raw) do
      {:ok, object} when is_map(object) -> {:ok, object}
      {:ok, _not_an_object} -> {:error, {:invalid_payload, :not_a_json_object}}
      {:error, reason} -> {:error, reason}
    end
  end

  # Mirrors CPython's `validate=True`: the shape check is a full-string match of
  # `[A-Za-z0-9+/]*={0,2}` before any decoding is attempted. Padding is only legal
  # at the end, so a `=` in the middle is a shape failure rather than a decode
  # failure, and both paths land on the same fallback.
  defp strict_base64(raw) do
    if base64_shape?(raw) and rem(byte_size(raw), 4) == 0 do
      Base.decode64(raw)
    else
      :error
    end
  end

  defp base64_shape?(<<>>), do: true
  defp base64_shape?(<<byte, rest::binary>>) when byte in ?A..?Z, do: base64_shape?(rest)
  defp base64_shape?(<<byte, rest::binary>>) when byte in ?a..?z, do: base64_shape?(rest)
  defp base64_shape?(<<byte, rest::binary>>) when byte in ?0..?9, do: base64_shape?(rest)
  defp base64_shape?(<<byte, rest::binary>>) when byte === ?+, do: base64_shape?(rest)
  defp base64_shape?(<<byte, rest::binary>>) when byte === ?/, do: base64_shape?(rest)
  # Padding: at most two, and only at the very end.
  defp base64_shape?(<<?=, ?=, rest::binary>>), do: rest == <<>>
  defp base64_shape?(<<?=, rest::binary>>), do: rest == <<>>
  defp base64_shape?(_raw), do: false
end
