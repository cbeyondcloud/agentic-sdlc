"""The day-0 bootstrap, end to end on a local kind staging (M3-05): the runbook
docs/runbooks/day-0-bootstrap.md, followed by a script so it can be repeated.

    uv run python deploy/helm/kind/bootstrap_proof.py --name b17-m3-05 --work DIR [--keep]

Needs Docker only (about 5 GB of free memory). Everything it starts carries `--name`: the
kind cluster, the tool container (kind, kubectl, helm), a fake SMTP relay (Mailpit, MIT)
and a local git host (git_https_server.py in the control-plane image). No cloud, no model.

 0. staging from the chart: the kind smoke (smoke.sh / smoke.ps1, ENVIRONMENT=staging,
    KEEP=1), which also checks the policy branch pin and takes a backup;
 1. the policy repo: `bootstrap.py policy-repo`, served by the local git host over https;
 2. git credentials: `bootstrap.py git-credentials` (a token file and the host's CA) into
    the chart's gitConfigSecret; SMTP and the public URL in the values; `helm upgrade`;
 3. the tenant and its first org admin: `bootstrap.py tenant` (the operator pod), and
    `db backup-role --check` and a `git ls-remote` with the tenant's credentials there;
 4. the CLI on an index: scripts/release.py's build, sign and publish to a loopback index,
    and a clean install from it;
 5. people: `bootstrap.py people` (a team lead and a developer, invited by mail);
 6. `bootstrap.py verify` and `bootstrap.py smoke` with the developer's invite link from
    the fake relay.
The cluster and the containers are removed at the end unless --keep."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts" / "tests")]
#: Mailpit v1.27.11 (MIT), a fake SMTP relay with an HTTP API, by tag and digest.
MAILPIT = (
    "axllent/mailpit:v1.27.11"
    "@sha256:e22dce5b36f93c77082e204a3942fb6b283b7896e057458400a4c88344c3df68"
)
NAMESPACE = "agentic"
SERVICE = "agentic-agentic-platform-api"
OPERATOR = "deploy/agentic-agentic-platform-operator"
ORG, TEAM = "acme", "checkout"
ADMIN, LEAD, DEV = "admin@acme.example.test", "lead@acme.example.test", "dev@acme.example.test"
SMOKE = "agentic-smoke@acme.example.test"  # its invite is the smoke run's (redeemed there)
REPO_PATTERN = "git.staging.example.test/acme/*"
MODELS = {
    "deep-reasoning": "staging-gw/deep",
    "balanced": "staging-gw/balanced",
    "fast": "staging-gw/fast",
    "local": "local/staging",
    "eval-judge": "staging-gw/judge",
}


def say(message: str) -> None:
    print(f"bootstrap-proof: {message}", flush=True)


def sh(cmd: list[str], *, check: bool = True, quiet: bool = False, **kwargs) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", **kwargs)
    if not quiet and done.stdout.strip():
        print("\n".join(f"  | {line}" for line in done.stdout.rstrip().splitlines()))
    if check and done.returncode != 0:
        raise SystemExit(f"bootstrap-proof: FAILED: {' '.join(cmd[:4])}: {done.stderr[-2000:]}")
    return done.stdout


def bootstrap(*argv: str) -> None:
    say(f"bootstrap.py {argv[0]}")
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "bootstrap.py"), *argv])
    if done.returncode != 0:
        raise SystemExit(
            f"bootstrap-proof: FAILED: bootstrap.py {argv[0]} exited {done.returncode}"
        )


def tls_files(folder: Path, host: str) -> None:
    """A throw-away CA and a server certificate for the local git host."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bootstrap proof CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    import ipaddress

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(host),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    (folder / "ca.pem").write_bytes(ca.public_bytes(pem))
    (folder / "server.pem").write_bytes(cert.public_bytes(pem))
    (folder / "server.key").write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )


def wait_for(what: str, probe, timeout: float = 240, every: float = 5):
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result:
            return result
        if time.monotonic() > deadline:
            raise SystemExit(f"bootstrap-proof: FAILED: {what} within {timeout:.0f} s")
        time.sleep(every)


