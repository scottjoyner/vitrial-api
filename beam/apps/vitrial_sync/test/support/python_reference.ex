defmodule VitrialSync.PythonReference do
  @moduledoc """
  Locating, running and decoding the Python parity oracle.

  The oracle is `parity_oracle.py`, next to this file. It AST-lifts the
  reference functions out of `app/`, executes them, and writes its answers as
  one JSON document. Nothing in this module decides anything; it is the plumbing
  that lets `VitrialSync.ParityTest` compare against the reference instead of
  against a reading of it.

  ## Three ways to be unavailable, and all three are silent-but-visible

  `unavailable_reason/0` returns a sentence or `nil`, and the caller is expected
  to *skip* on a sentence and run on `nil`. Every reason it can return is one a
  reader needs to see, because the alternative is a parity suite that quietly
  stops running:

    * no interpreter on `PATH` -- the container in `scripts/beam_gate.sh` has
      no `python3` at all
    * `app/` unreachable from the computed root -- that same container mounts
      `beam/` at `/w`, so the root resolves to `/` and there is no reference
      tree to compare against
    * an interpreter that cannot `import pydantic` -- checked with its own
      one-line probe rather than left to fall out of a decode failure, because a
      missing pydantic otherwise surfaces as a `{:error, :trailing_data}` that
      says nothing about why

  The skip has to be a *skip*, not a failure: `mix test` exits 0 on an
  all-skipped module, and the beam gate has no lane that can run the reference.
  That is a deliberate trade, and the companion drift guard in
  `drift_test.exs` exists because of it -- the drift guard needs only a file
  reader, so it runs whenever the tree is reachable, with or without Python.

  ## Why the document goes through a file

  `System.cmd(exe, args, stderr_to_stdout: true)` merges the interpreter's
  diagnostics into the stream being decoded, and a `DeprecationWarning` or a
  pydantic `UserWarning` on an otherwise successful run then reads as
  `{:error, :trailing_data}`. The oracle's failures are worse: they are
  uncaught `SystemExit` messages printed to stderr, which is exactly the text
  that merging would have destroyed.

  So the oracle writes its document to `--out <path>` and its diagnostics stay
  on stderr, where `load!/1` keeps them and puts them in the raise. A warning no
  longer breaks a good run, and a bad run arrives with its own explanation
  attached instead of a generic decode error.
  """

  @oracle Path.expand("parity_oracle.py", __DIR__)
  @repo_root Path.expand("../../../../..", __DIR__)
  @sync_service Path.join(@repo_root, "app/sync_service.py")
  @schemas Path.join(@repo_root, "app/schemas.py")

  @doc """
  The reference repository root: five levels up from `test/support`, which is
  `server-api/` -- the directory that holds `app/`.

  Not derived from `File.cwd!/0`. `mix test` runs from the umbrella root, from
  the app directory, and from `/w` inside the gate container, and only one of
  those is the repository root.
  """
  @spec repo_root() :: String.t()
  def repo_root, do: @repo_root

  @doc "The oracle script's own path."
  @spec oracle_path() :: String.t()
  def oracle_path, do: @oracle

  @doc """
  The Python interpreter, or `nil`.

  `python3` first because the reference is written and tested against it;
  `python` is a courtesy for distributions where only the unversioned name is on
  `PATH`, and it is checked by the pydantic probe below rather than trusted.
  """
  @spec interpreter() :: String.t() | nil
  def interpreter do
    System.find_executable("python3") || System.find_executable("python")
  end

  @doc """
  Why the differential suite cannot run, or `nil` when it can.

  A sentence rather than an atom because it is written verbatim into the test
  output, and "python reference tree app/ not reachable from /w" is the only
  form of that fact a reader in CI can act on.
  """
  @spec unavailable_reason() :: String.t() | nil
  def unavailable_reason do
    cond do
      is_nil(interpreter()) ->
        "no python3 on PATH"

      not reference_tree?() ->
        "python reference tree app/ not reachable from #{@repo_root}"

      not pydantic?() ->
        "the interpreter at #{interpreter()} cannot import pydantic"

      true ->
        nil
    end
  end

  @doc """
  Whether the Python reference tree is readable from here.

  Separate from `unavailable_reason/0` because the drift guard's precondition is
  exactly this and nothing else: it needs the reference *source* and no
  interpreter at all, so it must not be gated off by a missing `python3`.
  """
  @spec reference_tree?() :: boolean()
  def reference_tree?, do: File.regular?(@sync_service) and File.regular?(@schemas)

  @doc "The path to one reference module, for readers that need to open it."
  @spec reference_file(String.t()) :: String.t()
  def reference_file(name), do: Path.join([@repo_root, "app", name])

  @doc """
  Run the oracle once and return its document.

  Raises with the interpreter's own diagnostics attached if anything went wrong,
  because "the parity suite failed" without the traceback that explains it is
  the failure mode that costs a day.
  """
  @spec load!() :: map()
  def load! do
    out = out_path()

    try do
      {diagnostics, status} =
        System.cmd(interpreter(), [@oracle, "--repo", @repo_root, "--out", out],
          stderr_to_stdout: true
        )

      unless status == 0 do
        raise "parity oracle exited #{status}: #{diagnostics}"
      end

      decode!(File.read!(out), diagnostics)
    after
      File.rm(out)
    end
  end

  defp decode!(bytes, diagnostics) do
    case VitrialSync.JSON.decode(bytes) do
      {:ok, document} when is_map(document) ->
        document

      {:error, reason} ->
        raise "parity oracle emitted a document VitrialSync.JSON rejects: " <>
                inspect(reason) <> "\ninterpreter diagnostics: " <> diagnostics
    end
  end

  defp pydantic? do
    {_output, status} =
      System.cmd(interpreter(), ["-c", "import pydantic"], stderr_to_stdout: true)

    status == 0
  end

  defp out_path do
    Path.join(System.tmp_dir!(), "parity_oracle_#{System.unique_integer([:positive])}.json")
  end
end
