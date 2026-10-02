defmodule VitrialLifecycle do
  @moduledoc """
  Entity lifecycle states and the transitions between them.

  Separate from delivery because the two answer different questions: lifecycle
  governs whether an entity exists and what may be done to it, delivery governs
  the physical work. An entity can be in a legal lifecycle state and still be
  blocked by delivery policy, and collapsing the two makes that unexpressible.

  As with delivery, transitions are pure functions. There is deliberately no
  `valid_transitions/1` that a caller is expected to consult before calling
  `transition/3` -- that is a guard, guards get skipped, and a skipped guard on a
  lifecycle transition is a resurrected entity.
  """
end
