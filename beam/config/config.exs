import Config

# Umbrella configuration.
#
# Every app in `apps/` points `config_path` here, so this file is the single place
# runtime configuration is set for the whole BEAM estate. It was previously absent
# while seven of the eight apps declared a `config_path` pointing at it -- a
# dangling reference, and no way to configure anything.

# Structured logging is the observability contract the Python service already
# keeps; the BEAM estate matches it rather than inventing a second one. `json` is
# not the default here because the Python service emits key/value events with its
# own field names, and a formatter that reshapes keys would make the two estates
# incomparable in a shared log stream.
config :logger, :default_formatter,
  format: "$time $metadata[$level] $message\n",
  metadata: [:request_id, :principal_id, :organization_id, :duration_us]

# One logger per application, so a crash names the app that died rather than the
# umbrella.
config :logger, :default_handler_config, level: :info

# Cowboy is the HTTP edge for vitrial_web. Bound at the listener rather than in
# the request path so an oversized or slow client is refused before the body is
# buffered, mirroring the per-path admission limits app/request_size.py applies
# on the Python side.
#
# `http:` is read by VitrialWeb.Listener and passed straight to
# :cowboy.start_clear/3; `test.exs` sets port 0 so tests read the actually-bound
# port back with VitrialWeb.Listener.port/0.
#
# `request_body_max_bytes` is the LISTENER-level bound, not a per-path limit:
# the reference deliberately leaves unlisted paths unlimited
# (app/request_size.py:62-64), so this value is consumed as cowboy's
# max_skip_body_length -- how much of an unread request body the listener will
# drain to keep the connection alive before dropping it instead. It has no
# effect on which requests are admitted; that decision stays with the per-path
# table in VitrialWeb.RequestSize.
config :vitrial_web, VitrialWeb.Endpoint,
  http: [ip: {0, 0, 0, 0}, port: 8080],
  request_body_max_bytes: 4 * 1024 * 1024

# Wire value for GET /api/v1/version. app/settings.py:10 pins the default on the
# Python side; this is the same projection, served without touching the service
# layer.
config :vitrial_web, :service_version, "0.3.0"

import_config "#{config_env()}.exs"
