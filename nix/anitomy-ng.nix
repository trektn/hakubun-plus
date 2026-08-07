# Python bindings for the anitomy-ng anime filename parser.
#
# Lives here rather than in the consuming config because hakubun-plus declares
# it as a real runtime dependency (`anitomy-ng>=1.0.9,<2`) and nixpkgs does not
# carry it -- so anything that wants to build hakubun-plus needs it, and this
# flake should stand on its own. It is re-exported through the overlay, so a
# consumer gets `python3Packages.anitomy-ng` for free.
#
# The binding is a Rust extension built with maturin, living in the
# `anitomy-py/` subdirectory of a Cargo workspace whose Cargo.lock is at the
# repository root -- hence `sourceRoot` plus a lockFile path that reaches back
# out of it, and `cargoRoot` pointing at the workspace.
{ lib
, buildPythonPackage
, fetchFromGitHub
, rustPlatform
, cargo
, rustc
}:

buildPythonPackage rec {
  pname = "anitomy-ng";
  version = "1.0.9";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "tylergibbs2";
    repo = "anitomy-ng";
    rev = "v${version}";
    hash = "sha256-VxmxrVrLIUXJwVCrV5IXc2mD8TCTuQQwHVZFdHXwupM=";
  };

  sourceRoot = "${src.name}/anitomy-py";

  cargoDeps = rustPlatform.importCargoLock {
    lockFile = "${src}/Cargo.lock";
  };
  cargoRoot = "..";

  build-system = [
    rustPlatform.cargoSetupHook
    rustPlatform.maturinBuildHook
    cargo
    rustc
  ];

  # maturin writes its generated pyo3 config under the *workspace* target dir
  # -- ../target/maturin, since the crate builds from anitomy-py/ while the
  # workspace root is one level up -- and does not create it, so the build dies
  # with "failed to create file ... No such file or directory". Creating it by
  # hand is not an option either: the unpacked source root is read-only by then
  # ("mkdir: cannot create directory '../target': Permission denied").
  # Redirecting cargo's output to a writable scratch dir solves both.
  preBuild = ''
    export CARGO_TARGET_DIR="$NIX_BUILD_TOP/cargo-target"
    mkdir -p "$CARGO_TARGET_DIR/maturin"
  '';

  pythonImportsCheck = [ "anitomy_ng" ];

  meta = {
    description = "Anime video filename parser (Python bindings for the anitomy-ng Rust crate)";
    homepage = "https://github.com/tylergibbs2/anitomy-ng";
    license = lib.licenses.mpl20;
    platforms = lib.platforms.linux;
  };
}
