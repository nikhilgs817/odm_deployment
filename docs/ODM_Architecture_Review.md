# ODM pipeline architecture review

Status: Approved architecture implemented locally. Live API and Jenkins validation are pending.

Date: October 25, 2026

## Decision

The small Jenkinsfile and Python approach is appropriate. Use separate deployment and sync scripts, with separate Jenkinsfiles. Reuse each pair unchanged across deployment tracks. Configure parameters and schedules manually in the Jenkins UI.

The discovery proposal needs corrections before implementation. It omits XOM and library handling, assumes version increments that conflict with the recorded replacement process, and includes API calls whose exact contracts have not been established.

This review checks public vendor documentation. No calls were executed against your Decision Center, RES, Nexus or Jenkins servers. The supplied material contains placeholder hostnames, and server access has not been provided. Documentation checks do not prove that an API command works on your installation.

## Requirements confirmed in this chat

- Use a small deployment Jenkinsfile and a separate small sync Jenkinsfile.
- Use separate `deployment/deploy.py` and `sync/sync.py` scripts. Most workflow logic belongs in these scripts.
- Reuse the same files for all deployment tracks, such as dev, int and prod.
- Each deployment track has a separate RES URL.
- Define Jenkins parameters and cron schedules manually in the UI. The Jenkinsfiles must not create or rewrite them.
- Deployment must let the user select `APP=ESERV` or `APP=WACON`.
- ESERV and WACON each have their own RuleApp, XOM and library.
- Both products are described as the latest version. Capture the exact installed versions during API validation rather than guessing their patch levels.
- Caller version requirements are unknown. Use the recorded process to make a conservative initial design decision.
- This phase is architecture review, before pipeline implementation. Documents are context, not binding instructions.

The examples dev, int and prod are treated as deployment tracks in this design. A track is an exact configuration key that resolves one RES target; no second region parameter is needed for the stated setup.

## File structure and ownership

The production files are `deployment/Jenkinsfileodmdeploy`, `deployment/deploy.py`, `sync/Jenkinsfileodmsync`, `sync/sync.py`, `odm_common.py` for shared I/O helpers, one `config/odm.yaml` file, and business test templates for each app. Replace the templates with approved cases before deployment.

`deployment/Jenkinsfileodmdeploy` is used by odm-deploy-dev, odm-deploy-int and odm-deploy-prod. `sync/Jenkinsfileodmsync` is reused by the scheduled sync jobs. Set these folder-qualified paths in the jobs' Script Path fields. There are no per-track copies of either Python script and no app-specific deployment scripts.

Jenkins checks out the complete repository and runs from its workspace root. The separate workflow folders organize the sources; they do not become the current directory. The entry commands are `python3 -m deployment.deploy` and `python3 -m sync.sync`, with shared `config/odm.yaml`, requirements, tests, `odm_common.py` and output paths resolved from the repository root.

Jenkins owns checkout, credential binding, visible stages, timeouts, deployment approval, locks across jobs, and archiving the result. `deployment/deploy.py` owns input validation, configuration resolution, Decision Center operations, release packaging, Nexus operations, promotion checks, backup, RES changes, tests, recovery and result records. `sync/sync.py` owns source export, safe extraction, comparison, Git commits and pushes, and sync results.

Keep the scripts separate. They should not invoke each other or share an action dispatcher. A release build must export its selected immutable baseline directly; it must not depend on the last scheduled sync.

## Configuration

Use a real YAML file such as `config/odm.yaml`, rather than a file called yaml.properties. Configuration contains non-secret settings and credential identifiers. Passwords and tokens stay in Jenkins credentials.

Track configuration maps dev, int and prod to their separate RES management URLs, execution URLs, credential references, predecessor tracks and approval rules. It also states whether that track builds new releases: true for the first DEV track and false for higher tracks. Management and HTDS execution endpoints can differ, so store both explicitly. Track configuration also identifies the Decision Center source used for sync or new builds.

App configuration maps ESERV and WACON to their Decision Center service and branch identifiers, deployment configurations, actual RuleApp and ruleset names, XOM names, library names, Nexus artifact coordinates and tests. Allow track-specific identifiers where the systems differ. The Eligibility names in the walkthrough are examples; they are not established ESERV or WACON mappings.

For example, the combination INT and ESERV resolves the INT RES URL and ESERV objects on that server. The same release selected for PROD resolves the PROD URL without changing either the Jenkinsfile or deployment/deploy.py.

