resource "terraform_data" "rg" {
  input = "rg-example"
}

module "vnet" {
  source = "./modules/vnet"
  rg_id  = terraform_data.rg.id
}

module "pe" {
  source  = "./modules/pe"
  vnet_id = module.vnet.id
}

# Declared, but nothing in the root consumes its outputs: still a direct dependency.
module "naming" {
  source  = "Azure/naming/azurerm"
  version = "0.4.0"
}
