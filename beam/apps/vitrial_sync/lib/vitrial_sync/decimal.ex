defmodule VitrialSync.Decimal do
  @moduledoc """
  Exact decimal arithmetic, for the two places the delivery authority compares money.

  The reference reaches for `decimal.Decimal` three times:

      app/delivery_execution.py:73-79      _decimal
      app/delivery_execution.py:103-112    _quotation_total (sums line totals)
      app/ownership.py:159                _quotation_design_total (see below)

  `_decimal` is `Decimal(str(value))`, which is why it accepts an integer, a
  float and a numeric *string* alike and refuses `None` and `bool` -- and why the
  float path goes through `str()` first. In Python `str(1.1)` is the shortest
  round-tripping decimal `"1.1"`, and `Decimal("1.1")` is exactly one tenth, not
  the binary double nearest to it.

  ## Why BEAM floats are not a substitute

  Two reasons, and the first one bites in ordinary money arithmetic.

    * **`Decimal(str(x))` is decimal arithmetic on the shortest repr.** Summing
      line totals in binary doubles accumulates error: `0.1 + 0.2` is
      `0.30000000000000004` as a double, and `Decimal("0.1") + Decimal("0.2")` is
      exactly `0.3`. A quotation whose lines total `0.3` would match a delivery
      execution quoting `0.3` in Python and fail here.
    * **The BEAM has no `Decimal`.** `Decimal` is a C extension of CPython; there
      is nothing in the standard library to call. This app has no dependencies at
      all (`beam/mix.exs` declares `deps: []`), and the dependency doctrine
      (`vitrial-phase0/DEPENDENCY-DOCTRINE.md`) rules out a hex package for the
      same reason it rules out a JSON library.

  So this is a fixed-point decimal: a sign, a non-negative integer of digits, and
  a scale, meaning `sign * digits / 10 ** scale`. Every operation here is integer
  arithmetic on that triple, so `add/2` and `compare/2` are exact for any input
  that fits in a machine integer, which is the range the delivery contract
  admits.

  ## What it deliberately does not do

  Rounding, precision context and exponent bounds. CPython's `Decimal` has all
  three and none of them is reachable from JSON input here: the reference never
  sets a context, the default context has 28 significant digits, and a delivery
  amount with 29 of them would be an amount the server stores in a numeric column
  long before this function sees it. Parsing rejects what it cannot represent
  exactly rather than rounding it, so a 40-digit amount is a rejection -- which is
  what `Decimal(str(x))` followed by a comparison to a small bound also amounts to
  in practice.
  """

  @type t :: %__MODULE__{neg?: boolean(), digits: non_neg_integer(), scale: non_neg_integer()}

  @enforce_keys [:neg?, :digits, :scale]
  defstruct [:neg?, :digits, :scale]

  @doc "Decimal zero, the accumulator `_quotation_total` starts from."
  @spec zero() :: t()
  def zero, do: %__MODULE__{neg?: false, digits: 0, scale: 0}

  @max_digits 4_096
  @max_scale 4_096

  @doc """
  `_decimal(value, name=...)`, `app/delivery_execution.py:73-79`.

  Returns `{:ok, decimal}`, or `:error` where the reference raises
  `DeliveryExecutionRejected(f"{name} is invalid")`. `nil` and booleans are
  refused here rather than downstream, exactly as the reference refuses them
  before calling `str`.

      iex> VitrialSync.Decimal.parse(12)
      {:ok, %VitrialSync.Decimal{neg?: false, digits: 12, scale: 0}}

      iex> VitrialSync.Decimal.parse("1.50")
      {:ok, %VitrialSync.Decimal{neg?: false, digits: 150, scale: 2}}

      iex> VitrialSync.Decimal.parse(true)
      :error

      iex> VitrialSync.Decimal.parse(nil)
      :error
  """
  @spec parse(term()) :: {:ok, t()} | :error
  def parse(value) when is_integer(value), do: from_digits(Integer.to_string(value))

  def parse(value) when is_float(value) do
    # `Float.to_string/1` is Elixir's shortest round-tripping rendering, the same
    # one `inspect/1` produces and the same one CPython's `str(float)` produces.
    from_digits(Float.to_string(value))
  end

  def parse(value) when is_binary(value), do: from_digits(value)

  # `Decimal(str(True))` is `Decimal("True")`, which raises InvalidOperation, and
  # the reference checks the bool case before that anyway.
  def parse(_value), do: :error

  @doc """
  Parse a decimal literal: `[sign] digits [. digits] [ (e|E) [sign] digits ]`.

  Mirrors what `Decimal(str(value))` accepts for the shapes a JSON number or a
  numeric string can take. Rejects `NaN`, `Infinity`, an empty digit run, two
  decimal points, and anything whose scale or digit count is past this module's
  bounds -- a rejection rather than a silent round, because a rounded amount is
  a wrong amount and the reference would carry the full precision.
  """
  @spec from_digits(String.t()) :: {:ok, t()} | :error
  def from_digits(text) when is_binary(text) do
    with {:ok, rest, neg?} <- take_sign(text),
         {:ok, mantissa, exponent} <- split_exponent(rest) do
      assemble(neg?, mantissa, exponent)
    else
      _other -> :error
    end
  end

  def from_digits(_text), do: :error

  defp take_sign("-" <> rest), do: {:ok, rest, true}
  defp take_sign("+" <> rest), do: {:ok, rest, false}
  defp take_sign(rest), do: {:ok, rest, false}

  # `parts: 2` keeps the separator, so a second `e` lands inside the exponent and
  # is rejected by `Integer.parse/1` rather than silently ignored.
  defp split_exponent(text) do
    case String.split(text, ["e", "E"], parts: 2) do
      [mantissa] ->
        {:ok, mantissa, 0}

      [mantissa, exponent] ->
        case Integer.parse(exponent) do
          {value, ""} -> {:ok, mantissa, value}
          _other -> :error
        end
    end
  end

  defp assemble(neg?, mantissa, exponent) do
    case String.split(mantissa, ".", parts: 2) do
      [whole] -> finish(neg?, whole, "", exponent)
      [whole, fraction] -> finish(neg?, whole, fraction, exponent)
      _more -> :error
    end
  end

  # `"12.50"` is digits `1250` at scale 2 -- the fraction's length *is* the scale,
  # which is why whole and fraction are concatenated rather than parsed apart.
  defp finish(neg?, whole, fraction, exponent) do
    if all_digits?(whole) and all_digits?(fraction) and (whole != "" or fraction != "") do
      digits = String.to_integer(whole <> fraction)
      scale = byte_size(fraction) - exponent

      cond do
        scale > @max_scale -> :error
        scale >= 0 -> {:ok, %__MODULE__{neg?: neg?, digits: digits, scale: scale}}
        -scale > @max_digits -> :error
        true -> {:ok, %__MODULE__{neg?: neg?, digits: digits * Integer.pow(10, -scale), scale: 0}}
      end
    else
      :error
    end
  end

  defp all_digits?(""), do: true

  # A binary is not an Enumerable in Elixir, so the codepoints are unpacked
  # first. `:binary.match/2` would do it too, but `String.to_charlist/1` keeps the
  # comparison in characters rather than bytes.
  defp all_digits?(text) do
    text
    |> String.to_charlist()
    |> Enum.all?(fn codepoint -> codepoint >= ?0 and codepoint <= ?9 end)
  end

  @doc """
  Python `<=` on two decimals.

  `app/delivery_execution.py:265` and `:352` use `<= 0`; `_quotation_total`
  compares with `!=`. Both are exact here.
  """
  @spec compare(t(), t()) :: :lt | :eq | :gt
  def compare(%__MODULE__{} = left, %__MODULE__{} = right) do
    cond do
      left.neg? != right.neg? -> if left.neg?, do: :lt, else: :gt
      true -> compare_magnitude(left, right)
    end
  end

  defp compare_magnitude(%__MODULE__{neg?: true} = left, %__MODULE__{} = right) do
    # Two negatives compare the opposite way to their magnitudes.
    case compare_abs(left, right) do
      :lt -> :gt
      :eq -> :eq
      :gt -> :lt
    end
  end

  defp compare_magnitude(%__MODULE__{} = left, %__MODULE__{} = right),
    do: compare_abs(left, right)

  defp compare_abs(%__MODULE__{} = left, %__MODULE__{} = right) do
    scale = max(left.scale, right.scale)
    l = left.digits * Integer.pow(10, scale - left.scale)
    r = right.digits * Integer.pow(10, scale - right.scale)

    cond do
      l < r -> :lt
      l > r -> :gt
      true -> :eq
    end
  end

  @doc "`value <= 0`."
  @spec not_positive?(t()) :: boolean()
  def not_positive?(%__MODULE__{} = value), do: compare(value, zero()) != :gt

  @doc "`value > 0`."
  @spec positive?(t()) :: boolean()
  def positive?(%__MODULE__{} = value), do: compare(value, zero()) == :gt

  @doc "`value != other`."
  @spec not_eq?(t(), t()) :: boolean()
  def not_eq?(%__MODULE__{} = left, %__MODULE__{} = right), do: compare(left, right) != :eq

  @doc "`left + right`, as `_quotation_total` accumulates line totals."
  @spec add(t(), t()) :: t()
  def add(%__MODULE__{} = left, %__MODULE__{} = right) do
    scale = max(left.scale, right.scale)
    l = left.digits * Integer.pow(10, scale - left.scale)
    r = right.digits * Integer.pow(10, scale - right.scale)

    cond do
      left.neg? == right.neg? -> %__MODULE__{neg?: left.neg?, digits: l + r, scale: scale}
      l >= r -> %__MODULE__{neg?: left.neg?, digits: l - r, scale: scale}
      true -> %__MODULE__{neg?: right.neg?, digits: r - l, scale: scale}
    end
  end
end
