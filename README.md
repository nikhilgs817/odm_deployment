# ODM deployment and sync pipelines

Use `deployment/Jenkinsfileodmdeploy` with `deployment/deploy.py` for deployment and `sync/Jenkinsfileodmsync` with `sync/sync.py` for scheduled exports. Reuse the same files in every track. The job's UI-defined `DEPLOYMENT_TRACK` selects its configuration and separate RES URL. `APP` selects ESERV or WACON.

The supplied configuration is a template. Real URLs, app identifiers, business tests and installed API contracts are not known. No live IBM ODM, RES or Nexus calls have been verified. The scripts reject incomplete configuration and unverified adapters instead of attempting a guessed deployment.

## Files

- `deployment/Jenkinsfileodmdeploy`: plan, prepare, review, lock, deploy and refresh version choices.
- `deployment/deploy.py`: DEV build and publication; higher-track promotion; backup, deployment, tests and recovery.
- `sync/Jenkinsfileodmsync`: plan and lock the Git destination, then run the scheduled sync.
- `sync/sync.py`: export Decision Center sources, compare their file content and commit meaningful changes.
- `odm_common.py`: shared HTTP, configuration and archive safety helpers. The two workflows have separate entrypoints and do not call each other.
- `config/odm.yaml`: non-secret track and app configuration.
- `deployment/jenkins/eligible-versions.groovy`: optional UI script for the higher-track version dropdown.
- `tests/fixtures/`: business-request and expected-result templates to replace with approved cases.

Deployment and sync have separate folders. Shared configuration, requirements, tests and `odm_common.py` remain at the repository root. Jenkins checks out the whole repository into its workspace and keeps that workspace root as the current directory; the Script Path identifies the Jenkinsfile but does not change the working directory. Do not wrap the Python stages in `dir('deployment')` or `dir('sync')`.

Run the workflows from the repository root as modules: `python3 -m deployment.deploy` and `python3 -m sync.sync`. Their normal paths remain `config/odm.yaml` and `output/` relative to that root. Fixture paths in YAML are also relative to the repository root.

## Jenkins agent and plugins

The Jenkinsfiles use a Linux or Unix agent labelled `odm-python`, with Python 3.10 or later and Git installed. Install `requirements.txt` in the agent's Python environment before running the jobs. Keep the agent image and dependencies under normal release control; the pipeline does not install packages on every deployment.

Required Jenkins plugins are Pipeline, Credentials Binding, Pipeline Utility Steps (`readJSON`) and Lockable Resources. Active Choices is needed for the dynamic version dropdown. The scripts themselves do not require Jenkins to run.

Point every job at the same reviewed automation revision. Restrict write access to the code and configuration used by production jobs. Users choosing versions do not need permission to change pipeline code or credentials.

## Configure the jobs in the UI

Create deployment jobs such as `odm-deploy-dev`, `odm-deploy-int` and `odm-deploy-prod`. Set Pipeline definition to **Pipeline script from SCM** and Script Path to **deployment/Jenkinsfileodmdeploy**. Include each job's exact full name, including any folder path, in the corresponding track's `deployment_jobs` configuration.

Configure these parameters manually under **This project is parameterized**:

- `DEPLOYMENT_TRACK`: Choice parameter with one allowed value for that job, such as `int`.
- `APP`: Choice parameter containing `ESERV` and `WACON`.
- `ACTION`: Choice parameter containing `DEPLOY` first and `ROLLBACK` second.
- `JAR_VERSION`: String parameter in DEV for the new immutable version. In higher tracks, use the Active Choices setup below to select an existing eligible version. Leave a new DEV version blank by default.
- `BASELINE_ID`: String parameter, blank by default. Required for a new DEV build; leave it blank for higher-track deployment and rollback.

Create sync jobs with Script Path **sync/Jenkinsfileodmsync**. Set the exact full job names in `sync_jobs`. Configure `DEPLOYMENT_TRACK` with one fixed default and, optionally, `APP` with a fixed app selection. Without `APP`, the script syncs that track's configured `sync_apps` list. Configure **Build periodically** in the UI; `H/15 * * * *` runs approximately every 15 minutes without starting all jobs together.

Neither Jenkinsfile declares parameters, triggers or job properties. A scheduled sync uses saved defaults and never depends on the interactive version dropdown.

## Credentials

Create these Username with password credentials, using the same IDs in each job's Jenkins folder so the files remain identical:

- `odm-nexus`: release reads, DEV release publication where allowed, and the track's deployment records, recovery packages, in-progress state and catalog updates.
- `odm-res`: management and execution access to that folder's RES target only.
- `odm-dc`: Decision Center API access. Required only for DEV builds and sync jobs.
- `odm-git`: Git access to the configured source-history repository. Required for DEV source capture and sync.
- `odm-catalog-reader`: read-only access to the eligible-version catalogs. Used by the UI helper on the Jenkins controller, not by the deployment script. It must not have access to recovery packages, source archives or business-test payloads.

The Jenkinsfiles bind these to `NEXUS_USER`/`NEXUS_PASS`, `RES_USER`/`RES_PASS`, `DC_USER`/`DC_PASS` and `GIT_USER`/`GIT_PASS`. Do not put secret values in YAML, URLs, build parameters or command-line arguments. Higher-track preparation does not bind Decision Center or Git credentials.

Scope Nexus permissions to the required repositories and paths, and RES credentials to the correct track. A fixed UI choice helps prevent mistakes; credential permissions and configured full-job-name checks enforce the intended boundary.

TLS verification remains enabled. If the systems use an internal certificate authority, configure the CA bundle instead of disabling certificate checks. Jenkins' Java truststore also needs that CA for the controller-side dropdown request.

## Version selection and promotion

`JAR_VERSION` is the explicit release selector. The pipeline never chooses `latest`, the highest version or the newest upload. ESERV and WACON have distinct Nexus artifact IDs, so the same version string can identify separate releases without mixing apps.

DEV builds once from the selected immutable baseline, publishes the complete release, downloads it for verification, deploys to DEV and tests it. INT downloads that exact release only after checking successful DEV evidence. PROD requires successful INT evidence and approval. This policy means **previously passed** in the preceding track, rather than necessarily **currently deployed** there.

The release package contains the final RuleApp JAR, matching XOM content and a manifest describing library/ruleset dependencies and SHA-256 hashes. Higher tracks deploy the same package contents without rebuilding or rewriting the JAR. The distribution ZIP is unpacked locally; it is not submitted to RES as a RuleApp JAR.

Keep the Nexus release repository configured with **Disable redeploy**. Once a version exists, different bytes must get a new version. A deployment record is created only after deployment verification; artifact existence by itself does not establish promotion eligibility.

Promotion evidence must be a complete receipt in the script's supported schema. Checks include the canonical release path, run and plan identifiers, automation and content hashes, and successful business tests on every configured execution URL for the preceding track. A partial imported record or a marker saying deployed is not enough. Trust depends on protecting the credentials and repository paths allowed to write these records.

Use a separate Nexus raw repository for deployment records, recovery packages, in-progress state and catalogs. Normal catalog paths are `catalogs/{track}/{APP}.json`; rollback catalogs use `catalogs/{track}/{APP}-rollback.json`. The raw repository needs **Allow redeploy** for catalog and in-progress-state replacement; receipts and backups use unique per-run paths and the scripts refuse to overwrite different content. Restrict account paths and administration permissions accordingly. Release immutability is enforced separately by the release repository.

## Dynamic JAR_VERSION dropdown

For INT and PROD, create an **Active Choices Reactive Parameter** manually:

1. Name it `JAR_VERSION` and choose **Single Select**.
2. Set **Referenced parameters** to `APP,DEPLOYMENT_TRACK,ACTION`.
3. Copy `deployment/jenkins/eligible-versions.groovy` into its Groovy Script field.
4. Replace only its Nexus URL and repository name, and create the item-scoped `odm-catalog-reader` credential.
5. Have a Jenkins administrator review and approve this trusted controller script. It uses the Credentials API and HTTPS; do not turn off Jenkins script protection globally.
6. Use `return ['Version list is unavailable:selected']` as the fallback script.

The first choice is **Select a version**, which cannot pass backend validation. A failure to read the catalog displays **Version list is unavailable** instead of choosing a default release. The script reads one bounded JSON document from the configured Nexus host and does not follow redirects.

`deployment/deploy.py` refreshes the catalogs from deployment records after successful execution under a short catalog lock. The dropdown is a convenience index. Python rechecks app, release digest and authoritative promotion evidence before RES writes, even if a stale dropdown or an API caller supplied the version.

For rollback, set `ACTION=ROLLBACK` and choose the prior version explicitly. The dropdown reads the rollback catalog, which contains releases previously validated on that target. The backend rechecks that evidence independently.