The job must be restricted to its intended track. Use a single allowed DEPLOYMENT_TRACK value in each job and appropriately scoped credentials. Python must reject unknown or disallowed track and app values before any write. A dropdown alone is not an access boundary. Pin all jobs to the same reviewed automation revision when releasing pipeline updates.

## Jenkins UI parameters

For deploy jobs, configure these manually:

- `DEPLOYMENT_TRACK`: one allowed value for that job, such as int.
- `APP`: ESERV or WACON.
- `ACTION`: DEPLOY or ROLLBACK, with DEPLOY as the normal default in every job.
- `JAR_VERSION`: the immutable release to create or deploy.
- `BASELINE_ID`: required only for a normal new deployment in the first DEV track.

Python validates the combinations and derives the normal workflow from the selected track. A normal DEV deployment builds and publishes the new JAR_VERSION from BASELINE_ID, then deploys it. A normal INT or PROD deployment downloads the existing JAR_VERSION; these tracks cannot build from Decision Center and do not need its credentials. ROLLBACK downloads a release previously validated on that same target and follows the target's approval rules.

For sync jobs, configure DEPLOYMENT_TRACK with a fixed default and configure cron in the UI. The script can sync both configured apps for that track, so sync does not require an interactive app selection. If separate app-specific sync jobs are preferred, APP can instead be a fixed UI default in each job.

