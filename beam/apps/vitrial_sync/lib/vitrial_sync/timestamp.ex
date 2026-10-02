defmodule VitrialSync.Timestamp do
  @moduledoc """
  Python-`isoformat()`-compatible timestamp rendering for the mutation fingerprint.

  The fingerprint hashes a timestamp's text form, and the Python service builds
  that text with `datetime.isoformat()`. That is not the same text Elixir's
  `DateTime.to_iso8601/1` produces, and the difference is exactly the kind that
  fails silently:

      DateTime.to_iso8601(~U[2026-01-01 00:00:00Z])  #=> "2026-01-01T00:00:00Z"
      datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()  #=> "2026-01-01T00:00:00+00:00"

  A record pushed to the Elixir estate and the same record pushed to the Python
  estate would fingerprint differently, so a legitimate replay across the two would
  be rejected as a `mutation_id_collision` -- the one failure mode the fingerprint
  exists to prevent. Rendering is therefore pinned here rather than delegated.

  The three `isoformat()` behaviours that matter:

    * the offset is always rendered as `+HH:MM`, never `Z`, including for UTC
    * microseconds are omitted entirely when zero, and always six digits when not
    * the value is truncated to second precision, matching `isoformat(timespec=
      "seconds")`, because a sub-second difference in `updatedAt` is not a
      meaningful difference between two submissions of the same mutation
  """

  @doc """
  Normalise an ISO-8601 timestamp to the exact text Python would hash.

  Accepts anything `:crypto`-free Elixir can parse as a `DateTime` -- which means
  `Z`, `+00:00` and numeric offsets all normalise to the same string, so a client
  that varies its offset style between attempts still fingerprints as one
  mutation.

  Raises `ArgumentError` on unparseable input. Timestamps arrive on records that
  have already been schema-validated, so a failure here is a contract break
  between the validating layer and this one, not a bad client request.
  """
  @spec normalize(String.t()) :: String.t()
  def normalize(timestamp) when is_binary(timestamp) do
    case DateTime.from_iso8601(timestamp) do
      {:ok, datetime, _offset} ->
        render(datetime)

      {:error, reason} ->
        raise ArgumentError,
              "not an ISO-8601 timestamp: #{inspect(timestamp)} (#{inspect(reason)})"
    end
  end

  defp render(datetime) do
    [
      iso_date(datetime),
      ?T,
      iso_time(datetime),
      offset(datetime)
    ]
    |> IO.iodata_to_binary()
  end

  defp iso_date(datetime) do
    "#{pad(datetime.year, 4)}-#{pad(datetime.month, 2)}-#{pad(datetime.day, 2)}"
  end

  defp iso_time(datetime) do
    base = "#{pad(datetime.hour, 2)}:#{pad(datetime.minute, 2)}:#{pad(datetime.second, 2)}"

    # The zero test is on the VALUE, never on the precision. A `DateTime` parsed
    # from "...T00:00:00.000000Z" carries `{0, 6}` -- value zero, precision six --
    # so matching on the second element treats an explicitly-written zero
    # fraction as a non-zero one and emits `.000000`, which isoformat() omits.
    # Two spellings of the same instant would then fingerprint differently.
    case datetime.microsecond do
      {0, _precision} ->
        base

      {value, _precision} ->
        base <> "." <> microseconds(value)
    end
  end

  # ISO-8601 permits 1 or more fractional digits; isoformat always writes 6.
  defp microseconds(value) do
    value |> Integer.to_string() |> String.pad_leading(6, "0") |> binary_part(0, 6)
  end

  defp offset(datetime) do
    # utc_offset, not the time-zone struct: isoformat() renders the offset the
    # value actually carried, which is the DST-adjusted one for an instant that
    # fell inside a transition.
    total_minutes = div(datetime.utc_offset, 60)

    sign = if total_minutes < 0, do: "-", else: "+"
    magnitude = abs(total_minutes)

    "#{sign}#{pad(div(magnitude, 60), 2)}:#{pad(rem(magnitude, 60), 2)}"
  end

  defp pad(value, width), do: value |> Integer.to_string() |> String.pad_leading(width, "0")
end