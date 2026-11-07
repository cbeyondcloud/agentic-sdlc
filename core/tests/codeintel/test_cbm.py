"""The codebase-memory-mcp adapter (M3-27, S5 decision 3), against a fake of its CLI mode:
one-shot `cli` commands with JSON arguments on stdin, never an MCP session."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentic.codeintel import CodeIntel, CodeIntelError
from agentic.codeintel.cbm import CodebaseMemory

SEED = "src/main/java/a/Foo.java"
FAKE = r"""
import json, os, sys
log = os.environ["FAKE_CBM_LOG"] if "FAKE_CBM_LOG" in os.environ else sys.argv[1]
argv = sys.argv[2:]
args = json.loads(sys.stdin.read() or "{}")
with open(log, "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": argv, "args": args, "env": dict(os.environ)}) + "\n")
mode = os.path.join(os.path.dirname(log), "mode")
mode = open(mode).read().strip() if os.path.exists(mode) else "ok"
if mode == "fail":
    sys.stderr.write("boom\nindex failed\n"); sys.exit(3)
tool = argv[-1]
if tool == "index_repository":
    payload = {"project": "proj-1", "status": "indexed", "nodes": 9, "edges": 9}
elif tool == "query_graph":
    q = args["query"]
    if mode == "garbage":
        print("not json"); sys.exit(0)
    if "IN [" in q:
        rows = [["src/main/java/c/Far.java", "CALLS", 1], ["src/main/java/b/Bar.java", "CALLS", 9]]
    elif "count(DISTINCT" in q:
        rows = [[3, 7]]
    else:
        rows = [
            ["src/test/java/a/FooTest.java", "TESTS", 2],
            ["src/test/java/a/FooTest.java", "CALLS", 3],
            ["src/main/java/b/Bar.java", "IMPORTS", 4],
            ["src/main/java/a/Sibling.java", "SIMILAR_TO", 50],
            ["src/main/java/a/Foo.java", "CALLS", 1],
        ]
    payload = {"rows": rows}
else:
    sys.exit(f"unknown tool {tool}")
