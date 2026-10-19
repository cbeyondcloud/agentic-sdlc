"""S6 probe (throwaway): what srt blocks on Linux for a worktree-confined step.

Runs inside the probe container as a non-root user. Each case runs one command under
`srt --settings <file>` and compares the outcome with what the framework needs.
"""

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

HOME = Path.home()
WORK = HOME / "work"
WT = WORK / "wt"  # the step's worktree: the only writable place
OTHER = WORK / "other"  # another folder in the repo area: must stay untouched
SSH = HOME / ".ssh"
SOCK = WT / "probe.sock"
SETTINGS = HOME / "srt-settings.json"


def setup() -> None:
    for d in (WT, OTHER, SSH):
        d.mkdir(parents=True, exist_ok=True)
    (SSH / "id_test").write_text("PRIVATE KEY", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(WT)], check=True)
    SETTINGS.write_text(
        json.dumps(
            {
                "network": {
                    "allowedDomains": ["pypi.org", "files.pythonhosted.org"],
                    "deniedDomains": [],
                },
                "filesystem": {
                    "denyRead": [str(SSH)],
                    "allowWrite": [str(WT)],
                    "denyWrite": [str(WT / ".env")],
                },
            }
        ),
        encoding="utf-8",
    )


def unix_server() -> None:
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(SOCK))
    srv.listen(1)
    while True:
        conn, _ = srv.accept()
        conn.close()


CASES = [
    # (name, command run inside the sandbox, expected: "allowed" | "blocked", file to check)
    ("write inside worktree", f"echo ok > {WT}/a.txt", "allowed", WT / "a.txt"),
    ("write outside worktree", f"echo x > {OTHER}/b.txt", "blocked", OTHER / "b.txt"),
    ("write from a grandchild", f"sh -c 'sh -c \"echo x > {OTHER}/c.txt\"'", "blocked",
     OTHER / "c.txt"),
    ("write denied path in worktree", f"echo SECRET=1 > {WT}/.env", "blocked", WT / ".env"),
    ("write git hook in worktree", f"echo 'curl evil' > {WT}/.git/hooks/pre-commit", "blocked",
     WT / ".git/hooks/pre-commit"),
    ("write git config in worktree", f"echo '[core]' >> {WT}/.git/config", "blocked", None),
    ("write ~/.bashrc", f"echo x >> {HOME}/.bashrc", "blocked", None),
    ("read denied ~/.ssh key", f"cat {SSH}/id_test", "blocked", None),
    ("HTTPS to allowed host", "curl -sS -o /dev/null --max-time 20 https://pypi.org/simple/",
     "allowed", None),
    ("HTTPS to other host", "curl -sS -o /dev/null --max-time 20 https://example.com/",
     "blocked", None),
    ("HTTPS ignoring the proxy",
     "curl -sS -o /dev/null --max-time 10 --noproxy '*' https://pypi.org/simple/", "blocked",
     None),
    ("raw TCP to an IP", "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 5)\"",
     "blocked", None),
    ("DNS lookup", "python3 -c \"import socket; socket.getaddrinfo('example.com', 443)\"",
     "blocked", None),
    ("git status in worktree", "git status --short", "allowed", None),
    ("git commit in worktree",
     "git add a.txt && git -c user.name=p -c user.email=p@x commit -qm probe", "allowed", None),
    ("unix socket to host", f"python3 -c \"import socket; s=socket.socket(socket.AF_UNIX); "
     f"s.connect('{SOCK}')\"", "blocked", None),
]


def run(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["srt", "--settings", str(SETTINGS), "--", "bash", "-c", cmd],
        cwd=WT, capture_output=True, text=True, timeout=60,
    )


def main() -> None:
    setup()
    threading.Thread(target=unix_server, daemon=True).start()
    time.sleep(0.2)
    hook_before = (WT / ".git/config").read_bytes()
    results = []
    for name, cmd, expected, path in CASES:
        r = run(cmd)
        outcome = "allowed" if r.returncode == 0 else "blocked"
        if path is not None:  # judge writes by what landed on disk, not the exit code
            outcome = "allowed" if path.exists() else "blocked"
        if name == "write git config in worktree":
            outcome = "allowed" if (WT / ".git/config").read_bytes() != hook_before else "blocked"
        err = (r.stderr or r.stdout).strip().splitlines()
        results.append((name, expected, outcome, err[-1][:110] if err else ""))
    starts = []
    for _ in range(5):
        t = time.perf_counter()
        run("true")
        starts.append(time.perf_counter() - t)
    bwrap = subprocess.run(["bwrap", "--version"], capture_output=True, text=True).stdout.strip()
    print(f"srt 0.0.78, {bwrap}, uid {os.getuid()}")
    print(f"{'case':34} {'want':8} {'got':8} ok  last line")
    for name, want, got, err in results:
        print(f"{name:34} {want:8} {got:8} {'yes' if want == got else 'NO '} {err}")
    print(f"wrap overhead for 'true': median {sorted(starts)[2]:.2f}s over 5 runs")


if __name__ == "__main__":
    main()