def get_json(url: str, token: str | None = None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as reply:
            return json.loads(reply.read())
    except (OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default="agentic-bootstrap", help="prefix of everything started")
    parser.add_argument("--work", required=True, help="a folder for the proof's files")
    parser.add_argument("--keep", action="store_true", help="keep the cluster and containers")
    parser.add_argument("--skip-cluster", action="store_true", help="the cluster exists (--keep)")
    args = parser.parse_args()
    name, work = args.name, Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    tools, git_host, mailpit = f"{name}-tools", f"{name}-git", f"{name}-mailpit"
    image = f"agentic-control-plane:{name}"
    try:
        # ── 0. staging from the chart (the kind smoke, kept) ─────────────────────
        if not args.skip_cluster:
            say(f"0. staging on kind: the smoke with ENVIRONMENT=staging, KEEP=1 ({name})")
            env = {**os.environ, "KIND_CLUSTER": name, "CP_IMAGE": image, "KEEP": "1"}
            env["ENVIRONMENT"] = "staging"
            if os.name == "nt":
                smoke = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
                smoke.append(str(HERE / "smoke.ps1"))
            else:
                smoke = ["bash", str(HERE / "smoke.sh")]
            if subprocess.run(smoke, env=env).returncode != 0:
                raise SystemExit("bootstrap-proof: FAILED: the staging install (kind smoke)")
        digest = hashlib.sha256((HERE / "tools.Dockerfile").read_bytes()).hexdigest()[:12]
        sh(["docker", "rm", "-f", tools, git_host, mailpit], check=False, quiet=True)
        sh(
            ["docker", "run", "-d", "--name", tools, "--network", "kind", "-p", "127.0.0.1::8080"]
            + [
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock",
                "-e",
                "KUBECONFIG=/tmp/kubeconfig",
            ]
            + [f"agentic-kind-tools:{digest}", "sleep", "infinity"],
            quiet=True,
        )
        sh(
            [
                "docker",
                "exec",
                tools,
                "sh",
                "-c",
                f"kind get kubeconfig --internal --name {name} > /tmp/kubeconfig",
            ]
        )
        api_port = sh(["docker", "port", tools, "8080"], quiet=True).split(":")[-1].strip()
        api = f"http://127.0.0.1:{api_port}"
        kubectl = f"docker exec -i {tools} kubectl"

        # ── 1. the policy repo, on a local git host ──────────────────────────────
        policy = work / "policy"
        argv = ["policy-repo", "--out", str(policy), "--repo-pattern", REPO_PATTERN]
        argv += ["--admins", "@acme/agentic-org-admins", "--team", f"{TEAM}=@acme/checkout-leads"]
        argv += ["--cli-version", "0.1.0", "--sandbox-required-for", "none"]
        for tier, model in MODELS.items():
            argv += ["--model", f"{tier}={model}"]
        if not (policy / ".git").is_dir():
            bootstrap(*argv)
        say("1. the local git host (https, a private CA, a token)")
        secrets_dir = work / "git"
        secrets_dir.mkdir(exist_ok=True)
        tls_files(work, git_host)
        token = secrets.token_hex(20)
        token_file = secrets_dir / f"{ORG}.token"
        token_file.write_text(token + "\n", encoding="utf-8")
        bare = work / "repos" / "policy.git"
        if not bare.exists():
            sh(["git", "clone", "-q", "--bare", str(policy), str(bare)])
        sh(
            ["docker", "create", "--name", git_host, "--network", "kind", "--user", "0"]
            + ["--entrypoint", "python", image, "/srv/git_https_server.py", "--root", "/srv/repos"]
            + [
                "--cert",
                "/srv/server.pem",
                "--key",
                "/srv/server.key",
                "--token-file",
                "/srv/token",
            ],
            quiet=True,
        )
        sh(
            [
                "docker",
                "cp",
                str(HERE / "git_https_server.py"),
                f"{git_host}:/srv/git_https_server.py",
            ]
        )
        for file in ("server.pem", "server.key"):
            sh(["docker", "cp", str(work / file), f"{git_host}:/srv/{file}"])
        sh(["docker", "cp", str(token_file), f"{git_host}:/srv/token"])
        sh(["docker", "cp", str(bare.parent), f"{git_host}:/srv/repos"])
        sh(["docker", "start", git_host], quiet=True)
        repo_url = f"https://{git_host}:8443/policy.git"

        say("2. SMTP: a fake relay on the kind network")
        sh(
            [
                "docker",
                "run",
                "-d",
                "--name",
                mailpit,
                "--network",
                "kind",
                "-p",
                "127.0.0.1::8025",
                MAILPIT,
            ],
            quiet=True,
        )
        mail_port = sh(["docker", "port", mailpit, "8025"], quiet=True).split(":")[-1].strip()
        mail_api = f"http://127.0.0.1:{mail_port}/api/v1"

        # ── 2. git credentials, SMTP and the public URL: helm upgrade ────────────
        bootstrap(
            "git-credentials",
            "--tenant",
            ORG,
            "--out",
            str(secrets_dir),
            "--host",
            f"{git_host}:8443",
            "--ca-file",
            str(work / "ca.pem"),
        )
        sh(["docker", "cp", str(secrets_dir), f"{tools}:/tmp/git"])
        sh(
            [
                "docker",
                "exec",
                tools,
                "kubectl",
                "-n",
                NAMESPACE,
                "delete",
                "secret",
                "agentic-git",
                "--ignore-not-found",
            ],
            quiet=True,
        )
        sh(
            [
                "docker",
                "exec",
                tools,
                "kubectl",
                "-n",
                NAMESPACE,
                "create",
                "secret",
                "generic",
                "agentic-git",
            ]
            + [
                f"--from-file=/tmp/git/{ORG}.gitconfig",
                f"--from-file=/tmp/git/{ORG}.token",
                f"--from-file=/tmp/git/{ORG}-ca.pem",
            ],
            quiet=True,
        )
        sh(["docker", "exec", tools, "rm", "-rf", "/tmp/git"])
        say("2. helm upgrade: staging values plus SMTP, the public URL and the git config")
        sh(
            [
                "docker",
                "cp",
                str(ROOT / "deploy" / "helm" / "agentic-platform"),
                f"{tools}:/w-chart",
            ]
        )
        sets = {
            "image.repository": image.split(":")[0],
            "image.tag": name,
            "image.pullPolicy": "Never",
            "publicUrl": api,
            "smtp.host": mailpit,
            "smtp.port": "1025",
            "smtp.security": "none",
            "smtp.from": "agentic@staging.example.test",
            "gitConfigSecret": "agentic-git",
            "config.AGENTIC_CP_POLICY_POLL_SECONDS": "10",
            "worker.replicas": "1",
        }
        upgrade = [
            "docker",
            "exec",
            tools,
            "helm",
            "upgrade",
            "agentic",
            "/w-chart",
            "-n",
            NAMESPACE,
        ]
        upgrade += ["-f", "/w-chart/environments/staging.yaml", "--wait", "--timeout", "15m"]
        for key, value in sets.items():
            upgrade += [
                "--set-string" if key.startswith("config.") or key == "smtp.port" else "--set",
                f"{key}={value}",
            ]
        sh(upgrade, quiet=True)
        sh(
            [
                "docker",
                "exec",
                tools,
                "kubectl",
                "-n",
                NAMESPACE,
                "rollout",
                "status",
                "deploy",
                "-l",
                "app.kubernetes.io/component=api",
                "--timeout=300s",
            ],
            quiet=True,
        )
        sh(
            [
                "docker",
                "exec",
                "-d",
                tools,
                "kubectl",
                "-n",
                NAMESPACE,
                "port-forward",
                "--address",
                "0.0.0.0",
                f"svc/{SERVICE}",
                "8080:80",
            ]
        )
        wait_for("the API through the port-forward", lambda: get_json(f"{api}/readyz"), 60, 2)

        # ── 3. the tenant and its first org admin ─────────────────────────────────
        admin_token = work / "admin.token"
        admin_token.unlink(missing_ok=True)  # a re-run: --recover-token writes a new one
        bootstrap(
            "tenant",
            "--slug",
            ORG,
            "--name",
            "Acme",
            "--admin-email",
            ADMIN,
            "--token-file",
            str(admin_token),
            "--policy-repo",
            repo_url,
            "--kubectl",
            kubectl,
            "--keep-operator",
            "--recover-token",
        )
        say("3. in the operator pod: the backup role and the tenant's git access")
        exec_op = ["docker", "exec", tools, "kubectl", "-n", NAMESPACE, "exec", OPERATOR, "--"]
        sh(
            [
                *exec_op,
                "python",
                "-m",
                "agentic_cp",
                "db",
                "backup-role",
                "--role",
                "agentic_backup",
                "--check",
            ]
        )
        sh(
            [
                *exec_op,
                "env",
                f"GIT_CONFIG_GLOBAL=/etc/agentic/git/{ORG}.gitconfig",
                "git",
                "ls-remote",
                "--heads",
                repo_url,
            ]
        )
        sh(
            [
                "docker",
                "exec",
                tools,
                "kubectl",
                "-n",
                NAMESPACE,
                "scale",
                OPERATOR,
                "--replicas=0",
            ],
            quiet=True,
        )
        admin = admin_token.read_text(encoding="utf-8").strip()
        active = wait_for(
            "the policy repo synced (GET /v1/policy/active)",
            lambda: get_json(f"{api}/v1/policy/active", admin),
        )
        say(f"3. policy active: {active['version']} on {active['branch']}")

        # ── 4. the CLI on an index ────────────────────────────────────────────────
        say("4. the release on a loopback index (build, sign, publish), installed clean")
        from agentic_release import build, publish, signing, smoke, version
        from local_index import LocalIndex
        from release_support import IDENTITY, allowed, new_key

        index = LocalIndex(work / "index", password=secrets.token_hex(8)).start()
        release = work / "dist"
        if not release.exists():
            build.build_packages(ROOT, release)
        key, public = new_key(work)
        signing.sign_dir(release, signing.load_private_key(key))
        creds = {"UV_PUBLISH_USERNAME": index.username, "UV_PUBLISH_PASSWORD": index.password}
        publish.publish(
            release,
            index.url,
            allowed=signing.parse_allowed(allowed(public)),
            identity=IDENTITY,
            env=creds,
        )
        cli_version = version.read_version(ROOT)
        agentic = smoke.install(index.simple_url, cli_version, work / "venv", deps_from_lock=ROOT)
        smoke.check_version(agentic, cli_version)
        say(f"4. agentic {cli_version} installed from {index.simple_url}")

        # ── 5. people ─────────────────────────────────────────────────────────────
        bootstrap(
            "people",
            "--api",
            api,
            "--org",
            ORG,
            "--token-file",
            str(admin_token),
            "--assign",
            f"{LEAD}=team-lead@{TEAM}",
            "--assign",
            f"{DEV}=developer@{TEAM}",
            "--assign",
            f"{SMOKE}=developer@{TEAM}",
            "--agentic",
            str(agentic),
        )

        def invite_link():
            listing = get_json(f"{mail_api}/messages") or {}
            for message in listing.get("messages", []):
                if any(t.get("Address") == SMOKE for t in message.get("To", [])):
                    body = get_json(f"{mail_api}/message/{message['ID']}") or {}
                    found = re.search(
                        r"https?://\S+/invite#aci_[A-Za-z0-9_-]+", body.get("Text", "")
                    )
                    return found.group(0) if found else None
            return None

        link = wait_for("the smoke account's invite in the fake relay", invite_link)
        say("5. the invites arrived through SMTP")

        # ── 6. verify and the smoke run ──────────────────────────────────────────
        bootstrap(
            "verify",
            "--api",
            api,
            "--token-file",
            str(admin_token),
            "--mail-to",
            LEAD,
            "--mail-to",
            DEV,
            "--mail-to",
            SMOKE,
            "--index-url",
            index.simple_url,
            "--cli-version",
            cli_version,
        )
        bootstrap(
            "smoke",
            "--api",
            api,
            "--org",
            ORG,
            "--team",
            TEAM,
            "--invite-link",
            link,
            "--repo-remote",
            "https://git.staging.example.test/acme/smoke-repo.git",
            "--agentic",
            str(agentic),
        )
        index.stop()
        say("passed")
        return 0
    finally:
        if not args.keep:
            sh(
                ["docker", "exec", tools, "kind", "delete", "cluster", "--name", name],
                check=False,
                quiet=True,
            )
            sh(
                ["docker", "rm", "-f", tools, git_host, mailpit, f"{name}-control-plane"],
                check=False,
                quiet=True,
            )
            say("cluster and containers removed")


if __name__ == "__main__":
    sys.exit(main())
