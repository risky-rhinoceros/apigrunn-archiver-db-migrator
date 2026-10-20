{
  description = "apigrunn-archiver-db-migrator — upgrade apigrunn-archiver 0.1.0 archives to 0.2.0";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # No system Python is assumed: this shell provides the interpreter and uv.
      #   nix develop -c uv run pytest
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python313 pkgs.uv pkgs.ruff ];

          # uv must build the venv against this interpreter rather than
          # downloading its own, which would not find the Nix-provided libs.
          env.UV_PYTHON = "${pkgs.python313}/bin/python3.13";
          env.UV_PYTHON_DOWNLOADS = "never";

          shellHook = ''
            echo "apigrunn-archiver-db-migrator dev shell — $(python3 --version), uv $(uv --version | cut -d' ' -f2)" >&2
          '';
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