The [Active Choices documentation](https://plugins.jenkins.io/uno-choice/) describes its browser dependency. The dropdown is intended for people using **Build with Parameters**. Non-interactive deployment triggers must supply explicit valid parameter values; scheduled sync never uses it.

## Review and approval

Before deployment, Jenkins prints the selected app, release version, track, exact RES URL and SHA-256. Production approval displays the same values and restricts approval to the configured submitters. The approver's identity and approved plan hash are passed to Python. A changed plan, configuration or release cannot reuse the approval.

The target lock is acquired for execution and recovery; jobs targeting the same objects share the same lock name. This is separate from the job-level `disableConcurrentBuilds` option. Catalog updates use a separate short lock. Sync jobs lock the actual Git URL and branch, coordinating jobs that write to the same destination.

## Fixed service paths and recovery

The initial design preserves the demonstrated RuleApp, ruleset and library `1.0` paths. `JAR_VERSION=2026.10.25.1` identifies release content in Nexus; it does not turn the ODM service path into `2026.10.25.1`.

Replacing the same path is not atomic across RuleApp, XOM and library. Use a controlled deployment window and the existing means of pausing or draining callers. No zero-downtime guarantee is made. Before each RES write, the backend must capture and verify the complete previous state. An incomplete backup stops the deployment.

Existing XOM bytes are never overwritten or deleted. If a release needs different XOM bytes, publish a new immutable XOM version, discover the actual assigned version and bind the app's stable library `1.0` to it. Keep old resources for recovery. This preserves the service and library paths shown in the recording while avoiding destructive XOM replacement. The installed publish contract must be verified to preserve existing resource versions.

Before changing the active runtime, the script packages the verified previous RuleApp, XOM bytes, library metadata and recovery tests, then uploads and download-verifies `backups/{track}/{APP}/{run_id}.zip` in the raw repository. The result and in-progress state contain `recovery_package_path` and `recovery_package_sha256`. This package remains available if the agent or its workspace is lost. Restrict its read access because it contains executable rule artifacts and business-test data.

On a completed-write deployment or business-test failure, recovery restores the previous RuleApp and its exact XOM/library state, then tests it again. Deleting the new `1.0` object would not recover overwritten content. The initial implementation does not run remove-all-references or automatic old-version deletion.

The initial adapter expects an existing RuleApp and library with one direct managed XOM ZIP reference, matching the walkthrough. It stops on an empty target or additional/unknown dependencies. Initial server bootstrap and more complex dependency graphs require a separately verified adapter; they are not handled by deleting objects automatically.

A lost response to a write is not retried blindly. When completion of a library or RuleApp management write is unknown, the script records `inspection_required` and leaves the in-progress state as `needs_attention`. It does not race an automatic restore against a server operation that may still be running. Inspect and settle the server state before another write. A deployment that passed but could not save its receipt is reported distinctly so operators do not assume no changes occurred. Hard agent loss, forced termination or a Jenkins timeout can interrupt recovery itself. The script attempts recovery when it can handle a stop signal, but cannot recover from a process that no longer runs.

The backend writes `inflight/{track}/{APP}.json` in the raw repository after verifying backup and prior behavior, before target changes. It marks this state `finished` after saving a successful deployment receipt or completing verified recovery. Any previous state other than `finished` blocks another target write.

If a run stops with unresolved state, follow this reconciliation procedure:

1. Keep affected traffic controlled. Read the plan, result and in-progress marker; identify the exact app, target, selected release and previous backup. Download `recovery_package_path` from the raw repository and verify its SHA-256 against `recovery_package_sha256` before using it.
2. If a management write was ambiguous, first establish that the server operation has settled. Inspect the actual RuleApp content hash, library content and XOM content/version on RES. Run the corresponding business tests on all configured execution endpoints. Do not infer the state solely from the last console message.
3. Restore and verify the previous complete state using the verified recovery package, or prove that the selected release completed successfully. Save a missing success receipt only when its release digest, runtime state and test results have been established. It must retain the complete supported receipt schema and evidence; do not create a minimal success marker to bypass validation.
4. Through the normal controlled Nexus administration process, mark the in-progress record `finished` with the reconciled outcome and evidence. Refresh the catalogs if needed, then allow a new run.

Do not delete the marker or simply rerun the deployment to get past the block. Environment lock tokens catch accidental unlocked command execution; they do not authenticate a Jenkins lock. Job, credential and code permissions remain the trust boundary.

## Configure and verify the installed APIs

Replace the template URLs, app identifiers, job names and business fixtures. Inspect the installed Decision Center and RES Swagger specifications, then configure the operation paths, methods, parameters and response selectors used by the scripts. Keep `contracts_verified` false until the relevant calls and recovery behavior have been verified on a disposable DEV target. Setting this flag is an operator assertion, not automatic proof of compatibility. Likewise, set `baseline_is_immutable` and `xom_publish.preserves_existing` true only after verifying those guarantees. `baseline_selection_is_approval` is an explicit policy choice if the source API has no approval field; it is not inferred from the existence of a snapshot.

The RuleApp identity descriptor is `META-INF/archive.xml`. Configure `archive_identity.checks` to verify the RuleApp and every expected ruleset name and version, using the real XML namespaces and structure. The required role names are `ruleapp_name`, `ruleapp_version`, `ruleset_name` and `ruleset_version`. Set the proper attribute name or omit `attribute` when the value is element text. The placeholder checks are not a claim about the descriptor schema on your server.

If Decision Center returns a combined ZIP, set `decision_center.output.ruleapp_member` and `xom_member` to the exact member paths. If the XOM is embedded inside the RuleApp JAR, use `xom_in_ruleapp_member` instead. Otherwise configure the separate `download_xom` operation. Each actual binary export/download operation must bind `{baseline_id}` in its path or query parameters. The script refuses downloads with no explicit baseline binding. A separate build POST is optional only when the installed API requires it, and must include explicit completion assertions.

Combined-output extraction does not mean that arbitrary embedded-XOM RuleApp imports are supported. This workflow manages XOM versions and library bindings separately. The verified RuleApp publication contract and packaging must not independently change those bindings or modify old XOM bytes. The script rechecks the library and prior XOM content immediately after RuleApp publication and stops if they changed. Prove this behavior on the disposable DEV target before enabling the adapter.

The discovery notes need these corrections and checks:

- RES's documented RuleApp upload parameter is `version`, not `versioning`. The replace merge policy ignores RuleApp version increments.
- The complete HTDS REST path includes `/DecisionService/rest/v1/` and the selected RuleApp/ruleset versions.
- XOM and library operations must accompany the RuleApp deployment. Library JSON content contains the referenced resource URIs; a library `/content` endpoint must not be assumed.
- The notes' exact Decision Center baseline selectors and branch snapshots endpoint remain unverified. Confirm that export and build use the selected immutable source rather than branch head.
- Inspect actual archive contents and build reports. Do not rename arbitrary ZIP output to `.jar`.
- Confirm that the RES archive round trip preserves entry content. Verification ignores ZIP ordering and container timestamps, but deliberately does not normalize descriptor XML or discard unknown metadata. A server that rewrites entry content needs an explicitly reviewed comparison contract.
- Parse response bodies and resulting state as well as HTTP codes. Some RES operations report logical errors with 202 or missing objects with 204.
- Retrieving an HTDS description is not a business-rule test. Supply approved requests and expected decisions for both apps, and verify every configured runtime endpoint.

Reference: [IBM management Swagger UI](https://www.ibm.com/docs/en/odm/9.6.0?topic=api-using-rest-management-swagger-ui), [IBM library update example](https://www.ibm.com/docs/en/dbaoc?topic=wmli-adding-machine-learning-configuration-file-as-xom-resource), [Nexus components API](https://help.sonatype.com/en/components-api.html).

## Local checks

Install the dependency in a suitable Python environment, then run `python3 -m unittest discover -s tests -v`. These tests use local fixtures and simulated endpoints, not your real systems. The checked-in configuration and business fixtures intentionally require replacement; they are not ready for a production run.

For sync, `python3 -m sync.sync plan` produces the offline lock plan, `python3 -m sync.sync preflight` performs the configured read-only source checks, and `python3 -m sync.sync run` performs export and Git publication. For deployment, `python3 -m deployment.deploy preflight` checks the configured read-only inventory and baseline operations; it deliberately avoids downloads that may trigger builds. The workflow runs `plan`, then `prepare`, reviews the generated plan, and runs `execute`. The Jenkinsfiles provide approval and locks around those operations; running the commands manually does not supply those Jenkins controls.

Only sanitized plan, deployment/sync result and catalog-result JSON files are archived by the Jenkinsfiles. Protect workspaces and backup files because they can contain source rules and deployed artifacts. Do not publish business request payloads, credentials or raw server error pages in Jenkins logs.
