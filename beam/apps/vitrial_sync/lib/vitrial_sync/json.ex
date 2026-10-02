defmodule VitrialSync.JSON do
  @moduledoc """
  A strict RFC 8259 decoder, written here rather than taken as a dependency.

  ## Why this exists and is hand-written

  `app/schemas.py` types a sync payload as `bytes` and the server decodes it with
  `json.loads` in `decode_payload` (`app/sync_service.py:82-94`). The BEAM has no
  built-in JSON parser at all, so the obvious move is a hex package.

  The dependency doctrine (`vitrial-phase0/DEPENDENCY-DOCTRINE.md`) rules that out:
  a JSON library is a transitive dependency chain with a CVE surface, for a grammar
  that is a few hundred lines and is already fully specified. So this is a
  reimplementation, and it is scoped to exactly what the sync contract needs
  rather than to the whole of JSON.

  ## Parity, and where this deliberately is not

  The two estates have to agree on which bytes are a payload, or a mutation Python
  accepts is rejected by the BEAM and the client sees an error that depends on which
  server answered. Three behaviours are therefore copied from CPython's
  `json.loads` rather than from the RFC:

    * **BOM detection on byte input.** `json.loads(b"...")` sniffs a UTF-8/16/32
      byte-order mark and transcodes. Payload bytes arrive base64-decoded from the
      wire, so this is a real input class, not a hypothetical one.
    * **Duplicate object keys: last one wins.** CPython's scanner assigns into a
      dict in document order. `Map.put/3` does the same, so a duplicated key
      resolves identically rather than being an error.
    * **Leading zeros and bare fractions are rejected.** `01`, `1.`, `.1`, `+1` are
      all syntax errors in both, even though a permissive number scanner would
      accept several of them.

  Two behaviours are **narrower** than CPython, and both narrow toward rejecting
  rather than accepting:

    * **`NaN` / `Infinity` / `-Infinity` are rejected, as is overflow to
      infinity.** CPython accepts the literals by default, and it also lets a
      finite literal *become* an infinity: `1e999` decodes to `float('inf')`
      there and is a parse error here, because Erlang's float conversion
      refuses to represent it. The asymmetry is deliberate and is not a
      compatibility loss in practice: the value is destined for a PostgreSQL
      `JSONB` column, which rejects non-finite numbers too, so Python's
      acceptance converts into a database error several layers down rather than
      into a stored record.
    * **Unpaired surrogates are rejected.** A `\\u` escape naming a surrogate
      codepoint with no partner produces a Python `str` with a lone surrogate,
      which has no UTF-8 encoding. Accepting it here would mean carrying a term
      that cannot be written to a file, a socket, or a database.

  Both are divergences from the reference implementation. They are recorded here
  because an unrecorded divergence is a bug that surfaces to a client as
  "sometimes the server disagrees with itself", and that is the most expensive
  class of bug this port can produce.
  """

  # CPython's C scanner raises RecursionError past roughly this nesting depth. A
  # payload nested deeper than any real document is either an attack or a mistake,
  # and either way it must be an error rather than a stack overflow.
  @max_depth 1000

  @type value ::
          nil
          | boolean()
          | number()
          | String.t()
          | [value()]
          | %{optional(String.t()) => value()}

  @typedoc """
  Why a byte string is not JSON. Deliberately coarse: the caller only ever renders
  these into a rejection, and a client gains nothing from being told which byte
  tripped the parser.
  """
  @type error_reason ::
          :empty
          | :unexpected_end
          | :unexpected_byte
          | :invalid_escape
          | :invalid_unicode_escape
          | :unpaired_surrogate
          | :control_character_in_string
          | :invalid_number
          | :invalid_encoding
          | :too_deep
          | :trailing_data

  @doc """
  Decode JSON bytes into native terms.

  Returns `{:ok, value}`, or `{:error, reason}`.

      iex> VitrialSync.JSON.decode(~s({"a": [1, 2], "b": null}))
      {:ok, %{"a" => [1, 2], "b" => nil}}

      iex> VitrialSync.JSON.decode("{} {}")
      {:error, :trailing_data}
  """
  @spec decode(binary()) :: {:ok, value()} | {:error, error_reason()}
  def decode(bytes) when is_binary(bytes) do
    with {:ok, body} <- strip_bom(bytes) do
      case skip_whitespace(body) do
        <<>> ->
          {:error, :empty}

        body ->
          # The parser hands back whatever is left after the value so callers can
          # chain; the public contract is a whole document, so a remainder here is
          # `{} {}` -- two documents, which is not one.
          case parse_value(body, 0) do
            {:ok, value, trailing} ->
              case skip_whitespace(trailing) do
                <<>> -> {:ok, value}
                _ -> {:error, :trailing_data}
              end

            {:error, reason} ->
              {:error, reason}
          end
      end
    end
  end

  @doc """
  Decode, requiring a JSON **object** at the top level.

  This is the shape `decode_payload` insists on, and it is a separate function
  rather than a guard at the call site so that the requirement has one
  implementation instead of one per caller.
  """
  @spec decode_object(binary()) :: {:ok, %{optional(String.t()) => value()}} | {:error, error_reason()}
  def decode_object(bytes) when is_binary(bytes) do
    case decode(bytes) do
      {:ok, object} when is_map(object) -> {:ok, object}
      {:ok, _other} -> {:error, :unexpected_byte}
      {:error, reason} -> {:error, reason}
    end
  end

  # -- encoding detection ------------------------------------------------------

  # Order matters: the four-byte UTF-32 marks begin with the same two bytes as the
  # UTF-16 marks, so a UTF-32 little-endian document would be truncated to a
  # UTF-16 one and then mis-decoded.
  defp strip_bom(<<0xEF, 0xBB, 0xBF, rest::binary>>), do: {:ok, rest}
  defp strip_bom(<<0xFF, 0xFE, 0x00, 0x00, rest::binary>>), do: transcode(rest, :utf32, :little)
  defp strip_bom(<<0x00, 0x00, 0xFE, 0xFF, rest::binary>>), do: transcode(rest, :utf32, :big)
  defp strip_bom(<<0xFF, 0xFE, rest::binary>>), do: transcode(rest, :utf16, :little)
  defp strip_bom(<<0xFE, 0xFF, rest::binary>>), do: transcode(rest, :utf16, :big)
  defp strip_bom(bytes), do: {:ok, bytes}

  defp transcode(bytes, family, endian) do
    {:ok, :unicode.characters_to_binary(bytes, {family, endian}, :utf8)}
  rescue
    # A BOM that promises an encoding the bytes do not honour is malformed input,
    # and letting Erlang raise out of a payload decode would escape as a crash
    # rather than as a rejected record. Both failure modes are covered: a badarg
    # from a truncated unit, and UnicodeConversionError from a byte that is not
    # valid in the declared encoding at all.
    ArgumentError -> {:error, :invalid_encoding}
    UnicodeConversionError -> {:error, :invalid_encoding}
  end

  # -- values ------------------------------------------------------------------

  # Whitespace is skipped HERE rather than at each call site. RFC 8251 allows it
  # before any value, so a caller that has just consumed a separator -- `{`, `,`,
  # or `:` -- must be able to hand over leading whitespace. Skipping in the
  # callers instead means four places to remember, and the one that is forgotten
  # rejects a valid document.
  defp parse_value(bin, depth) do
    case skip_whitespace(bin) do
      <<>> -> {:error, :unexpected_end}
      skipped -> parse_value_here(skipped, depth)
    end
  end

  defp parse_value_here(<<"true", rest::binary>>, _depth), do: {:ok, true, rest}
  defp parse_value_here(<<"false", rest::binary>>, _depth), do: {:ok, false, rest}
  defp parse_value_here(<<"null", rest::binary>>, _depth), do: {:ok, nil, rest}
  defp parse_value_here(<<?{, rest::binary>>, depth), do: enter_object(rest, depth, %{})
  defp parse_value_here(<<?[, rest::binary>>, depth), do: enter_array(rest, depth, [])
  defp parse_value_here(<<?", rest::binary>>, _depth), do: parse_string(rest, [])

  defp parse_value_here(<<?-, _rest::binary>> = bin, _depth), do: parse_number(bin)

  defp parse_value_here(<<digit, _rest::binary>> = bin, _depth) when digit >= ?0 and digit <= ?9,
    do: parse_number(bin)

  defp parse_value_here(<<_byte, _rest::binary>>, _depth), do: {:error, :unexpected_byte}

  defp enter_object(bin, depth, acc) do
    if depth > @max_depth do
      {:error, :too_deep}
    else
      case skip_whitespace(bin) do
        <<?}, rest::binary>> -> {:ok, acc, rest}
        skipped -> parse_object_member(skipped, depth, acc)
      end
    end
  end

  defp parse_object_member(bin, depth, acc) do
    with {:ok, key, after_key} <- parse_key(bin),
         # Whitespace is permitted between the key and its colon -- `{"a" : 1}`
         # is the same document as `{"a": 1}`. Skipping it at the value instead
         # is not enough, because the colon has to be consumed before the value
         # is reached.
         {:ok, after_colon} <- expect(skip_whitespace(after_key), 0x3A),
         {:ok, value, rest} <- parse_value(after_colon, depth + 1) do
      # The value is folded into the accumulator before the separator is checked,
      # which is what makes a duplicate key resolve last-one-wins.
      acc = Map.put(acc, key, value)

      case skip_whitespace(rest) do
        <<0x2C, more::binary>> -> parse_object_member(more, depth, acc)
        <<?}, more::binary>> -> {:ok, acc, more}
        <<>> -> {:error, :unexpected_end}
        _ -> {:error, :unexpected_byte}
      end
    end
  end

  defp enter_array(bin, depth, acc) do
    if depth > @max_depth do
      {:error, :too_deep}
    else
      case skip_whitespace(bin) do
        <<?], rest::binary>> -> {:ok, Enum.reverse(acc), rest}
        skipped -> parse_array_element(skipped, depth, acc)
      end
    end
  end

  defp parse_array_element(bin, depth, acc) do
    with {:ok, value, rest} <- parse_value(bin, depth + 1) do
      acc = [value | acc]

      case skip_whitespace(rest) do
        <<0x2C, more::binary>> -> parse_array_element(more, depth, acc)
        <<?], more::binary>> -> {:ok, Enum.reverse(acc), more}
        <<>> -> {:error, :unexpected_end}
        _ -> {:error, :unexpected_byte}
      end
    end
  end

  defp parse_key(bin) do
    case skip_whitespace(bin) do
      <<0x22, rest::binary>> -> parse_string(rest, [])
      <<>> -> {:error, :unexpected_end}
      _ -> {:error, :unexpected_byte}
    end
  end

  defp expect(<<byte, rest::binary>>, expected) when byte === expected, do: {:ok, rest}
  defp expect(<<>>, _expected), do: {:error, :unexpected_end}
  defp expect(<<_byte, _rest::binary>>, _expected), do: {:error, :unexpected_byte}

  defp skip_whitespace(<<byte, rest::binary>>) when byte in [0x20, 0x09, 0x0A, 0x0D],
    do: skip_whitespace(rest)

  defp skip_whitespace(bin), do: bin

  # -- strings -----------------------------------------------------------------

  defp parse_string(<<0x22, rest::binary>>, acc),
    do: {:ok, acc |> Enum.reverse() |> IO.iodata_to_binary(), rest}
  defp parse_string(<<?\\, rest::binary>>, acc), do: parse_escape(rest, acc)

  defp parse_string(<<char::utf8, rest::binary>>, acc) when char >= 0x20,
    do: parse_string(rest, [<<char::utf8>> | acc])

  defp parse_string(<<byte, _rest::binary>>, _acc) when byte < 0x20,
    do: {:error, :control_character_in_string}

  defp parse_string(<<>>, _acc), do: {:error, :unexpected_end}

  # Reached when the next byte cannot begin a UTF-8 sequence, which is a malformed
  # string rather than a control character. Matching `<<char::utf8>>` above simply
  # fails to match, so this clause catches the residue.
  defp parse_string(<<_byte, _rest::binary>>, _acc), do: {:error, :invalid_encoding}

  defp parse_escape(<<?", rest::binary>>, acc), do: parse_string(rest, [0x22 | acc])
  defp parse_escape(<<?\\, rest::binary>>, acc), do: parse_string(rest, [0x5C | acc])
  defp parse_escape(<<?/, rest::binary>>, acc), do: parse_string(rest, [0x2F | acc])
  defp parse_escape(<<?b, rest::binary>>, acc), do: parse_string(rest, [0x08 | acc])
  defp parse_escape(<<?f, rest::binary>>, acc), do: parse_string(rest, [0x0C | acc])
  defp parse_escape(<<?n, rest::binary>>, acc), do: parse_string(rest, [0x0A | acc])
  defp parse_escape(<<?r, rest::binary>>, acc), do: parse_string(rest, [0x0D | acc])
  defp parse_escape(<<?t, rest::binary>>, acc), do: parse_string(rest, [0x09 | acc])
  defp parse_escape(<<?u, rest::binary>>, acc), do: parse_unicode_escape(rest, acc)
  defp parse_escape(<<>>, _acc), do: {:error, :unexpected_end}
  defp parse_escape(<<_byte, _rest::binary>>, _acc), do: {:error, :invalid_escape}

  defp parse_unicode_escape(<<a, b, c, d, rest::binary>>, acc) do
    case hex_quad(a, b, c, d) do
      {:ok, high} when high in 0xD800..0xDBFF -> parse_low_surrogate(high, rest, acc)
      {:ok, low} when low in 0xDC00..0xDFFF -> {:error, :unpaired_surrogate}
      {:ok, codepoint} -> parse_string(rest, [<<codepoint::utf8>> | acc])
      :error -> {:error, :invalid_unicode_escape}
    end
  end

  defp parse_unicode_escape(<<>>, _acc), do: {:error, :unexpected_end}
  defp parse_unicode_escape(<<_byte, _rest::binary>>, _acc), do: {:error, :invalid_unicode_escape}

  # A high surrogate is only meaningful as the first half of a pair. Anything else
  # -- a different escape, a plain character, the end of the string -- is an
  # unpaired surrogate rather than a value to salvage.
  defp parse_low_surrogate(high, <<0x5C, ?u, a, b, c, d, rest::binary>>, acc) do
    case hex_quad(a, b, c, d) do
      {:ok, low} when low in 0xDC00..0xDFFF ->
        codepoint = 0x10000 + (high - 0xD800) * 0x400 + (low - 0xDC00)
        parse_string(rest, [<<codepoint::utf8>> | acc])

      _ ->
        {:error, :unpaired_surrogate}
    end
  end

  defp parse_low_surrogate(_high, _rest, _acc), do: {:error, :unpaired_surrogate}

  defp hex_quad(a, b, c, d) do
    with {:ok, ha} <- hex_digit(a),
         {:ok, hb} <- hex_digit(b),
         {:ok, hc} <- hex_digit(c),
         {:ok, hd} <- hex_digit(d) do
      {:ok, ha * 4096 + hb * 256 + hc * 16 + hd}
    end
  end

  defp hex_digit(byte) when byte >= ?0 and byte <= ?9, do: {:ok, byte - ?0}
  defp hex_digit(byte) when byte >= ?a and byte <= ?f, do: {:ok, byte - ?a + 10}
  defp hex_digit(byte) when byte >= ?A and byte <= ?F, do: {:ok, byte - ?A + 10}
  defp hex_digit(_byte), do: :error

  # -- numbers -----------------------------------------------------------------

  defp parse_number(bin) do
    {sign, rest} =
      case bin do
        <<?-, more::binary>> -> {"-", more}
        _ -> {"", bin}
      end

    case rest do
      # A leading zero admits no further digits: "01" is two tokens, not a number.
      <<?0, more::binary>> -> parse_number_tail(sign <> "0", more)
      <<digit, _more::binary>> when digit >= ?1 and digit <= ?9 ->
        {digits, more} = take_digits(rest, [])
        parse_number_tail(sign <> digits, more)

      <<>> ->
        {:error, :unexpected_end}

      _ ->
        {:error, :invalid_number}
    end
  end

  defp parse_number_tail(integer_part, <<?., rest::binary>>) do
    {fraction, more} = take_digits(rest, [])

    if fraction == "" do
      {:error, :invalid_number}
    else
      parse_exponent(integer_part <> "." <> fraction, more)
    end
  end

  defp parse_number_tail(literal, <<marker, _rest::binary>> = rest) when marker === ?e or marker === ?E,
    do: parse_exponent(literal, rest)

  defp parse_number_tail(literal, rest), do: finish_number(literal, rest)

  defp parse_exponent(literal, <<marker, rest::binary>>) when marker === ?e or marker === ?E do
    {sign, rest} =
      case rest do
        <<?+, more::binary>> -> {"", more}
        <<?-, more::binary>> -> {"-", more}
        _ -> {"", rest}
      end

    {digits, rest} = take_digits(rest, [])

    if digits == "" do
      {:error, :invalid_number}
    else
      finish_number(literal <> "e" <> sign <> digits, rest)
    end
  end

  defp parse_exponent(literal, rest), do: finish_number(literal, rest)

  # The integer/float split has to be decided by the literal's shape, not by its
  # value: JSON has one number type, but `payload_json` round-trips through
  # PostgreSQL `JSONB` and through Python, and a revision or a count that arrives
  # as `1.0` where the other estate sees `1` is a digest mismatch waiting to happen.
  defp finish_number(literal, rest) do
    if String.contains?(literal, [".", "e", "E"]) do
      case Float.parse(literal) do
        {value, ""} -> {:ok, value, rest}
        :error -> {:error, :invalid_number}
      end
    else
      case Integer.parse(literal) do
        {value, ""} -> {:ok, value, rest}
        _ -> {:error, :invalid_number}
      end
    end
  end

  defp take_digits(<<digit, rest::binary>>, acc) when digit >= ?0 and digit <= ?9,
    do: take_digits(rest, [digit | acc])

  defp take_digits(bin, acc), do: {acc |> Enum.reverse() |> List.to_string(), bin}
end
