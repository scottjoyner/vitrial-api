import Config

# Development. Verbose, single process, no persistence assumptions.
config :logger, level: :debug
config :vitrial_web, VitrialWeb.Endpoint, http: [port: 8080]
