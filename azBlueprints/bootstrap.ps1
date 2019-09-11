###
### Helper Functions for Blueprint Management REST API
###

function get-aztoken {
  $azContext = Get-AzContext
  $azProfile = [Microsoft.Azure.Commands.Common.Authentication.Abstractions.AzureRmProfileProvider]::Instance.Profile
  $profileClient = New-Object -TypeName Microsoft.Azure.Commands.ResourceManager.Common.RMProfileClient -ArgumentList ($azProfile)
  $token = $profileClient.AcquireAccessToken($azContext.Subscription.TenantId)
  $authHeader = @{
    'Content-Type'='application/json'
    'Authorization'='Bearer ' + $token.AccessToken
  }
  
  return $authHeader
}

function New-Blueprint {
  param(
    [string]$file,
    [string]$name,
    [string]$managementGroup
  )
  
  $body = Get-Content -path $file 
  $restUri = "https://management.azure.com/providers/Microsoft.Management/managementGroups/$managementGroup/providers/Microsoft.Blueprint/blueprints/"+$name+"?api-version=2018-11-01-preview"
  $response = Invoke-RestMethod -Uri $restUri -Method Put -Headers $authHeader -Body $body
  return $response
}


function Add-BlueprintArtifact {
  param(
    [string]$file,
    [string]$blueprint,
    [string]$name,
    [string]$managementGroup
  )

  $body = Get-Content -path $file 
  $restUri = "https://management.azure.com/providers/Microsoft.Management/managementGroups/$managementGroup/providers/Microsoft.Blueprint/blueprints/$blueprint/artifacts/"+$name+"?api-version=2018-11-01-preview"
  $response = Invoke-RestMethod -Uri $restUri -Method PUT -Headers $authHeader -Body $body
  return $response
}

function Publish-Blueprint {
  param(
    [string]$blueprint,
    [string]$version,
    [string]$managementGroup
  )

  $restUri= "https://management.azure.com/providers/Microsoft.Management/managementGroups/$managementGroup/providers/Microsoft.Blueprint/blueprints/$blueprint/versions/"+$version+"?api-version=2018-11-01-preview"
  $response = Invoke-RestMethod -Uri $restUri -Method PUT -Headers $authHeader 
  return $response
}


function Assign-Blueprint {
  param(
    [string]$subscriptionID,
    [string]$assignmentName,
    [string]$parametersFile
  )

  $body = Get-Content -Path $parametersFile

  $restUri = "https://management.azure.com/subscriptions/$subscriptionID/providers/Microsoft.Blueprint/blueprintAssignments/"+$assignmentName+"?api-version=2018-11-01-preview"
  $response = Invoke-RestMethod -Uri $restUri -Method PUT -Headers $authHeader -Body $body
  return $response
}

###
### POC Blueprint Deployment and Assignment
###

$authHeader = get-aztoken

$blueprintName = "mySpokeBlueprint"
$managementGroup = "vdc"
$subscriptionId = "5384458d-aaaa-bbbb-2222-499b8c749420"

# Start by creating a blueprint at the ManagementGroup
$blueprintObject = New-Blueprint -file .\blu.arm.mySpoke.s1.json -name $blueprintName -managementGroup $managementGroup


# Add a Teamplate to the Blueprint
$artifactTemplate2 = Add-BlueprintArtifact -file .\artifact.template.res.arm.storage.json -blueprint $blueprintName -name templateStorage -managementGroup $managementGroup


# Publish the Blueprint with a Version
$publishedBlueprint = publish-blueprint -blueprint $blueprintName -version 0.1.5 -managementGroup $managementGroup

# Assing the Blueprint
$assignedBlueprint = Assign-Blueprint -subscriptionID  -assignmentName $blueprintName -parametersFile .\poc\blu.arm.mySpoke.s1.parameters.json


