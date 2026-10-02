# Formatter configuration for this app.
#
# Globs here are resolved relative to THIS directory, because `mix format` recurses
# into an umbrella's apps and reads this file when it does. They deliberately stay
# app-relative -- an `apps/*/lib/**` pattern here would match nothing, since there
# is no `apps/` directory inside an app.
#
# The root .formatter.exs carries the tree-wide globs so a single root invocation
# also covers everything. `config/` is not listed here because each app's
# `config_path` points at the umbrella root's config/, which belongs to this file's
# inputs by way of the root's, not its own.

[
  inputs: ["{mix,.formatter}.exs", "{config,lib,test}/**/*.{ex,exs}"]
]
