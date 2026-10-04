defmodule VitrialWeb.LogFormat do
  @moduledoc """
  The event wire shape (port of app/observability.py:142-185).

  The reference's JsonFormatter emits NDJSON with CAMELCASE keys via
  json.dumps(sort_keys=True): timestamp, level, event, requestID,
  organizationRef, actorRef, membershipRef, sessionRef, deviceRef, appVersion,
  appBuild, plus the event's own fields (routeTemplate, durationMs,
  statusCode, retryAfterSeconds, errorType, method). Logger metadata keys are
  NOT wire names -- `:request_id` becomes `requestID`, `:device_ref` becomes
  `deviceRef` -- and an emitter that forwards raw metadata keys silently breaks
  the log-shape parity this module exists to keep. The mapping is explicit
  here, in `event_map/4`.

  Encoding is OTP `:json` (no new JSON dependency, doctrine §4.1). Two
  deviations from json.dumps are deliberate and noted at the point of
  divergence: key ORDER is not sorted (OTP's map encoder is order-free; every
  consumer parses JSON, and the reference's sort_keys is a formatting choice,
  not a field), and Elixir's `nil` is walked to Erlang's `null` atom before
  encoding.

  Redaction (app/observability.py:14-28, :142-149) travels with the wire shape
  because that is where it is enforced: event FIELDS are redacted before
  merging, sensitive keys collapse to "<redacted>".
  """

  # app/observability.py:14-28, verbatim, compacted (non-alphanumerics
  # removed, lowercased) exactly as _sensitive_key compares them.
  @sensitive_keys MapSet.new([
                    "authorization",
                    "proxyauthorization",
                    "bearer",
                    "cookie",
                    "setcookie",
                    "credential",
                    "credentials",
                    "password",
                    "secret",
                    "apikey",
                    "accesstoken",
                    "refreshtoken",
                    "xvitrialadminkey"
                  ])

  @doc "app/observability.py:142-149."
  def sensitive_key?(key) when is_binary(key) do
    compact = key |> String.downcase() |> String.replace(~r/[^a-z0-9]/, "")

    MapSet.member?(@sensitive_keys, compact) or
      String.ends_with?(compact, "token") or
      String.ends_with?(compact, "password") or
      String.ends_with?(compact, "secret")
  end

  @doc "app/observability.py:152-161, applied to the event fields."
  def redact(value, key \\ nil)

  # Containers recurse; the incoming key belongs to the PARENT and is not
  # consulted again, exactly like the reference re-keys by str(k).
  def redact(value, _key) when is_map(value) do
    Map.new(value, fn {k, v} -> {to_string(k), redact(v, to_string(k))} end)
  end

  def redact(value, _key) when is_list(value), do: Enum.map(value, &redact(&1, nil))

  def redact(value, key) do
    if key != nil and sensitive_key?(key), do: "<redacted>", else: value
  end

  @doc """
  The full wire payload for one event: the fixed top-level keys with their
  values from process metadata, then the (redacted) event fields merged over
  them -- app/observability.py:168-182's payload + payload.update(fields).
  """
  def event_map(event, level, fields, metadata) do
    metadata = metadata || %{}

    base = %{
      "timestamp" => timestamp(),
      "level" => level_name(level),
      "event" => event,
      "requestID" => metadata[:request_id],
      "organizationRef" => metadata[:organization_ref],
      "actorRef" => metadata[:actor_ref],
      "membershipRef" => metadata[:membership_ref],
      "sessionRef" => metadata[:session_ref],
      "deviceRef" => metadata[:device_ref],
      "appVersion" => metadata[:app_version],
      "appBuild" => metadata[:app_build]
    }

    Map.merge(base, redact(fields || %{}))
  end

  @doc "NDJSON line via OTP :json. `nil` walks to Erlang `null` first."
  def encode(map) do
    map
    |> nils_to_null()
    |> :json.encode()
    |> IO.iodata_to_binary()
  end

  @doc """
  The same timestamp shape Python's `datetime.now(timezone.utc).isoformat()`
  emits: microsecond precision with an explicit +00:00 offset (DateTime's
  default rendering ends in "Z").
  """
  def timestamp do
    String.replace_suffix(DateTime.to_iso8601(DateTime.utc_now()), "Z", "+00:00")
  end

  defp level_name(level) when is_atom(level), do: Atom.to_string(level)

  defp nils_to_null(%{__struct__: _} = value), do: value

  defp nils_to_null(map) when is_map(map),
    do: Map.new(map, fn {k, v} -> {k, nils_to_null(v)} end)

  defp nils_to_null(list) when is_list(list), do: Enum.map(list, &nils_to_null/1)

  defp nils_to_null(nil), do: :null

  defp nils_to_null(value), do: value
end
