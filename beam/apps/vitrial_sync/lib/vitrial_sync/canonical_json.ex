defmodule VitrialSync.CanonicalJSON do
  @moduledoc """
  Byte-exact canonical JSON for the sync mutation fingerprint.

  The fingerprint is what distinguishes a legitimate replay of a
  `clientMutationID` from accidental or malicious reuse of that id for different
  bytes. That distinction is only sound if the canonical encoding is a *function
  of the mutation's content alone* -- two processes, in either language, must
  derive the same bytes for the same mutation, or a legitimate replay will look
  like a collision and be rejected.

  This is written rather than taken from a hex dependency because the shape it has
  to encode is tiny and closed: a flat map of `String.t()` keys whose values are
  `String.t()`, non-negative integers, or `nil`. A general JSON encoder would carry
  a transitive CVE surface and a large API to defend in exchange for encoding six
  keys. Anything outside that closed shape raises `Unsupported` rather than being
  coerced, so widening the fingerprint's inputs is a deliberate act with a failing
  test attached, not something that happens by accident.

  The encoding matches Python's `json.dumps(material, separators=(",", ":"),
  sort_keys=True)` exactly, because `app/idempotency.py` on the Python service
  produces the bytes this has to agree with:

    * keys sorted by code point, which for this key set is `baseServerRevision`,
      `deletedAt`, `entityID`, `entityType`, `payload`, `updatedAt`
    * no insignificant whitespace
    * `ensure_ascii=True`, so every code point above 0x7F becomes `\\uXXXX`, and a
      code point above the BMP becomes a surrogate pair rather than its UTF-8 octets
    * `\\"` and `\\\\` are always escaped
    * `\\b`, `\\f`, `\\n`, `\\r` and `\\t` use their short escapes; every other
      control character below 0x20 becomes `\\u00XX`
    * `\/` is NOT escaped (Python leaves the solidus alone)
  """

  @typedoc """
  Any JSON value.

  A `nil` maps to JSON `null`, `true`/`false` to the literals (NOT to `1`/`0`),
  integers to bare decimals including negatives, and floats to Python's `repr`.
  """
  @type scalar :: String.t() | integer() | float() | boolean() | nil
  @type value :: scalar | [value()] | %{optional(String.t()) => value()}
  @type object :: %{optional(String.t()) => value()}

  defmodule Unsupported do
    @moduledoc """
    Raised when a value falls outside the closed shape above.

    This is a bug in the caller, not a bad request: the fingerprint's inputs are
    built from an already-validated record, so anything unexpected means the
    record and this module have drifted apart.
    """

    defexception [:value, :path]

    @impl true
    def message(%__MODULE__{value: value, path: path}) do
      "canonical JSON cannot encode #{inspect(value)} at #{path}"
    end
  end

  # The two mandatory escapes plus Python's short forms. `\/` is deliberately
  # absent: Python does not escape the solidus, and matching that is the point.
  #
  # EVERY key here is an integer, including the quote and the backslash. Writing
  # those two as the strings `"\""` and `"\\"` compiles and looks correct, but
  # `escape_char/1` is called with an integer -- a character off a binary -- so
  # `Map.fetch(@short_escapes, char)` never matches a string key and both escapes
  # are silently skipped. Every string containing a double quote or a backslash
  # then fingerprints differently from Python, which is a wrong digest rather than
  # an error. The character literal `?"` is not an option: Elixir's lexer reads
  # it as a question mark followed by the OPENING of a string, so the rest of the
  # line fails with "unexpected token: :" at a column nowhere near the cause.
  @short_escapes %{
    0x22 => "\\\"",
    0x5C => "\\\\",
    ?\b => "\\b",
    ?\f => "\\f",
    ?\n => "\\n",
    ?\r => "\\r",
    ?\t => "\\t"
  }

  @doc """
  The short-escape table, exposed so its own key types can be tested.

  Public because of how it went wrong once: a mixed-type table (integer keys for
  the control characters, string keys for the quote and the backslash) compiles
  cleanly, raises nothing, and silently skips both escapes -- see
  `canonical_json_test.exs`.
  """
  @spec short_escapes() :: %{non_neg_integer() => String.t()}
  def short_escapes, do: @short_escapes

  @doc """
  Encode `object` to canonical JSON bytes.

  Keys are sorted, so the same content always yields the same bytes regardless of
  map iteration order. This is the whole point: the caller cannot be trusted to
  build two equal maps in the same insertion order.
  """
  @spec encode(object()) :: binary()
  def encode(object) when is_map(object) do
    encode_pairs(object, [])
  end

  @doc """
  The SHA-256 hex digest of the canonical encoding of `object`.

  This is the value stored in `sync_mutation_fingerprints.request_fingerprint`,
  and it is what `VitrialSync.Fingerprint` exposes for a single mutation record.
  """
  @spec digest(object()) :: String.t()
  def digest(object), do: sha256_hex(encode(object))

  @doc """
  The SHA-256 hex digest of already-encoded bytes.

  For callers that had to build the canonical encoding themselves to compare it,
  and would otherwise re-encode it -- or worse, hash the encoded *string* as if it
  were a JSON object, which is a silently different digest rather than an error.
  """
  @spec sha256_hex(binary()) :: String.t()
  def sha256_hex(bytes) when is_binary(bytes) do
    :crypto.hash(:sha256, bytes) |> Base.encode16(case: :lower)
  end

  defp encode_pairs(object, path) do
    # Sort on the raw key, not on the encoded fragment: after escaping, keys are
    # iolists and no longer comparable as strings.
    pairs =
      object
      |> Enum.map(fn {key, value} -> {key, encode_value(value, path ++ [key])} end)
      |> Enum.sort_by(&elem(&1, 0))

    case pairs do
      [] ->
        "{}"

      _ ->
        body =
          pairs
          |> Enum.map(fn {key, value} -> ["\"", escape(key), "\":", value] end)
          |> Enum.intersperse(?,)

        [?{, body, ?}] |> IO.iodata_to_binary()
    end
  end

  defp encode_value(value, _path) when is_binary(value) do
    ["\"", escape(value), "\""]
  end

  # Booleans are matched before nil and integers so the literal is explicit.
  # In Elixir `true`/`false` are atoms rather than integers so there is no
  # overlap today -- but a digest that silently became `1`/`0` would be exactly
  # the class of defect this module exists to prevent.
  defp encode_value(true, _path), do: "true"
  defp encode_value(false, _path), do: "false"
  defp encode_value(nil, _path), do: "null"

  defp encode_value(value, _path) when is_integer(value) do
    Integer.to_string(value)
  end

  defp encode_value(value, _path) when is_float(value) do
    float(value)
  end

  defp encode_value([], _path), do: "[]"

  defp encode_value(values, path) when is_list(values) do
    body =
      values
      |> Enum.with_index()
      |> Enum.map(fn {value, index} -> encode_value(value, path ++ [index]) end)
      |> Enum.intersperse(?,)

    ["[", body, "]"] |> IO.iodata_to_binary()
  end

  defp encode_value(value, path) when is_map(value) do
    encode_pairs(value, path)
  end

  defp encode_value(value, path), do: raise(Unsupported, value: value, path: path)

  # Python renders floats with `repr`, which is the shortest string that
  # round-trips -- the same algorithm Erlang's `:short` uses, and the two agree on
  # every value tested. They differ only in scientific notation:
  #
  #     Erlang [:short]   Python repr
  #     1.0e22            1e+22
  #     1.0e-5            1e-05
  #
  # So: drop a trailing ".0" from an integral mantissa, and render the exponent
  # with a mandatory sign and at least two digits. The magnitude at which Erlang
  # switches to scientific notation already matches Python's.
  defp float(value) do
    case String.split(:erlang.float_to_binary(value, [:short]), "e") do
      [fixed] ->
        fixed

      [mantissa, exponent] ->
        mantissa =
          if String.ends_with?(mantissa, ".0"),
            do: String.trim_trailing(mantissa, ".0"),
            else: mantissa

        mantissa <> "e" <> python_exponent(exponent)
    end
  end

  defp python_exponent("-" <> digits), do: "-" <> String.pad_leading(digits, 2, "0")
  defp python_exponent(digits), do: "+" <> String.pad_leading(digits, 2, "0")

  defp escape(string) do
    # `::utf8` is load-bearing. Walking bytes would turn a single code point
    # above the BMP into its UTF-8 octets and emit four \u00XX escapes, where
    # Python emits a surrogate pair -- a digest that differs only for exactly the
    # inputs most likely to be human-authored, and only in the one field an
    # operator would not think to check.
    for <<char::utf8 <- string>>, into: "" do
      escape_char(char)
    end
  end

  defp escape_char(char) when char > 0xFFFF do
    offset = char - 0x10000

    escape_unit(0xD800 + Bitwise.bsr(offset, 10)) <>
      escape_unit(0xDC00 + Bitwise.band(offset, 0x3FF))
  end

  defp escape_char(char) do
    # The short-escape table is consulted BEFORE the numeric ranges, and has to
    # be: tab, newline, carriage return, backspace and form feed all sit below
    # 0x20, so a guard clause ahead of this lookup would make every one of them
    # emit \u0009-style escapes -- producing a digest that differs from Python's
    # for every string containing a newline.
    case Map.fetch(@short_escapes, char) do
      {:ok, escaped} -> escaped
      :error when char < 0x20 or char >= 0x7F -> escape_unit(char)
      :error -> <<char>>
    end
  end

  defp escape_unit(code_unit) do
    "\\u" <>
      (code_unit |> Integer.to_string(16) |> String.downcase() |> String.pad_leading(4, "0"))
  end
end
