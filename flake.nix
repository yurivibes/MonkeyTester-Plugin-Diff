{
  description = "MonkeyTester-Plugin-Diff: MonkeyTester test plugin for SNAPSHOT image diffs and ASSERT_LAYOUT checks";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ] (system:
      let
        pkgs = import nixpkgs { inherit system; };
        # Standard library only: no Python packages.
        python = pkgs.python3;
        src = pkgs.lib.cleanSource ./.;

        plugin = pkgs.writeShellApplication {
          name = "monkeytester-diff";
          runtimeInputs = [ python ];
          text = ''
            export PLUGIN_PORT="''${PLUGIN_PORT:-8095}"
            exec python3 ${src}/diff_plugin.py "$@"
          '';
        };

        tests = pkgs.writeShellApplication {
          name = "monkeytester-diff-tests";
          runtimeInputs = [ python ];
          text = ''
            cd ${src}
            exec python3 -m unittest discover -s tests -v "$@"
          '';
        };
      in {
        packages.default = plugin;
        packages.monkeytester-diff = plugin;

        # nix run .#         -> DIFF plugin on 127.0.0.1:8095
        # nix run .#tests    -> unit tests
        apps.default = { type = "app"; program = "${plugin}/bin/monkeytester-diff"; };
        apps.tests = { type = "app"; program = "${tests}/bin/monkeytester-diff-tests"; };

        devShells.default = pkgs.mkShell { packages = [ python ]; };
      });
}
