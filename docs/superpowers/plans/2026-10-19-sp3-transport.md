# SP3 Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every model call the framework makes goes through one HTTP client that stamps the run's identity, refuses TokenLean behaviour headers, works behind corporate proxies and CAs, captures each call's cost (actual, estimated or unavailable — never a silent zero), and charges it to a run → step → child budget ledger that stops spending at its limits.

**Architecture:** A new `agentic.transport` package in `core`. `identity.py` turns a resolved run/agent into the ten `X-Agentic-*` headers. `client.py` builds `httpx.Client`/`AsyncClient` with request hooks that stamp them and `traceparent`. `network.py` builds the TLS context, using the OS trust store via truststore. `cost.py` reads cost from TokenLean responses and streams. `budget.py` is the hierarchical ledger. Org policy gains an optional `prices.yaml` for estimated costs. Nothing here calls a model: the agent runtime (SP4) passes these clients to Pydantic AI and the provider SDKs.

**Tech Stack:** Python 3.12, httpx 0.28 (BSD-3), truststore 0.10 (MIT), opentelemetry-api 1.45 (Apache-2.0), `decimal.Decimal` for money; tests: pytest, trustme (MIT/Apache-2.0, dev only).

**Spec:** `docs/superpowers/specs/2026-10-16-agentic-sdlc-design.md` — §8.4 (child limits), §12.9 (budget hierarchy), §14.2 (identity headers), §15 (corporate network), §17 (TokenLean P0 items). Master plan rows M1-11…M1-14.

## Facts this plan relies on (TokenLean, read 2026-10-19 from `d:\token-optimisation`)

- **Non-streaming, OpenAI route** (`POST /v1/chat/completions`): the body carries `_token_opt` with `cost_actual_usd` (float, USD), `response_tokens`, `tokens_provider_billed`, and optional `cache_read_tokens` / `cache_write_tokens`. The provider `usage` passes through. TokenLean prices this from its own config table, so it is an estimate too, but it is TokenLean's and is the number its `usage_events` keep → our source `reported`.
- **Response headers on every non-stream 2xx:** `x-tokenlean-request-id`, `x-tokenlean-routed-model`, `x-tokenlean-cache` (`miss`/`hit`/`hit:<level>`), `x-tokenlean-cache-read-tokens`, `x-tokenlean-cache-write-tokens`, and `x-trace-id` (also on streams, when OTel is on). **There is no cost header yet.** `x-tokenlean-cost-usd` is planned (ST-3, M1-65); read it first when present.
- **Anthropic route** (`POST /v1/messages`): `_token_opt` is dropped. `usage` is `{input_tokens, output_tokens}`; cache figures only in the headers.
- **Live streams:** no `x-tokenlean-*` headers, no `_token_opt`, no cost frame. OpenAI: a final usage chunk (`usage.prompt_tokens/completion_tokens`) arrives only if the request sends no `stream_options` or `stream_options.include_usage: true`. TokenLean strips it otherwise. Anthropic: `message_start` carries `input_tokens: 0`, and the final `message_delta.usage` carries `input_tokens` and `output_tokens`.
- **Behaviour headers TokenLean reads** (they change caching, routing, memory or the prompt): `x-step-name`, `x-step-inputs-hash`, `x-template-version`, `x-workflow-id`, `x-session-id`, `x-complexity`, `x-route-to`, `x-no-cache`, `x-compress-user`, `x-prefix-profile`, `x-template-id`, `x-echo-prompt`, `x-confidence-score`, `x-user-id`, `x-tenant-id`, `x-rag-collection`. TokenLean does not read `traceparent` yet (ST-5).

## Global Constraints

