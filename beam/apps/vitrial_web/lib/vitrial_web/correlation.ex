defmodule VitrialWeb.Correlation do
  @moduledoc """
  Request correlation and identity refs (port of the identity half of
  app/observability.py:68-134; the log-shape half lives in
  `VitrialWeb.LogFormat`).

  Where the reference keeps per-request values in contextvars, a cowboy
  connection process serves its requests one at a time on the SAME process, so
  the lifecycle maps onto process logger metadata: `begin/1` binds a fresh
  metadata set (the contextvar set()), `bind/1` fills the device/app labels,
  and `end_request/0` clears everything -- which is what stops one keep-alive
  request's identity leaking into the next one's log lines.

  UUIDv4 is `:crypto.strong_rand_bytes/1` with version and variant bits set,
  per DEPENDENCY-DOCTRINE.md §1.1 -- no hex UUID package.
  """

  @release_pattern ~r/\A[A-Za-z0-9._+\-]+\z/

  import Bitwise

  # app/observability.py:76-85. Python's uuid.UUID accepts canonical, hyphenless,
  # braced and urn: forms, case-insensitive, and str() re-emits the normalized
  # lowercase-hyphenated shape; anything else gets a fresh uuid4.
  @doc """
  Accept exactly what `str(uuid.UUID(value))` accepts and re-emit the
  normalized form; garbage in, fresh uuid4 out.
  """
  def request_id_for_header(value) do
    case parse_uuid(value) do
      nil -> uuid4()
      parsed -> parsed
    end
  end

  # CPython 3.12 uuid.UUID.__init__, hex branch:
  #   hex.replace('urn:', '').replace('uuid:', '')   -- case-sensitive, ALL
  #   hex.strip('{}').replace('-', '')               -- braces at the ends only
  #   len == 32 and int(hex, 16)
  #
  # Deliberate residual divergence, recorded: CPython's int(hex, 16) also
  # accepts surrounding whitespace, one leading sign, a 0x prefix and digit
  # underscores; a request id carrying those gets a FRESH uuid here instead of
  # normalization. Whitespace never reaches this function through a real
  # header (the transport strips outer OWS), and the other three are not forms
  # a client sends by accident.
  defp parse_uuid(binary) when is_binary(binary) do
    cleaned =
      binary
      |> String.replace("urn:", "")
      |> String.replace("uuid:", "")
      |> strip_braces()
      |> strip_braces_tail()
      |> String.replace("-", "")

    if byte_size(cleaned) == 32 and String.match?(cleaned, ~r/^[0-9a-fA-F]{32}$/) do
      <<a::binary-size(8), b::binary-size(4), c::binary-size(4), d::binary-size(4),
        e::binary-size(12)>> = String.downcase(cleaned)

      "#{a}-#{b}-#{c}-#{d}-#{e}"
    else
      nil
    end
  end

  defp parse_uuid(_), do: nil

  # CPython's strip("{}"): either brace character, from either end, repeatedly.
  # (Elixir's String.trim/2 does NOT treat a multi-char argument as a set.)
  defp strip_braces(bin) do
    case bin do
      <<c::utf8, rest::binary>> when c == ?{ or c == ?} -> strip_braces(rest)
      _ -> bin
    end
  end

  defp strip_braces_tail(bin) do
    case String.last(bin) do
      "{" -> strip_braces_tail(binary_part(bin, 0, byte_size(bin) - 1))
      "}" -> strip_braces_tail(binary_part(bin, 0, byte_size(bin) - 1))
      _ -> bin
    end
  end

  @doc "Fresh RFC 4122 uuid4 from the CSPRNG (doctrine §1.1's six-liner)."
  def uuid4 do
    # 8-4-4-4-12 hex = 32+16+16+16+48 bits = the full 128. The third group
    # carries the version nibble, the fourth the variant -- u2/u3 here.
    <<u0::32, u1::16, u2::16, u3::16, u4::48>> = :crypto.strong_rand_bytes(16)

    # Version nibble -> 0100; variant bits -> 10xx, pinned to the literal 'a'
    # nibble like the doctrine's format string.
    u2 = 0x4000 ||| (u2 &&& 0x0FFF)
    u3 = 0xA000 ||| (u3 &&& 0x3FFF)

    :io_lib.format("~8.16.0b-~4.16.0b-~4.16.0b-~4.16.0b-~12.16.0b", [u0, u1, u2, u3, u4])
    |> IO.iodata_to_binary()
    |> String.downcase()
  end

  @doc """
  Stable, non-reversible correlation reference (app/observability.py:68-73):
  sha256, first 16 hex characters, prefixed so logs read as refs rather than
  identities.
  """
  def correlation_ref(value)

  def correlation_ref(value) when is_binary(value) and value != "" do
    <<digest::binary-size(8), _rest::binary>> = :crypto.hash(:sha256, value)
    "sha256:" <> Base.encode16(digest, case: :lower)
  end

  def correlation_ref(_), do: nil

  @doc """
  Operational release labels only, never arbitrary free-form log fields
  (app/observability.py:88-97): stripped, at most 64 bytes, `[A-Za-z0-9._+-]`.
  """
  def release_value(value)

  def release_value(value) when is_binary(value) do
    normalized = String.trim(value)

    if normalized != "" and byte_size(normalized) <= 64 and
         String.match?(normalized, @release_pattern) do
      normalized
    else
      nil
    end
  end

  def release_value(_), do: nil

  @doc "Bind the request's identity metadata (app/observability.py:100-110)."
  def begin(request_id) do
    :logger.set_process_metadata(%{
      request_id: request_id,
      organization_ref: nil,
      actor_ref: nil,
      membership_ref: nil,
      session_ref: nil,
      device_ref: nil,
      app_version: nil,
      app_build: nil
    })
  end

  @doc """
  Device/app-version/app-build labels, one-way where they identify
  (app/observability.py:124-132).
  """
  def bind(opts) do
    device_ref =
      case opts[:device_id] do
        id when is_binary(id) ->
          trimmed = String.trim(id)
          if trimmed != "", do: correlation_ref(trimmed), else: nil

        _ ->
          nil
      end

    :logger.update_process_metadata(%{
      device_ref: device_ref,
      app_version: release_value(opts[:app_version]),
      app_build: release_value(opts[:app_build])
    })
  end

  @doc """
  Clear the request's metadata (app/observability.py:113-121). Load-bearing on
  a keep-alive connection, where the next request reuses this process.
  """
  def end_request do
    :logger.set_process_metadata(%{})
  end

  @doc "durationMs, rounded like main.py:89,100 (`round(x * 1000, 2)`)."
  def elapsed_ms(since_micro) do
    Float.round((System.monotonic_time(:microsecond) - since_micro) / 1_000, 2)
  end

  @doc "type(exc).__name__ for the errorType field (main.py:90)."
  def error_type(%struct_name{}), do: struct_name |> Module.split() |> List.last()

  def error_type({:exit, _}), do: "exit"

  def error_type({kind, _}) when is_atom(kind), do: Atom.to_string(kind)

  def error_type(other), do: inspect(other)
end
