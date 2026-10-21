"""Does the sandbox on THIS machine block what it must? (spike S6's cases, as a tool)

    python -m agentic.sandbox.probe [--workdir DIR] [--unsandboxed]

Runs each case through `SrtSandbox` in a scratch folder and prints what was wanted
and what happened; exits 1 on any mismatch. Run it once in WSL2 (DF-60) and once on
a Mac (DF-61); `agentic doctor` will run it too (M3-09). `--unsandboxed` runs the
same cases with no sandbox at all - every block then fails, which proves the probe
can tell the difference.
"""

import argparse
import asyncio
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from agentic.sandbox.process import ShellResult, run_process
from agentic.sandbox.rules import SandboxRules
from agentic.sandbox.srt import SrtSandbox

ALLOWED_HOST = "pypi.org"

# (name, command, wanted, judged by): a path is judged by whether it exists afterwards,
# "config" by whether .git/config changed, "exit" by the exit code.
CASES: list[tuple[str, str, str, str]] = [
    ("write inside worktree", "echo ok > {wt}/a.txt", "allowed", "{wt}/a.txt"),
    ("write outside worktree", "echo x > {other}/b.txt", "blocked", "{other}/b.txt"),
    (
        "write from a grandchild",
        "sh -c 'sh -c \"echo x > {other}/c.txt\"'",
        "blocked",
        "{other}/c.txt",
    ),
    ("write denied path in worktree", "echo SECRET=1 > {wt}/.env", "blocked", "{wt}/.env"),
    (
        "write git hook in worktree",
        "echo boom > {wt}/.git/hooks/pre-commit",
        "blocked",
        "{wt}/.git/hooks/pre-commit",
    ),
    ("write git config in worktree", "echo '[core]' >> {wt}/.git/config", "blocked", "config"),
    ("read denied folder", "cat {secrets}/key", "blocked", "exit"),
    (
        "HTTPS to allowed host",
        # a HEAD request: reachability, not bandwidth (the index page is ~47 MB)
        f"curl -sS -I -o /dev/null --max-time 20 https://{ALLOWED_HOST}/simple/",
        "allowed",
        "exit",
    ),
    (
        "HTTPS to other host",
        "curl -sS -o /dev/null --max-time 20 https://example.com/",
        "blocked",
        "exit",
    ),
    (
        "HTTPS ignoring the proxy",
        f"curl -sS -o /dev/null --max-time 10 --noproxy '*' https://{ALLOWED_HOST}/simple/",
        "blocked",
        "exit",
    ),
    (
        "raw TCP to an IP",
        "{py} -c \"import socket; socket.create_connection(('1.1.1.1', 443), 5)\"",
        "blocked",
        "exit",
    ),
    (
        "DNS lookup",
        "{py} -c \"import socket; socket.getaddrinfo('example.com', 443)\"",
        "blocked",
        "exit",
    ),
    ("git status in worktree", "git -C {wt} status --short", "allowed", "exit"),
    (
        "git commit in worktree",
        "git -C {wt} add a.txt && git -C {wt} -c user.name=p -c user.email=p@x commit -qm probe",
        "allowed",
        "exit",
    ),
    (
        "unix socket to host",
        "{py} -c \"import socket; s = socket.socket(socket.AF_UNIX); s.connect('{sock}')\"",
        "blocked",
        "exit",
    ),
]


def _unix_server(path: Path) -> None:
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(4)
    while True:
        connection, _ = server.accept()
        connection.close()


class _Unsandboxed:
    """The platform's own shell, no sandbox: only to show the probe notices."""

    async def run(self, command: str, cwd: Path, timeout_s: float) -> ShellResult:
        return await run_process(command, cwd, timeout_s)


def run_cases(workdir: Path, *, unsandboxed: bool = False) -> list[tuple[str, str, str, str]]:
    """(case, wanted, got, last output line) for every case."""
    wt, other, secrets = workdir / "wt", workdir / "other", workdir / "secrets"
    for folder in (wt, other, secrets):
        folder.mkdir(parents=True, exist_ok=True)
    (secrets / "key").write_text("PRIVATE KEY\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    sock = wt / "probe.sock"
    cases = list(CASES)
    if hasattr(socket, "AF_UNIX"):
        threading.Thread(target=_unix_server, args=(sock,), daemon=True).start()
    else:
        cases = [c for c in cases if c[0] != "unix socket to host"]
    rules = SandboxRules(
        write=(wt,),
        deny_write=(wt / ".env",),
        deny_read=(secrets,),
        allowed_domains=(ALLOWED_HOST, "files.pythonhosted.org"),
    )
    box = _Unsandboxed() if unsandboxed else SrtSandbox(rules, workdir / "settings")
    values = {"wt": wt, "other": other, "secrets": secrets, "sock": sock, "py": sys.executable}
    config_before = (wt / ".git" / "config").read_bytes()
    results = []
    for name, template, wanted, judge in cases:
        command = template.format(**{k: Path(v).as_posix() for k, v in values.items()})
        result = asyncio.run(box.run(command, wt, 60))
        if judge == "exit":
            got = "allowed" if result.exit_code == 0 else "blocked"
        elif judge == "config":
            changed = (wt / ".git" / "config").read_bytes() != config_before
            got = "allowed" if changed else "blocked"
        else:
            got = "allowed" if Path(judge.format(**values)).exists() else "blocked"
        lines = (result.stderr or result.stdout).strip().splitlines()
        results.append((name, wanted, got, lines[-1][:100] if lines else ""))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check what the sandbox blocks here.")
    parser.add_argument("--workdir", type=Path, help="scratch folder (default: a temp folder)")
    parser.add_argument("--unsandboxed", action="store_true", help="run with no sandbox")
    args = parser.parse_args(argv)
    if args.workdir is None:
        with tempfile.TemporaryDirectory(prefix="agentic-probe-") as folder:
            return _report(run_cases(Path(folder), unsandboxed=args.unsandboxed))
    return _report(run_cases(args.workdir, unsandboxed=args.unsandboxed))


def _report(results: list[tuple[str, str, str, str]]) -> int:
    print(f"{'case':32} {'want':8} {'got':8} ok   last line")
    for name, wanted, got, line in results:
        print(f"{name:32} {wanted:8} {got:8} {'yes' if wanted == got else 'NO ':4} {line}")
    bad = [r for r in results if r[1] != r[2]]
    print("all cases behaved as required" if not bad else f"{len(bad)} case(s) did NOT")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
