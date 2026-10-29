# Releasing agentic-sdlc, upgrading and rolling back (M3-21)

One version for all four packages (agentic-core, sdlc-commons, agentic-cli,
agentic-control-plane), built from a tag, signed with the org's key, published to the
org's internal package index, and smoke-tested from that index. Everything below is
`scripts/release.py`; the workflow template `templates/release/release.yml` runs the
same command in CI.

## What a release contains

| File | What it is |
|---|---|
| `*-X.Y.Z-py3-none-any.whl`, `*-X.Y.Z.tar.gz` | the four packages, plus Zyroflo's `sdlc-harness` built from the commit `uv.lock` pins (it is on no public index) |
| `agentic-sdlc-X.Y.Z.cdx.json` | CycloneDX 1.5 SBOM: every shipped package and extra, with licences |
| `third-party-notices-X.Y.Z.md` | licences and licence texts of every dependency, and the notices of adapted code |
| `constraints-X.Y.Z.txt` | the exact third-party versions of `uv.lock`, for reproducible installs |
| `release.json` | version, commit, build time (the commit's), uv version, sha256 of every file |
| `SHA256SUMS` and `*.sig` | the hashes, and an SSH signature of every file and of `SHA256SUMS` |

Builds are reproducible: the same commit built again (any OS, uv 0.12.1) gives
byte-identical wheels and sdists. Anyone can check a published release by rebuilding the
tag with `release.py build --out <folder>` and comparing the `SHA256SUMS` lines.

The CLI wheel carries the standalone template and the default agents, pipelines and
skills (`agentic_cli/_framework/`), so `agentic init --standalone` works from an
installed CLI with no checkout.

## One-time setup (platform team)

1. **The release key.** On a trusted machine:

   ```bash
   ssh-keygen -t ed25519 -C "agentic-sdlc release" -f agentic-release
   ```

   Put the private key (`agentic-release`) in the org's secret store as
   `AGENTIC_RELEASE_SIGNING_KEY` (the CI `release` environment), then delete the local
   copy. Nobody needs it on a laptop.
2. **The trust root.** Commit `release/allowed_signers` with the public key:

   ```text
   release@your-org namespaces="agentic-release" ssh-ed25519 AAAA... agentic-sdlc release
   ```

   Rotating the key adds a line (keep the old one until the releases it signed are out of
   use). Revoking a key deletes its line; then re-sign or withdraw what it signed.
3. **The index.** An upload account for the internal index (devpi, Artifactory, Nexus,
   GitLab, pypiserver) as `UV_PUBLISH_USERNAME` / `UV_PUBLISH_PASSWORD` (or
   `UV_PUBLISH_TOKEN`), and the variables `AGENTIC_RELEASE_INDEX_URL` (upload URL),
   `AGENTIC_RELEASE_CHECK_URL` (its simple URL) and `AGENTIC_RELEASE_IDENTITY`
   (`release@your-org`). `release.py publish` refuses PyPI and TestPyPI and plain http.
   Configure the index so the internal packages' names never resolve from a public
   index (devpi `mirror_whitelist`, Artifactory/Nexus routing rules): the CLI pins its
   internal dependencies exactly, and `sdlc-harness` to the locked version.
4. **The workflow.** Copy `templates/release/release.yml` to `.github/workflows/` in the
   org's mirror, create the `release` environment with required reviewers, and give it
   `AGENTIC_RELEASE_GIT_TOKEN` (read access to the Zyroflo repository).

## Cut a release

```bash
uv run python scripts/release.py version --bump auto      # or --set 1.4.0 / --bump minor
uv run python scripts/release.py changelog --write        # prepends CHANGELOG.md
uv run python scripts/check.py --all
git commit -am "release: v1.4.0"                          # review and merge as usual
git tag v1.4.0 && git push origin v1.4.0                  # on the merged commit
```

`--bump auto` reads the commits since the last `v*` tag: a breaking change (`feat!:` or a
`BREAKING CHANGE:` footer) is major, a `feat:` minor, anything else patch. A version is
`MAJOR.MINOR.PATCH`, optionally `aN`, `bN` or `rcN`, and always greater than the last one,
so it is always a valid `min_cli_version`. `version --check` (run by every build) fails
when any package version or internal pin disagrees.

The tag starts the workflow: checks, a gitleaks scan of the whole history, then
`release.py release --tag v1.4.0`, which builds (refusing a tag that is not the version,
or uncommitted changes), signs, verifies, publishes and installs the release from the
index into a clean environment for `agentic --version` and a standalone dry run.

Without CI, the same from a release manager's machine (the key from the secret store into
a file only you can read, deleted afterwards):