- Python 3.12; dependencies pinned in `uv.lock`; only permissive licences (MIT, Apache-2.0, BSD, ISC).
- **Every model call goes through TokenLean** via `base_url`; no direct provider calls from product code.
- Identity is sent only in `X-Agentic-Team`, `X-Agentic-User`, `X-Agentic-Repo`, `X-Agentic-Pipeline`, `X-Agentic-Run-Id`, `X-Agentic-Step`, `X-Agentic-Agent` (name@version), `X-Agentic-Parent-Agent`, `X-Agentic-Tier`, `X-Agentic-Policy-Version`, plus W3C `traceparent`. **Never** `X-Step-Name`, `X-Workflow-ID` or `X-Session-ID` for identity.
- **Unmeasured values are null, never 0** (Zyroflo metrics doctrine): an unknown cost is `None` with a reason, never `Decimal(0)`.
- Money is `decimal.Decimal`, built from `str(value)`; never float arithmetic on USD.
- Must run on Windows, macOS and Linux; `scripts/check.py --all` (Windows + Linux container) is the gate.
- Code style: ruff, line length 100; tests next to the package in `core/tests/transport/`.

## Review Focus

1. **A user or repo value with a newline, carriage return or non-ASCII character** (a display name like "José", a pasted repo URL with a trailing newline) must be refused when the identity is built, by field name and without echoing the value, never sent as a header (header injection). Test in Task 1.
2. **A stream that ends early** (cancelled, network drop, no usage chunk) must produce `CallCost(source="unavailable")` with tokens `None`, and the ledger must settle it at the reserved amount, never at zero. Tests in Tasks 4 and 5.
3. **Children settling at the same time from several threads** must leave totals that reconcile exactly. Test in Task 5.
4. **A call that costs more than its reservation** must have its real cost recorded before `LimitHit` is raised. The spend is never lost because the limit tripped. Test in Task 5.
5. **A caller that passes `stream_options` without `include_usage`** must not lose the usage chunk, and an Anthropic stream's `message_start` zero must not be counted on top of `message_delta`. Tests in Task 4.

---

### Task 1 (M1-11): identity headers and the client factory

**Files:**
- Modify: `core/pyproject.toml` (add `httpx>=0.28,<0.29`, `opentelemetry-api>=1.45,<2`)
- Create: `core/src/agentic/transport/__init__.py`, `core/src/agentic/transport/identity.py`, `core/src/agentic/transport/client.py`
- Test: `core/tests/transport/__init__.py` (empty), `core/tests/transport/test_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (the engine builds `CallIdentity` from `ResolvedAgent` + run context later).
- Produces:
  - `CallIdentity(team: str, user: str, repo: str, pipeline: str, run_id: str, step: str, agent: str, policy_version: str, tier: str | None = None, parent_agent: str | None = None)`, a frozen dataclass whose `__post_init__` validates every value: non-empty, printable ASCII (0x21–0x7E plus inner spaces), no CR/LF, at most 256 characters. It raises `IdentityError(field_name)`, and the message names the field, never the value.
  - `CallIdentity.headers() -> dict[str, str]`: the ten header names above, exactly. `X-Agentic-Tier` and `X-Agentic-Parent-Agent` are omitted when `None`.
  - `BEHAVIOUR_HEADERS: frozenset[str]`: the 16 lower-case TokenLean behaviour header names listed in "Facts".
  - `ForbiddenHeaderError(ValueError)` and `IdentityHeaderError(ValueError)`.
  - `make_client(base_url: str, identity: CallIdentity, *, api_key: str | None = None, network: NetworkSettings | None = None, allow: frozenset[str] = frozenset(), timeout: float = 600.0) -> httpx.Client` and `make_async_client(...)` with the same parameters, returning `httpx.AsyncClient`. `network` is wired in Task 2; in this task accept the parameter and ignore it.
- Behaviour of the request hook (one function used by both clients; the async client gets an `async def` wrapper):
  1. Any header in `BEHAVIOUR_HEADERS - allow` raises `ForbiddenHeaderError(header)`.
  2. A request that already carries an `X-Agentic-*` header with a different value than the identity's raises `IdentityHeaderError(header)` (agent code cannot re-label a call).
  3. Stamp `identity.headers()`.
  4. Stamp `traceparent`: if `opentelemetry.propagate.inject` puts one into a dict carrier (an active span), use it. Otherwise generate `f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"`. A caller-supplied `traceparent` is kept.
