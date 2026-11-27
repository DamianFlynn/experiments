# Azure Verified Modules for Bicep — October 2026

October 2026 brought one breaking change to plan for: the Data Factory module replaced its eleven Git parameters with a single `gitConfiguration` object in version 0.13.0. The Dev Test Lab module declared the deprecation of its `secret` child module, and a group of Azure Local and Arc modules moved to newer API versions. No new module was published; three proposed ones are waiting for review. Most of the month's merged work was plumbing (49 of 64 classified pull requests), much of it CI and test-environment work, and 12 people opened their first pull request or issue in the repository.

## Breaking changes and deprecations

**Declared by the maintainers**

- **`avm/res/data-factory/factory` 0.13.0** (published on 14 October 2026): the eleven `git*` parameters, `gitConfigureLater` included, were removed in favour of one `gitConfiguration` object, and "`collaborationBranch` and `rootFolder` no longer default to `'main'` and `'/'`." If you set a Git repository, move its values into `gitConfiguration` (the module's changelog shows the before and after). If you never did: "Deployments that relied on `gitConfigureLater` defaulting to `true` require no change - omitting `gitConfiguration` preserves the existing behaviour." ([#7360](https://github.com/Azure/bicep-registry-modules/pull/7360); the story is below.)
- **`avm/res/dev-test-lab/lab` 0.6.0** (declared in the changelog; not yet published as of 31 October 2026): "Deprecated `avm/res/dev-test-lab/lab/secret` as no longer supported by Resource Provider". A deprecation marker file was added for that child module, and the `secrets` parameter is no longer in the module's `main.bicep`; check before upgrading if you pass it. ([#7454](https://github.com/Azure/bicep-registry-modules/pull/7454))

**Detected in the code, not declared: check before upgrading**

- `avm/res/hybrid-container-service/provisioned-cluster-instance` 0.3.0 (declared, not yet published): the `hciSecurityProfile` parameter is no longer in `main.bicep`. ([#7439](https://github.com/Azure/bicep-registry-modules/pull/7439))
- `avm/res/devices/iot-hub` 0.4.0: the child module `avm/res/devices/iot-hub/consumergroup` is now found at `avm/res/devices/iot-hub/event-hub-endpoint/consumergroup`. ([#7469](https://github.com/Azure/bicep-registry-modules/pull/7469))
- `avm/res/db-for-my-sql/flexible-server` 0.11.2 (declared, not yet published): the child module `avm/res/db-for-my-sql/flexible-server/advanced-threat-protection` is now found at `avm/res/db-for-my-sql/flexible-server/advanced-threat-protection-setting`, published as 0.1.0. ([#7454](https://github.com/Azure/bicep-registry-modules/pull/7454))

<!-- omitted: new_modules — no module is new in the window (module_changes.summary.new_modules is empty) -->

## What changed in existing modules

- **Azure Local and Arc modules on newer API versions:**
  - `avm/res/hybrid-compute/gateway` and `avm/res/hybrid-compute/license`, both 0.2 → 0.3.0, to API version `2026-07-15` ([#7434](https://github.com/Azure/bicep-registry-modules/pull/7434), [#7435](https://github.com/Azure/bicep-registry-modules/pull/7435)).
  - `avm/res/kubernetes/connected-cluster` 0.2 → 0.3.0, "from the preview `2026-02-01-preview` to the stable `2026-05-01`" ([#7431](https://github.com/Azure/bicep-registry-modules/pull/7431)); `avm/res/kubernetes-runtime/load-balancer` 0.1 → 0.2.0 references it with `2026-05-01` (declared, not yet published; [#7466](https://github.com/Azure/bicep-registry-modules/pull/7466)).
  - `avm/res/hybrid-container-service/provisioned-cluster-instance` 0.3.0 (declared, not yet published) "Kept `Microsoft.HybridContainerService/provisionedClusterInstances` on the stable `2024-01-01`, as Azure Local's AKS Arc extension doesn't accept `2026-04-01-preview` for creating clusters at the time of this release" ([#7439](https://github.com/Azure/bicep-registry-modules/pull/7439)).
- `avm/res/network/network-security-group` 0.5.4: a newer API version, and corrected parameter descriptions now say "that `securityRules` replaces all custom rules of the Network Security Group" ([#7496](https://github.com/Azure/bicep-registry-modules/pull/7496)).
- `avm/res/event-hub/namespace` 0.15.2: "Fixed `eventhubs[].captureDescription` so that setting `enabled` to `false` is honored instead of omitting the capture configuration." ([#7499](https://github.com/Azure/bicep-registry-modules/pull/7499))
- `avm/res/devices/iot-hub` 0.3 → 0.4.0: "Serialized private endpoint deployments to prevent concurrent IoT Hub updates from failing with ETag conflicts." ([#7469](https://github.com/Azure/bicep-registry-modules/pull/7469))
- `avm/res/sql/managed-instance` 0.5.1 was published; its changelog gained no entry in October 2026. ([#7444](https://github.com/Azure/bicep-registry-modules/pull/7444))
- The Data Factory, Dev Test Lab and MySQL flexible server changes are listed under breaking changes above.

Also updated: `avm/res/azure-stack-hci/virtual-machine-instance` 0.3.0, `avm/res/desktop-virtualization/host-pool` 0.8.2 (adds the `MultiplePersistent` load-balancer type), `avm/res/insights/scheduled-query-rule` 0.6.1 (adds the `SimpleLogAlert` kind), `avm/ptn/network/private-link-private-dns-zones` 0.7.4 (adds the Redis private DNS zone) and `avm/res/api-management/service` 0.15.0 (published; no changelog entry in October 2026).

## Stories

<!-- story: train-issue-7503 -->
**Keeping secrets secure when a module hands them to its child modules (#7472): a repository-wide checker was dropped after review, and the PR now waits on test-environment fixes.**

On 6 October 2026 andyleejordan opened [#7472](https://github.com/Azure/bicep-registry-modules/pull/7472); on 7 October 2026 its title changed from "fix: avm/res/api-management/service secure cache connection strings" to "fix: secure credential inputs and parent UDT properties" ([rename](https://github.com/Azure/bicep-registry-modules/pull/7472#event-32722310535)). Besides the module changes, it carried a repository-wide check under `utilities/tests/pipelines`, with a `baseline.json` of existing findings.

AlexanderSehr reviewed it on 18 October 2026. He asked for the tests of every updated workflow to be run and their badges attached, as the contribution guidelines ask ([comment](https://github.com/Azure/bicep-registry-modules/pull/7472#discussion_r4307010888)). Of `baseline.json` he asked "Is this a list of all secure inputs in the repository?" and noted it "would appear to be a prime candidate for running stale within a week" ([comment](https://github.com/Azure/bicep-registry-modules/pull/7472#discussion_r4307030621)); seven more comments were on the check's files and test script.

On 21 October 2026 the author agreed it would go stale and wrote "I've pulled the checker out of this PR", leaving "just the `@secure()` hardening" ([reply](https://github.com/Azure/bicep-registry-modules/pull/7472#discussion_r4339443374)). On 29 October 2026 the author wrote that the replacement, a compiler rule, is "still a draft, not an available compiler feature" ([reply](https://github.com/Azure/bicep-registry-modules/pull/7472#discussion_r4408579798)). The PR's commits in the record were all committed after 31 October 2026, so the record does not show the removal itself.

The badges wait on other work: the deployment tests "fail on test-environment problems", worked on in #7449, "plus some fixture fixes in #7527" ([reply](https://github.com/Azure/bicep-registry-modules/pull/7472#discussion_r4339442988)).

**Where it stands.** #7472 is open with no review verdict yet; the author replied last, on 29 October 2026, so by the record's sequence of events the next move is with the reviewer, AlexanderSehr. The checks of #7472 were read only for commits made after 31 October 2026, so its head's results at the window's end are not in the record. #7527 is open, its checks failing on the head commit; #7449 is open. #7503, the `fluxConfigurations` description typo the author noticed while reviewing #7472, is open. Reviewing #7527 and #7449 helps this land.
<!-- /story -->

<!-- story: train-issue-7342 -->
**Data Factory 0.13.0: one `gitConfiguration` object replaces eleven Git parameters, after a Git setup passed to the module was being dropped.**

On 11 September 2026 sajwvanzundert reported that a Git configuration given to `avm/res/data-factory/factory` 0.12.0 reached the deployment, but "the config in data factory is then wiped and needs to be set again" ([#7342](https://github.com/Azure/bicep-registry-modules/issues/7342)). TomazMlakar opened [#7360](https://github.com/Azure/bicep-registry-modules/pull/7360) on 17 September 2026 with a fix that sent only the properties belonging to the configured repository type.

Reviewing it on 18 September 2026, AlexanderSehr asked about the `gitConfigureLater` switch in `main.bicep`: "Should it not be up to the user to define the configuration simply by providing it or not?" ([comment](https://github.com/Azure/bicep-registry-modules/pull/7360#discussion_r4046771344)). On 1 October 2026 the author replied "Refactored" ([reply](https://github.com/Azure/bicep-registry-modules/pull/7360#discussion_r4155414876)), and the commit "Updates after review" removed `gitConfigureLater` and the flat `git*` parameters from `main.bicep` and added one nullable `gitConfiguration` parameter typed against the resource provider's repository configuration ([commit](https://github.com/Azure/bicep-registry-modules/pull/7360/commits/84df76c8dffcb2c6b22f3bfd3436395ae00177bc)). The changelog declared the parameter change breaking. AlexanderSehr approved and merged the PR on 6 October 2026 ([approval](https://github.com/Azure/bicep-registry-modules/pull/7360#pullrequestreview-5428039248)).

The release did not follow at once. On 12 October 2026 AlexanderSehr reopened the issue: "the PR was merged yet the validation workflow failed (due to a naming conflict that has nothing todo with the PR)", so the update was not published ([comment](https://github.com/Azure/bicep-registry-modules/issues/7342#issuecomment-6123115703)). TomazMlakar's [#7501](https://github.com/Azure/bicep-registry-modules/pull/7501) renamed the test's dependency resources; AlexanderSehr approved it on 14 October 2026 ([approval](https://github.com/Azure/bicep-registry-modules/pull/7501#pullrequestreview-5521333112)).

**What landed.** Version 0.13.0, tagged on 14 October 2026, with four child modules republished as 0.1.2. The changelog: "Replaced the `gitConfigureLater` flag and the flat `git*` parameters with a single optional `gitConfiguration` parameter. Omitting it deploys the Data Factory without a Git repository configuration." #7360 is merged, #7501 is merged, and #7342 is closed. The checks passed on the head commit of both PRs.
<!-- /story -->

More discussions from October 2026, in short:

- NetApp account: an `activeDirectorySite` input for Active Directory connections, approved and not yet merged ([#6891](https://github.com/Azure/bicep-registry-modules/issues/6891)).
- Scheduled query rules: support for the `SimpleLogAlert` kind, shipped in 0.6.1 ([#7345](https://github.com/Azure/bicep-registry-modules/issues/7345)).
- SQL managed instance: the maximum allowed for `storageSizeInGB`, fixed by merged pull requests ([#7344](https://github.com/Azure/bicep-registry-modules/issues/7344)).
- Cosmos DB: an `enableAllVersionsAndDeletesChangeFeed` setting, in progress ([#7156](https://github.com/Azure/bicep-registry-modules/issues/7156)).
- Automation account: user-defined runtime environments, in review ([#7530](https://github.com/Azure/bicep-registry-modules/pull/7530)).
- Service endpoints in `subResourceWrapper.bicep`, open ([#6483](https://github.com/Azure/bicep-registry-modules/issues/6483)).

## People

**First contributions.** 12 people opened their first pull request or issue in the repository. Four landed merged work:

- Arjen Huitema (@arjenhuitema, Microsoft) updated the network security group module to 0.5.4 ([#7496](https://github.com/Azure/bicep-registry-modules/pull/7496)) and was added as its owner ([#7471](https://github.com/Azure/bicep-registry-modules/pull/7471)); a first SQL virtual machine module is in review ([#7485](https://github.com/Azure/bicep-registry-modules/pull/7485)).
- Nikolaj Petersen (@NPetersenDK) added the Redis private DNS zone to the private DNS zones pattern ([#7558](https://github.com/Azure/bicep-registry-modules/pull/7558)).
- @Theuno fixed the disabled capture setting in the Event Hub namespace module ([#7499](https://github.com/Azure/bicep-registry-modules/pull/7499)).
- Andy Jordan (@andyleejordan, Microsoft) fixed a documentation typo in the API Management module ([#7493](https://github.com/Azure/bicep-registry-modules/pull/7493)) and opened the secure-inputs work in the story above.

First pull requests still open: Chris Bragg (@chbragg, Microsoft), SQL server diagnostic settings ([#7448](https://github.com/Azure/bicep-registry-modules/pull/7448)). From the community (by their profile), smerek (@smereczynski), VM scale set gallery applications ([#7552](https://github.com/Azure/bicep-registry-modules/pull/7552)), and Charlie Ward (@wardch), container app job identity settings ([#7524](https://github.com/Azure/bicep-registry-modules/pull/7524)). First issues: @col1582 ([#7489](https://github.com/Azure/bicep-registry-modules/issues/7489)), brian.pieper (@kt-pieper) ([#7541](https://github.com/Azure/bicep-registry-modules/issues/7541)), @Miteshc1 ([#7565](https://github.com/Azure/bicep-registry-modules/issues/7565)), @Torrer93 ([#7551](https://github.com/Azure/bicep-registry-modules/issues/7551)). Stephen Just (@stephenjust, Microsoft) too ([#7461](https://github.com/Azure/bicep-registry-modules/issues/7461)).

**Welcome back.** After more than 3 months away, Vincenzo Sciarra (@reikor0x, Microsoft) landed the host pool change ([#7502](https://github.com/Azure/bicep-registry-modules/pull/7502)). Felipe Binotto (@fbinotto), also back after more than 3 months, opened two NetApp fixes ([#7549](https://github.com/Azure/bicep-registry-modules/pull/7549), [#7550](https://github.com/Azure/bicep-registry-modules/pull/7550)). After more than 6 months, @gpacetti opened the automation account work ([#7530](https://github.com/Azure/bicep-registry-modules/pull/7530)). After more than 9 months, Preston Alvarado (@coolhome) ([#7540](https://github.com/Azure/bicep-registry-modules/issues/7540)) and @dfwlonghorn ([#7554](https://github.com/Azure/bicep-registry-modules/issues/7554)) opened issues, as did Sidney Andrews (@seesharprun) ([#7562](https://github.com/Azure/bicep-registry-modules/issues/7562)).

**Who did the work.** Of the 64 pull requests merged by people, 51 (80%) came from people labelled Microsoft, 12 (19%) from people of unknown affiliation and 1 (2%) from the community. That one is the `SimpleLogAlert` fix by Maarten Balliere (@balliere) ([#7346](https://github.com/Azure/bicep-registry-modules/pull/7346)). Of 61 reviews, 43 (70%) were by people labelled Microsoft and 18 (30%) by people of unknown affiliation.

**Maintainers.** Alexander Sehr (@AlexanderSehr) reviewed 21 pull requests and merged 5 by others; Jack Tracey (@jtracey93) reviewed 16 and merged 2; @chirag1603 reviewed 7 and merged 7, the Azure Local and Arc updates by @abhisheksharma-ops; Jared Holgate (@jaredfholgate) authored 44 merged pull requests, 43 of them plumbing. Thank you all.

## Coming up and stuck

32 open pull requests had activity in October 2026.

- **Likely next:** the NetApp `activeDirectorySite` input is approved with no later change request and checks passing on its head commit ([#7032](https://github.com/Azure/bicep-registry-modules/pull/7032)). It is not ready to merge yet: by the record's sequence of events, its author's latest comment asks someone else to respond.
- **New modules proposed:** `avm/res/azure-stack-hci/storage-container` ([#7539](https://github.com/Azure/bicep-registry-modules/pull/7539)) and `avm/res/azure-stack-hci/gallery-image` ([#7560](https://github.com/Azure/bicep-registry-modules/pull/7560)) are waiting for a first review; the SQL virtual machine module ([#7485](https://github.com/Azure/bicep-registry-modules/pull/7485)) is waiting for a reviewer.
- **Checks failing on the head commit:** the end-to-end test fixes [#7449](https://github.com/Azure/bicep-registry-modules/pull/7449) (`Validate e2e resource names offline`), [#7483](https://github.com/Azure/bicep-registry-modules/pull/7483) and [#7527](https://github.com/Azure/bicep-registry-modules/pull/7527) (deployment validation), and the Cosmos DB change feed setting [#7494](https://github.com/Azure/bicep-registry-modules/pull/7494) (`Validate PR title`).
- **Waiting for a first review:** 11 pull requests, among them the two proposed modules.

How to help: review a pull request waiting for a first review, or try one of the proposed modules and comment on it.

## Plumbing

49 of the 64 classified merged pull requests (77%) were plumbing: 39 touched only CI files, the rest documentation, tests or repository-wide sweeps. The widest were [#7458](https://github.com/Azure/bicep-registry-modules/pull/7458) (test fixtures look up built-in provider principals), [#7454](https://github.com/Azure/bicep-registry-modules/pull/7454) (README generation) and [#7525](https://github.com/Azure/bicep-registry-modules/pull/7525) (reserved Azure Local test subscriptions).

## By the numbers

- 65 pull requests merged (64 by people), 99 opened; 40 issues opened.
- 29 modules touched, 17 with a change a user can see; no new module; 2 with a declared breaking change; 19 version tags (modules and child modules) published.
- 23 people opened a pull request or issue: 12 for the first time, 6 back after a gap.
- 229 people in the month's records: 54 labelled Microsoft, 33 community, 142 (62%) unknown.

*Coverage.* Records as of 31 October 2026; titles and labels as gathered on 27 November 2026. Unknown affiliation is never counted as community. The people registry covers only October 2026, so tenure and roles in other repositories are not given. A version not yet published by 31 October 2026 may have been tagged since. For some pull requests only the latest commits, check results, review threads or events were read.
