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
          testInputs = with pkgs; [
            python3
            python3Packages.pytest
            python3Packages.pytest-xdist
            git
          ];
          # These disposable repositories do not need crash-durable writes.
          checkRunner =
            pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux
              "${pkgs.libeatmydata}/bin/eatmydata "
            + "pytest";
          checkTests = testFile: ''
            runHook preCheck
            patchShebangs bin
            testJobs=''${NIX_BUILD_CORES:-1}
            if [ "$testJobs" -eq 0 ] || [ "$testJobs" -gt 4 ]; then
              testJobs=4
            fi
            ${checkRunner} -n "$testJobs" ${testFile}
            runHook postCheck
          '';
        in
        {
          git-pile = pkgs.stdenvNoCC.mkDerivation {
            pname = "git-pile";
            version = "0.0.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            nativeCheckInputs = testInputs;
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

            checkPhase = checkTests "tests/test_commands.py";

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
            nativeCheckInputs = testInputs ++ [ pkgs.jujutsu ];
            doCheck = true;

            installPhase = ''
              runHook preInstall
              mkdir -p "$out/bin"
              cp bin/jj-pile "$out/bin/"
              chmod +x "$out/bin/jj-pile"
              wrapProgram "$out/bin/jj-pile" --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.python3 pkgs.git pkgs.gh pkgs.jujutsu ]}
              runHook postInstall
            '';

            checkPhase = checkTests "tests/test_jj_pile.py";

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

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              git
              jujutsu
              python3
              python3Packages.pytest
              python3Packages.pytest-xdist
            ] ++ pkgs.lib.optional pkgs.stdenv.hostPlatform.isLinux pkgs.libeatmydata;
          };
        });
    };
}
