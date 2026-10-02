import Config

# Production. start_permanent is set in each app's mix.exs, so a crash in any app
# takes the release down rather than looping silently. Compose restarts the
# container; that is the intended recovery path, not supervision inside the VM.
config :logger, level: :info
config :vitrial_web, VitrialWeb.Endpoint, http: [port: 8080]
