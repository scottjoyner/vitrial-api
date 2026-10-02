import Config

# Test. Quiet logs so ExUnit output stays readable -- a failing test that also
# prints structured events is two failures to read instead of one.
config :logger, level: :warning
config :vitrial_web, VitrialWeb.Endpoint, http: [port: 0]
