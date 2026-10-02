defmodule VitrialEvidence.Application do
  @moduledoc """
  Supervision root for vitrial evidence.

  The child list is empty, and the evidence pipeline is a stream, not a process.

  multipart uploads are handled by the HTTP boundary, and the collector is a
  scheduled job. The first supervised child this app gets is the pool that owns
  S3 connections, and it arrives with the pool.
  """

  use Application

  @impl true
  def start(_type, _args) do
    Supervisor.start_link([], strategy: :one_for_one, name: VitrialEvidence.Supervisor)
  end
end
