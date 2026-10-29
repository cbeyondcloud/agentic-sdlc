"""The load test's target stack (`compose.yml`) through Docker or Podman compose (M3-22).

The passwords and the metrics token are random per stack, written to the git-ignored
`.state/stack.json` (owner-only where the OS has modes) and passed to compose in the
environment; nothing prints them. Every command names the project, so `down` removes this
stack's containers and volume and nothing else."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2

PROJECT = "agentic-m3-load-test"
HERE = Path(__file__).resolve().parent.parent
REPO_ROOT = HERE.parents[1]
COMPOSE = HERE / "compose.yml"
STATE = HERE / ".state"
SECRETS = ("LT_SUPERUSER_PASSWORD", "LT_OWNER_PASSWORD", "LT_APP_PASSWORD", "LT_METRICS_TOKEN")


def container_engine() -> str:
    """CONTAINER_ENGINE, else docker or podman on PATH (scripts/check.py decides)."""
    spec = importlib.util.spec_from_file_location("_lt_check", REPO_ROOT / "scripts" / "check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_lt_check", module)
    spec.loader.exec_module(module)
    engine = module.container_engine()
    if not engine:
        raise SystemExit("no container engine: install Docker or Podman, or set CONTAINER_ENGINE")
    return engine


@dataclass
class Stack:
    engine: str
    secrets: dict[str, str]

    @classmethod
    def load(cls, *, create: bool = False) -> Stack:
        path = STATE / "stack.json"
        if path.exists():
            found = json.loads(path.read_text(encoding="utf-8"))
        elif create:
            found = {name: secrets.token_hex(16) for name in SECRETS}
            STATE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8", newline="\n")
            os.chmod(path, 0o600)
        else:
            raise SystemExit("no load-test stack: run `loadtest.py up` first")
        return cls(container_engine(), found)

    def compose(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
        command = [self.engine, "compose", "-p", PROJECT, "-f", str(COMPOSE), *args]
        return subprocess.run(
            command,
            env={**os.environ, **self.secrets},
            capture_output=capture,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def up(self, replicas: int, *, build: bool = True) -> list[str]:
        args = ["up", "-d", "--scale", f"lt-cp={replicas}"]
        done = self.compose(*args, *(["--build"] if build else []))
        if done.returncode != 0:
            raise SystemExit(f"compose up failed (exit {done.returncode})")
        bases = self.bases()
        wait_ready(bases)
        return bases

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans")
        (STATE / "stack.json").unlink(missing_ok=True)

    def _port(self, service: str, port: int, index: int = 1) -> int:
        done = self.compose("port", "--index", str(index), service, str(port), capture=True)
        found = done.stdout.strip().splitlines()
        if done.returncode != 0 or not found:
            raise SystemExit(f"{service} #{index} publishes no port {port}: is the stack up?")
        return int(found[0].rpartition(":")[2])

    def postgres_port(self) -> int:
        return self._port("lt-postgres", 5432)

    def bases(self) -> list[str]:
        names = [n for n in self.containers() if "-lt-cp-" in n]
        return [
            f"http://127.0.0.1:{self._port('lt-cp', 8080, i)}" for i in range(1, len(names) + 1)
        ]

    def containers(self) -> list[str]:
        done = self.compose("ps", "--format", "{{.Name}}", capture=True)
        return sorted(line.strip() for line in done.stdout.splitlines() if line.strip())

    def dsn(self, role: str) -> str:
        user, secret = {
            "admin": ("postgres", "LT_SUPERUSER_PASSWORD"),
            "owner": ("cp_owner", "LT_OWNER_PASSWORD"),
            "app": ("cp_app", "LT_APP_PASSWORD"),
        }[role]
        database = "agentic"
        password = self.secrets[secret]
        return f"postgresql://{user}:{password}@127.0.0.1:{self.postgres_port()}/{database}"

    def info(self) -> dict:
        done = subprocess.run(
            [self.engine, "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        parts = done.stdout.split()
        if len(parts) < 3:
            return {"engine": self.engine}
        return {
            "engine": self.engine,
            "vm_cpus": int(parts[0]),
            "vm_memory_gib": round(int(parts[1]) / 2**30, 1),
            "server_version": parts[2],
        }


@dataclass
class External:
    """A deployment this harness did not start (staging): from LT_BASES, LT_ADMIN_DSN,
    LT_OWNER_DSN, LT_APP_DSN and optionally LT_METRICS_TOKEN. No container samples."""

    env: dict[str, str]
    engine: None = None

    @property
    def secrets(self) -> dict[str, str]:
        return {"LT_METRICS_TOKEN": self.env.get("LT_METRICS_TOKEN", "")}

    def bases(self) -> list[str]:
        return [b.strip().rstrip("/") for b in self.env["LT_BASES"].split(",") if b.strip()]

    def dsn(self, role: str) -> str:
        name = f"LT_{role.upper()}_DSN"
        if not self.env.get(name):
            raise SystemExit(f"{name} is not set (see control-plane/loadtest/README.md)")
        return self.env[name]

    def containers(self) -> list[str]:
        return []

    def info(self) -> dict:
        return {"engine": "external"}


def target() -> Stack | External:
    """The deployment `run` loads: LT_BASES names one, else the stack `up` started."""
    if os.environ.get("LT_BASES", "").strip():
        return External(dict(os.environ))
    return Stack.load()


def wait_ready(bases: list[str], timeout_s: float = 300) -> None:
    """Until every replica's /readyz answers 200 (migrated, isolation enforced)."""
    deadline = time.monotonic() + timeout_s
    for base in bases:
        while True:
            try:
                if httpx2.get(f"{base}/readyz", timeout=5).status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit(f"{base} did not become ready in {timeout_s:.0f} s")
            time.sleep(1)
