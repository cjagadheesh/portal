# Layer 3 of the shutdown design, deployed together with the rest of the control plane so
# `portal init` is the only setup step.
module "reaper" {
  source = "../reaper"

  region      = var.region
  name_prefix = var.name_prefix
  dry_run     = var.reaper_dry_run
}
