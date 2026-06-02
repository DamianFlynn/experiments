module "vnet" {
  source = "../../modules/vnet"
}

module "naming" {
  source  = "Azure/naming/azurerm"
  version = "0.4.0"
}

resource "azurerm_resource_group" "this" {
  name     = "example"
  location = "eastus"
}
