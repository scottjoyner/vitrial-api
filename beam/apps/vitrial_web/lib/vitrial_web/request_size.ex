defmodule VitrialWeb.RequestSize do
  @moduledoc """
  Whole-request byte limits, enforced at the header and during the read
  (port of app/request_size.py).

  Two enforcement points, because one is not enough:

    * an oversized `Content-Length` is refused before a single body byte is
      read (app/request_size.py:186-188);
    * otherwise the body is read HERE, counted, and stopped one byte past the
      limit -- a dishonest or absent `Content-Length` is caught regardless
      (:190-194, :202-224). On overrun the buffered prefix is discarded, so
      nobody can make the service hold an unbounded body in order to reject it.

  In ASGI this stage wraps `receive` and replays the buffered body downstream.
  A cowboy handler owns body reads, so the buffering lives here and the body
  travels on the request context (`:body`); the pipeline decision that makes
  this work is in VitrialWeb.Handler's moduledoc.

  ## Limits

  Per-path, longest matching prefix wins (:113-120); unlisted paths are
  deliberately UNLIMITED (:62-64) -- refusing a legitimate request on a path
  nobody reasoned about is a worse outcome than accepting an oversized one.
  Each prefix is overridable through the environment, with the same generated
  variable names (:100-110).

  The 413 wire shape is the reference's (:226-240): fixed JSON body,
  `cache-control: no-store`, and `x-vitrial-max-body-bytes` naming the limit
  that was exceeded. Like every pre-correlation refusal it carries no
  X-Request-ID -- in the reference the size guard sits OUTSIDE the correlation
  middleware (main.py:401), and the same order is sequenced in
  VitrialWeb.Handler.
  """

  @mib 1024 * 1024

  # (path prefix, default limit). Longest matching prefix wins.
  # app/request_size.py:77-82, verbatim.
  @default_path_limits [
    {"/api/v1/auth/pair", 16 * 1024},
    {"/api/v1/sync/push", 4 * @mib},
    {"/api/v2/sync/push", 4 * @mib},
    {"/internal/admin/v1", 64 * 1024}
  ]

  @doc "app/request_size.py:77-82, for tests and the limit table's readers."
  def default_path_limits, do: @default_path_limits

  @doc """
  Effective limits, honouring per-prefix environment overrides
  (VITRIAL_MAX_BODY_BYTES_<PREFIX>, prefix mapped like app/request_size.py:100-101).
  """
  def limits do
    Enum.map(@default_path_limits, fn {prefix, default} ->
      override = System.get_env("VITRIAL_MAX_BODY_BYTES_#{env_suffix(prefix)}")
      {prefix, positive_int(override, default)}
    end)
  end

  @doc "Longest matching prefix wins; `nil` means this path is not size-limited."
  def limit_for_path(path), do: limit_for_path(path, limits())

  @doc "The matcher over an explicit table, so the longest-prefix rule is testable."
  def limit_for_path(path, limits) do
    limits
    |> Enum.reduce({nil, -1}, fn {prefix, limit}, acc = {_best, best_length} ->
      if String.starts_with?(path, prefix) and byte_size(prefix) > best_length do
        {limit, byte_size(prefix)}
      else
        acc
      end
    end)
    |> elem(0)
  end

  @doc """
  Parse `content-length`, treating anything unusable as unknown (:123-137).
  Unknown is deliberately NOT zero: coercing a malformed header to 0 would mark
  the request compliant and hand the decision entirely to the stream check.
  """
  def declared_length(headers) do
    case headers["content-length"] do
      nil ->
        nil

      value ->
        case Integer.parse(value) do
          {parsed, ""} -> if parsed >= 0, do: parsed, else: nil
          _ -> nil
        end
    end
  end

  @doc """
  The admission stage. Returns `{:ok, ctx}` with the buffered body on the
  context, or `{:reject, reply}` -- the 413 the reference sends (:226-240).
  """
  def admit(ctx) do
    limit = limit_for_path(ctx.path)

    cond do
      is_nil(limit) ->
        {:ok, ctx}

      ctx.method == "GET" ->
        # No body to bound. Avoids reading a request that has none (:181-184).
        {:ok, ctx}

      true ->
        headers = :cowboy_req.headers(ctx.req)
        declared = declared_length(headers)

        if declared != nil and declared > limit do
          {:reject, reject(limit)}
        else
          case read_capped(ctx.req, limit) do
            {:over, _req} -> {:reject, reject(limit)}
            {:ok, body, req} -> {:ok, %{ctx | req: req, body: body}}
          end
        end
    end
  end

  defp reject(limit) do
    %{
      status: 413,
      headers: %{
        "content-type" => "application/json",
        "cache-control" => "no-store",
        "x-vitrial-max-body-bytes" => Integer.to_string(limit)
      },
      body: ~s({"detail":"request body too large"})
    }
  end

  # Buffer the body, stopping one byte past `limit`. On overrun the buffered
  # prefix is discarded (:202-224). `length: limit + 1` makes cowboy stop
  # reading exactly one byte past the ceiling, so an oversized body is never
  # fully buffered.
  defp read_capped(req, limit), do: read_capped(req, limit, 0, [])

  defp read_capped(req, limit, total, acc) do
    case :cowboy_req.read_body(req, %{length: limit + 1, period: 5000}) do
      {:ok, data, req} ->
        if total + byte_size(data) > limit do
          {:over, req}
        else
          {:ok, IO.iodata_to_binary([acc, data]), req}
        end

      {:more, data, req} ->
        total = total + byte_size(data)

        if total > limit do
          {:over, req}
        else
          read_capped(req, limit, total, [acc | data])
        end
    end
  end

  # app/request_size.py:100-101: non-alphanumeric -> "_", strip, uppercase.
  defp env_suffix(prefix) do
    prefix
    |> String.graphemes()
    |> Enum.map(fn ch ->
      if ch =~ ~r/[a-zA-Z0-9]/, do: ch, else: "_"
    end)
    |> Enum.join()
    |> String.trim("_")
    |> String.upcase()
  end

  defp positive_int(nil, fallback), do: fallback

  defp positive_int(raw, fallback) do
    case Integer.parse(raw) do
      {value, ""} -> if value > 0, do: value, else: fallback
      _ -> fallback
    end
  end
end
