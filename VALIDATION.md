# Local validation

Repository layout updated: October 26, 2026

The deployment and sync workflows have been moved into separate folders. The complete local test suite passed in this reorganized repository: **75 tests**. Python source compilation also passed. The checks ran from the repository root on October 26, 2026.

Run checks from the repository root with `python3 -m unittest discover -s tests -v`. The workflow entrypoints are `python3 -m deployment.deploy` and `python3 -m sync.sync`; the Jenkins Script Paths are `deployment/Jenkinsfileodmdeploy` and `sync/Jenkinsfileodmsync`. The Jenkins checkout remains rooted at the workspace, where shared configuration, dependencies and tests are stored.

The tests use simulated HTTP endpoints, in-memory deployment adapters and temporary local Git repositories. They do not contact live IBM ODM, RES, Nexus or Jenkins systems.

Coverage includes:

- Explicit app and release selection, job-to-track restrictions and approval binding.
- Building once in DEV and promoting identical release bytes through separate INT and PROD targets.
- Rejection of missing, malformed or mismatched promotion evidence.
- Source baseline binding, archive identity, release checksums and immutable Git tags.
- Recovery of the previous RuleApp and library after a failed business test.
- Preserving a successful deployment when its receipt cannot be saved.
- Blocking unfinished deployments and requiring inspection after an ambiguous management write.
- Keeping recovery available when local result-file writing fails.
- Stable sync exports, unchanged polls, source provenance and concurrent Git updates.
- Safe HTTP authentication, redirects, retry rules and ZIP extraction.
- Loading the supplied YAML configuration with the pinned PyYAML dependency.

Live API compatibility, the actual business test cases and native Jenkins pipeline execution remain unverified. The template adapters remain disabled. Follow README.md to configure the jobs and verify the installed API contracts before enabling deployment.
