{
  description = "git-pile scripts";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      overlays.default = final: prev: {
        inherit (self.packages.${final.stdenv.hostPlatform.system}) jj-pile;
      };

      homeManagerModules.default = { ... }: {
        imports = [ ./nix/home-manager.nix ];
        nixpkgs.overlays = [ self.overlays.default ];
      };

      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          runtimeInputs = with pkgs; [
            fzy
            gh
            git
            python3
          ];
          # These disposable repositories do not need crash-durable writes.
          checkPython =
            pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux
              "${pkgs.libeatmydata}/bin/eatmydata "
            + "python3";
        in
        {
          git-pile = pkgs.stdenvNoCC.mkDerivation {
            pname = "git-pile";
            version = "0.0.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            nativeCheckInputs = [ pkgs.python3 pkgs.git ];
            doCheck = true;

            installPhase = ''
              runHook preInstall
              mkdir -p "$out/bin"
              cp bin/git-* bin/_git_pile.py "$out/bin/"
              chmod +x "$out/bin/"git-*
              for script in "$out/bin/"git-*; do
                wrapProgram "$script" --prefix PATH : ${pkgs.lib.makeBinPath runtimeInputs}
              done
              runHook postInstall
            '';

            checkPhase = ''
              runHook preCheck
              patchShebangs bin
              ${checkPython} -m unittest discover -s tests -p test_commands.py -v
              runHook postCheck
            '';

            meta = with pkgs.lib; {
              description = "Stacked-diff workflow scripts for git and GitHub";
              homepage = "https://github.com/keith/git-pile";
              license = licenses.mit;
              platforms = platforms.all;
            };
          };

          jj-pile = pkgs.stdenvNoCC.mkDerivation {
            pname = "jj-pile";
            version = "0.0.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            nativeCheckInputs = [ pkgs.python3 pkgs.git pkgs.jujutsu ];
            doCheck = true;

            installPhase = ''
              runHook preInstall
              mkdir -p "$out/bin"
              cp bin/jj-pile "$out/bin/"
              chmod +x "$out/bin/jj-pile"
              wrapProgram "$out/bin/jj-pile" --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.python3 pkgs.git pkgs.gh pkgs.jujutsu ]}
              runHook postInstall
            '';

            checkPhase = ''
              runHook preCheck
              patchShebangs bin
              ${checkPython} -m unittest discover -s tests -p test_jj_pile.py -v
              runHook postCheck
            '';

            meta = with pkgs.lib; {
              description = "Change-based pull requests for Jujutsu and GitHub";
              homepage = "https://github.com/keith/git-pile";
              license = licenses.mit;
              platforms = platforms.all;
              mainProgram = "jj-pile";
            };
          };

          default = self.packages.${system}.git-pile;
        });
    };
}