The Jenkinsfiles must contain no parameters block, trigger declaration or job-properties operation that rewrites this UI configuration. Use ordinary choice parameters for APP and DEPLOYMENT_TRACK. Use a string JAR_VERSION for a new DEV release and a dynamic JAR_VERSION choice for existing releases in higher tracks. Scheduled sync jobs use saved defaults and do not depend on dynamic parameter evaluation. Python always validates the supplied values. The [Active Choices documentation](https://plugins.jenkins.io/uno-choice/) describes its dependence on the browser UI.

## Version decision from the walkthrough

Preserve the existing RuleApp and ruleset 1.0 paths for the initial design, and preserve the library name and 1.0 reference shown in the walkthrough. Do not automatically introduce 1.1 or 2.0 service paths. Record and verify the exact XOM resource versions and bindings used by each release.

This is a compatibility decision based on the recording notes. It is not proof that callers explicitly pin 1.0. The walkthrough deploys RuleApp and ruleset 1.0 and emphasizes library 1.0. It also retrieves a description with Latest ruleset version selected; it does not show application caller configuration.

Keep the Nexus release identifier separate from ODM runtime versions. Several immutable releases can intentionally target the same runtime path 1.0. The release digest and deployment record distinguish their contents.

Replacing the same runtime path is not an atomic change across RuleApp, XOM and library. Use a controlled deployment window and the existing means of pausing or draining affected traffic. The pipeline must not promise zero downtime or isolated testing before activation. Validate the interruption and restoration procedure in dev before production use.

Prefer replacement without deleting the existing RuleApp first where the installed API supports it. Do not copy the walkthrough's remove-all-references sequence automatically. If dependency replacement requires removal, inspect all references and limit changes to the selected app. Preserve old XOM bytes and library definitions for recovery.

IBM documents that REPLACE_MERGING_POLICY ignores the RuleApp version policy. Replacement and automatic minor-version creation therefore cannot be combined as proposed in the notes. See [IBM merge policies](https://www.ibm.com/docs/en/odm/9.6.0?topic=management-res-deploy).

## Release bundle and promotion

Store one immutable release ZIP per app and release ID in Nexus. Its contents are the deployable RuleApp JAR, the matching XOM ZIP or embedded XOM content, a manifest, and the required library and ruleset dependency definitions. The wrapper ZIP is a distribution package; do not submit it to RES as a RuleApp JAR.

The manifest records APP, release ID, Decision Center baseline and deployment configuration identifiers, source export digest and Git reference, expected RuleApp and ruleset paths, XOM and library bindings, build and runtime compatibility information, and SHA-256 hashes of the contained artifacts. Identify the exact source of the XOM. If it is built elsewhere, resolve a pinned artifact rather than taking whatever ZIP happens to be in Dev or Downloads.

Inspect what Decision Center actually produces. If it embeds all required XOM content, separate distribution of duplicate XOM bytes is unnecessary, but their bindings and hashes still need verification. A ZIP response must not simply be renamed to jar. IBM documents [embedded managed Java XOMs](https://www.ibm.com/docs/en/odmoc?topic=applications-embedded-managed-java-xom-in-decision-center).

Build and package once in DEV. INT and PROD download the same release ZIP and verify its digest and contents; they do not rebuild or rewrite its JAR. A single package also avoids assuming that multiple Nexus asset uploads form a transaction.

Use different artifact IDs for ESERV and WACON. Disable redeployment of release coordinates. Download and verify a newly published package before declaring it ready. The [Sonatype Components API](https://help.sonatype.com/en/components-api.html) supports multipart Maven uploads; the sample in the discovery notes uploads only a JAR, despite describing a manifest too.

Write a unique deployment receipt after successful target verification. A small Nexus raw repository is suitable for these records. Each receipt identifies APP, release digest, target track and URL, actual runtime paths, prior state, test result, automation revision and Jenkins run. Keep receipts separate from immutable release content.

Promotion requires a trusted successful receipt for the same app and release digest in the preceding track. The proposed policy is previously passed in DEV for INT, and previously passed in INT for PROD. It does not require the release to remain the current deployment in the preceding track. Reject failed, incomplete, rolled-back or explicitly revoked evidence. Recheck eligibility immediately before target changes.

### How higher tracks select the release

The user selects APP and supplies an explicit JAR_VERSION. For example, ESERV release 2026.10.25.1 identifies one immutable Nexus component under the configured ESERV artifact ID. WACON has a separate artifact ID, even if it uses the same release version string. Python derives the complete Nexus coordinates and filename from configuration, rather than accepting an arbitrary URL from the user.

DEV builds that release once, publishes it, deploys it to the DEV RES URL, tests it, and writes a successful DEV record. INT accepts that exact app and release only after verifying the DEV record, downloads and verifies the same bytes, deploys them to the INT RES URL, then tests and records the result. PROD applies the same rule using the INT record and requires production approval. Uploading to Nexus alone does not make a release eligible for promotion.

The release version is the selector; the deployment record is the eligibility evidence. Never choose the highest Nexus version, the newest upload, a SNAPSHOT or a moving latest label automatically. The Nexus release version can be 2026.10.25.1 while the deployed ODM RuleApp and ruleset paths remain 1.0.

Nexus does offer component tags, but Sonatype documents this as a Pro feature. Tags can be optional visible labels such as dev-verified; they should not replace the exact version, digest and deployment evidence. The proposed design does not require that paid feature. See [Sonatype tagging](https://help.sonatype.com/en/tagging.html). Enforce immutable release coordinates using [Disable redeploy](https://help.sonatype.com/en/configurable-repository-fields.html).

JAR_VERSION is an explicit required parameter. In DEV it is a new version supplied for the build. In INT and PROD it is a UI-configured dropdown of eligible existing versions, dependent on APP and DEPLOYMENT_TRACK. Do not silently default to the newest version: require an explicit selection, with a non-deployable Select a version entry first. The same deployment/deploy.py and Jenkinsfile handle both parameter forms.

For that dynamic dropdown, configure Active Choices manually in the UI. Keep its read-only script small: it reads a per-app, per-track eligible-version catalog from the Nexus raw repository. deployment/deploy.py generates the catalog from trusted deployment records after success, under a short catalog-update lock. The catalog contains versions and release digests, with INT entries derived from successful DEV verification and PROD entries from successful INT verification. It is a convenience index; the immutable release and trusted deployment records remain authoritative. Rebuild it from those records if needed. If the catalog cannot be read, show a clear error and do not invent a default.

This adds no third Python workflow or service. A stale catalog can affect what the dropdown shows, but deployment/deploy.py rechecks current eligibility, release identity and digest before writing to RES. Reading the version list happens during the interactive deploy form; cron sync never depends on it. Native production approval displays the resolved APP, JAR_VERSION, target RES URL and digest. Every deployment produces the same visible plan before changes, so the selected release and destination are clear. There is no automatic latest-version selection.

Avoid a reusable deployed-dev.properties marker. It loses deployment history and becomes awkward with immutable coordinates. A Nexus HEAD response proves existence only. If listing releases, filter by repository, group, app and asset type and follow pagination; see the [Sonatype Search API](https://help.sonatype.com/en/search-api.html).

## Deployment flow

1. Resolve the track and app. Validate parameters, endpoint configuration, credentials, artifact completeness and promotion evidence. For a DEV build, resolve the approved immutable baseline and prove the build uses that source.
2. For a new release, export that baseline, record its Git commit or tag, obtain the RuleApp and matching XOM, inspect and package them, and publish and download-verify the immutable release. A baseline being present does not itself prove business approval.
3. Produce a deployment plan naming APP, exact RES target, release digest, fixed runtime paths, dependency changes, tests and recovery package. Obtain the required production approval in Jenkins against this exact plan. The approved digest and target cannot change afterward.
4. Acquire a lock across jobs for the actual target and app objects. Recheck target state and eligibility under the lock. Confirm the controlled deployment window, then export and verify a complete backup of the existing RuleApp, XOM bytes, library content, properties and references. Establish a restore path even for pre-pipeline releases. Stop before writing if backup cannot be verified.
5. Apply dependencies and RuleApp in the order demonstrated to work in DEV. Prefer preparing XOM and library dependencies before activating rules where supported. Verify the resulting bindings and the deployment response; do not assume a successful HTTP code means successful deployment.
6. Retrieve the service description, verify the expected RuleApp, rulesets and XOM bindings, and execute approved app-specific requests with expected results. Test the exact runtime paths and the real execution endpoint. Where RES serves multiple runtime nodes, verify the affected nodes have refreshed; a random response from one load-balanced node is insufficient.
7. On success, save the receipt before reporting complete success. On deployment or test failure, restore the complete previous state and test it again. Record whether recovery succeeded. Retain the lock through verification or recovery.
8. Archive the sanitized plan and result, then remove local temporary files. Do not delete old RES objects automatically in the initial implementation.

Rollback uses the same deployment and verification path with the prior release bundle. Deleting the newly deployed 1.0 object cannot undo an overwrite. A successful restore still leaves the attempted release marked failed.

If a write times out, inspect the actual target state before retrying. Do not blindly retry deployment POSTs or Nexus uploads. If deployment succeeds but receipt writing fails, report that distinct state and reconcile it; do not report that nothing was deployed. Preserve a local result for manual reconciliation.

## Sync flow

The UI cron starts sync/sync.py with the track defaults. It resolves the configured Decision Center sources, exports each selected branch through the verified API, checks completeness and source consistency, safely extracts the archive, compares it with the corresponding Git tree and commits only meaningful changes. Commit metadata identifies source, app and observed revision or snapshot. Failed or empty exports must never erase the previous tree.

Keep each source in an explicit track/app location or branch. Serialize writers to a shared Git branch, or use separate branches with bounded fast-forward retries. Do not force-push. If several track jobs read the same Decision Center source, identify one canonical capture or separate the destinations deliberately; do not let jobs overwrite each other's export.

Ignore ZIP ordering and container timestamps for comparisons. Preserve file contents unless a specific normalization has been proven safe. Blanket timestamp removal or XML whitespace changes can hide real rule changes. Retain a hash of the original export. Validate snapshot consistency rather than claiming an export of a moving branch is automatically immutable.

A 15-minute poll captures observed states. It cannot capture every edit made between polls, and Git export history does not replace Decision Center's own audit history.

## API findings

### RES upload and inventory

The documented upload is POST to the RuleApp collection with binary archive content. Its query parameter is `version`, not `versioning`. The API reference also lists `resVersion` and `libVersion`; their use for the actual XOM packaging must be checked. The documented ruleset collection path in the notes is supported, but its returned contents must match the expected app. See the [IBM RuleApp REST reference](https://www.ibm.com/docs/en/odmoc?topic=SS7J8H%2Fcom.ibm.odm.dserver.rules.ref.res%2Fhtml%2Frestapi%2Fhtml%2Fruleapps_ecmapi.htm).

Treat response codes according to each endpoint. IBM documents logical errors returned with 202 for some object operations and missing resources returned with 204. Parse response bodies and inspect resulting state.

The management base `/res/apiauth/v1` is documented for preemptive basic authentication. Context roots and authentication still depend on the installation. See [IBM resource URIs](https://www.ibm.com/docs/en/odm/9.6.0?topic=api-resource-uris). Add XOM and library operations to the validated API inventory; a RuleApp-only client is incomplete.

### HTDS invocation

The draft omits `/v1`. The documented fully specified execution path is `/DecisionService/rest/v1/{ruleapp}/{ruleappVersion}/{ruleset}/{rulesetVersion}`. Use the configured execution host and JSON content type. See [IBM service invocation](https://www.ibm.com/docs/en/odm/9.5.0?topic=services-calling-decision-service-by-using-rest-api).

The walkthrough retrieves a JSON service description; it does not demonstrate a business-rule invocation. Both apps need approved requests and expected outputs before the pipeline can claim deployment validation.

### Decision Center source and build

Public documentation establishes the export and deployment download capabilities, but does not establish the exact `baselineId` selectors shown in the notes. A branch ID must not be assumed interchangeable with a baseline or snapshot ID. `/branches/{branchId}/snapshots` also remains unverified. Use the installed Decision Center API specification to settle these contracts and validate the parent service and branch of every selected snapshot. IBM directs users to the installed API interface in its [Decision Center API overview](https://www.ibm.com/docs/en/odm/9.0.0?topic=api-overview).

Do not assume that every installation needs a separate build POST, or that download always returns a prebuilt JAR. IBM's [Decision Center build example](https://community.ibm.com/community/user/blogs/guilhem-molines1/2020/12/15/the-odm-and-devops-series-post-3) describes a download operation that generates the RuleApp. Confirm baseline binding, build completion, report handling and archive contents on this installation before choosing the implementation.

### Nexus

The proposed components upload, repository download and search endpoint families are documented for Nexus 3. Maven repositories also support direct PUT; see [Sonatype upload methods](https://support.sonatype.com/hc/en-us/articles/115006744008-Repository-How-can-I-programmatically-upload-files-into-Nexus-3). Confirm the installed repository format, deployment policy, permissions and exact search fields. No live Nexus validation has occurred.

## Validation required before pipeline implementation

First perform read-only checks from the intended Jenkins agent: capture exact versions and API specifications, validate TLS and authentication, list both apps and their deployment configurations and baselines, read RES inventory including XOM and library bindings, and test Nexus reads. Do not disable TLS verification.

Then use a disposable DEV target and test repository to verify writes and recovery. These are separate from production changes:

- Prove the selected baseline remains the build source after branch head changes.
- Inspect actual RuleApp and XOM outputs and prove ESERV and WACON mappings independently.
- Test complete package upload, download and digest verification; reject different bytes at an existing release coordinate.
- Reproduce fixed-path replacement without broad reference deletion and verify every dependency.
- Test an intentional validation failure and prove complete restoration of the previous working release.
- Simulate a lost response and a second run; reconcile state without creating unintended versions or overwriting a release.
- Verify that an ESERV deployment leaves WACON objects unchanged and vice versa.
- Verify predecessor checks, production approval, locks across jobs and receipt failure handling.
- Run sync twice without a source change, then with a change, and confirm correct commit behavior; verify cron works without opening the parameter form.

Record sanitized requests, expected and actual responses, and resulting object state. Only then label commands verified on your installation. The actual URLs, service account access, app identifiers, XOM provenance and expected business test results are still required for that work.

## Error messages

Use short factual messages that state what happened and, when needed, the next action:

- ESERV was not deployed. Its XOM file is missing.
- This release has not passed INT testing.
- The release checksum does not match. Deployment stopped.
- RES did not confirm the deployment. Check its state before trying again.
- Deployment failed. The previous release was restored and tested.
- Deployment failed, and the previous release could not be restored. Check ESERV on INT.
- Deployment passed, but the deployment record could not be saved. Check RES before running again.

Keep detailed diagnostics in a sanitized result artifact. Do not expose passwords, tokens or full business payloads in console output.

## Alternatives and consequences

The selected fixed-path design preserves the demonstrated service contract and supports separate app deployments. It requires a controlled interruption and tested restoration because replacing dependencies and rules is not transactional.

Deploying new RuleApp versions alongside old versions could improve rollback later, but requires verified caller routing and release activation behavior. It is not the initial choice because those facts are missing and the recording shows replacement at 1.0.

Keeping all logic in Jenkins would make the pipeline larger and harder to test. Separate deployment/deploy.py and sync/sync.py preserve the requested small Jenkinsfiles while keeping both workflows understandable.

The corrected architecture is suitable to take into API validation. The original discovery commands and delete-based rollback are not ready to implement unchanged.