print(json.dumps({"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}))
"""


@pytest.fixture
def fake(tmp_path: Path) -> tuple[list[str], Path]:
    script = tmp_path / "fake" / "cbm.py"
    script.parent.mkdir()
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "fake" / "calls.jsonl"
    return [sys.executable, str(script), str(log)], log


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    for rel in (SEED, "src/test/java/a/FooTest.java", "src/main/java/b/Bar.java"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("class X {}\n", encoding="utf-8")
    (root / "src/main/java/c").mkdir(parents=True)
    (root / "src/main/java/c/Far.java").write_text("class Far {}\n", encoding="utf-8")
    return root


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def adapter(fake, tree: Path, tmp_path: Path, **kwargs) -> CodebaseMemory:
    return CodebaseMemory(fake[0], tree, tmp_path / "data", srt=fake_srt(tmp_path), **kwargs)


#: A stand-in for `srt --settings <file> -- env NAME=value ... <program>`: it records the
#: settings it was given, then runs the program (DF-921: the real one is the OS sandbox).
FAKE_SRT = r"""
import json, os, subprocess, sys
argv = sys.argv[1:]
assert argv[0] == "--settings" and argv[2] == "--", argv
with open(argv[1], encoding="utf-8") as settings:
    rules = json.load(settings)
rest, env = argv[3:], dict(os.environ)
if rest[:1] == ["env"]:
    rest = rest[1:]
    while rest and "=" in rest[0]:
        name, value = rest.pop(0).split("=", 1)
        env[name] = value
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "srt.jsonl")
with open(log, "a", encoding="utf-8") as out:
    out.write(json.dumps({"settings": rules, "argv": rest, "home": os.environ.get("HOME")}) + "\n")
done = subprocess.run(rest, input=sys.stdin.read(), env=env, text=True, capture_output=True)
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
"""


def fake_srt(tmp_path: Path) -> list[str]:
    script = tmp_path / "srt" / "srt.py"
    if not script.is_file():
        script.parent.mkdir()
        script.write_text(FAKE_SRT, encoding="utf-8")
    return [sys.executable, str(script)]


def sandboxed(tmp_path: Path) -> list[dict]:
    return calls(tmp_path / "srt" / "srt.jsonl")


def test_it_is_a_codeintel_adapter(fake, tree, tmp_path):
    intel: CodeIntel = adapter(fake, tree, tmp_path)
    assert intel.name == "codebase-memory-mcp"
    assert intel.supports == frozenset({"dependents", "impact", "tests_for", "overview"})


def test_tests_for_is_the_tests_edges_into_the_seed(fake, tree, tmp_path):
    found = adapter(fake, tree, tmp_path).tests_for(SEED)
    assert found.files == ("src/test/java/a/FooTest.java",)
    assert found.source == "codebase-memory-mcp"


def test_dependents_rank_code_edges_then_a_second_hop(fake, tree, tmp_path):
    found = adapter(fake, tree, tmp_path).dependents(SEED).files
    # FooTest (2+3 edges) before Bar (4); similarity edges and the seed itself are not
    # dependencies; then the files with an edge into those, best first
    assert found == (
        "src/test/java/a/FooTest.java",
        "src/main/java/b/Bar.java",
        "src/main/java/c/Far.java",
    )


def test_every_query_reindexes_first_and_never_starts_an_mcp_session(fake, tree, tmp_path):
    adapter(fake, tree, tmp_path).tests_for(SEED)
    made = calls(fake[1])
    assert [c["argv"][-1] for c in made] == ["index_repository", "query_graph"]
    assert all(c["argv"][:3] == ["cli", "--quiet", "--json"] for c in made)
    assert made[0]["args"] == {"repo_path": str(tree.resolve())}
    assert made[1]["args"]["project"] == "proj-1" and made[1]["args"]["format"] == "json"
    assert "initialize" not in json.dumps(made)


def test_the_tool_keeps_its_home_and_cache_inside_the_data_folder(
    fake, tree, tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("SOME_TOKEN", "secret-value")
    adapter(fake, tree, tmp_path).tests_for(SEED)
    env = calls(fake[1])[0]["env"]
    data = (tmp_path / "data").resolve()
    for name in ("HOME", "USERPROFILE", "XDG_CACHE_HOME", "CBM_CACHE_DIR"):
        assert Path(env[name]).resolve().is_relative_to(data), name
    assert "HTTPS_PROXY" not in env and "SOME_TOKEN" not in env


def test_a_failing_tool_is_a_codeintel_error_with_its_last_line(fake, tree, tmp_path):
    (fake[1].parent / "mode").write_text("fail")
    with pytest.raises(CodeIntelError, match="index failed"):
        adapter(fake, tree, tmp_path).tests_for(SEED)


def test_an_unreadable_answer_is_a_codeintel_error(fake, tree, tmp_path):
    (fake[1].parent / "mode").write_text("garbage")
    with pytest.raises(CodeIntelError, match="no answer"):
        adapter(fake, tree, tmp_path).tests_for(SEED)


def test_a_missing_binary_is_a_codeintel_error(tree, tmp_path):
    missing = [str(tmp_path / "nowhere" / "cbm")]
    intel = CodebaseMemory(missing, tree, tmp_path / "data", srt=fake_srt(tmp_path))
    with pytest.raises(CodeIntelError, match="could not run"):
        intel.tests_for(SEED)


def test_quotes_in_a_path_cannot_change_the_query(fake, tree, tmp_path):
    odd = "src/main/java/a/It's\\Odd.java"
    adapter(fake, tree, tmp_path).tests_for(odd)
    query = calls(fake[1])[-1]["args"]["query"]
    assert "'src/main/java/a/It\\'s\\\\Odd.java'" in query


def test_impact_unions_the_diff_files_and_their_dependents(fake, tree, tmp_path):
    diff = f"--- a/{SEED}\n+++ b/{SEED}\n--- a/../outside.java\n+++ b/README.md\n"
    found = adapter(fake, tree, tmp_path).impact(diff).files
    assert found[0] == SEED and "src/main/java/b/Bar.java" in found
    assert not any("outside" in f or f == "README.md" for f in found)


def test_answers_name_only_files_still_in_the_tree(fake, tree, tmp_path):
    (tree / "src/main/java/b/Bar.java").unlink()
    assert "src/main/java/b/Bar.java" not in adapter(fake, tree, tmp_path).dependents(SEED).files


def test_overview_counts_the_graph(fake, tree, tmp_path):
    assert "3 files, 7 symbols" in adapter(fake, tree, tmp_path).overview()


# ── review fixes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stdout",
    [
        json.dumps({"content": "not a list", "isError": False}),
        json.dumps({"content": [{"text": json.dumps({"rows": [["a.java", "CALLS", "many"]]})}]}),
        json.dumps({"content": [{"text": json.dumps({"rows": [[1]]})}]}),
    ],
)
def test_malformed_answers_are_codeintel_errors(stdout, tree, tmp_path, monkeypatch):
    from agentic.codeintel import cbm

    def fake_run(argv, **kwargs):
        index = '{"content": [{"type": "text", "text": "{\\"project\\": \\"p\\"}"}]}'
        out = index if argv[-1] == "index_repository" else stdout
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(cbm, "run_contained", fake_run)
    intel = CodebaseMemory([sys.executable], tree, tmp_path / "data")
    with pytest.raises(CodeIntelError):
        intel.dependents(SEED)
    with pytest.raises(CodeIntelError):
        intel.overview()


def test_a_tool_that_cannot_start_is_not_tried_again(tree, tmp_path, monkeypatch):
    from agentic.codeintel import cbm

    started = []

    def fake_run(argv, **kwargs):
        started.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(cbm, "run_contained", fake_run)
    intel = CodebaseMemory([sys.executable], tree, tmp_path / "data")
    for _ in range(3):
        with pytest.raises(CodeIntelError, match="could not run"):
            intel.tests_for(SEED)
    assert len(started) == 1


def test_queries_get_a_shorter_timeout_than_indexing(tree, tmp_path, monkeypatch):
    from agentic.codeintel import cbm

    timeouts = {}

    def fake_run(argv, **kwargs):
        timeouts[argv[-1]] = kwargs["timeout"]
        text = '{"project": "p", "rows": []}'
        envelope = json.dumps({"content": [{"type": "text", "text": text}]})
        return subprocess.CompletedProcess(argv, 0, envelope, "")

    monkeypatch.setattr(cbm, "run_contained", fake_run)
    CodebaseMemory([sys.executable], tree, tmp_path / "data").tests_for(SEED)
    assert timeouts["query_graph"] < timeouts["index_repository"]


def test_tool_paths_are_normalised_into_the_tree(tree, tmp_path, monkeypatch):
    from agentic.codeintel import cbm

    def fake_run(argv, **kwargs):
        windows_spelling = r"src\test\java\a\FooTest.java"
        rows = [[windows_spelling, "TESTS", 1], ["./src/main/java/b/Bar.java", "TESTS", 1]]
        text = json.dumps({"project": "p", "rows": rows})
        return subprocess.CompletedProcess(argv, 0, json.dumps({"content": [{"text": text}]}), "")

    monkeypatch.setattr(cbm, "run_contained", fake_run)
    found = CodebaseMemory([sys.executable], tree, tmp_path / "data").tests_for(SEED).files
    assert set(found) == {"src/test/java/a/FooTest.java", "src/main/java/b/Bar.java"}


def test_impact_expands_at_most_the_capped_number_of_files(fake, tree, tmp_path, monkeypatch):
    from agentic.codeintel import cbm

    monkeypatch.setattr(cbm, "MAX_IMPACT_SEEDS", 1)
    diff = f"+++ b/{SEED}\n+++ b/src/main/java/b/Bar.java\n"
    found = adapter(fake, tree, tmp_path).impact(diff)
    assert "first 1 changed file" in found.note
    assert [c["argv"][-1] for c in calls(fake[1])].count("index_repository") == 1


# ── DF-921: always inside the OS sandbox, no host allowed (owner decision 2026-11-07) ──


def test_every_call_runs_inside_the_sandbox_with_no_allowed_host(fake, tree, tmp_path):
    adapter(fake, tree, tmp_path).tests_for(SEED)
    made = sandboxed(tmp_path)
    assert len(made) == 2  # index, then the query: both sandboxed
    data = (tmp_path / "data").resolve()
    for call in made:
        assert call["argv"][: len(fake[0])] == fake[0]
        assert call["settings"]["network"] == {"allowedDomains": [], "deniedDomains": []}
        writable = call["settings"]["filesystem"]["allowWrite"]
        assert writable and all(Path(p).resolve().is_relative_to(data) for p in writable)


def test_policy_read_denials_are_hidden_from_the_tool(fake, tree, tmp_path):
    secrets = tmp_path / "home" / ".ssh"
    adapter(fake, tree, tmp_path, deny_read=(secrets,)).tests_for(SEED)
    assert secrets.as_posix() in sandboxed(tmp_path)[0]["settings"]["filesystem"]["denyRead"]


def test_without_the_sandbox_the_tool_never_runs(fake, tree, tmp_path):
    missing = [str(tmp_path / "nowhere" / "srt")]
    intel = CodebaseMemory(fake[0], tree, tmp_path / "data", srt=missing)
    with pytest.raises(CodeIntelError, match="could not run"):
        intel.tests_for(SEED)
    assert not fake[1].exists()  # never started unsandboxed


def test_adapters_sharing_a_data_folder_keep_their_own_sandbox_settings(tree, tmp_path):
    # parallel steps of one run share it: one settings file each, never a half-written one
    first, second = (CodebaseMemory([sys.executable], tree, tmp_path / "data") for _ in "ab")
    assert first.sandbox.settings != second.sandbox.settings


def test_the_tool_gets_its_home_inside_the_sandbox_and_srt_keeps_its_own(
    fake, tree, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    adapter(fake, tree, tmp_path).tests_for(SEED)
    outside = sandboxed(tmp_path)[0]
    assert outside["home"] == str(tmp_path / "real-home")  # srt, a Node program, keeps it
    assert outside["argv"][: len(fake[0])] == fake[0]
    home = Path(calls(fake[1])[0]["env"]["HOME"]).resolve()
    assert home.is_relative_to((tmp_path / "data").resolve())


@pytest.mark.skipif(shutil.which("srt") is None, reason="needs the real srt (macOS, Linux, WSL2)")
def test_under_the_real_sandbox_the_tool_answers_and_reaches_no_host(tree, tmp_path):
    data = tmp_path / "data"
    script = data / "fake" / "cbm.py"  # inside the only writable folder, with its log
    script.parent.mkdir(parents=True)
    script.write_text(FAKE, encoding="utf-8")
    log = data / "fake" / "calls.jsonl"
    intel = CodebaseMemory([sys.executable, str(script), str(log)], tree, data)
    assert intel.tests_for(SEED).files == ("src/test/java/a/FooTest.java",)
    settings = json.loads(intel.sandbox.settings.read_text(encoding="utf-8"))
    assert settings["network"]["allowedDomains"] == []
