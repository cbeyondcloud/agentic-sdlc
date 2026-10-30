# The promotion dry run on kind (M3-04), sourced by inside.sh after the first install with
# ENVIRONMENT's values; docs/runbooks/environments-and-promotion.md records a run.
#   1. the API and the worker track the environment's policy branch (AGENTIC_CP_POLICY_BRANCH)
#   2. operator commands, in a one-off pod with the same image, both DSNs and that branch:
#      a tenant created without a branch tracks it; pointing it at another branch is refused
#      (a policy change reaches this environment only by a merge into its branch)
#   3. the chart's backup CronJob runs once and writes a dump (the restore drill's input)
# The first admin's token is redacted from the output.

case "$ENVIRONMENT" in dev) expected=dev ;; staging) expected=staging ;; esac
selector=app.kubernetes.io/instance=agentic,app.kubernetes.io/part-of=agentic-platform

say "promotion dry run: the policy branch $ENVIRONMENT tracks"
for component in api worker; do
  branch=$(kubectl -n "$NS" get deploy -l "$selector,app.kubernetes.io/component=$component" \
    -o jsonpath='{.items[0].spec.template.spec.containers[0].env[?(@.name=="AGENTIC_CP_POLICY_BRANCH")].value}')
  say "$component: AGENTIC_CP_POLICY_BRANCH=$branch"
  [ "$branch" = "$expected" ] || die "$component tracks '$branch', expected '$expected'"
done

# operator NAME EXPECT ARGS...: `python -m agentic_cp ARGS` in a one-off pod; EXPECT is
# Succeeded or Failed (the pod's phase)
operator() {
  name=$1; expect=$2; shift 2
  args=""
  for arg in "$@"; do args="$args, \"$arg\""; done
  kubectl -n "$NS" apply -f - >/dev/null <<POD
apiVersion: v1
kind: Pod
metadata:
  name: $name
spec:
  restartPolicy: Never
  enableServiceLinks: false
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001}
  containers:
    - name: operator
      image: $CP_IMAGE
      imagePullPolicy: Never
      command: ["python", "-m", "agentic_cp"$args]
      env:
        - {name: AGENTIC_CP_POLICY_BRANCH, value: "$branch"}
        - name: AGENTIC_CP_DATABASE_URL
          valueFrom: {secretKeyRef: {name: agentic-platform, key: database-url}}
        - name: AGENTIC_CP_OWNER_DATABASE_URL
          valueFrom: {secretKeyRef: {name: agentic-platform, key: owner-database-url}}
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
POD
  kubectl -n "$NS" wait "pod/$name" --for=jsonpath='{.status.phase}'="$expect" --timeout=180s >/dev/null \
    || { kubectl -n "$NS" logs "$name" || true; die "operator $* did not end $expect"; }
  kubectl -n "$NS" logs "$name" | sed '/^token (shown once/{n;s/.*/<redacted>/;}' | sed 's/^/  | /'
  kubectl -n "$NS" delete pod "$name" --wait=false >/dev/null
}

say "promotion dry run: a new tenant follows the environment's branch"
operator op-create Succeeded admin tenant create --slug dry-run --name "Dry run" \
  --admin-email admin@dry-run.example.test
say "promotion dry run: pointing it at main from $ENVIRONMENT is refused"
operator op-refuse Failed admin tenant set-policy-repo --tenant dry-run \
  --repo https://git.example.test/org/policy.git --branch main
say "promotion dry run: the policy repo, on the environment's branch"
operator op-source Succeeded admin tenant set-policy-repo --tenant dry-run \
  --repo https://git.example.test/org/policy.git
operator op-list Succeeded admin tenant list

say "promotion dry run: one backup with the chart's CronJob"
cron=$(kubectl -n "$NS" get cronjob -l "$selector,app.kubernetes.io/component=backup" -o jsonpath='{.items[*].metadata.name}')
if [ -z "$cron" ]; then
  [ "$ENVIRONMENT" != staging ] || die "the staging values have no backup CronJob"
  say "no backup CronJob in the $ENVIRONMENT values"
else
kubectl -n "$NS" create job backup-dry-run --from="cronjob/$cron" >/dev/null
kubectl -n "$NS" wait job/backup-dry-run --for=condition=complete --timeout=300s \
  || { kubectl -n "$NS" logs job/backup-dry-run || true; die "the backup job failed"; }
kubectl -n "$NS" logs job/backup-dry-run | sed 's/^/  | /'
fi
say "promotion dry run: passed"
