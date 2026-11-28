# Daily social posts — October 2026 (sample drafts)

Drafts for the owner to approve, written neutral from the record of Azure/bicep-registry-modules for October 2026 (store recomputed on 27 November 2026). Nothing here is scheduled or published. One post per story, at the first event a reader can act on; plumbing rolls into one weekly item per theme. The rules are in `reference/social.md`; the checkable drafts of week 41 are `avm-bicep-2026-10-week41.posts.md`.

## Posts per day, October 2026

| Day | Story / module posts | Weekly plumbing items | Held for a release |
|---|---|---|---|
| Thu 2026-10-01 | 3 | 0 | 1 |
| Fri 2026-10-02 | 1 | 0 | 1 |
| Sat 2026-10-03 | 2 | 0 | 1 |
| Sun 2026-10-04 | 1 | 4 | 0 |
| Mon 2026-10-05 | 1 | 0 | 0 |
| Tue 2026-10-06 | 3 | 0 | 1 |
| Wed 2026-10-07 | 1 | 0 | 1 |
| Thu 2026-10-08 | 0 | 0 | 1 |
| Fri 2026-10-09 | 2 | 0 | 0 |
| Sat 2026-10-10 | 0 | 0 | 0 |
| Sun 2026-10-11 | 0 | 5 | 0 |
| Mon 2026-10-12 | 1 | 0 | 0 |
| Tue 2026-10-13 | 0 | 0 | 0 |
| Wed 2026-10-14 | 3 | 0 | 0 |
| Thu 2026-10-15 | 0 | 0 | 0 |
| Fri 2026-10-16 | 0 | 0 | 0 |
| Sat 2026-10-17 | 0 | 0 | 0 |
| Sun 2026-10-18 | 2 | 3 | 0 |
| Mon 2026-10-19 | 1 | 0 | 0 |
| Tue 2026-10-20 | 0 | 0 | 0 |
| Wed 2026-10-21 | 1 | 0 | 0 |
| Thu 2026-10-22 | 1 | 0 | 0 |
| Fri 2026-10-23 | 1 | 0 | 0 |
| Sat 2026-10-24 | 0 | 0 | 0 |
| Sun 2026-10-25 | 0 | 5 | 0 |
| Mon 2026-10-26 | 0 | 0 | 0 |
| Tue 2026-10-27 | 0 | 0 | 0 |
| Wed 2026-10-28 | 0 | 0 | 0 |
| Thu 2026-10-29 | 0 | 0 | 0 |
| Fri 2026-10-30 | 0 | 0 | 0 |
| Sat 2026-10-31 | 0 | 3 | 0 |

Total: 24 story and module posts and 20 weekly plumbing items in October 2026 (44 posts, each in two forms). Week 41 (5–11 October 2026) is the busiest full week and is written out below.

## Monday 2026-10-05

### `story:Azure/bicep-registry-modules:train-pr-7463`

- Trigger: **published** (story `train-pr-7463`)
- Must state: published version avm/res/azure-stack-hci/virtual-machine-instance 0.3.0

**X** — check: pass, length 237 (weighted, links count 23)

```text
Published: avm/res/azure-stack-hci/virtual-machine-instance 0.3.0, the Azure Stack HCI Virtual Machine Instance module. Its changelog: "these changes were previously listed under 0.2.0, which was never published." https://github.com/Azure/bicep-registry-modules/tree/avm/res/azure-stack-hci/virtual-machine-instance/0.3.0
```

**LinkedIn** — check: pass, length 635

```text
Version 0.3.0 of the Azure Stack HCI Virtual Machine Instance module (avm/res/azure-stack-hci/virtual-machine-instance) was published on 5 October 2026.

The module's changelog explains the version number: "Released as 0.3.0: these changes were previously listed under 0.2.0, which was never published." The main change it carries: "Updated the referenced avm/res/hybrid-compute/machine module from 0.4.1 to 0.6.0, which creates the Arc machine with API version 2026-07-15 instead of 2024-07-10."

https://github.com/Azure/bicep-registry-modules/tree/avm/res/azure-stack-hci/virtual-machine-instance/0.3.0

#Bicep #AzureVerifiedModules
```

## Tuesday 2026-10-06

- Held: `train-pr-7466` (merged 2026-10-06, posts on 2026-10-09 unless a release comes first).

