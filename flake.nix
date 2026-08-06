{
  description = "hakubun-plus -- open multi-site list manager for Unix-like systems";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);

      # Read the version straight out of pyproject.toml so releases only ever
      # have to be bumped in one place.
      version =
        let
          m = builtins.match ''.*[\n]?version = "([^"]+)".*''
            (builtins.readFile ./pyproject.toml);
        in
        if m == null then "0.0.0" else builtins.head m;
    in
    {
      # Consumers get both hakubun-plus and its otherwise-unpackaged dependency
      # anitomy-ng, the latter injected into every Python package set so
      # `python3Packages.anitomy-ng` and `python3.withPackages` both resolve.
      overlays.default = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (pyfinal: _pyprev: {
            anitomy-ng = pyfinal.callPackage ./nix/anitomy-ng.nix { };
          })
        ];

        hakubun-plus = final.callPackage ./nix/hakubun-plus.nix {
          inherit version;
          src = self;
        };
      };

      packages = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system}.extend self.overlays.default;
        in {
          default = pkgs.hakubun-plus;
          inherit (pkgs) hakubun-plus;
          anitomy-ng = pkgs.python3Packages.anitomy-ng;
        });

      # `nix develop` -- the uv workflow, unchanged. uv still manages the
      # virtualenv and uv.lock here; Nix only supplies the interpreter and the
      # native libraries the GTK/Qt bindings link against, which is the part uv
      # cannot install for you.
      devShells = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          default = pkgs.mkShell {
            packages = with pkgs; [
              uv
              python3
              ruff
              cargo
              rustc
              pkg-config
              gobject-introspection
              cairo
              gtk3
              
              gdk-pixbuf
              librsvg
              qt6.qtbase
            ];

            shellHook = ''
              # Wheels built or installed by uv are not patched for Nix, so they
              # look for their libraries on the ordinary loader path.
              export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath (with pkgs; [
                stdenv.cc.cc gtk4 libadwaita gdk-pixbuf librsvg cairo
                glib qt6.qtbase
              ])}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
              export UV_PYTHON="${pkgs.python3}/bin/python3"
              echo "hakubun-plus dev shell -- uv $(uv --version 2>/dev/null | cut -d' ' -f2)"
            '';
          };
        });
    };
}
