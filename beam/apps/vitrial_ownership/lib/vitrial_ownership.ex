defmodule VitrialOwnership do
  @moduledoc """
  Canonical ownership and capability resolution.

  Two questions this app answers, and nothing else:

    * may this principal perform this capability at all?
    * whose data does this record belong to?

  Roles are informational. Explicit capabilities are authority. A role that
  implies a capability the principal does not hold confers nothing, because the
  mapping from one to the other is a product decision that changes and a stored
  role is a snapshot that does not.

  Ownership resolves from server state, never from the client. A record naming
  its own parent is a claim to be checked, not a fact to be trusted -- accepting
  it would let any principal attach a record to any other principal's tree by
  putting the right identifier in the right field.
  """
end