- `api_key` is sent as `Authorization: Bearer <key>` (TokenLean accepts Bearer on both routes).

- [ ] **Step 1: Write the failing tests** in `core/tests/transport/test_client.py`, using `httpx.MockTransport` to capture requests (pass `transport=` through a private `_transport` keyword on `make_client` used only by tests):

```python
IDENTITY = CallIdentity(team="payments", user="u123", repo="github.com/acme/api",
                        pipeline="feature-delivery", run_id="r-20261019-abc", step="build",
                        agent="developer@1.2.0", policy_version="git:" + "a" * 40,
                        tier="balanced", parent_agent="lead@1.0.0")

def test_all_ten_identity_headers_and_traceparent_are_sent():
    sent = capture(make_client("https://tl.example/v1", IDENTITY, _transport=...))
    assert {h: sent.headers[h] for h in TEN} == {"X-Agentic-Team": "payments", ..., "X-Agentic-Parent-Agent": "lead@1.0.0"}
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", sent.headers["traceparent"])

def test_optional_headers_are_omitted_when_unset(): ...  # tier=None, parent_agent=None -> absent

@pytest.mark.parametrize("header", ["X-Step-Name", "X-Workflow-ID", "X-Session-ID", "X-Complexity", "X-No-Cache"])
def test_behaviour_headers_are_refused(header): ...  # client.post(..., headers={header: "x"}) raises ForbiddenHeaderError

def test_a_behaviour_header_can_be_allowed_on_purpose(): ...  # allow=frozenset({"x-no-cache"}) -> sent

def test_agent_code_cannot_relabel_a_call(): ...  # headers={"X-Agentic-Team": "other"} -> IdentityHeaderError

@pytest.mark.parametrize(("field", "value"), [("user", "José"), ("repo", "github.com/a/b\n"),
                                              ("team", "a\r\nX-Evil: 1"), ("step", ""), ("agent", "x" * 257)])
def test_unsafe_identity_values_are_refused_by_field_name(field, value):
    with pytest.raises(IdentityError) as error: replace(IDENTITY, **{field: value})
    assert field in str(error.value) and (not value.strip() or value.strip() not in str(error.value))

def test_the_async_client_stamps_the_same_headers(): ...  # asyncio.run(...) with httpx.MockTransport

def test_an_active_span_traceparent_is_propagated(): ...  # monkeypatch opentelemetry.propagate.inject to write a fixed traceparent
```

- [ ] **Step 2: Run** `uv run pytest core/tests/transport/test_client.py -q`. Expected: FAIL (module `agentic.transport` missing).
- [ ] **Step 3: Implement** `identity.py` and `client.py` as specified in Interfaces. Add the dependencies with `uv add --package agentic` (or by editing `core/pyproject.toml`, then `uv lock`).
- [ ] **Step 4: Run** the tests and `uv run python scripts/check.py --all`. Expected: PASS on Windows and Linux.
- [ ] **Step 5: Commit** `feat(core): transport client with identity headers and behaviour-header guard (M1-11)`, mark M1-11 Done in `TRACKER.md`.

### Task 2 (M1-14): corporate network — proxy and CA bundles

**Files:**
- Modify: `core/pyproject.toml` (add `truststore>=0.10,<0.11`; dev group: `trustme>=1.2`)
- Create: `core/src/agentic/transport/network.py`
- Modify: `core/src/agentic/transport/client.py` (use `network`)
- Test: `core/tests/transport/test_network.py`, `core/tests/transport/_servers.py` (test helpers)

