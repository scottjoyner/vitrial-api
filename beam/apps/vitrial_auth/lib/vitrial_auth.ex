defmodule VitrialAuth do
  @moduledoc """
  Device pairing and session authority.

  Access tokens are stored only as SHA-256 hashes. A database disclosure yields
  hashes, and a hash of a bearer token cannot be presented as a bearer token.

  Pairing is a code exchange; a session is the long-lived thing it produces. The
  rate limiter in front of both is deliberately in-process, which is correct for
  the single-replica deployment this service ships as and is wrong the moment
  there is a second replica -- `deploy/env.production.example` says so at the
  setting, because a ceiling that silently stops being a ceiling is worse than
  one that was never claimed.
  """
end
