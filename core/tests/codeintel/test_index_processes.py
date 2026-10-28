"""The code index and process cleanup (M2-22): a build cannot outlive the step that
started it (DF-328), and two processes sharing one data folder build once (DF-338)."""

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

from _process_support import gone

from agentic.backends.processes import process_scope
from agentic.codeintel import CodeIntelError
from agentic.codeintel.index import LocalIndex

# A stand-in for code-review-graph: `build`/`update` count themselves, take a while and
# leave a real index; with HANG set, a build starts a child and never ends.
FAKE_CRG = """\
import os, sqlite3, subprocess, sys, time
from pathlib import Path

command, args = sys.argv[1], sys.argv[2:]
data = Path(args[args.index("--data-dir") + 1])
if command in ("build", "update"):
    with open(os.environ["FAKE_CRG_COUNT"], "a", encoding="utf-8") as count:
        count.write(command + "\\n")
    if os.environ.get("FAKE_CRG_HANG"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        Path(os.environ["FAKE_CRG_HANG"]).write_text(str(child.pid), encoding="utf-8")
        time.sleep(120)
    time.sleep(2)
    data.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data / "graph.db") as con:
        con.execute("create table if not exists nodes (file_path text)")
elif command == "status":
    print("{}")
"""

ENSURE = """\
import sys
from pathlib import Path
from agentic.codeintel.index import LocalIndex

tree, data, home, fake = map(Path, sys.argv[1:5])
LocalIndex(tree, data, home_dir=home, cli=[sys.executable, str(fake)]).ensure()
"""


def fake_cli(tmp_path: Path) -> Path:
    fake = tmp_path / "fake_crg.py"
    fake.write_text(FAKE_CRG, encoding="utf-8")
    return fake


def test_a_build_in_a_tool_thread_dies_with_the_step(fixture_repo, tmp_path, monkeypatch):
    marker = tmp_path / "grandchild.pid"
    monkeypatch.setenv("FAKE_CRG_COUNT", str(tmp_path / "count"))
    monkeypatch.setenv("FAKE_CRG_HANG", str(marker))
    index = LocalIndex(
        fixture_repo,
        tmp_path / "data",
        home_dir=tmp_path / "home",
        cli=[sys.executable, str(fake_cli(tmp_path))],
    )
    ended = threading.Event()
    seen: list[BaseException] = []

    def tool() -> None:  # a code-intelligence tool, run in a worker thread
        try:
            index.ensure()
        except CodeIntelError as error:
            seen.append(error)
        finally:
            ended.set()

    async def step() -> None:
        with process_scope():
            work = asyncio.ensure_future(asyncio.to_thread(tool))
            while not marker.exists() or not marker.read_text():
                await asyncio.sleep(0.05)
            try:  # the step's time limit: the agent run is cancelled, its tool thread is not
                await asyncio.wait_for(asyncio.shield(work), 0.2)
            except TimeoutError:
                pass

    started = time.monotonic()
    asyncio.run(step())
    assert gone(int(marker.read_text()))
    assert ended.wait(15)  # not left waiting out the build timeout
    assert time.monotonic() - started < 40
    assert seen  # the killed build is a refusal for the tool, not a hang


def test_two_processes_on_one_data_folder_build_once(fixture_repo, tmp_path):
    count = tmp_path / "count"
    script = tmp_path / "ensure.py"
    script.write_text(ENSURE, encoding="utf-8")
    argv = [
        sys.executable,
        str(script),
        str(fixture_repo),
        str(tmp_path / "data"),
        str(tmp_path / "home"),
        str(fake_cli(tmp_path)),
    ]
    env = {**__import__("os").environ, "FAKE_CRG_COUNT": str(count)}
    runs = [subprocess.Popen(argv, env=env, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    errors = [run.communicate(timeout=120)[1] for run in runs]
    assert [run.returncode for run in runs] == [0, 0], errors
    assert count.read_text(encoding="utf-8").split() == ["build"]
