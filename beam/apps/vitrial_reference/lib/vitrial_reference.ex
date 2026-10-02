defmodule VitrialReference do
  @moduledoc """
  Reference data and its published manifests.

  Reference data is data the whole system agrees on: type vocabularies, policy
  tables, anything a manifest has to name. It is versioned and published rather
  than fetched live, so that a client and a server can disagree loudly and
  detectably instead of one silently rendering what the other cannot see.

  Reads are pure. A manifest GET does not write; it did, once, and the fix
  mattered more than the code it touched -- a GET that mutates is a GET that
  cannot be cached, retried, prefetched or reasoned about.
  """
end
