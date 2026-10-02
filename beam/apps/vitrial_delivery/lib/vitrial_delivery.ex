defmodule VitrialDelivery do
  @moduledoc """
  Delivery as a set of state machines, not a status column.

  Procurement, materials, installation, execution, production and closeout are
  six transitions with six different preconditions, and they share entities.
  Modelling them as one enum means every rule about "can this move yet" becomes a
  conditional over which enum it is, which is where the rules rot.

  Each machine is a pure transition: state plus event in, state or refusal out.
  Illegal transitions are refused by construction rather than by a guard that
  someone has to remember to call, because a guard that is not called is a bug
  that no test in this module can see.
  """
end
