# Azure Verified Modules for Bicep — October 2026

October 2026 brought no new module, but plenty of change in existing ones: a group of Azure Local and Arc modules moved to newer API versions, the host pool module gained a load-balancer type a first-time contributor had asked for, and the Data Factory module replaced its eleven Git parameters with a single `gitConfiguration` object in version 0.13.0 — a breaking change, described under [Breaking changes and deprecations](#breaking-changes-and-deprecations). The Dev Test Lab module declared the deprecation of its `secret` child module. Three proposed modules are waiting for review. Most of the month's merged work was plumbing (49 of 64 classified pull requests): it is told theme by theme under [Plumbing: what was engineered](#plumbing-what-was-engineered). 12 people opened their first pull request or issue in the repository.

<!-- placeholder: story counts — the stories below are 15 landed, 9 in flight, 12 stalled with activity in October 2026 and 3 closed, in full; 20 stalled threads carried over in one line each; 72 plumbing trains by theme -->

<!-- omitted: new_modules — no module is new in the window (module_changes.summary.new_modules is empty) -->

## What changed in existing modules

**Azure Local and Arc modules on newer API versions.** A run of pull requests by @abhisheksharma-ops, merged by @chirag1603, moved these modules to newer API versions:

- [`avm/res/hybrid-compute/gateway`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/hybrid-compute/gateway) and [`avm/res/hybrid-compute/license`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/hybrid-compute/license) were updated to API version `2026-07-15` for `Microsoft.HybridCompute/gateways` and `Microsoft.HybridCompute/licenses`; both are released as 0.3.0 ([0.3.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/hybrid-compute/gateway/0.3.0), [0.3.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/hybrid-compute/license/0.3.0); [#7434](https://github.com/Azure/bicep-registry-modules/pull/7434), [#7435](https://github.com/Azure/bicep-registry-modules/pull/7435)).
- [`avm/res/kubernetes/connected-cluster`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/kubernetes/connected-cluster) [0.3.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/kubernetes/connected-cluster/0.3.0): "Updated API version for `Microsoft.Kubernetes/connectedClusters` from the preview `2026-02-01-preview` to the stable `2026-05-01`." ([#7431](https://github.com/Azure/bicep-registry-modules/pull/7431))
- [`avm/res/kubernetes-runtime/load-balancer`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/kubernetes-runtime/load-balancer) 0.2.0 (declared in the changelog, not yet published as of 31 October 2026): "Updated the `Microsoft.Kubernetes/connectedClusters` reference from `2024-01-01` to `2026-05-01`." ([#7466](https://github.com/Azure/bicep-registry-modules/pull/7466))
- [`avm/res/hybrid-container-service/provisioned-cluster-instance`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/hybrid-container-service/provisioned-cluster-instance) 0.3.0 (declared in the changelog, not yet published): it now references the connected-cluster module 0.3.0, and the changelog explains a deliberate limit: "Kept `Microsoft.HybridContainerService/provisionedClusterInstances` on the stable `2024-01-01`, as Azure Local's AKS Arc extension doesn't accept `2026-04-01-preview` for creating clusters at the time of this release; the preview-only `securityProfile` and `gpuCountPerNode` properties are therefore not offered" ([#7439](https://github.com/Azure/bicep-registry-modules/pull/7439)). The `hciSecurityProfile` parameter is no longer in `main.bicep`; see the next section.
- [`avm/res/azure-stack-hci/virtual-machine-instance`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/azure-stack-hci/virtual-machine-instance) [0.3.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/azure-stack-hci/virtual-machine-instance/0.3.0): "Updated the referenced `avm/res/hybrid-compute/machine` module from `0.4.1` to `0.6.0`, which creates the Arc machine with API version `2026-07-15` instead of `2024-07-10`." Why 0.3.0 and not 0.2.0: "Released as `0.3.0`: these changes were previously listed under `0.2.0`, which was never published." ([#7463](https://github.com/Azure/bicep-registry-modules/pull/7463))

**Host pool: multiple personal desktops per user.** [`avm/res/desktop-virtualization/host-pool`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/desktop-virtualization/host-pool) [0.8.2](https://github.com/Azure/bicep-registry-modules/tree/avm/res/desktop-virtualization/host-pool/0.8.2), published on 18 October 2026: "Added the missing `MultiplePersistent` allowed value to `loadBalancerType` for Personal host pools." It came from a request by @col1582, a first-time contributor, in [#7489](https://github.com/Azure/bicep-registry-modules/issues/7489): "the parameter loadBalancerType does not allow MultiplePersistent to be passed, this is a valid configuration for Personal host pool types (hostPoolType)". Vincenzo Sciarra (@reikor0x, Microsoft) made the change from a fork in [#7502](https://github.com/Azure/bicep-registry-modules/pull/7502), with a new end-to-end test scenario, `tests/e2e/personal-multiple-desktops`; it was merged on 18 October 2026 and released as [0.8.2](https://github.com/Azure/bicep-registry-modules/tree/avm/res/desktop-virtualization/host-pool/0.8.2). Who it is for: the pull request's description (as last gathered) says the value "allows multiple personal desktops per user for Personal host pools".

**Scheduled query rules: the `SimpleLogAlert` kind.** [`avm/res/insights/scheduled-query-rule`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/insights/scheduled-query-rule) [0.6.1](https://github.com/Azure/bicep-registry-modules/tree/avm/res/insights/scheduled-query-rule/0.6.1), published on 21 October 2026: "Added support for kind `SimpleLogAlert`. Only the `SimpleLogAlert`-supported properties (`severity`, `autoMitigate`, `resolveConfiguration`) are applied for this kind." The request came from Maarten Balliere (@balliere) in [#7345](https://github.com/Azure/bicep-registry-modules/issues/7345) ("kind: 'SimpleLogAlert' is missing"), and he made the change himself from a fork in [#7346](https://github.com/Azure/bicep-registry-modules/pull/7346), with two new end-to-end tests; its story is below.

**Network security group: newer API version and clearer descriptions.** [`avm/res/network/network-security-group`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/network/network-security-group) [0.5.4](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/network-security-group/0.5.4), published on 14 October 2026: "Updated `Microsoft.Network/networkSecurityGroups` API version from `2025-05-01` to `2025-09-01`", and "Corrected parameter descriptions, including documenting that `securityRules` replaces all custom rules of the Network Security Group and that `flushConnection` fails in regions where connection flushing is not available". Arjen Huitema (@arjenhuitema, Microsoft) made the change in [#7496](https://github.com/Azure/bicep-registry-modules/pull/7496) (its story is below), after being added as the module's owner in [#7471](https://github.com/Azure/bicep-registry-modules/pull/7471).

**Event Hub namespace: a disabled capture setting is kept.** [`avm/res/event-hub/namespace`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/event-hub/namespace) [0.15.2](https://github.com/Azure/bicep-registry-modules/tree/avm/res/event-hub/namespace/0.15.2), published on 18 October 2026, with its `eventhub` child module as [0.2.3](https://github.com/Azure/bicep-registry-modules/tree/avm/res/event-hub/namespace/eventhub/0.2.3): "Fixed `eventhubs[].captureDescription` so that setting `enabled` to `false` is honored instead of omitting the capture configuration. The `destination` property is now only emitted when provided." The fix came from @Theuno, a first-time contributor, from a fork in [#7499](https://github.com/Azure/bicep-registry-modules/pull/7499); its description (as last gathered) says the whole block "caused the **entire** `captureDescription` block to be silently dropped" before.

**IoT Hub: no more concurrent-update conflicts.** [`avm/res/devices/iot-hub`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/devices/iot-hub) [0.4.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/devices/iot-hub/0.4.0), published on 6 October 2026: "Serialized private endpoint deployments to prevent concurrent IoT Hub updates from failing with ETag conflicts." The change followed a failed-pipeline report, [#7459](https://github.com/Azure/bicep-registry-modules/issues/7459), and came from Tomaz Mlakar (@TomazMlakar) from a fork in [#7469](https://github.com/Azure/bicep-registry-modules/pull/7469); its story is below.

**Private DNS zones pattern: the Redis zone.** [`avm/ptn/network/private-link-private-dns-zones`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/ptn/network/private-link-private-dns-zones) [0.7.4](https://github.com/Azure/bicep-registry-modules/tree/avm/ptn/network/private-link-private-dns-zones/0.7.4), published on 22 October 2026: "Added Redis zone `privatelink.redis.azure.net`". Nikolaj Petersen (@NPetersenDK), a first-time contributor, made it from a fork in [#7558](https://github.com/Azure/bicep-registry-modules/pull/7558).

**SQL managed instance: the storage limit.** [`avm/res/sql/managed-instance`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/sql/managed-instance) [0.5.1](https://github.com/Azure/bicep-registry-modules/tree/avm/res/sql/managed-instance/0.5.1) was published on 3 October 2026 with no changelog entry in the window. It answers [#7344](https://github.com/Azure/bicep-registry-modules/issues/7344) by @mirabellodj, which said the module "contains an incorrect validation constraint on the storageSizeInGB property"; Jared Holgate (@jaredfholgate) closed it with [#7442](https://github.com/Azure/bicep-registry-modules/pull/7442) and [#7444](https://github.com/Azure/bicep-registry-modules/pull/7444). Its story is below.

**API Management: a documentation fix, republished.** [`avm/res/api-management/service`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/api-management/service) [0.15.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/api-management/service/0.15.0) was published on 5 October 2026 with no changelog entry in the window; the window's change to it is Andy Jordan's documentation fix in [#7493](https://github.com/Azure/bicep-registry-modules/pull/7493) (no version change of its own).

**Data Factory, Dev Test Lab and MySQL flexible server.** The [`avm/res/data-factory/factory`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/data-factory/factory), [`avm/res/dev-test-lab/lab`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/dev-test-lab/lab) and [`avm/res/db-for-my-sql/flexible-server`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/db-for-my-sql/flexible-server) changes are described in the next section, with what to check before upgrading.

## Breaking changes and deprecations

**Declared by the maintainers**

- **[`avm/res/data-factory/factory`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/data-factory/factory) [0.13.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/data-factory/factory/0.13.0)** (published on 14 October 2026): the eleven `git*` parameters, `gitConfigureLater` included, were removed in favour of one `gitConfiguration` object, and "`collaborationBranch` and `rootFolder` no longer default to `'main'` and `'/'`." If you set a Git repository, move its values into `gitConfiguration` (the module's changelog shows the before and after). If you never did: "Deployments that relied on `gitConfigureLater` defaulting to `true` require no change - omitting `gitConfiguration` preserves the existing behaviour." ([#7360](https://github.com/Azure/bicep-registry-modules/pull/7360); the story is below.)
- **[`avm/res/dev-test-lab/lab`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/dev-test-lab/lab) 0.6.0** (declared in the changelog; not yet published as of 31 October 2026): "Deprecated `avm/res/dev-test-lab/lab/secret` as no longer supported by Resource Provider". A deprecation marker file was added for that child module, and the `secrets` parameter is no longer in the module's `main.bicep`; check before upgrading if you pass it. ([#7454](https://github.com/Azure/bicep-registry-modules/pull/7454))

**Detected in the code, not declared: check before upgrading**

- [`avm/res/hybrid-container-service/provisioned-cluster-instance`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/hybrid-container-service/provisioned-cluster-instance) 0.3.0 (declared, not yet published): the `hciSecurityProfile` parameter is no longer in `main.bicep`. ([#7439](https://github.com/Azure/bicep-registry-modules/pull/7439))
- [`avm/res/devices/iot-hub`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/devices/iot-hub) [0.4.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/devices/iot-hub/0.4.0): the child module `avm/res/devices/iot-hub/consumergroup` is now found at `avm/res/devices/iot-hub/event-hub-endpoint/consumergroup`. ([#7469](https://github.com/Azure/bicep-registry-modules/pull/7469))
- [`avm/res/db-for-my-sql/flexible-server`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/db-for-my-sql/flexible-server) 0.11.2 (declared, not yet published): the child module `avm/res/db-for-my-sql/flexible-server/advanced-threat-protection` is now found at [`avm/res/db-for-my-sql/flexible-server/advanced-threat-protection-setting`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/db-for-my-sql/flexible-server/advanced-threat-protection-setting), published as [0.1.0](https://github.com/Azure/bicep-registry-modules/tree/avm/res/db-for-my-sql/flexible-server/advanced-threat-protection-setting/0.1.0). ([#7454](https://github.com/Azure/bicep-registry-modules/pull/7454))

## Stories

### Landed

<!-- story pending: train-issue-7342 — written from the story work package -->

<!-- story pending: train-issue-7345 — written from the story work package -->

<!-- story pending: train-issue-7344 — written from the story work package -->

<!-- story pending: train-pr-7496 — written from the story work package -->

<!-- story pending: train-pr-7439 — written from the story work package -->

<!-- story pending: train-issue-7489 — written from the story work package -->

<!-- story pending: train-issue-7459 — written from the story work package -->

<!-- story pending: train-pr-7431 — written from the story work package -->

<!-- story pending: train-pr-7463 — written from the story work package -->

<!-- story pending: train-pr-7558 — written from the story work package -->

<!-- story pending: train-pr-7434 — written from the story work package -->

<!-- story pending: train-pr-7435 — written from the story work package -->

<!-- story pending: train-pr-7466 — written from the story work package -->

<!-- story pending: train-pr-7499 — written from the story work package -->

<!-- story pending: train-pr-7473 — written from the story work package -->

### In flight

<!-- story pending: train-issue-6891 — written from the story work package -->

<!-- story pending: train-issue-6483 — written from the story work package -->

<!-- story pending: train-pr-7422 — written from the story work package -->

<!-- story pending: train-issue-7335 — written from the story work package -->

<!-- story pending: train-issue-7269 — written from the story work package -->

<!-- story pending: train-pr-7552 — written from the story work package -->

<!-- story pending: train-pr-7560 — written from the story work package -->

<!-- story pending: train-issue-7554 — written from the story work package -->

<!-- story pending: train-pr-7557 — written from the story work package -->

### Stalled

<!-- story pending: train-issue-7503 — written from the story work package -->

<!-- story pending: train-issue-3377 — written from the story work package -->

<!-- story pending: train-issue-7156 — written from the story work package -->

<!-- story pending: train-pr-7530 — written from the story work package -->

<!-- story pending: train-pr-7528 — written from the story work package -->

<!-- story pending: train-issue-7523 — written from the story work package -->

<!-- story pending: train-pr-7286 — written from the story work package -->

<!-- story pending: train-pr-7485 — written from the story work package -->

<!-- story pending: train-pr-7504 — written from the story work package -->

<!-- story pending: train-pr-7539 — written from the story work package -->

<!-- story pending: train-pr-7483 — written from the story work package -->

<!-- story pending: train-pr-7522 — written from the story work package -->

Carried over without a turn in October 2026 (state, days without recorded activity, and the derived next move):

- [#3860](https://github.com/Azure/bicep-registry-modules/issues/3860) [`avm/res/container-service/managed-cluster`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/container-service/managed-cluster): "[AVM Module Issue]: 0.5.1~0.8.3 incompatible with BYO vnet PodSubnetID `container-service/managed-cluster`" — stalled, 43 days; waiting: awaiting author.
- [#6518](https://github.com/Azure/bicep-registry-modules/issues/6518) [`avm/res/consumption/budget`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/consumption/budget): "[AVM Module Issue]: Fine control for notification thresholds in budget" — stalled, 37 days; waiting: awaiting author.
- [#6167](https://github.com/Azure/bicep-registry-modules/issues/6167) [`avm/res/api-management/service`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/api-management/service): "[AVM Module Issue]: Disable weak cipher suites in API Management service module?" — stalled, 43 days; waiting: awaiting mentioned.
- [#6955](https://github.com/Azure/bicep-registry-modules/issues/6955) [`avm/res/app/managed-environment`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/app/managed-environment): "[AVM Module Issue]: Add Aspire Dashboard component support to avm/res/app/managed-environment" — stalled, 47 days; waiting: awaiting author.
- [#4794](https://github.com/Azure/bicep-registry-modules/issues/4794) [`avm/res/cognitive-services/account`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/cognitive-services/account): "[AVM Module Issue]: Unable to create a cognitive services account with multiple deployments" — stalled, 42 days; waiting: awaiting reviewer.
- [#5561](https://github.com/Azure/bicep-registry-modules/issues/5561) [`avm/res/compute/disk`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/compute/disk): "[AVM Module Issue]: Missing properties securityProfile sections (or securityEncryptionType) in Compute module" — stalled, 123 days; waiting: awaiting mentioned.
- [#6968](https://github.com/Azure/bicep-registry-modules/issues/6968) [`avm/res/network/bastion-host`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/network/bastion-host): "[AVM Module Issue]: AVM Bastion Host Module — Session Recording Storage Configuration Gap" — stalled, 90 days; waiting: awaiting mentioned.
- [#6694](https://github.com/Azure/bicep-registry-modules/pull/6694) "feat: check for module existence in MAR" — stalled, 72 days; waiting: awaiting reviewer.
- [#7122](https://github.com/Azure/bicep-registry-modules/issues/7122) [`avm/res/api-management/service`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/api-management/service): "[AVM Module Issue]: avm/res/api-management/service logger description not set in api-management deployed instance of AVM module" — stalled, 37 days; waiting: awaiting mentioned.
- [#6900](https://github.com/Azure/bicep-registry-modules/issues/6900) [`avm/ptn/mgmt-groups/subscription-placement`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/ptn/mgmt-groups/subscription-placement): "[AVM Module Issue]: subscriptions never placed unless disableSubscriptionPlacement: true" — stalled, 103 days; waiting: awaiting mentioned.
- [#7278](https://github.com/Azure/bicep-registry-modules/issues/7278) [`avm/res/api-management/service`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/api-management/service): "[AVM Module Issue]: API management `skuCapacity` default of `3` fails deployment when `sku` is `Basic` (max capacity 2)" — stalled, 37 days; waiting: awaiting mentioned.
- [#6929](https://github.com/Azure/bicep-registry-modules/pull/6929) "feat: Subscription Vending PTN - UMI Role Assignments absolute scopes" — stalled, 113 days; waiting: awaiting reviewer.
- [#7310](https://github.com/Azure/bicep-registry-modules/pull/7310) "feat: AVM Module Upgrade for BYOCC" — stalled, 43 days; waiting: awaiting mentioned.
- [#7259](https://github.com/Azure/bicep-registry-modules/pull/7259) "feat: static test for using multiple versions in one module" — stalled, 72 days; waiting: awaiting author.
- [#7245](https://github.com/Azure/bicep-registry-modules/pull/7245) "Fix: CWYD AVM upgrade v2" — stalled, 78 days; waiting: awaiting a first review.
- [#7381](https://github.com/Azure/bicep-registry-modules/pull/7381) "feat: Databricks Workspace - Updated API version to 2026-01-01 and added computeMode" — stalled, 36 days; waiting: awaiting mentioned.
- [#7226](https://github.com/Azure/bicep-registry-modules/issues/7226) [`avm/res/api-management/service`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/api-management/service): "[AVM Module Issue]: subscriptionsLimit on product cant be set to unlimited" — stalled, 37 days; waiting: awaiting mentioned.
- [#7394](https://github.com/Azure/bicep-registry-modules/issues/7394) "fix(avm/res/azure-stack-hci/cluster): Validate deploymentSettings deleted before Deploy wipes validationStatus (portal 'Validate input = Unknown')" — stalled, 33 days; waiting: awaiting a first review.
- [#7333](https://github.com/Azure/bicep-registry-modules/pull/7333) "AVM module updated for CWYD v2" — stalled, 43 days; waiting: awaiting author.
- [#7263](https://github.com/Azure/bicep-registry-modules/pull/7263) "Upgrade FinOps hub to Toolkit v14" — stalled, 81 days; waiting: awaiting a first review.

### Closed without landing

<!-- story pending: train-issue-7541 — written from the story work package -->

<!-- story pending: train-issue-7542 — written from the story work package -->

<!-- story pending: train-issue-7543 — written from the story work package -->

## Module ownership

- **A new owner.** On 6 October 2026 Arjen Huitema (@arjenhuitema, Microsoft) was added as owner of [`avm/res/network/network-security-group`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/network/network-security-group) and [`avm/res/sql/server`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/sql/server) in the modules' owner files ([commit](https://github.com/Azure/bicep-registry-modules/commit/0d3c275347434e9755ad33904a4a416282554f9b), [#7471](https://github.com/Azure/bicep-registry-modules/pull/7471)). He went on to update the network security group module himself (see above).
- **Marked orphaned before that.** On 2 October 2026 a pull request by Chris Bragg (@chbragg, Microsoft) for [`avm/res/sql/server`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/sql/server), [#7448](https://github.com/Azure/bicep-registry-modules/pull/7448), carried the label "Status: Module Orphaned :yellow_circle:" — four days before the module gained its new owner. The same label was on [#7454](https://github.com/Azure/bicep-registry-modules/pull/7454), a repository-wide README-generation pull request by Alexander Sehr (@AlexanderSehr) opened on 3 October 2026 that names no single module, so no module is attributed to it here.
- **Without an owner at 31 October 2026:** [`avm/res/code-signing/code-signing-account`](https://github.com/Azure/bicep-registry-modules/tree/main/avm/res/code-signing/code-signing-account) (an `ORPHANED.md` file in its folder), as at the start of the month. No request for a new owner was recorded in October 2026.

## People

**First contributions.** 12 people opened their first pull request or issue in the repository. Four of them contributed code or documentation that is now in the main branch:

- Arjen Huitema (@arjenhuitema, Microsoft) updated [`avm/res/network/network-security-group`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/network/network-security-group) in [#7496](https://github.com/Azure/bicep-registry-modules/pull/7496), released as [0.5.4](https://github.com/Azure/bicep-registry-modules/tree/avm/res/network/network-security-group/0.5.4), and was added as its owner in [#7471](https://github.com/Azure/bicep-registry-modules/pull/7471) (no version change); a first SQL virtual machine module is in review ([#7485](https://github.com/Azure/bicep-registry-modules/pull/7485)).
- Nikolaj Petersen (@NPetersenDK) added the Redis private DNS zone to [`avm/ptn/network/private-link-private-dns-zones`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/ptn/network/private-link-private-dns-zones) from a fork in [#7558](https://github.com/Azure/bicep-registry-modules/pull/7558), released as [0.7.4](https://github.com/Azure/bicep-registry-modules/tree/avm/ptn/network/private-link-private-dns-zones/0.7.4).
- @Theuno fixed the disabled capture setting in [`avm/res/event-hub/namespace`](https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/event-hub/namespace) from a fork in [#7499](https://github.com/Azure/bicep-registry-modules/pull/7499), released as [0.15.2](https://github.com/Azure/bicep-registry-modules/tree/avm/res/event-hub/namespace/0.15.2).
- Andy Jordan (@andyleejordan, Microsoft) fixed a documentation typo in the API Management module in [#7493](https://github.com/Azure/bicep-registry-modules/pull/7493) (no version change) and opened the secure-inputs work told in the stories.

First pull requests still open: Chris Bragg (@chbragg, Microsoft), SQL server diagnostic settings ([#7448](https://github.com/Azure/bicep-registry-modules/pull/7448)). From the community (by their profile), smerek (@smereczynski), VM scale set gallery applications ([#7552](https://github.com/Azure/bicep-registry-modules/pull/7552)), and Charlie Ward (@wardch), container app job identity settings ([#7524](https://github.com/Azure/bicep-registry-modules/pull/7524)). First issues: @col1582 ([#7489](https://github.com/Azure/bicep-registry-modules/issues/7489), which led to the host pool change above), brian.pieper (@kt-pieper) ([#7541](https://github.com/Azure/bicep-registry-modules/issues/7541)), @Miteshc1 ([#7565](https://github.com/Azure/bicep-registry-modules/issues/7565)), @Torrer93 ([#7551](https://github.com/Azure/bicep-registry-modules/issues/7551)). Stephen Just (@stephenjust, Microsoft) too ([#7461](https://github.com/Azure/bicep-registry-modules/issues/7461)).

**Welcome back.** After more than 3 months away, Vincenzo Sciarra (@reikor0x, Microsoft) made the host pool change from a fork in [#7502](https://github.com/Azure/bicep-registry-modules/pull/7502), released as [0.8.2](https://github.com/Azure/bicep-registry-modules/tree/avm/res/desktop-virtualization/host-pool/0.8.2). Felipe Binotto (@fbinotto), also back after more than 3 months, opened two NetApp fixes ([#7549](https://github.com/Azure/bicep-registry-modules/pull/7549), [#7550](https://github.com/Azure/bicep-registry-modules/pull/7550)). After more than 6 months, @gpacetti opened the automation account work ([#7530](https://github.com/Azure/bicep-registry-modules/pull/7530)). After more than 9 months, Preston Alvarado (@coolhome) ([#7540](https://github.com/Azure/bicep-registry-modules/issues/7540)) and @dfwlonghorn ([#7554](https://github.com/Azure/bicep-registry-modules/issues/7554)) opened issues, as did Sidney Andrews (@seesharprun) ([#7562](https://github.com/Azure/bicep-registry-modules/issues/7562)).

**Who did the work.** Of the 64 pull requests merged by people, 51 (80%) came from people labelled Microsoft, 12 (19%) from people of unknown affiliation and 1 (2%) from the community. That one is the `SimpleLogAlert` fix by Maarten Balliere (@balliere) from a fork in [#7346](https://github.com/Azure/bicep-registry-modules/pull/7346), released as [0.6.1](https://github.com/Azure/bicep-registry-modules/tree/avm/res/insights/scheduled-query-rule/0.6.1). Of 61 reviews, 43 (70%) were by people labelled Microsoft and 18 (30%) by people of unknown affiliation.

**Maintainers.** Alexander Sehr (@AlexanderSehr) reviewed 21 pull requests and merged 5 by others; Jack Tracey (@jtracey93) reviewed 16 and merged 2; @chirag1603 reviewed 7 and merged 7, the Azure Local and Arc updates by @abhisheksharma-ops; Jared Holgate (@jaredfholgate) authored 44 merged pull requests, 43 of them plumbing. Thank you all.

**Automation.** The automation account @azure-verified-modules[bot] synced the module list in [#7473](https://github.com/Azure/bicep-registry-modules/pull/7473) (no version change) and opened 36 issues. The automation account @dependabot[bot] has 7 dependency-update pull requests in this month's records, none of them merged. The automation accounts @copilot-pull-request-reviewer[bot] and @github-advanced-security[bot] each reviewed 4 pull requests.

## Coming up and stuck

32 open pull requests had activity in October 2026.

- **Likely next:** the NetApp `activeDirectorySite` input is approved with no later change request ([#7032](https://github.com/Azure/bicep-registry-modules/pull/7032)). It is not ready to merge yet: by the record's sequence of events, its author's latest comment asks someone else to respond.
- **New modules proposed:** `avm/res/azure-stack-hci/storage-container` ([#7539](https://github.com/Azure/bicep-registry-modules/pull/7539)) and `avm/res/azure-stack-hci/gallery-image` ([#7560](https://github.com/Azure/bicep-registry-modules/pull/7560)) are waiting for a first review; the SQL virtual machine module ([#7485](https://github.com/Azure/bicep-registry-modules/pull/7485)) is waiting for a reviewer.
- **Checks failing on the head commit:** the end-to-end test fixes [#7449](https://github.com/Azure/bicep-registry-modules/pull/7449) (`Validate e2e resource names offline`), [#7483](https://github.com/Azure/bicep-registry-modules/pull/7483) and [#7527](https://github.com/Azure/bicep-registry-modules/pull/7527) (deployment validation), the Key Vault reference update [#7374](https://github.com/Azure/bicep-registry-modules/pull/7374) (deployment validation), and the Cosmos DB change feed setting [#7494](https://github.com/Azure/bicep-registry-modules/pull/7494) (`Validate PR title`).
- **Waiting for a first review:** 10 pull requests, among them the two proposed modules.

How to help: review a pull request waiting for a first review, or try one of the proposed modules and comment on it.

## Plumbing: what was engineered

<!-- theme pending: pipeline_utilities — written from the theme batch -->

<!-- theme pending: ci_workflows — written from the theme batch -->

<!-- theme pending: test_environment — written from the theme batch -->

<!-- theme pending: docs_generation — written from the theme batch -->

<!-- theme pending: repository_policy — written from the theme batch -->

<!-- theme pending: dependency_updates — written from the theme batch -->

<!-- theme pending: other_plumbing — written from the theme batch -->

<!-- story pending: train-pr-7482 — written under its theme -->

49 of the 64 classified merged pull requests (77%) were plumbing: 39 touched only CI files, the rest documentation, tests or repository-wide sweeps.

## By the numbers

- 65 pull requests merged (64 by people), 99 opened; 40 issues opened.
- 29 modules touched, 17 with a change a user can see; no new module; 2 with a declared breaking change; 19 version tags (modules and child modules) published.
- 23 people opened a pull request or issue: 12 for the first time, 6 back after a gap.
- 229 people in the month's records: 54 labelled Microsoft, 33 community, 142 (62%) unknown.

*Coverage.* Records as of 31 October 2026; titles and labels as gathered on 27 November 2026. Unknown affiliation is never counted as community. The people registry covers only October 2026, so tenure and roles in other repositories are not given. A version not yet published by 31 October 2026 may have been tagged since. For 6 pull requests only the latest commits, check results, review threads or events were read.
