<!-- post: story:Azure/bicep-registry-modules:train-pr-7463 channel: x day: 2026-10-05 -->
Published: avm/res/azure-stack-hci/virtual-machine-instance 0.3.0, the Azure Stack HCI Virtual Machine Instance module. Its changelog: "these changes were previously listed under 0.2.0, which was never published." https://github.com/Azure/bicep-registry-modules/tree/avm/res/azure-stack-hci/virtual-machine-instance/0.3.0
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-pr-7463 channel: linkedin day: 2026-10-05 -->
Version 0.3.0 of the Azure Stack HCI Virtual Machine Instance module (avm/res/azure-stack-hci/virtual-machine-instance) was published on 5 October 2026.

The module's changelog explains the version number: "Released as 0.3.0: these changes were previously listed under 0.2.0, which was never published." The main change it carries: "Updated the referenced avm/res/hybrid-compute/machine module from 0.4.1 to 0.6.0, which creates the Arc machine with API version 2026-07-15 instead of 2024-07-10."

https://github.com/Azure/bicep-registry-modules/tree/avm/res/azure-stack-hci/virtual-machine-instance/0.3.0

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-issue-7459 channel: x day: 2026-10-06 -->
Published: avm/res/devices/iot-hub 0.4.0, the IoT Hub module. "Serialized private endpoint deployments to prevent concurrent IoT Hub updates from failing with ETag conflicts." https://github.com/Azure/bicep-registry-modules/tree/avm/res/devices/iot-hub/0.4.0
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-issue-7459 channel: linkedin day: 2026-10-06 -->
Version 0.4.0 of the IoT Hub module (avm/res/devices/iot-hub) was published on 6 October 2026.

From its changelog: "Serialized private endpoint deployments to prevent concurrent IoT Hub updates from failing with ETag conflicts." The fix followed a failed-pipeline report and was contributed by Tomaz Mlakar from a fork.

The same release moves the child module avm/res/devices/iot-hub/consumergroup to avm/res/devices/iot-hub/event-hub-endpoint/consumergroup: check before upgrading if you reference it.

https://github.com/Azure/bicep-registry-modules/tree/avm/res/devices/iot-hub/0.4.0

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: owner_added:Azure/bicep-registry-modules:avm/res/network/network-security-group:2026-10-06 channel: x day: 2026-10-06 -->
The Network Security Group module, avm/res/network/network-security-group, has a new owner: Arjen Huitema was added to its owner file on 6 October 2026. https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/network/network-security-group
<!-- /post -->

<!-- post: owner_added:Azure/bicep-registry-modules:avm/res/network/network-security-group:2026-10-06 channel: linkedin day: 2026-10-06 -->
The Network Security Group module (avm/res/network/network-security-group) has a new owner. On 6 October 2026 Arjen Huitema was added to the module's owner file, after the module had been without one.

https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/network/network-security-group

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: owner_added:Azure/bicep-registry-modules:avm/res/sql/server:2026-10-06 channel: x day: 2026-10-06 -->
The Azure SQL Server module, avm/res/sql/server, has a new owner: Arjen Huitema was added to its owner file on 6 October 2026. https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/sql/server
<!-- /post -->

<!-- post: owner_added:Azure/bicep-registry-modules:avm/res/sql/server:2026-10-06 channel: linkedin day: 2026-10-06 -->
The Azure SQL Server module (avm/res/sql/server) has a new owner. On 6 October 2026 Arjen Huitema was added to the module's owner file. Four days earlier, a pull request for the module had carried the label "Status: Module Orphaned :yellow_circle:".

https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/sql/server

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-pr-7485 channel: x day: 2026-10-07 -->
Proposed new module: avm/res/sql-virtual-machine/sql-virtual-machine, for SQL Server on Azure virtual machines (Microsoft.SqlVirtualMachine/sqlVirtualMachines), in an open pull request by Arjen Huitema. Not published. https://github.com/Azure/bicep-registry-modules/pull/7485
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-pr-7485 channel: linkedin day: 2026-10-07 -->
A new module is proposed: avm/res/sql-virtual-machine/sql-virtual-machine. Its metadata describes it as "AVM Resource Module for SQL Virtual Machine", for the resource type Microsoft.SqlVirtualMachine/sqlVirtualMachines.

Arjen Huitema opened the pull request on 7 October 2026. It is open and the module is not published.

https://github.com/Azure/bicep-registry-modules/pull/7485

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-pr-7466 channel: x day: 2026-10-09 -->
Merged, not yet published: avm/res/kubernetes-runtime/load-balancer 0.2.0, the Kubernetes Runtime Load Balancer module. "Updated the Microsoft.Kubernetes/connectedClusters reference from 2024-01-01 to 2026-05-01." https://github.com/Azure/bicep-registry-modules/pull/7466
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-pr-7466 channel: linkedin day: 2026-10-09 -->
A change to the Kubernetes Runtime Load Balancer module (avm/res/kubernetes-runtime/load-balancer) was merged on 6 October 2026. Its changelog declares version 0.2.0, which is not yet published.