**Interfaces:**
- Produces:
  - `NetworkSettings(ca_bundle: Path | None = None, system_trust: bool = True, trust_env: bool = True)`, frozen.
  - `ssl_context(settings: NetworkSettings) -> ssl.SSLContext`: `truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` when `system_trust`, otherwise `ssl.create_default_context()`. Then `load_verify_locations(cafile=...)` for `ca_bundle`, and, when `trust_env`, for each of `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` that names an existing file. A `ca_bundle` that does not exist raises `NetworkError` naming the path. (Probed 2026-10-19: truststore accepts `load_verify_locations` and verifies a trustme CA on Windows.)
  - `make_client` / `make_async_client` pass `verify=ssl_context(network or NetworkSettings())` and `trust_env=(network or NetworkSettings()).trust_env`. httpx then honours `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and `NO_PROXY`.
- Test helpers in `_servers.py`:
  - `https_server(ca: trustme.CA) -> contextmanager[str]`: a threaded `http.server` on `localhost:0`, wrapped with a cert the trustme CA issued for `localhost`, answering `200 ok`. Yields its URL.
  - `connect_proxy() -> contextmanager[tuple[str, list[str]]]`: a threaded CONNECT-only proxy on `127.0.0.1:0` that tunnels bytes and records each `CONNECT host:port`. Yields its URL and the record list.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_custom_ca_bundle_is_trusted(tmp_path):  # bundle = ca.cert_pem written to tmp_path
    with https_server(ca) as url:
        client = make_client(url, IDENTITY, network=NetworkSettings(ca_bundle=bundle))
        assert client.get("/").text == "ok"

def test_without_the_ca_the_server_is_refused(): ...  # httpx.ConnectError
def test_ssl_cert_file_from_the_environment_is_honoured(monkeypatch): ...  # SSL_CERT_FILE=bundle, no ca_bundle
def test_a_missing_ca_bundle_is_a_clear_error(tmp_path): ...  # NetworkError mentions the path

def test_https_proxy_is_used(monkeypatch):
    with https_server(ca) as url, connect_proxy() as (proxy, seen):
        monkeypatch.setenv("HTTPS_PROXY", proxy); monkeypatch.delenv("NO_PROXY", raising=False)
        make_client(url, IDENTITY, network=NetworkSettings(ca_bundle=bundle)).get("/")
        assert seen == [f"localhost:{port}"]

def test_no_proxy_bypasses_the_proxy(monkeypatch): ...  # NO_PROXY=localhost -> seen == [] and the call succeeds
def test_trust_env_false_ignores_proxy_variables(monkeypatch): ...
```

- [ ] **Step 2: Run** `uv run pytest core/tests/transport/test_network.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `network.py` and wire it into both factories.
- [ ] **Step 4: Run** the tests and `scripts/check.py --all`. Expected: PASS on Windows and Linux. In the container, the proxy and server stay on loopback, so no network is needed.
- [ ] **Step 5: Commit** `feat(core): corporate proxy and CA bundles for every model client (M1-14)`, mark M1-14 Done.

### Task 3 (M1-12, part 1): model prices in org policy

**Files:**
- Modify: `core/src/agentic/policy/model.py`, `core/src/agentic/policy/load.py`, `templates/` is untouched (policy template is M2-51)
- Create: `core/tests/fixtures/policy/policy/prices.yaml`
- Test: `core/tests/policy/test_prices.py`

**Interfaces:**
- Produces:
  - `ModelPrice(input_per_mtok: Decimal, output_per_mtok: Decimal, cache_read_per_mtok: Decimal | None = None, cache_write_per_mtok: Decimal | None = None)`, all `ge=0`. The unit is **USD per million tokens**, written as numbers in YAML and read as `Decimal` from their string form.
  - `OrgPolicy.prices: dict[str, ModelPrice] = {}`, loaded from the optional file `prices.yaml` with top-level key `prices` (added to `FILE_FIELDS`).
  - `OrgPolicy.price_for(model: str) -> ModelPrice | None`: an exact key wins, otherwise the longest key contained in the model name, matched case-insensitively (TokenLean's rule, so both sides price the same model alike). Returns `None` when nothing matches: never a default price.
- File comment (top of the fixture and of the docstring): these prices are for client-side estimates on streams and the Anthropic route, and should match TokenLean's pricing table.

- [ ] **Step 1: Write the failing tests:**

```python
def test_prices_load_as_decimals(fixture_policy):
    p = fixture_policy.prices["claude-sonnet-5-5"]
    assert p.input_per_mtok == Decimal("3") and p.output_per_mtok == Decimal("15")

