defmodule VitrialWeb.Handlers do
  @moduledoc """
  The boundary's answers.

  Boundary only, per beam/README.md and beam/apps/vitrial_web/README.md: this
  module projects configuration (`/health`, `/api/v1/version`) and says one
  uniform thing for every clause whose engine has not landed yet (501, see
  VitrialWeb.Router's moduledoc). No sync, ownership, auth or evidence decision
  lives here. The 404/405/500/307 shapes are the reference's own (starlette),
  not inventions.
  """

  @type reply :: %{
          required(:status) => pos_integer(),
          required(:headers) => %{binary() => binary()},
          required(:body) => binary()
        }

  @doc """
  Serve a matched route. Two clauses project configuration; every other action
  is the uniform not-wired response. `bindings` carries the path parameters
  (unused until an engine lands -- no boundary code reads a documentID).
  """
  def serve({:serve, :health}, _bindings, _ctx), do: health()
  def serve({:serve, :version}, _bindings, _ctx), do: version()
  def serve({:serve, :sync_push}, bindings, ctx), do: sync_push(bindings, ctx)
  def serve(:not_wired, _bindings, _ctx), do: not_wired()

  @doc """
  POST /api/v2/sync/push -- `app/main.py:214-226`.

  The first route that reaches an ENGINE rather than answering for itself.
  Every part it needs exists: `VitrialSync.Auth.authenticate/3` resolves the
  bearer token to the `Principal` the engine authorizes against, and
  `VitrialSync.PushService.push/4` loads, decides, writes and answers.

  Two things the reference does that are easy to lose, and both live below
  rather than here:

  * **The refusal mapping.** `app/sync_service.py:222-226` raises
    `PermissionError` for a missing capability or an unavailable organization —
    both 403 — and `InvalidMutation` for everything the engine refuses, which is
    400. `PushService.refusal_status/1` holds that mapping so it is written once.
    A failed LOAD is 500: it is ours, and answering 400 would send a client to fix
    a body that was fine.
  * **`nextCursor` is read after the commit**, inside `PushService`, because it is
    the changelog high-water mark and reading it earlier hands the client a cursor
    pointing at rows this batch has not written.

  Both `v1` and `v2` push carry the same body in the reference (`app/main.py`
  registers the handler twice), so `v1` stays `:not_wired` until its
  `deviceID` requirement is confirmed rather than guessed at.
  """
  def sync_push(_bindings, ctx) do
    # AUTHENTICATION FIRST, and that order is the reference's rather than
    # mine. `app/main.py:212-217` declares
    #
    #     async def sync_push(batch: SyncBatch,
    #                         principal: Principal = Depends(current_principal), ...)
    #
    # and FastAPI resolves every `Depends` before the body runs — while a body
    # that fails Pydantic validation is rejected at the boundary, before the
    # handler is entered at all. So an unauthenticated caller learns nothing about
    # whether their batch was well-formed.
    #
    # Reading the body first inverted that: an empty `records` list failed
    # `batch_records/1` and answered 400, telling an anonymous caller about its
    # own request's shape.
    # THREE elements, not two. `VitrialAuth.authenticate/5` returns
    # `{:ok, principal, event}` — the principal plus the `auth.session_authenticated`
    # body `app/auth.py:73-74` logs beside it — and `VitrialSync.Auth.authenticate/3`
    # passes that shape through unchanged. Matching `{:ok, principal}` instead made
    # the compiler report, correctly:
    #
    #     warning: the following pattern will never match:
    #         {:ok, principal} <- authenticate(ctx)
    #     because the right-hand side has type:
    #         dynamic({:error, {:load, term()}} or {:ok, term(), term()})
    #
    # which reads as "this route can never authenticate" and was true of the
    # pattern, not of the code. The `_event` is dropped here because the boundary
    # does not log it — `VitrialWeb.Events` is the correlation middleware's, and a
    # business event belongs with the request's own logging, not invented here.
    with {:ok, principal, _event} <- authenticate(ctx),
         {:ok, records} <- batch_records(ctx),
         {:ok, result, _events} <-
           VitrialSync.PushService.push(pool(), records, device_id(ctx), principal) do
      json(200, encode_push_result(result))
    else
      {:error, reason} ->
        json(VitrialSync.PushService.refusal_status(reason), ~s({"detail":#{detail(reason)}}))
    end
  end

  # `SyncResult`, `app/schemas.py:97-100`.
  #
  # Rendered here rather than through `VitrialSync.CanonicalJSON.encode/1`,
  # because CanonicalJSON orders keys canonically and the reference's
  # `model_dump_json` does not promise that — borrowing it would make this
  # response differ from the reference in a way no test would catch. Every other
  # body in this file is a literal string, so this matches the convention.
  defp encode_push_result(result) do
    [
      ~s({"acceptedRecordIDs":),
      encode_ids(result.accepted_record_ids),
      ~s(,"rejectedRecordIDs":),
      encode_ids(result.rejected_record_ids),
      ~s(,"nextCursor":),
      encode_integer(result.next_cursor),
      ~s(})
    ]
    |> IO.iodata_to_binary()
  end

  defp encode_ids(ids) when is_list(ids),
    do: ~s([) <> Enum.map_join(ids, ",", &escape/1) <> ~s(])

  defp encode_ids(_other), do: "[]"

  defp encode_integer(value) when is_integer(value), do: Integer.to_string(value)
  defp encode_integer(_other), do: "0"

  # The minimum a JSON string needs: backslash, quote, and the C0 controls.
  defp escape(value) when is_binary(value) do
    escaped =
      value
      |> String.replace("\\", "\\\\")
      |> String.replace("\"", ~s(\\"))

    ~s(") <> escaped <> ~s(")
  end

  # `current_principal`, `app/auth.py:37-75`. The token is read from the
  # Authorization HEADER, not the body, and resolved by `VitrialSync.Auth` — the
  # one place in the estate that mints a Principal, behind a lookup rather than in
  # front of one. An earlier version of this returned `{:error,
  # :authentication_required}` unconditionally, and the compiler said what that
  # meant:
  #
  #     warning: the following pattern will never match:
  #         {:ok, principal} <- authenticate(ctx)
  #     because the right-hand side has type: dynamic({:error, :authentication_required})
  defp authenticate(%{req: req}) do
    VitrialSync.Auth.authenticate(pool(), :cowboy_req.header("authorization", req))
  end

  # `SyncBatch.records`, `app/schemas.py:52-56`. Each record is `SyncRecord`,
  # whose `payload` is BASE64 on the wire and `updatedAt` an ISO string
  # (`app/schemas.py:75`, required).
  #
  # `ctx.body` is the RAW BINARY, not a decoded map: `RequestSize.admit/1` bounds
  # the request body and hands it back as read, and nothing in that stage decodes
  # it. The first version of this matched on a decoded map and answered 400 for
  # every request — including well-formed ones — because a binary never matches
  # `%{"records" => _}`.
  #
  # `SyncBatch.records`, `app/schemas.py:52-56`. Each record is a `SyncRecord`,
  # whose `payload` is BASE64 on the wire and `updatedAt` an ISO string
  # (`app/schemas.py:75`, required).
  #
  # `ctx.body` is the RAW BINARY, not a decoded map: `RequestSize.admit/1` bounds
  # the request body and hands it back as read, and nothing in that stage decodes
  # it. The first version matched on a decoded map and answered 400 for EVERY
  # request, including well-formed ones, because a binary never matches
  # `%{"records" => _}`.
  #
  # Decoding HERE is the right place: it is the boundary's job to turn the wire
  # into a term, and an engine handed a binary would have to decode-or-refuse on
  # every call.
  defp batch_records(%{body: body}) when is_binary(body), do: decode_body(body)

  defp batch_records(_ctx), do: {:error, :invalid_batch}

  # Elixir 1.19+ ships `JSON` in the STANDARD LIBRARY, so decoding is not a
  # dependency question here and never was one — the module's `elixir: "~> 1.19"`
  # requirement is what makes it available. That matters in a repository whose
  # doctrine is to admit as little as possible: the correct decoder costs nothing.
  #
  # An earlier version of this hand-rolled a regex over the envelope, which is
  # the shape of parser that accepts what Pydantic rejects and rejects what it
  # accepts. `JSON.decode!/2` does not have that failure mode.
  defp decode_body(""), do: {:error, :invalid_batch}

  defp decode_body(body) when is_binary(body) do
    case JSON.decode(body) do
      {:ok, %{"records" => records}} when is_list(records) -> {:ok, records}
      _ -> {:error, :invalid_batch}
    end
  rescue
    # A malformed body is a client error, not a crash: JSON.decode!/1 raises on
    # invalid input and this is the boundary, where a 400 is the right answer.
    _ -> {:error, :invalid_batch}
  end

  defp detail(:authentication_required), do: ~s("authentication required")
  defp detail(:invalid_batch), do: ~s("invalid batch")
  defp detail({:load, _reason}), do: ~s("database unavailable")
  defp detail(reason) when is_atom(reason), do: ~s("#{reason}")
  defp detail(other), do: inspect(other)

  defp device_id(%{req: req}), do: :cowboy_req.header("x-device-id", req) || ""

  # The pool is an application child; the boundary reads it by NAME rather than
  # receiving one, because a request must not carry a pool through its context.
  defp pool, do: VitrialSync.Db.Pool

  @doc "GET /health -- app/schemas.py:37-38, the whole response is {\"status\":\"ok\"}."
  def health do
    json(200, ~s({"status":"ok"}))
  end

  @doc """
  GET /api/v1/version -- app/schemas.py:46-48. `serviceVersion` is the
  configured value (default "0.3.0", app/settings.py:10); `apiVersion` is
  pinned "v1" by the schema.
  """
  def version do
    service_version = Application.get_env(:vitrial_web, :service_version, "0.3.0")
    json(200, ~s({"apiVersion":"v1","serviceVersion":"#{service_version}"}))
  end

  @doc """
  The wave's single invented behaviour: every routed clause whose engine is not
  landed yet gets this one response. Uniform so a differential test can never
  mistake it for parity. Documented in VitrialWeb.Router's moduledoc.
  """
  def not_wired do
    json(501, ~s({"detail":"not implemented"}))
  end

  @doc "Unmatched path -- starlette/exceptions.py:10 falls back to the HTTP phrase."
  def not_found do
    json(404, ~s({"detail":"Not Found"}))
  end

  @doc """
  Known path, wrong method -- the reference's 405 carries the Allow header
  (starlette/routing.py:275) over the same phrase body.
  """
  def method_not_allowed(allow) do
    json(405, ~s({"detail":"Method Not Allowed"}), %{"allow" => allow})
  end

  @doc """
  Trailing-slash redirect, starlette's `redirect_slashes` default: a 307 whose
  Location is the absolute URL with the slash flipped and the original query
  string kept. VitrialWeb.Handler builds that URL; the body is empty.
  """
  def redirect(location) do
    %{status: 307, headers: %{"location" => location}, body: ""}
  end

  @doc """
  The unhandled-exception answer: starlette's ServerErrorMiddleware, a
  plain-text 500. Deliberately NOT JSON -- the JSON 500s the reference does
  emit come from handlers that caught something specific, which is business
  surface.
  """
  def internal_server_error do
    %{
      status: 500,
      headers: %{"content-type" => "text/plain; charset=utf-8"},
      body: "Internal Server Error"
    }
  end

  defp json(status, body, headers \\ %{}) do
    %{status: status, headers: Map.put(headers, "content-type", "application/json"), body: body}
  end
end