### `story:Azure/bicep-registry-modules:train-issue-7459`

- Trigger: **published** (story `train-issue-7459`)
- Must state: published version avm/res/devices/iot-hub 0.4.0

**X** — check: pass, length 199 (weighted, links count 23)

```text
Published: avm/res/devices/iot-hub 0.4.0, the IoT Hub module. "Serialized private endpoint deployments to prevent concurrent IoT Hub updates from failing with ETag conflicts." https://github.com/Azure/bicep-registry-modules/tree/avm/res/devices/iot-hub/0.4.0
```

**LinkedIn** — check: pass, length 618

```text
Version 0.4.0 of the IoT Hub module (avm/res/devices/iot-hub) was published on 6 October 2026.

From its changelog: "Serialized private endpoint deployments to prevent concurrent IoT Hub updates from failing with ETag conflicts." The fix followed a failed-pipeline report and was contributed by Tomaz Mlakar from a fork.

The same release moves the child module avm/res/devices/iot-hub/consumergroup to avm/res/devices/iot-hub/event-hub-endpoint/consumergroup: check before upgrading if you reference it.

https://github.com/Azure/bicep-registry-modules/tree/avm/res/devices/iot-hub/0.4.0

#Bicep #AzureVerifiedModules
```

### `owner_added:Azure/bicep-registry-modules:avm/res/network/network-security-group:2026-10-06`

- Trigger: **owner_added**
- Must state: owner added avm/res/network/network-security-group

**X** — check: pass, length 176 (weighted, links count 23)

```text
The Network Security Group module, avm/res/network/network-security-group, has a new owner: Arjen Huitema was added to its owner file on 6 October 2026. https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/network/network-security-group
```

**LinkedIn** — check: pass, length 364

```text
The Network Security Group module (avm/res/network/network-security-group) has a new owner. On 6 October 2026 Arjen Huitema was added to the module's owner file, after the module had been without one.

https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/network/network-security-group

#Bicep #AzureVerifiedModules
```

### `owner_added:Azure/bicep-registry-modules:avm/res/sql/server:2026-10-06`

- Trigger: **owner_added**
- Must state: owner added avm/res/sql/server

**X** — check: pass, length 150 (weighted, links count 23)

```text
The Azure SQL Server module, avm/res/sql/server, has a new owner: Arjen Huitema was added to its owner file on 6 October 2026. https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/sql/server
```

**LinkedIn** — check: pass, length 393

```text
The Azure SQL Server module (avm/res/sql/server) has a new owner. On 6 October 2026 Arjen Huitema was added to the module's owner file. Four days earlier, a pull request for the module had carried the label "Status: Module Orphaned :yellow_circle:".

https://github.com/Azure/bicep-registry-modules/tree/82950838e7cefc9a32e813055fbf407ea5819e15/avm/res/sql/server

#Bicep #AzureVerifiedModules
```

## Wednesday 2026-10-07

- Held: `train-pr-7466` (merged 2026-10-06, posts on 2026-10-09 unless a release comes first).

### `story:Azure/bicep-registry-modules:train-pr-7485`

- Trigger: **new_module_proposed** (story `train-pr-7485`)
- Must state: proposed module avm/res/sql-virtual-machine/sql-virtual-machine in an open pull request, not published

**X** — check: pass, length 241 (weighted, links count 23)

```text
Proposed new module: avm/res/sql-virtual-machine/sql-virtual-machine, for SQL Server on Azure virtual machines (Microsoft.SqlVirtualMachine/sqlVirtualMachines), in an open pull request by Arjen Huitema. Not published. https://github.com/Azure/bicep-registry-modules/pull/7485
```

**LinkedIn** — check: pass, length 411

```text
A new module is proposed: avm/res/sql-virtual-machine/sql-virtual-machine. Its metadata describes it as "AVM Resource Module for SQL Virtual Machine", for the resource type Microsoft.SqlVirtualMachine/sqlVirtualMachines.

Arjen Huitema opened the pull request on 7 October 2026. It is open and the module is not published.

https://github.com/Azure/bicep-registry-modules/pull/7485

#Bicep #AzureVerifiedModules
```

## Thursday 2026-10-08

Nothing became postable.
- Held: `train-pr-7466` (merged 2026-10-06, posts on 2026-10-09 unless a release comes first).

