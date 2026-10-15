# Optional dependencies for runner deployments

Optional workflow dependencies are part of the deployment artifact. Declare
them in the workflow project's package metadata, select the required extras in
CI or the resource-specific deployment configuration, and build a locked
production environment before starting `simstack_runner`.

The runner does not install, upgrade, or synchronize packages. It does not
inspect a fixed lock-file name or a per-user extras file, and it never mutates
its active environment after startup. This is intentional: package-manager and
repository choices belong to the deployment system, not to task execution.

A safe update sequence is:

1. Build a new secret-free source snapshot and production environment outside
   the running runner.
2. Install the resource's explicitly selected optional dependency groups with
   the deployment system's package manager.
3. Run import and workflow validation against that artifact.
4. Atomically activate the validated artifact and restart the runner.

If any build or validation step fails, keep the current runner deployment
unchanged. Do not add package-install commands to workflow code or runner
process-launch configuration.