From the changelog: "Updated the Microsoft.Kubernetes/connectedClusters reference from 2024-01-01 to 2026-05-01."

https://github.com/Azure/bicep-registry-modules/pull/7466

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-pr-7504 channel: x day: 2026-10-09 -->
Proposed new utility module: avm/utl/general/naming, "Pure resource naming functions using the Terraform AVM naming catalog.", in an open pull request by Jared Holgate. Not published. https://github.com/Azure/bicep-registry-modules/pull/7504
<!-- /post -->

<!-- post: story:Azure/bicep-registry-modules:train-pr-7504 channel: linkedin day: 2026-10-09 -->
A new utility module is proposed: avm/utl/general/naming. Its metadata describes it as "Pure resource naming functions using the Terraform AVM naming catalog."

Jared Holgate opened the pull request on 9 October 2026. It is open and the module is not published.

https://github.com/Azure/bicep-registry-modules/pull/7504

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:pipeline_utilities:2026-W41 channel: x day: 2026-10-11 -->
What was engineered, week to 11 October 2026: shared pipeline utilities. 12 pull requests merged, among them the consolidation of CI reliability fixes in #7517. No module changes. https://github.com/Azure/bicep-registry-modules/pull/7517
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:pipeline_utilities:2026-W41 channel: linkedin day: 2026-10-11 -->
What was engineered in the week to 11 October 2026: the shared pipeline utilities every module's test pipeline runs.

20 pull requests were opened and 12 merged. Among them, #7517 consolidated separately proposed CI reliability fixes into one change. None of them changed a module.

https://github.com/Azure/bicep-registry-modules/pull/7517

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:ci_workflows:2026-W41 channel: x day: 2026-10-11 -->
What was engineered, week to 11 October 2026: CI workflows. 2 pull requests merged; #7482 removed the last stored-secret sign-in from module deployment tests. https://github.com/Azure/bicep-registry-modules/pull/7482
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:ci_workflows:2026-W41 channel: linkedin day: 2026-10-11 -->
What was engineered in the week to 11 October 2026: the CI workflows.

4 pull requests were opened and 2 merged. #7482 removed the last exception to OIDC sign-in from module deployment tests, and #7507 fixed how the workflow selects the changes of a pull request.

https://github.com/Azure/bicep-registry-modules/pull/7482

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:test_environment:2026-W41 channel: x day: 2026-10-11 -->
What was engineered, week to 11 October 2026: test fixtures and the test environment. 3 pull requests merged, among them a reusable host-image builder for the Azure Local tests, #7491. https://github.com/Azure/bicep-registry-modules/pull/7491
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:test_environment:2026-W41 channel: linkedin day: 2026-10-11 -->
What was engineered in the week to 11 October 2026: test fixtures and the test environment.

4 pull requests were opened and 3 merged. #7491 added a reusable host-image builder for the Azure Local module tests, #7505 separated its build and replication regions, and #7458 replaced tenant-specific CI inputs in the test entrypoints of nine modules.

https://github.com/Azure/bicep-registry-modules/pull/7491

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:other_plumbing:2026-W41 channel: x day: 2026-10-11 -->
What was engineered, week to 11 October 2026: 2 metadata fixes merged, a typo that broke README generation for API Management (#7493) and a new module owner entry (#7471). https://github.com/Azure/bicep-registry-modules/pull/7493
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:other_plumbing:2026-W41 channel: linkedin day: 2026-10-11 -->
What was engineered in the week to 11 October 2026: two small changes to module metadata files.

#7493 fixed a typo in the API Management metadata that made the README check fail for any pull request touching that module, and #7471 added a module owner.

https://github.com/Azure/bicep-registry-modules/pull/7493

#Bicep #AzureVerifiedModules
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:repository_policy:2026-W41 channel: x day: 2026-10-11 -->
What was engineered, week to 11 October 2026: an automation account kept the issue form's module list in sync with the published catalog (#7473). https://github.com/Azure/bicep-registry-modules/pull/7473
<!-- /post -->

<!-- post: weekly:Azure/bicep-registry-modules:repository_policy:2026-W41 channel: linkedin day: 2026-10-11 -->
What was engineered in the week to 11 October 2026: an automation account opened and merged #7473, which keeps the "Module Name" dropdown of the module issue form in line with the published module catalog.

https://github.com/Azure/bicep-registry-modules/pull/7473

#Bicep #AzureVerifiedModules
<!-- /post -->
