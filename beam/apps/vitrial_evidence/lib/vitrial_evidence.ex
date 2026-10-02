defmodule VitrialEvidence do
  @moduledoc """
  Evidence capture, verification and retention.

  Evidence is a digest, not a file. Upload streams, the digest is computed while
  it streams, and promotion to canonical happens only after the digest checks out
  -- a client that claims a digest does not get to assert it.

  Deletion is the interesting half. A tombstone does not unlink anything; it
  marks the evidence, and the collector rechecks canonical and reference state
  before the physical unlink. Evidence referenced by a live record outlives the
  tombstone that announced its own expiry, because the tombstone is a client's
  opinion and the reference is the server's.
  """
end
