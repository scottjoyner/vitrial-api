defmodule VitrialWeb.Events do
  @moduledoc """
  The structured event emitter (port of app/observability.py:202-207's
  log_event).

  Every boundary event -- request.started / request.completed /
  request.failed / auth.rate_limit_refused / auth.rate_limit_bypassed -- goes
  through here, so the wire shape is owned by `VitrialWeb.LogFormat` and the
  request-id/refs come from the process metadata correlation binds. Nothing
  else in the app logs with raw metadata keys.

  The emitter hands Logger the finished NDJSON line, so the event fields on
  the shared log stream are exactly the reference's regardless of the
  umbrella's formatter config (beam/config/config.exs keeps the stock
  formatter on purpose -- see the comment there; switching the whole node's
  log stream to NDJSON is a deploy decision, not a wave-6 one). Level
  filtering is Logger's, which is why info-level request events are absent
  from test output and the warning-level refused event is capturable.
  """

  alias VitrialWeb.LogFormat

  require Logger

  @doc "log_event(event, level=INFO, **fields) over the reference's wire shape."
  def emit(event, level \\ :info, fields \\ %{}) do
    metadata =
      case :logger.get_process_metadata() do
        :undefined -> %{}
        map when is_map(map) -> map
      end

    line = LogFormat.event_map(event, level, fields, metadata) |> LogFormat.encode()

    Logger.log(level, line)
  end
end