## Friday 2026-10-09

### `story:Azure/bicep-registry-modules:train-pr-7466`

- Trigger: **merged_not_published** (story `train-pr-7466`)
- Must state: merged on 2026-10-06, not yet published; declared avm/res/kubernetes-runtime/load-balancer 0.2.0

**X** — check: pass, length 237 (weighted, links count 23)

```text
Merged, not yet published: avm/res/kubernetes-runtime/load-balancer 0.2.0, the Kubernetes Runtime Load Balancer module. "Updated the Microsoft.Kubernetes/connectedClusters reference from 2024-01-01 to 2026-05-01." https://github.com/Azure/bicep-registry-modules/pull/7466
```

**LinkedIn** — check: pass, length 398

```text
A change to the Kubernetes Runtime Load Balancer module (avm/res/kubernetes-runtime/load-balancer) was merged on 6 October 2026. Its changelog declares version 0.2.0, which is not yet published.

From the changelog: "Updated the Microsoft.Kubernetes/connectedClusters reference from 2024-01-01 to 2026-05-01."

https://github.com/Azure/bicep-registry-modules/pull/7466

#Bicep #AzureVerifiedModules
```

### `story:Azure/bicep-registry-modules:train-pr-7504`

- Trigger: **new_module_proposed** (story `train-pr-7504`)
- Must state: proposed module avm/utl/general/naming in an open pull request, not published

**X** — check: pass, length 207 (weighted, links count 23)

```text
Proposed new utility module: avm/utl/general/naming, "Pure resource naming functions using the Terraform AVM naming catalog.", in an open pull request by Jared Holgate. Not published. https://github.com/Azure/bicep-registry-modules/pull/7504
```

**LinkedIn** — check: pass, length 350

```text
A new utility module is proposed: avm/utl/general/naming. Its metadata describes it as "Pure resource naming functions using the Terraform AVM naming catalog."

Jared Holgate opened the pull request on 9 October 2026. It is open and the module is not published.

https://github.com/Azure/bicep-registry-modules/pull/7504

#Bicep #AzureVerifiedModules
```

## Saturday 2026-10-10

Nothing became postable.
## Sunday 2026-10-11

### `weekly:Azure/bicep-registry-modules:pipeline_utilities:2026-W41`

- Trigger: **weekly_theme**
- Must state: 12 merged, 20 opened in 2026-W41

**X** — check: pass, length 203 (weighted, links count 23)

```text
What was engineered, week to 11 October 2026: shared pipeline utilities. 12 pull requests merged, among them the consolidation of CI reliability fixes in #7517. No module changes. https://github.com/Azure/bicep-registry-modules/pull/7517
```

**LinkedIn** — check: pass, length 370

```text
What was engineered in the week to 11 October 2026: the shared pipeline utilities every module's test pipeline runs.

20 pull requests were opened and 12 merged. Among them, #7517 consolidated separately proposed CI reliability fixes into one change. None of them changed a module.

https://github.com/Azure/bicep-registry-modules/pull/7517

#Bicep #AzureVerifiedModules
```

### `weekly:Azure/bicep-registry-modules:ci_workflows:2026-W41`

- Trigger: **weekly_theme**
- Must state: 2 merged, 4 opened in 2026-W41

**X** — check: pass, length 182 (weighted, links count 23)

```text
What was engineered, week to 11 October 2026: CI workflows. 2 pull requests merged; #7482 removed the last stored-secret sign-in from module deployment tests. https://github.com/Azure/bicep-registry-modules/pull/7482
```

**LinkedIn** — check: pass, length 352

```text
What was engineered in the week to 11 October 2026: the CI workflows.

4 pull requests were opened and 2 merged. #7482 removed the last exception to OIDC sign-in from module deployment tests, and #7507 fixed how the workflow selects the changes of a pull request.

https://github.com/Azure/bicep-registry-modules/pull/7482

#Bicep #AzureVerifiedModules
```

### `weekly:Azure/bicep-registry-modules:test_environment:2026-W41`

- Trigger: **weekly_theme**
- Must state: 3 merged, 4 opened in 2026-W41

**X** — check: pass, length 208 (weighted, links count 23)

