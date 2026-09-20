{ config, lib, pkgs, ... }:

let
  cfg = config.programs.jj-pile;
in
{
  options.programs.jj-pile = {
    enable = lib.mkEnableOption "jj-pile";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.jj-pile;
      defaultText = lib.literalExpression "pkgs.jj-pile";
      description = "The jj-pile package to install.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    programs.jujutsu = {
      enable = true;
      settings.aliases.pile = {
        definition = [ "util" "exec" "--" "jj-pile" ];
        doc = "Publish individual changes as pull requests";
      };
    };
  };
}
