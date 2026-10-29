"""`GitHubHost.branch_protection` (M3-09, D33): whether a branch requires pull request review
by code owners, read from classic branch protection and from rulesets - against recorded
GitHub responses (`core/tests/fixtures/github/`), never the live API."""

import json
from pathlib import Path

import httpx2
import pytest

from agentic.hosting import RepoRef
from agentic.hosting.base import BranchProtection
from agentic.hosting.github import GITHUB_COM_API, GitHubHost, HostingApiError

TOKEN = "gho_" + "Fake" + "Token" + "0123456789"  # gitleaks:allow - built from pieces
REPO = RepoRef("github.com", "acme", "agentic-org-policy")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
PROTECTION = "/repos/acme/agentic-org-policy/branches/main/protection"
RULES = "/repos/acme/agentic-org-policy/rules/branches/main"


def recorded(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def host(answers: dict[str, tuple[int, str]], seen: list | None = None) -> GitHubHost:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request.url.path)
        status, name = answers[request.url.path]
        return httpx2.Response(status, json=recorded(name))

    return GitHubHost(GITHUB_COM_API, TOKEN, transport=httpx2.MockTransport(handler))


def test_classic_protection_with_code_owners():
    seen: list = []
    found = host(
        {PROTECTION: (200, "protection-code-owners.json"), RULES: (200, "rules-no-review.json")},
        seen,
    ).branch_protection(REPO, "main")
    assert found == BranchProtection(
        protected=True,
        code_owner_reviews=True,
        required_approvals=2,
        source="classic",
        classic_readable=True,
    )
    assert seen == [PROTECTION, RULES]


def test_classic_protection_without_code_owners():
    found = host(
        {PROTECTION: (200, "protection-no-code-owners.json"), RULES: (200, "rules-no-review.json")}
    ).branch_protection(REPO, "main")
    assert found.protected and not found.code_owner_reviews
    assert found.required_approvals == 1


def test_unprotected_branch_with_a_code_owner_ruleset():
    found = host(
        {PROTECTION: (404, "protection-not-protected.json"), RULES: (200, "rules-code-owners.json")}
    ).branch_protection(REPO, "main")
    assert found == BranchProtection(
        protected=True,
        code_owner_reviews=True,
        required_approvals=1,
        source="rulesets",
        classic_readable=True,
    )


def test_unprotected_branch_without_rules():
    found = host(
        {PROTECTION: (404, "protection-not-protected.json"), RULES: (200, "rules-no-review.json")}
    ).branch_protection(REPO, "main")
    assert found.protected is False
    assert found.code_owner_reviews is False
    assert found.source == "none"


def test_no_admin_rights_falls_back_to_rulesets():
    found = host(
        {PROTECTION: (403, "protection-no-admin.json"), RULES: (200, "rules-code-owners.json")}
    ).branch_protection(REPO, "main")
    assert found.code_owner_reviews is True
    assert found.source == "rulesets"
    assert found.classic_readable is False


def test_no_admin_and_no_rules_cannot_tell():
    found = host(
        {PROTECTION: (403, "protection-no-admin.json"), RULES: (200, "rules-no-review.json")}
    ).branch_protection(REPO, "main")
    assert found.protected is False
    assert found.classic_readable is False  # the caller says "cannot check", not "unprotected"


def test_nothing_readable_is_an_api_error():
    with pytest.raises(HostingApiError) as caught:
        host(
            {
                PROTECTION: (403, "protection-no-admin.json"),
                RULES: (403, "protection-no-admin.json"),
            }
        ).branch_protection(REPO, "main")
    assert caught.value.status == 403
    assert TOKEN not in str(caught.value)


def test_a_branch_name_is_quoted():
    seen: list = []

    def handler(request):
        seen.append(request.url.raw_path)
        return httpx2.Response(200, json=[] if b"/rules/" in request.url.raw_path else {})

    GitHubHost(GITHUB_COM_API, TOKEN, transport=httpx2.MockTransport(handler)).branch_protection(
        REPO, "release/2026"
    )
    assert seen[0].endswith(b"/branches/release%2F2026/protection")
