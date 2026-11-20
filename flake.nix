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
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          runtimeInputs = with pkgs; [
            fzy
            gh
            glab
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
              cp bin/git-* bin/_git_pile*.py "$out/bin/"
              chmod +x "$out/bin/"git-*
              for script in "$out/bin/"git-*; do
                wrapProgram "$script" --prefix PATH : ${pkgs.lib.makeBinPath runtimeInputs}
              done
              runHook postInstall
            '';

            checkPhase = ''
              runHook preCheck
              patchShebangs bin
              testJobs=''${NIX_BUILD_CORES:-1}
              if [ "$testJobs" -eq 0 ] || [ "$testJobs" -gt 4 ]; then
                testJobs=4
              fi
              ${checkRunner} -n "$testJobs" tests
              runHook postCheck
            '';

            meta = with pkgs.lib; {
              description = "Stacked-diff workflow scripts for git and GitHub";
              homepage = "https://github.com/keith/git-pile";
              license = licenses.mit;
              platforms = platforms.all;
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
              python3
              python3Packages.pytest
              python3Packages.pytest-xdist
            ] ++ pkgs.lib.optional pkgs.stdenv.hostPlatform.isLinux pkgs.libeatmydata;
          };
        });
    };
}
