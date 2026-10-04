defmodule VitrialSync.Db.UrlTest do
  use ExUnit.Case, async: true

  alias VitrialSync.Db.Url

  describe "parse/1" do
    test "parses the reference default, stripping the +asyncpg driver segment" do
      # The Python default carries the driver tag (app/settings.py:8); the
      # parser must not leak it into the scheme the transport layer sees.
      assert {:ok, url} = Url.parse("postgresql+asyncpg://vitrial:vitrial@localhost:5432/vitrial")

      assert url == %Url{
               scheme: "postgresql",
               username: "vitrial",
               password: "vitrial",
               host: "localhost",
               port: 5432,
               database: "vitrial"
             }
    end

    test "accepts the plain postgresql scheme and the postgres alias" do
      assert {:ok, %{scheme: "postgresql"}} = Url.parse("postgresql://u:p@db.example:5433/other")
      assert {:ok, %{scheme: "postgres"}} = Url.parse("postgres://u:p@db.example:5433/other")

      assert {:ok, %{scheme: "postgres", port: 5433}} =
               Url.parse("postgres://u:p@db.example:5433/other")
    end

    test "defaults the port to 5432 when absent" do
      assert {:ok, %{port: 5432}} = Url.parse("postgresql://u:p@db.example/vitrial")
    end

    test "accepts a bare username without a password" do
      assert {:ok, %{username: "solo", password: ""}} = Url.parse("postgresql://solo@h/vitrial")
    end

    test "refuses other schemes by name" do
      assert {:error, message} = Url.parse("mysql://u:p@h/vitrial")
      assert message =~ "mysql"
      assert message =~ "postgresql"
    end

    test "refuses a URL with no scheme" do
      assert {:error, message} = Url.parse("vitrial:vitrial@localhost/vitrial")
      assert message =~ "no scheme"
    end

    test "refuses a URL with no host" do
      assert {:error, message} = Url.parse("postgresql:///vitrial")
      assert message =~ "no host"
    end

    test "refuses a URL with no database name in the path" do
      assert {:error, message} = Url.parse("postgresql://vitrial:vitrial@localhost")
      assert message =~ "no database name"
    end

    test "fails loud on sslmode, by name, because dropping it silently changes the connection's posture" do
      assert {:error, message} = Url.parse("postgresql://u:p@h/vitrial?sslmode=require")
      assert message =~ "sslmode"
      assert message =~ "require"
      assert message =~ "silently dropped"
    end

    test "fails loud on any other query parameter, naming them" do
      assert {:error, message} = Url.parse("postgresql://u:p@h/vitrial?application_name=w5")
      assert message =~ "application_name"
      assert message =~ "refused"
    end

    test "refuses a non-string" do
      assert {:error, message} = Url.parse(42)
      assert message =~ "must be a string"
    end
  end
end