```bash
uv run python scripts/release.py release --tag v1.4.0 --key ./agentic-release \
  --allowed-signers release/allowed_signers --identity release@your-org \
  --url https://pypi.your-org.internal/upload/ --check-url https://pypi.your-org.internal/simple/
```

## Verify a download

```bash
sha256sum -c SHA256SUMS
ssh-keygen -Y verify -f release/allowed_signers -I release@your-org -n agentic-release \
  -s SHA256SUMS.sig < SHA256SUMS
```

On Windows (OpenSSH ships with Windows 10 and later) from PowerShell:

```powershell
cmd /c "ssh-keygen -Y verify -f release\allowed_signers -I release@your-org -n agentic-release -s SHA256SUMS.sig < SHA256SUMS"
Get-FileHash -Algorithm SHA256 .\agentic_cli-1.4.0-py3-none-any.whl
```

Or all at once: `uv run python scripts/release.py verify --dir <folder> --allowed-signers
release/allowed_signers --identity release@your-org`.

## Upgrade the fleet

Developers (Windows, macOS, Linux) install and upgrade from the internal index:

```bash
uv tool install agentic-cli==1.4.0 --index-url https://pypi.your-org.internal/simple/
uv tool upgrade agentic-cli          # later releases; the index comes from uv's config
agentic --version
```

The minimum CLI version (M2-08) is raised **after** the new release is published,
verified and adopted, never before:

1. Publish 1.4.0 and announce it. Old CLIs keep working.
2. Watch adoption (the index's download counts, or ask teams for `agentic --version`).
3. Raise the minimum: `min_cli_version: "1.4.0"` in the policy repo's `policy/org.yaml`
   (a reviewed pull request; it applies when the policy version is accepted), and/or the
   control plane's deployment floor `AGENTIC_CP_MIN_CLI_VERSION`. The higher of the two
   applies.
4. From then on an older CLI cannot register runs: `agentic run` exits 2 with "upgrade the
   agentic CLI to 1.4.0 or later (`uv tool upgrade agentic-cli` ...)". Evidence an old CLI
   already recorded is still accepted.

**Standalone users.** `init` never overwrites an installed standalone policy copy
(`~/.agentic/policy/standalone`) on its own, so an upgraded CLI keeps the old agents,
pipelines and skills there; `agentic init` says so when the copy came from another
template (the copy records its CLI version and template digest in
`.agentic-template.json`). To take the new template run
`agentic init --standalone --refresh-template`: your `policy/` files (tiers, limits,
classifications) are carried over, everything else becomes the new template's, the report
lists what changed, and the old copy is kept whole as
`~/.agentic/policy/standalone.previous-<UTC time>` (copy any edits you made outside
`policy/` back from there). If the repository guard refuses, nothing is replaced. To roll
back, move the new folder aside and rename the `.previous-...` one back.

## Roll back

Published files are never overwritten or deleted: indexes refuse a second file of the same
name, and a release must stay reproducible.

1. **Prefer rolling forward.** Fix, `version --bump patch`, release 1.4.1.
2. **Pin back.** Developers reinstall the previous release:
   `uv tool install agentic-cli==1.3.2 --index-url ... --reinstall`. If `min_cli_version`
   was already raised to 1.4.0, lower it first (policy pull request, and/or
   `AGENTIC_CP_MIN_CLI_VERSION`), or the pinned-back CLI is refused.
3. **Yank** the bad version on the index (PEP 592; devpi, Artifactory and Nexus support
   it) so new installs skip it while pinned installs still resolve.
4. **The control plane** rolls back with `helm rollback` ([upgrade.md](upgrade.md)). Its
   database migrations move forward only; the previous release serves the newer schema
   because migrations expand first and contract a release later. If a release broke
   that rule, roll forward instead.

## Troubleshooting

| Message | Meaning |
|---|---|
| `the versions disagree` | a package version or internal pin was edited by hand: `release.py version --set <the version>` |
| `the tree has uncommitted changes` | commit first; `--allow-dirty` is for test builds, and `release.json` records it |
| `the tag vX is not vY` | the tag does not match `pyproject.toml`: delete the tag, bump, tag again |
| `sdlc-harness at ... is version A, but uv.lock says B` | run `uv lock` and commit |
| `... is a public index` | point `--url` at the internal index |
| `no allowed key for ... made this signature` | wrong key or identity, or the key was revoked from `allowed_signers` |
