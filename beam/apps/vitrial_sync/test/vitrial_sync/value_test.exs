defmodule VitrialSync.ValueTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Value

  describe "normalized/1 -- _normalized, app/ownership.py:128-132" do
    test "trims, and a string that is empty once trimmed is absent" do
      assert Value.normalized("  item-1  ") == "item-1"
      assert Value.normalized("   ") == nil
      assert Value.normalized("") == nil
    end

    test "a non-string is absent, whatever it is" do
      for value <- [nil, 1, 0, true, false, %{}, []] do
        assert Value.normalized(value) == nil, "normalized(#{inspect(value)})"
      end
    end
  end

  describe "int?/1 -- isinstance(x, int) and not isinstance(x, bool)" do
    test "an integer passes and a boolean does not, because in Python bool is int" do
      assert Value.int?(0)
      assert Value.int?(7)
      refute Value.int?(true)
      refute Value.int?(false)
      refute Value.int?(nil)
      refute Value.int?("1")
      refute Value.int?(1.0)
    end
  end

  describe "py_eq?/2 -- Python == on decoded JSON" do
    test "numbers compare by value across int and float" do
      assert Value.py_eq?(1, 1.0)
      assert Value.py_eq?(0, 0.0)
      assert Value.py_eq?(-3, -3.0)
      refute Value.py_eq?(1, 2)
    end

    test "a boolean equals 1 and 0, because Python's bool subclasses int" do
      # Confirmed against CPython: True == 1 -> True, True != 1 -> False.
      assert Value.py_eq?(true, 1)
      assert Value.py_eq?(false, 0)
      assert Value.py_eq?(1, true)
      refute Value.py_ne?(1, true)
    end

    test "a boolean does not equal a non-numeric value" do
      refute Value.py_eq?(true, "true")
      refute Value.py_eq?(false, nil)
    end

    test "nil equals nil and nothing else" do
      assert Value.py_eq?(nil, nil)
      refute Value.py_ne?(nil, nil)
      refute Value.py_eq?(nil, false)
    end

    test "strings compare by content" do
      assert Value.py_eq?("open", "open")
      refute Value.py_eq?("open", "Open")
    end

    test "arrays and objects compare structurally, with the numeric rule inside" do
      # Confirmed against CPython: [{'a': 1}] == [{'a': 1.0}] -> True.
      assert Value.py_eq?([1, 2], [1.0, 2.0])
      assert Value.py_eq?([1, 2], [1, 2])
      refute Value.py_eq?([1, 2], [2, 1])
      refute Value.py_eq?([1, 2], [1, 2, 3])

      assert Value.py_eq?(%{"a" => 1, "b" => 2}, %{"b" => 2, "a" => 1.0})
      refute Value.py_eq?(%{"a" => 1}, %{"a" => 1, "b" => 2})
      refute Value.py_eq?(%{"a" => 1}, %{"a" => 2})
    end

    test "a value of a different shape is never equal" do
      refute Value.py_eq?(1, "1")
      refute Value.py_eq?([], %{})
      refute Value.py_eq?(%{a: 1}, %{"a" => 1})
    end
  end

  describe "required_string/2 -- _value/2, app/ownership.py:104-108" do
    test "returns the trimmed value, or nil when absent, blank or not a string" do
      assert Value.required_string(%{"projectID" => "  p1 "}, "projectID") == "p1"
      assert Value.required_string(%{}, "projectID") == nil
      assert Value.required_string(%{"projectID" => "  "}, "projectID") == nil
      assert Value.required_string(%{"projectID" => 1}, "projectID") == nil
    end
  end

  describe "event_list/2 -- _events, app/lifecycle.py:27-34" do
    test "an absent optional history is empty, and an absent required one is not" do
      assert Value.event_list(nil, false) == {:ok, []}
      assert Value.event_list(nil, true) == {:error, :required}
    end

    test "a non-list is always :required, even when optional" do
      assert Value.event_list("nope", false) == {:error, :required}
      assert Value.event_list(%{}, false) == {:error, :required}
      assert Value.event_list(1, true) == {:error, :required}
    end

    test "an empty list is :required only when the history is mandatory" do
      assert Value.event_list([], false) == {:ok, []}
      assert Value.event_list([], true) == {:error, :required}
    end

    test "a list holding a non-object is :malformed either way" do
      assert Value.event_list([%{"id" => "e1"}, "nope"], false) == {:error, :malformed}
      assert Value.event_list([%{"id" => "e1"}, "nope"], true) == {:error, :malformed}
    end

    test "a well-formed list passes through unchanged" do
      events = [%{"id" => "e1"}, %{"id" => "e2"}]
      assert Value.event_list(events, true) == {:ok, events}
    end
  end

  describe "member?/2 -- `value in {...}`" do
    test "membership uses Python equality, so a boolean never matches a status" do
      statuses = MapSet.new(["open", "resolved"])

      assert Value.member?("open", statuses)
      refute Value.member?("closed", statuses)
      refute Value.member?(true, statuses)
      refute Value.member?(nil, statuses)
      refute Value.member?(:open, statuses)
    end

    test "membership is not a type test: a value equal to a member is in the set" do
      revisions = MapSet.new([1, 2])

      assert Value.member?(1.0, revisions)
      assert Value.member?(true, revisions)
      refute Value.member?(2.5, revisions)
    end

    test "a bare list works as well as a MapSet" do
      assert Value.member?("ready", ["draft", "ready"])
      refute Value.member?("sent", ["draft", "ready"])
    end
  end

  describe "object?/1" do
    test "a plain map is an object and a struct is not" do
      assert Value.object?(%{"a" => 1})
      refute Value.object?([])
      refute Value.object?(nil)
      refute Value.object?("a")
      refute Value.object?(%{__struct__: Something})
    end
  end
end
