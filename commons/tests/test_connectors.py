"""Tests for the connector adapter over Zyroflo's JSON-path mappings (M2-27)."""

import json

import pytest

from sdlc_commons import connectors
from sdlc_commons.connectors import ABSENT, ConnectorError, ExternalItem

# What each tracker's API returns, cut down to the fields the shipped mappings read.
JIRA_ISSUE = {
    "id": "10042",
    "key": "PAY-42",
    "self": "https://jira.example.invalid/rest/api/2/issue/10042",
    "fields": {
        "summary": "Refunds over 10k need a second approver",
        "description": "Finance asked for four-eyes on large refunds.",
        "status": {"name": "To Do"},
    },
}
GITHUB_ISSUE = {
    "number": 7,
    "title": "Timeout is not configurable",
    "body": "The client waits 30 s whatever the setting says.",
    "html_url": "https://github.example.invalid/acme/payments/issues/7",
    "state": "open",
}


# ── the shipped mappings ─────────────────────────────────────────────────────


def test_zyroflo_ships_the_jira_github_issue_and_alert_mappings():
    shipped = connectors.shipped_mappings()

    assert set(shipped) == {"jira", "github-issue", "alert"}
    assert all(mapping["title"] for mapping in shipped.values())
    assert shipped["jira"]["title"] == "fields.summary"
    assert shipped["github-issue"]["url"] == "html_url"


def test_a_jira_issue_becomes_a_requirement_with_its_key_and_link():
    mapping = connectors.mapping_for(None, "jira")

    item = connectors.item_for("jira", mapping, JIRA_ISSUE)

    assert isinstance(item, ExternalItem)
    assert item.source == "jira"
    assert item.title == "Refunds over 10k need a second approver"
    assert item.kind == "requirement"
    assert item.external_id == "PAY-42"
    assert item.url == "https://jira.example.invalid/rest/api/2/issue/10042"
    assert item.spec.startswith("title: Refunds over 10k need a second approver")
    assert "Finance asked for four-eyes on large refunds." in item.spec
    assert "`PAY-42`" in item.spec


def test_a_github_issue_becomes_a_defect_with_its_number_and_link():
    mapping = connectors.mapping_for(None, "github-issue")

    item = connectors.item_for("github-issue", mapping, GITHUB_ISSUE)

    assert item.title == "Timeout is not configurable"
    assert item.kind == "defect"
    assert item.external_id == "7"
    assert item.url == "https://github.example.invalid/acme/payments/issues/7"
    assert "The client waits 30 s" in item.spec


def test_the_same_item_has_the_same_identity():
    mapping = connectors.mapping_for(None, "github-issue")
    first = connectors.item_for("github-issue", mapping, GITHUB_ISSUE)
    again = connectors.item_for("github-issue", mapping, dict(GITHUB_ISSUE))
    edited = connectors.item_for("github-issue", mapping, {**GITHUB_ISSUE, "body": "Other text."})

    assert first.identity == again.identity
    assert first.identity != edited.identity
    assert first.identity.startswith("sha256:")  # Zyroflo's spec_hash


def test_an_item_with_no_description_or_id_still_files():
    mapping = connectors.mapping_for(None, "github-issue")

    item = connectors.item_for("github-issue", mapping, {"title": "Only a title"})

    assert item.external_id is None and item.url is None
    assert "carried no description" in item.spec


# ── the repo's own mapping ───────────────────────────────────────────────────


def declare(repo, source, mapping):
    folder = repo / ".sdlc" / "connectors"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{source}.json").write_text(json.dumps(mapping), encoding="utf-8")


def test_a_repo_mapping_overrides_the_shipped_one(tmp_path):
    declare(tmp_path, "jira", {"title": "fields.headline", "kind": "defect", "id": "key"})

    mapping = connectors.mapping_for(tmp_path, "jira")
    item = connectors.item_for("jira", mapping, {"key": "OPS-1", "fields": {"headline": "Down"}})

    assert item.title == "Down" and item.kind == "defect" and item.external_id == "OPS-1"


def test_a_repo_can_declare_a_source_zyroflo_does_not_ship(tmp_path):
    declare(tmp_path, "servicedesk", {"title": "subject", "body": "detail"})

    mapping = connectors.mapping_for(tmp_path, "servicedesk")

    assert connectors.item_for("servicedesk", mapping, {"subject": "Printer"}).title == "Printer"


def test_an_unknown_source_is_refused_naming_where_mappings_live(tmp_path):
    with pytest.raises(ConnectorError, match=r"\.sdlc[\\/]connectors"):
        connectors.mapping_for(tmp_path, "servicedesk")


@pytest.mark.parametrize(
    "source", ["../../../secret", "..", "sub/jira", "sub\\jira", "JIRA", "", " jira", "-x"]
)
def test_a_source_that_is_not_a_plain_name_is_refused(tmp_path, source):
    """Final review F3: the source names a file in `.sdlc/connectors/` and nothing else -
    no climbing out, no folders, no absolute path, one spelling on every OS."""
    (tmp_path / "repo" / ".sdlc" / "connectors" / "sub").mkdir(parents=True)
    (tmp_path / "secret.json").write_text('{"title": "x.y", "body": "a"}', encoding="utf-8")
    nested = tmp_path / "repo" / ".sdlc" / "connectors" / "sub" / "jira.json"
    nested.write_text('{"title": "t"}', encoding="utf-8")  # a file each spelling could reach

    with pytest.raises(ConnectorError, match="not a connector name"):
        connectors.mapping_for(tmp_path / "repo", source)


def test_an_absolute_source_is_refused(tmp_path):
    (tmp_path / "repo" / ".sdlc" / "connectors").mkdir(parents=True)
    (tmp_path / "secret.json").write_text('{"title": "x.y", "body": "a"}', encoding="utf-8")

    with pytest.raises(ConnectorError, match="not a connector name"):
        connectors.mapping_for(tmp_path / "repo", str(tmp_path / "secret"))


def test_a_mapping_without_a_title_is_refused(tmp_path):
    declare(tmp_path, "jira", {"body": "fields.description"})

    with pytest.raises(ConnectorError, match="declares no title"):
        connectors.mapping_for(tmp_path, "jira")


def test_a_mapping_that_is_not_json_is_refused(tmp_path):
    folder = tmp_path / ".sdlc" / "connectors"
    folder.mkdir(parents=True)
    (folder / "jira.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ConnectorError, match="could not be read"):
        connectors.mapping_for(tmp_path, "jira")


# ── payloads ─────────────────────────────────────────────────────────────────


def test_a_null_title_is_refused_differently_from_an_absent_one():
    mapping = connectors.mapping_for(None, "jira")

    with pytest.raises(ConnectorError, match="it is null") as null:
        connectors.item_for("jira", mapping, {"key": "PAY-1", "fields": {"summary": None}})
    with pytest.raises(ConnectorError, match="has nothing at") as absent:
        connectors.item_for("jira", mapping, {"key": "PAY-1", "fields": {}})

    assert str(null.value) != str(absent.value)


def test_a_hand_built_mapping_without_a_title_is_refused_by_name():
    with pytest.raises(ConnectorError, match="declares no title"):
        connectors.item_for("jira", {"body": "fields.description"}, JIRA_ISSUE)


def test_dig_tells_an_absent_path_from_a_null_value():
    payload = {"fields": {"summary": None, "labels": ["a", "b"]}}

    assert connectors.dig(payload, "fields.summary", missing=ABSENT) is None
    assert connectors.dig(payload, "fields.nothing", missing=ABSENT) is ABSENT
    assert connectors.dig(payload, "fields.labels.1") == "b"
