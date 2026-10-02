# Formatter configuration for the whole umbrella.
#
# `mix format` recurses into apps in an umbrella and uses each app's own
# `.formatter.exs` from there, so the per-app files cover `apps/*/{lib,test}`.
# What they cannot cover is the umbrella root's `config/`, because every app's
# `config_path` points there -- a file that belongs to all eight apps and to none
# of their input globs. That gap is why `config/config.exs` sat unformatted with
# no check reporting it.
#
# The `apps/*` globs are spelled out here as well, so a root invocation alone
# covers the entire tree. Redundant with the recursion, and the redundancy is the
# point: a file should not be able to fall outside every input pattern, which is
# how unformatted sources end up with nothing checking them.

[
  inputs: [
    "{mix,.formatter}.exs",
    "config/**/*.{ex,exs}",
    "apps/*/mix.exs",
    "apps/*/lib/**/*.{ex,exs}",
    "apps/*/test/**/*.{ex,exs}"
  ]
]