def test_exact_match_wins_then_longest_substring(): ...  # "openai/gpt-5-mini" -> "gpt-5-mini" not "gpt-5"
def test_an_unknown_model_has_no_price(): ...  # price_for("mystery-1") is None
def test_negative_prices_are_refused(tmp_path): ...  # PolicyError
def test_prices_yaml_is_optional(tmp_path): ...  # policy without the file -> prices == {}
```

- [ ] **Step 2: Run** `uv run pytest core/tests/policy/test_prices.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `ModelPrice`, `prices`, `price_for` and the loader entry.
- [ ] **Step 4: Run** the full suite. Expected: PASS (existing policy tests unaffected).
- [ ] **Step 5: Commit** `feat(core): model prices in org policy for estimated costs (M1-12 part 1)`.

### Task 4 (M1-12, part 2): cost capture

**Files:**
- Create: `core/src/agentic/transport/cost.py`
- Create: `core/tests/fixtures/transport/` with recorded TokenLean responses as JSON: `openai_nonstream.json` (body + headers), `anthropic_nonstream.json`, `openai_stream_events.json` (list of chunk dicts ending with a usage chunk), `anthropic_stream_events.json` (`message_start` … `message_delta`), `openai_stream_no_usage.json`, `cache_hit.json`
- Test: `core/tests/transport/test_cost.py`

**Interfaces:**
- Consumes: `OrgPolicy.price_for`, `ModelPrice` (Task 3).
- Produces:
  - `CallCost(source: Literal["reported", "estimated", "unavailable"], cost_usd: Decimal | None, tokens_in: int | None, tokens_out: int | None, tokens_cache_read: int | None, tokens_cache_write: int | None, model: str | None, request_id: str | None, trace_id: str | None, reason: str = "")`, frozen. `cost_usd is None` exactly when `source == "unavailable"`. `reason` is required (non-empty) unless `source == "reported"`.
  - `Pricer = Callable[[str], ModelPrice | None]` (pass `policy.price_for`).
  - `cost_from_response(headers: Mapping[str, str], body: Mapping | None, price_for: Pricer) -> CallCost`. Order of evidence:
    1. `x-tokenlean-cost-usd` header → `reported`.
    2. `body["_token_opt"]["cost_actual_usd"]` → `reported`; tokens from `_token_opt` (`tokens_provider_billed` → in, `response_tokens` → out, `cache_read_tokens`, `cache_write_tokens`) falling back to `usage`.
    3. `usage` priced with `price_for(model)` → `estimated` (`reason="priced from usage"`).
    4. `unavailable` (`reason` says which evidence was missing).

    The model is `x-tokenlean-routed-model`, else `body["model"]`. `request_id` is `x-tokenlean-request-id`; `trace_id` is `x-trace-id`. Cache tokens also come from the `x-tokenlean-cache-read-tokens` / `-write-tokens` headers when the body lacks them. A cost that is negative, NaN or not a number is not evidence: fall through to the next step. Header names are case-insensitive.
  - `StreamUsage()` with `observe(event: Mapping) -> None` for OpenAI chunk dicts and Anthropic event dicts, and `result(headers: Mapping[str, str], price_for: Pricer, model: str | None = None) -> CallCost`. Rules:
    - The OpenAI final chunk's `usage` (`prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens`) is taken as is.
    - For Anthropic, keep the **largest** `input_tokens` / `output_tokens` seen across `message_start.message.usage` and `message_delta.usage` (never their sum).
    - The `cost_usd` header still wins → `reported`. Usage plus a price → `estimated` (`reason="stream usage chunk"`). Usage without a price → `unavailable` with tokens filled. No usage → `unavailable`, tokens `None`.
  - `ensure_stream_usage(body: dict) -> dict`: a copy where, if `body.get("stream")` is true, `stream_options.include_usage` is `True` (any other `stream_options` keys kept); otherwise unchanged.
  - `estimate_usd(price: ModelPrice, tokens_in, tokens_out, cache_read=None, cache_write=None) -> Decimal`: `(in × input + out × output + read × cache_read + write × cache_write) / 1_000_000`. Missing cache prices are charged at the input price.

