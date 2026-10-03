defmodule VitrialSync.Value do
  @moduledoc """
  Python's comparison rules, for decoded JSON.

  `_apply_push_once` and everything it calls compare payload values with `==` and
  `!=`. That is *not* Elixir's `==`:

      Python:  1 == 1.0  -> True       1 == True  -> True
      Elixir:  1 === 1.0 -> false      1 === true -> false

  Elixir's `==` already matches Python's numeric equality, so the interesting
  differences are bool-versus-number -- Python's `bool` subclasses `int` -- and
  structural comparison of objects and arrays, which Elixir's `==` also gets
  right. What is left is this module, and it is small on purpose: it exists so
  that the lifecycle, delivery and ownership comparisons are visibly *Python's*
  comparisons and not a reader's guess at them.

  S-106 ("immutability is only checked when the client supplies the field") and
  SUSPECT-9 (a JSON `true` satisfies `authorizationRevision != 1`) both fall out
  of `py_eq?/2`. A guard that used `===` instead would reject payloads the
  reference accepts, and the difference would only surface as a client that
  "sometimes" cannot save.

  One comparison is deliberately *not* Python's: the evidence-reference
  containment test, which in the reference is a PostgreSQL `jsonb @>` predicate.
  `jsonb` does not consider `1` and `1.0` equal, and neither does
  `VitrialSync.Canonical.evidence_referenced?/2`.
  """

  @doc """
  `_normalized/1`, `app/ownership.py:128-132` and its four copies elsewhere.

  Not a string, or a string that is empty once trimmed, is `nil`. Every guard in
  the reference opens with this test, and a guard that forgets it accepts a field
  the server treats as absent -- `"  "` as an item id is the case that bites.
  """
  @spec normalized(term()) :: String.t() | nil
  def normalized(value) when is_binary(value) do
    case String.trim(value) do
      "" -> nil
      trimmed -> trimmed
    end
  end

  def normalized(_value), do: nil

  @doc """
  Whether a decoded JSON value is a Python `int` that is not a `bool`.

  The reference writes this pair constantly -- `isinstance(revision, int) and not
  isinstance(revision, bool)` -- because in Python `True` is an `int`. A BEAM
  boolean is not an integer, so `is_integer/1` is the whole test, and using it
  alone is what makes a JSON `true` fail a revision check here exactly as it
  fails one there.
  """
  @spec int?(term()) :: boolean()
  def int?(value), do: is_integer(value)

  @doc "Python `==` on two decoded JSON values."
  @spec py_eq?(term(), term()) :: boolean()
  def py_eq?(left, right) do
    cond do
      numeric?(left) and numeric?(right) -> numeric(left) == numeric(right)
      is_binary(left) and is_binary(right) -> left == right
      is_nil(left) and is_nil(right) -> true
      is_list(left) and is_list(right) -> list_eq?(left, right)
      object?(left) and object?(right) -> object_eq?(left, right)
      true -> false
    end
  end

  @doc "Python `!=` on two decoded JSON values."
  @spec py_ne?(term(), term()) :: boolean()
  def py_ne?(left, right), do: not py_eq?(left, right)

  @doc """
  The reference's `_value/2` read of a required parent key,
  `app/ownership.py:104-108`.

  Returns the trimmed string, or `nil` when the key is missing, not a string, or
  blank. Callers turn `nil` into their own rejection message, because the
  reference puts a different one on each branch.
  """
  @spec required_string(map(), String.t()) :: String.t() | nil
  def required_string(payload, key) do
    case Map.get(payload, key) do
      value when is_binary(value) -> normalized(value)
      _other -> nil
    end
  end

  @doc "Whether a decoded JSON value is a JSON object."
  @spec object?(term()) :: boolean()
  def object?(value), do: is_map(value) and not is_struct(value)

  @doc """
  `value in {...}`, with Python equality.

  The reference writes this constantly -- `status not in _QUOTATION_STATUSES`,
  `kind not in _BLOCKER_EVENT_KINDS`, `revision not in PROCUREMENT_FROZEN_STATUSES`
  -- and it is not a type test. A JSON value that happens to compare equal to a
  member is *in* the set: Python's `True == 1`, so `py_eq?/2` is what decides.

  Taken as a `MapSet` because that is how the vocabularies are declared here; a
  caller passing a bare list works too.
  """
  @spec member?(term(), MapSet.t() | [term()]) :: boolean()
  def member?(value, set) when is_list(set) do
    Enum.any?(set, &py_eq?(&1, value))
  end

  def member?(value, set) when is_map(set) do
    member?(value, MapSet.to_list(set))
  end

  @doc """
  The shared shape test for a payload's event history.

  `_events(payload, name=..., required=...)`, `app/lifecycle.py:27-34`, with its
  two rejections kept apart because the reference's messages differ and the
  callers are easier to read when the two cases are named:

      raw = payload.get("events")
      if raw is None and not required: return []
      if not isinstance(raw, list) or (required and not raw): raise "... history is required"
      if not all(isinstance(event, dict) for event in raw): raise "... history is malformed"

  So a `null` history is `[]` when optional and `:required` when not, a non-list
  is always `:required`, an empty list is `:required` only when the history is
  mandatory, and a list holding a non-object is `:malformed` either way.
  """
  @spec event_list(term(), boolean()) ::
          {:ok, [map()]} | {:error, :required} | {:error, :malformed}
  def event_list(value, required) do
    cond do
      is_nil(value) and not required -> {:ok, []}
      not is_list(value) -> {:error, :required}
      required and value == [] -> {:error, :required}
      not Enum.all?(value, &object?/1) -> {:error, :malformed}
      true -> {:ok, value}
    end
  end

  defp numeric?(value), do: is_integer(value) or is_float(value) or is_boolean(value)
  defp numeric(true), do: 1
  defp numeric(false), do: 0
  defp numeric(value), do: value

  defp list_eq?([], []), do: true

  defp list_eq?([left | l], [right | r]), do: py_eq?(left, right) and list_eq?(l, r)

  defp list_eq?(_left, _right), do: false

  defp object_eq?(left, right) do
    map_size(left) == map_size(right) and
      Enum.all?(left, fn {key, value} ->
        case Map.fetch(right, key) do
          {:ok, other} -> py_eq?(value, other)
          :error -> false
        end
      end)
  end
end