```text
What was engineered, week to 11 October 2026: test fixtures and the test environment. 3 pull requests merged, among them a reusable host-image builder for the Azure Local tests, #7491. https://github.com/Azure/bicep-registry-modules/pull/7491
```

**LinkedIn** — check: pass, length 436

```text
What was engineered in the week to 11 October 2026: test fixtures and the test environment.

4 pull requests were opened and 3 merged. #7491 added a reusable host-image builder for the Azure Local module tests, #7505 separated its build and replication regions, and #7458 replaced tenant-specific CI inputs in the test entrypoints of nine modules.

https://github.com/Azure/bicep-registry-modules/pull/7491

#Bicep #AzureVerifiedModules
```

### `weekly:Azure/bicep-registry-modules:other_plumbing:2026-W41`

- Trigger: **weekly_theme**
- Must state: 2 merged, 2 opened in 2026-W41

**X** — check: pass, length 195 (weighted, links count 23)

```text
What was engineered, week to 11 October 2026: 2 metadata fixes merged, a typo that broke README generation for API Management (#7493) and a new module owner entry (#7471). https://github.com/Azure/bicep-registry-modules/pull/7493
```

**LinkedIn** — check: pass, length 342

```text
What was engineered in the week to 11 October 2026: two small changes to module metadata files.

#7493 fixed a typo in the API Management metadata that made the README check fail for any pull request touching that module, and #7471 added a module owner.

https://github.com/Azure/bicep-registry-modules/pull/7493

#Bicep #AzureVerifiedModules
```

### `weekly:Azure/bicep-registry-modules:repository_policy:2026-W41`

- Trigger: **weekly_theme**
- Must state: 1 merged, 1 opened in 2026-W41

**X** — check: pass, length 169 (weighted, links count 23)

```text
What was engineered, week to 11 October 2026: an automation account kept the issue form's module list in sync with the published catalog (#7473). https://github.com/Azure/bicep-registry-modules/pull/7473
```

**LinkedIn** — check: pass, length 294

```text
What was engineered in the week to 11 October 2026: an automation account opened and merged #7473, which keeps the "Module Name" dropdown of the module issue form in line with the published module catalog.

https://github.com/Azure/bicep-registry-modules/pull/7473

#Bicep #AzureVerifiedModules
```

## A voice pass, checked again

A voice pass may change wording, order and person, add a greeting or emoji, within the limits; it may not change a fact. Two voiced versions of posts above, checked with `--check-post VOICED.md --against DRAFTS.md`:

`story:Azure/bicep-registry-modules:train-issue-7459` (x): **pass**

```text
We just published 0.4.0 of our IoT Hub module, avm/res/devices/iot-hub: "Serialized private endpoint deployments to prevent concurrent IoT Hub updates from failing with ETag conflicts." https://github.com/Azure/bicep-registry-modules/tree/avm/res/devices/iot-hub/0.4.0
```

`story:Azure/bicep-registry-modules:train-pr-7466` (x): **FAIL** — {"must_state_missing": ["'not yet published'"], "release_status_mismatch": [{"version": "avm/res/kubernetes-runtime/load-balancer/0.2.0", "view": "declared_not_published", "stated": [{"section": "plumbing", "claim": "published", "sentence": "Merged and on its way: avm/res/kubernetes-runtime/load-balancer 0.2.0 is published now."}]}]}

```text
Merged and on its way: avm/res/kubernetes-runtime/load-balancer 0.2.0 is published now. "Updated the Microsoft.Kubernetes/connectedClusters reference from 2024-01-01 to 2026-05-01." https://github.com/Azure/bicep-registry-modules/pull/7466
```

## Ledger after this week

```json
{
 "kind": "social_ledger",
 "posts": [
  {
   "post_id": "story:Azure/bicep-registry-modules:train-pr-7463",
   "day": "2026-10-05",
   "channels": [
    "x",
    "linkedin"
   ]
  },
  {
   "post_id": "story:Azure/bicep-registry-modules:train-issue-7459",
   "day": "2026-10-06",
   "channels": [
    "x",
    "linkedin"
   ]
  },
  {
   "post_id": "story:Azure/bicep-registry-modules:train-pr-7466",
   "day": "2026-10-09",
   "channels": [
    "x",
    "linkedin"
   ]
  }
 ]
}
(... one row per post approved and published; urls added when known)
```