- [ ] **Step 1: Record fixtures.** Build them from the shapes in "Facts": the non-stream OpenAI body with `_token_opt.cost_actual_usd = 0.004321`, the headers listed, and the two stream event lists. Each fixture file starts with a `"_source"` key naming the TokenLean file:line it mirrors.
- [ ] **Step 2: Write the failing tests:**

```python
def test_tokenlean_body_cost_is_reported_exactly():
    c = cost_from_response(H, BODY, prices)
    assert (c.source, c.cost_usd, c.request_id) == ("reported", Decimal("0.004321"), "req-1")

def test_the_cost_header_wins_when_tokenlean_sends_it(): ...  # x-tokenlean-cost-usd: 0.01 -> 0.01 reported
def test_anthropic_route_is_estimated_from_usage_and_prices(): ...  # 1000 in, 200 out @ 3/15 -> Decimal("0.006")
def test_a_model_without_a_price_is_unavailable_not_zero(): ...  # cost_usd is None, tokens kept, reason mentions the model
def test_a_bad_cost_value_is_not_evidence(): ...  # "NaN", -1, "abc" -> falls through to usage
def test_cache_hit_cost_is_reported_as_given(): ...  # cache_hit fixture
def test_openai_stream_usage_chunk_is_estimated(): ...  # StreamUsage over fixture -> estimated, tokens from final chunk
def test_anthropic_stream_takes_the_largest_counts_not_their_sum(): ...  # message_start input 0 + message_delta 812 -> 812
def test_a_stream_that_ended_without_usage_is_unavailable(): ...  # tokens None, cost None  (Review Focus 2)
def test_ensure_stream_usage_adds_include_usage_and_keeps_other_options(): ...  # Review Focus 5
def test_ensure_stream_usage_leaves_non_stream_bodies_alone(): ...
def test_unmeasured_tokens_are_none_not_zero(): ...  # body without usage -> tokens None
```

- [ ] **Step 3: Run** `uv run pytest core/tests/transport/test_cost.py -q`. Expected: FAIL.
- [ ] **Step 4: Implement** `cost.py`.
- [ ] **Step 5: Run** the tests and `scripts/check.py --all`. Expected: PASS.
- [ ] **Step 6: Commit** `feat(core): cost capture from TokenLean responses and streams (M1-12)`, mark M1-12 Done. Note in its row: ST-3 header not yet in TokenLean (body and estimates used); S1 confirms the Pydantic AI hook path at M1-15.

### Task 5 (M1-13): budget ledger

**Files:**
- Create: `core/src/agentic/transport/budget.py`
- Test: `core/tests/transport/test_budget.py`

