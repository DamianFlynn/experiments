metadata name = 'Example Pattern Module'
metadata description = 'Fixture entrypoint for Phase 3c edge extraction.'

@description('Required. Name prefix.')
param name string

module storageAccount 'br/public:avm/res/storage/storage-account:0.9.0' = {
  name: 'storageDeployment'
  params: {
    name: '${name}stg'
  }
}

module keyVault 'br/public:avm/res/key-vault/vault:0.6.1' = {
  name: 'kvDeployment'
  params: {
    name: '${name}kv'
  }
}

module shared '../../utl/types/avm-common-types/main.bicep' = {
  name: 'sharedDeployment'
}
