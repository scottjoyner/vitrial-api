defmodule VitrialSync.DecimalTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Decimal

  describe "parse/1 -- Decimal(str(value)), app/delivery_execution.py:73-79" do
    test "an integer keeps its scale of zero" do
      assert Decimal.parse(12) == {:ok, %Decimal{neg?: false, digits: 12, scale: 0}}
      assert Decimal.parse(-3) == {:ok, %Decimal{neg?: true, digits: 3, scale: 0}}
      assert Decimal.parse(0) == {:ok, Decimal.zero()}
    end

    test "a float goes through its shortest round-tripping decimal, not its binary value" do
      # CPython: Decimal(str(1.1)) is exactly 1.1. A BEAM float is the nearest
      # double, which is not, so parse/1 must go through the rendering.
      assert Decimal.parse(1.1) == {:ok, %Decimal{neg?: false, digits: 11, scale: 1}}
      assert Decimal.parse(100.0) == {:ok, %Decimal{neg?: false, digits: 1000, scale: 1}}
    end

    test "a numeric string parses exactly as a number would" do
      assert Decimal.parse("1.50") == {:ok, %Decimal{neg?: false, digits: 150, scale: 2}}
      assert Decimal.parse("-0.5") == {:ok, %Decimal{neg?: true, digits: 5, scale: 1}}
      assert Decimal.parse("+7") == {:ok, %Decimal{neg?: false, digits: 7, scale: 0}}
    end

    test "leading zeros, a trailing point and an exponent all parse" do
      assert Decimal.parse("007") == {:ok, %Decimal{neg?: false, digits: 7, scale: 0}}
      assert Decimal.parse("5.") == {:ok, %Decimal{neg?: false, digits: 5, scale: 0}}
      assert Decimal.parse(".5") == {:ok, %Decimal{neg?: false, digits: 5, scale: 1}}
      assert Decimal.parse("1e3") == {:ok, %Decimal{neg?: false, digits: 1000, scale: 0}}
      assert Decimal.parse("1E+3") == {:ok, %Decimal{neg?: false, digits: 1000, scale: 0}}
    end

    test "a boolean and nil are refused before str() is ever reached" do
      # Decimal(str(True)) would raise InvalidOperation, and the reference checks
      # the bool case before calling str anyway.
      assert Decimal.parse(true) == :error
      assert Decimal.parse(false) == :error
      assert Decimal.parse(nil) == :error
    end

    test "garbage, containers and the special literals are refused" do
      for value <- ["", " ", "abc", "1.2.3", "1e", "--1", "NaN", "Infinity", [], %{}, [1]] do
        assert Decimal.parse(value) == :error, "parse(#{inspect(value)})"
      end
    end
  end

  describe "add/2 -- the quotation total accumulator" do
    test "summing two tenths gives three tenths, not a float artefact" do
      # CPython: Decimal('0.1') + Decimal('0.2') == Decimal('0.3'), while
      # 0.1 + 0.2 == 0.30000000000000004 in binary floating point. This is the
      # whole reason `quotedTotal` cannot be compared as a float.
      {:ok, tenth} = Decimal.parse(0.1)
      {:ok, fifth} = Decimal.parse(0.2)
      total = Decimal.add(tenth, fifth)

      assert Decimal.compare(total, Decimal.parse(0.3) |> then(fn {:ok, d} -> d end)) == :eq
    end

    test "scale is realigned, so 1.50 + 0.50 is 2.00 and equals 2" do
      {:ok, a} = Decimal.parse("1.50")
      {:ok, b} = Decimal.parse("0.50")
      {:ok, two} = Decimal.parse(2)

      assert Decimal.add(a, b) == %Decimal{neg?: false, digits: 200, scale: 2}
      assert Decimal.compare(Decimal.add(a, b), two) == :eq
    end

    test "adding across zero signs takes the larger magnitude with its own sign" do
      {:ok, five} = Decimal.parse(5)
      {:ok, minus_two} = Decimal.parse(-2)

      # 5 - 2 is 3 and -2 + 5 is 3: the larger magnitude takes its own sign.
      assert Decimal.add(five, minus_two) == %Decimal{neg?: false, digits: 3, scale: 0}
      assert Decimal.add(minus_two, five) == %Decimal{neg?: false, digits: 3, scale: 0}
    end

    test "adding two negatives keeps the negative sign" do
      {:ok, a} = Decimal.parse("-1.5")
      {:ok, b} = Decimal.parse("-2.25")

      assert Decimal.add(a, b) == %Decimal{neg?: true, digits: 375, scale: 2}
    end
  end

  describe "compare/2 and the predicates built on it" do
    test "1.0 equals 1, which is CPython's Decimal equality" do
      # Confirmed against CPython: Decimal(str(1.0)) == Decimal('1') -> True.
      {:ok, one_point_zero} = Decimal.parse(1.0)
      {:ok, one} = Decimal.parse(1)

      assert Decimal.compare(one_point_zero, one) == :eq
      refute Decimal.not_eq?(one_point_zero, one)
    end

    test "ordering is exact regardless of scale" do
      {:ok, small} = Decimal.parse("0.9")
      {:ok, large} = Decimal.parse("1.0")

      assert Decimal.compare(small, large) == :lt
      assert Decimal.compare(large, small) == :gt
    end

    test "negative numbers order the other way round" do
      {:ok, minus_one} = Decimal.parse(-1)
      {:ok, one} = Decimal.parse(1)
      {:ok, minus_two} = Decimal.parse(-2)

      assert Decimal.compare(minus_one, one) == :lt
      assert Decimal.compare(minus_two, minus_one) == :lt
      assert Decimal.compare(minus_one, minus_one) == :eq
    end

    test "not_positive?/1 and positive?/1 are the two halves of `value <= 0`" do
      {:ok, zero} = Decimal.parse(0)
      {:ok, negative} = Decimal.parse(-0.01)
      {:ok, positive} = Decimal.parse(0.01)

      assert Decimal.not_positive?(zero)
      assert Decimal.not_positive?(negative)
      refute Decimal.not_positive?(positive)

      assert Decimal.positive?(positive)
      refute Decimal.positive?(zero)
      refute Decimal.positive?(negative)
    end
  end
end