**Interfaces:**
- Consumes: `CallCost` (Task 4).
- Produces:
  - `LimitHit(Exception)` with attributes `kind: Literal["budget"]`, `scope: Literal["run", "step", "child"]`, `name: str`, `limit_usd: Decimal`, `spent_usd: Decimal`. The engine pauses the run on `run`/`step` and stops the child on `child` (§8.4).
  - `BudgetLedger(run_limit_usd: Decimal | None)`. `.run` is the root `BudgetSlice(scope="run", name="run")`. A `None` limit means unbounded but still tracked. One `threading.RLock` guards the whole tree.
  - `BudgetSlice`:
    - `.open(name: str, scope: Literal["step", "child"], limit_usd: Decimal | None) -> BudgetSlice` carves a slice. Its limit is `min(requested, self.available())`; a `None` request takes `self.available()`. While the slice is open, its limit is held from the parent. Opening a slice when `available() <= 0` raises `LimitHit` at the parent's scope.
    - `.reserve(estimate_usd: Decimal) -> Reservation` raises `LimitHit(scope=self.scope)` if `estimate_usd > self.available()` (finite only).
    - `.available() -> Decimal | None`: `limit − spent − open reservations − limits held by open child slices`; `None` if unbounded.
    - `.spent() -> Decimal` (own settled spend plus the spend of closed children).
    - `.total_spent() -> Decimal` (also the spend inside open children).
    - `.close() -> None` returns unused budget: the parent's hold is released and the slice's `total_spent()` is added to the parent's `spent()`. Closing twice is a no-op. Reserving on a closed slice raises `BudgetError`.
  - `Reservation`:
    - `.settle(cost: CallCost) -> Decimal`: charges `cost.cost_usd`, or the reserved estimate when the cost is `None` (conservative). Then, if `spent()` now exceeds the limit, it raises `LimitHit` **after recording** the spend. Returns the amount charged.
    - `.release() -> None` (the call failed, nothing spent).
    - Settling or releasing twice raises `BudgetError`.
  - Every `settle` with `cost.source == "unavailable"` increments `BudgetSlice.unpriced_calls` (an int, per slice), so the run can report how much of its spend was assumed.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_child_cannot_spend_past_its_slice():
    run = BudgetLedger(Decimal("5")).run; step = run.open("build", "step", Decimal("2"))
    child = step.open("security-review", "child", Decimal("1"))
    child.reserve(Decimal("0.6")).settle(cost(Decimal("0.6")))
    with pytest.raises(LimitHit) as hit: child.reserve(Decimal("0.5"))
    assert (hit.value.kind, hit.value.scope, hit.value.limit_usd) == ("budget", "child", Decimal("1"))

def test_a_step_limit_hit_raises_limit_hit_budget(): ...  # scope "step"
def test_a_slice_never_gets_more_than_its_parent_has_left(): ...  # step 2, child requests 5 -> limit 2
def test_open_children_hold_their_budget_from_siblings(): ...  # step 2: child A 1.5 open -> child B gets 0.5
def test_closing_a_slice_returns_what_it_did_not_spend(): ...
def test_an_overshoot_is_recorded_before_limit_hit_is_raised(): ...  # reserve 0.5, settle 1.2 -> spent 1.2 then LimitHit (Review Focus 4)
def test_an_unpriced_call_is_charged_its_reservation_and_counted(): ...  # unavailable -> charged estimate, unpriced_calls == 1 (Review Focus 2)
def test_release_frees_the_reservation(): ...
def test_settling_twice_is_refused(): ...
def test_an_unbounded_run_still_tracks_spend(): ...  # BudgetLedger(None)
def test_totals_reconcile_under_concurrent_children():  # Review Focus 3
    # 8 threads x 100 calls of random Decimal costs on separate child slices of one step
    assert ledger.run.total_spent() == sum(all charged amounts)
```

- [ ] **Step 2: Run** `uv run pytest core/tests/transport/test_budget.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `budget.py`.
- [ ] **Step 4: Run** the tests and `scripts/check.py --all`. Expected: PASS on Windows and Linux.
- [ ] **Step 5: Commit** `feat(core): run, step and child budget ledger with LimitHit (M1-13)`, mark M1-13 Done.

### Task 6: SP3 review and tracker

- [ ] **Step 1:** Run the Sonnet 5.5 high-effort review of SP3 (diff from the commit before Task 1 to HEAD) in its own worktree, with this plan, the spec sections listed above and the Review Focus. The reviewer probes with Python only and never runs commands through a shell.
- [ ] **Step 2:** Fix Critical/Important findings with tests first; record rulings and deferred minors in the executor ledger.
- [ ] **Step 3:** Update `docs/architecture/call-flow.md`: mark the header, cost and budget boxes as implemented (file:function).
