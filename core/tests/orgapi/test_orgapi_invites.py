"""Invite links (M2-18, ruling P8): the token travels in the fragment, and the CLI takes
either the link or the bare token."""

import pytest
from pydantic import ValidationError

from agentic.orgapi import (
    InviteRedeemed,
    RunRegistration,
    ServiceAccountRequest,
    TenantRef,
    invite_url,
    parse_invite,
)

TOKEN = "aci_" + "x" * 43  # the shape of an invite token, not a real one


def test_invite_url_puts_the_token_in_the_fragment():
    assert (
        invite_url("https://cp.acme.example/", TOKEN) == f"https://cp.acme.example/invite#{TOKEN}"
    )


@pytest.mark.parametrize(
    ("link", "base"),
    [
        (f"https://cp.acme.example/invite#{TOKEN}", "https://cp.acme.example"),
        (f"https://cp.acme.example/agentic/invite#{TOKEN}", "https://cp.acme.example/agentic"),
        (f"  https://cp.acme.example/invite#{TOKEN}\n", "https://cp.acme.example"),
        (TOKEN, None),
    ],
)
def test_parse_invite_reads_the_base_and_token(link, base):
    assert parse_invite(link) == (base, TOKEN)


def test_secrets_never_show_in_a_repr():
    redeemed = InviteRedeemed(
        tenant=TenantRef(slug="acme", name="Acme"),
        email="dev@acme.example",
        control_plane_token="acp_" + "t" * 43,
        token_expires_at="2026-12-01T00:00:00Z",
        gateway_key="gw-" + "k" * 30,
    )
    assert "t" * 43 not in repr(redeemed) and "k" * 30 not in repr(redeemed)


@pytest.mark.parametrize("days", [0, 366])
def test_service_account_days_are_required_and_bounded(days):
    with pytest.raises(ValidationError):
        ServiceAccountRequest(name="nightly", team="payments", days=days)
    with pytest.raises(ValidationError):
        ServiceAccountRequest(name="nightly", team="payments")


def test_run_budget_is_a_decimal_string():
    body = {"run_id": "r", "team": "payments", "pipeline": "p", "policy_version": "v"}
    assert RunRegistration(**body, budget_usd="12.50").budget_usd == "12.50"
    with pytest.raises(ValidationError):
        RunRegistration(**body, budget_usd="1e3")
