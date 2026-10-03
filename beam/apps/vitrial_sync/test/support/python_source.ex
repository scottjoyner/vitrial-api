defmodule VitrialSync.PythonSource do
  @moduledoc """
  Just enough Python source reading to check the reference has not drifted.

  `VitrialSync.DriftTest` asserts that constructs the BEAM port depends on are
  still present in `app/sync_service.py` and `app/rejection_codes.py`. Those
  assertions are only meaningful against CODE: a construct that survives only as
  a comment, or only as a line of the prose explaining it, must not satisfy
  them. So comments and docstrings are removed first, and this module is that
  removal.

  It is a hand-written scanner rather than a `String.replace` because the naive
  version is wrong in a way that matters here: removing `#` to end-of-line would
  also cut a `#` that is inside a string literal, and every assertion about a
  string-valued construct would then be asserting against text the reference does
  not contain.

  `strip/1` is public so `DriftTest` can exercise it against synthetic sources --
  including the cases the reference itself does not happen to contain, which is
  most of them.
  """

  @doc """
  Remove comments and docstrings; keep string literals exactly as written.

  A comment collapses to nothing at the start of a line and to a newline
  elsewhere, so the statements around an end-of-line comment do not get joined
  onto one line. A docstring leaves a newline behind for the same reason: the
  definition below it has to stay findable by `function_body/2`.
  """
  @spec strip(String.t()) :: String.t()
  def strip(source) do
    {acc, _previous} = source |> String.to_charlist() |> scan(:code, {[], nil})
    acc |> Enum.reverse() |> to_string()
  end

  # -- the scanner -----------------------------------------------------------
  #
  # The accumulator is `{reversed_output, last_emitted_char}`. The last emitted
  # character is carried rather than recovered with `List.last/1`, because that
  # is O(n) on a list built by prepending and turns a linear scan into a
  # quadratic one -- on a 588-line module that is a test timeout, not a slow test.

  defp scan([], _mode, state), do: state
  defp scan([?\n | rest], :comment, {acc, nil}), do: scan(rest, :comment, {acc, nil})
  defp scan([?\n | rest], :comment, {acc, _}), do: scan(rest, :code, {[?\n | acc], ?\n})

  defp scan([?# | _] = chars, :code, state), do: scan(drop_line(chars), :comment, state)
  # A docstring opens on three identical delimiters. Matching `???` plus three
  # delimiters instead would never fire -- there is no `?` in `"""` -- and the
  # whole document would go through as if it were code.
  defp scan([delimiter, delimiter, delimiter | rest], :code, {acc, previous})
       when delimiter in [?", ?'],
       do: skip_triple(rest, delimiter, {acc, previous})

  defp scan([?\\, char | rest], mode, {acc, _}), do: scan(rest, mode, {[char, ?\\ | acc], char})

  defp scan([?" | rest], :dq, {acc, _}), do: scan(rest, :code, {[?" | acc], ?"})
  defp scan([?' | rest], :sq, {acc, _}), do: scan(rest, :code, {[?' | acc], ?'})

  defp scan([?\n | rest], mode, {acc, _}), do: scan(rest, mode, {[?\n | acc], ?\n})

  # The opening quote is emitted AND the mode changes in the same step. Handing
  # the quote back to the scanner in string mode instead would let the
  # closing-quote clause match it immediately, closing the string on the
  # character that opened it -- and then `a = "# not a comment"` would have its
  # hash treated as a comment.
  defp scan([?" | rest], :code, {acc, _}), do: scan(rest, :dq, {[?" | acc], ?"})

  # An apostrophe only opens a string when it does not continue a word, which is
  # what tells `'item_audit_event'` from the `'` in a word in prose.
  defp scan([?' | rest], :code, {acc, previous}) do
    mode = if word_char?(previous), do: :code, else: :sq
    scan(rest, mode, {[?' | acc], ?'})
  end

  defp scan([char | rest], mode, {acc, _}), do: scan(rest, mode, {[char | acc], char})

  defp skip_triple([], _delimiter, state), do: state

  defp skip_triple([delimiter, delimiter, delimiter | rest], delimiter, {acc, previous}),
    do: scan(rest, :code, {[?\n | acc], previous})

  defp skip_triple([??, delimiter | rest], delimiter, state),
    do: skip_triple(rest, delimiter, state)

  defp skip_triple([_ | rest], delimiter, state), do: skip_triple(rest, delimiter, state)

  defp drop_line([?\n | _] = rest), do: rest
  defp drop_line([_ | rest]), do: drop_line(rest)
  defp drop_line([]), do: []

  defp word_char?(char) when char in ?a..?z, do: true
  defp word_char?(char) when char in ?A..?Z, do: true
  defp word_char?(char) when char in ?0..?9, do: true
  defp word_char?(?_), do: true
  defp word_char?(_), do: false

  @doc """
  Read a reference module, stripped and whitespace-collapsed.

  Space runs collapse to one so a multi-line expression in the reference can be
  written as one line of expected text by a caller, and indentation is removed so
  a top-level definition can be found by prefix. Without this the drift guard
  would be sensitive to the reference's indentation choices, which is not a
  thing a drift guard should be.
  """
  @spec read(String.t()) :: String.t()
  def read(path) do
    {:ok, source} = File.read(path)

    source
    |> strip()
    |> String.replace(~r/[ \t]*\n[ \t]*/, "\n")
    |> String.replace(~r/[ \t]+/, " ")
  end

  @doc """
  The body of one top-level `def`/`async def`, up to the next top-level
  definition.

  Expects `read/1` output. Blank lines are dropped first so a `def foo` mentioned
  inside a docstring cannot terminate the region early.
  """
  @spec function_body(String.t(), String.t()) :: String.t()
  def function_body(source, header) do
    lines =
      source
      |> String.split("\n")
      |> Enum.reject(&(String.trim(&1) == ""))

    start =
      Enum.find_index(lines, &String.starts_with?(&1, header)) ||
        raise "no top-level #{inspect(header)} in the reference any more"

    {body, _rest} =
      lines
      |> Enum.drop(start + 1)
      |> Enum.split_while(
        &(not String.starts_with?(&1, "def ") and
            not String.starts_with?(&1, "async def ") and
            not String.starts_with?(&1, "class "))
      )

    Enum.join(body, "\n")
  end

  @doc """
  Every double-quoted member of a `Literal[...]` alias.

  Reading the reference's own `RejectionCode` list rather than retyping it is
  what makes `DriftTest`'s vocabulary comparison mean something: the comparison
  is between the two declarations, not between one declaration and a copy.
  """
  @spec literal_members(String.t(), String.t()) :: [String.t()]
  def literal_members(source, header) do
    # `parts: 2`, because without it `String.split/2` returns EVERY line after
    # the header as a separate element and the tail is a list, not the binary
    # the `split_while` below is written against.
    case String.split(source, header, parts: 2) do
      [_before, rest] ->
        {members, _after} =
          rest |> String.split("\n") |> Enum.split_while(&(not String.starts_with?(&1, "]")))

        Regex.scan(~r/"([a-z_]+)"/, Enum.join(members, "\n"))
        |> Enum.map(fn [_all, token] -> token end)

      _ ->
        raise "the reference does not declare #{inspect(header)}"
    end
  end
end
