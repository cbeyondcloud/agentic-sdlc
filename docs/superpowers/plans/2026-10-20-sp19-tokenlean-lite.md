# SP19 (M1 part) — TokenLean cost on every response + LITE profile + local LITE stack

**Rows:** M1-65 (ST-3), M1-66 (ST-6 initial), M1-67 (ST-8 local) · **Date:** 2026-10-20 · **Repo:** TokenLean, worktree `D:\token-optimisation-wt\sp19-lite`, branch `feat/agentic-lite-profile` (base `b7db81e`)

## Goal

1. A caller learns the **actual cost** of every call: `x-tokenlean-cost-usd` on non-streamed responses, and a final cost frame on streams (off by default).
2. A **LITE profile** (`config/profiles/agentic.yaml`): the gateway runs, every optimisation that changes what the model sees or answers is off. The request body reaches the provider unchanged except provider prompt caching.
3. A **local LITE stack** (`deploy/lite/local/`): proxy + Postgres + Redis, optional Qdrant / Langfuse, with `up` / `down` / `smoke` in bash and PowerShell, engine-agnostic (`CONTAINER_ENGINE=docker|podman`).

## What already exists (read 2026-10-20)

- Non-stream headers (`main._savings_headers`): request-id, routed-model, cache, tokens-saved, pct-saved, cost-saved-usd, latency-ms (+ conditional ones). **No actual-cost header.** The actual cost is `ctx.savings.cost_actual_usd` (G18 `price_response`; streams via `_price_stream`).
- Streams (`main._stream_response`): chunks relayed unchanged, then `data: [DONE]`; priced in `finally` **after** `[DONE]`. No x-tokenlean headers and no cost on streams. Anthropic/Gemini streams are re-framed by `_translate_stream`, which drops any non-`data:` frame.
- No `/v1/responses` endpoint, so the smoke covers chat completions + messages only.
- Config: one YAML file (`CONFIG_LOCAL_PATH` or GCS) + `params_dir` merge. No overlay/profile mechanism. G05 has no switch for the L2 semantic tier.

## Architecture

- **ST-3 header:** one more field in `_savings_headers` (`x-tokenlean-cost-usd`, 6 dp). Always on (purely additive, like the other headers). It covers all paths that use `_savings_headers`: normal, cascade/agent, cache hit, bypass, content filter. Translated protocols keep `x-*` headers.
- **ST-3 stream:** new operator knob `observability.stream_cost_event` (default **false**, so streams stay byte-identical). When true, a clean stream ends with an **SSE comment** frame placed right before the terminator:
  `: x-tokenlean-cost {"request_id":"…","cost_usd":"0.000123","usage":"reported|estimated"}`
  SSE parsers (OpenAI/Anthropic/Gemini SDKs, EventSource) ignore comment lines, so callers see no change. A raw-byte consumer (agentic transport) reads it. The stream is priced once, before the frame. One-shot streams (a cache hit served as a stream) get the same frame from the response's own header. The Anthropic/Gemini translator passes the frame through after its own terminal frames.
- **ST-6 overlays:** new env var `CONFIG_OVERLAYS` (comma-separated YAML paths). The overlays are deep-merged over the loaded config in order: after `params_dir`, before `${VAR}` expansion, on every reload. Maps merge, anything else replaces. Unset means nothing changes. A missing or invalid overlay fails the load (startup refuses to start, hot reload keeps the last good config). It must never fall back to "all optimisations on" without saying so.
- **ST-6 L2 switch:** `groups.G5_cache.l2_enabled` (default true, unchanged). False skips the semantic lookup, the semantic store and the embedding warm-up. L1 exact-match caching stays.
- **ST-6 profile:** `config/profiles/agentic.yaml` is an overlay on `config/config.yaml.template`. A guard test pins the set of groups allowed to be on, so a future template group that ships enabled fails the test until someone decides about it.
- **ST-8:** `deploy/lite/` (layout from the master plan). The compose project is `tokenlean-lite`, with no `container_name`, so it runs next to the full `token-opt` stack. Only the proxy port is published (`127.0.0.1:4100`). The proxy image is built from `src/proxy/Dockerfile` (the OSS image; the commercial files are dockerignored). `TOKENLEAN_IMAGE` can point at a pre-built image. Compose profiles: `qdrant`, `langfuse`, `mock`. The `mock` profile runs a stdlib OpenAI-compatible stand-in on the proxy image, plus an overlay that points one generic provider at it, so `smoke` runs without provider keys or spend. Live mode uses whatever `LLM_KEY_*` is set.

## Tech stack

Python 3.12 (tests) / image `python:3.11-slim` (existing Dockerfile), FastAPI, pytest (asyncio auto), Docker Compose v2 spec. Images pinned (two weeks or older on 2026-10-20, multi-arch amd64+arm64): `pgvector/pgvector:0.8.7-pg15` (2026-10-01, PostgreSQL licence), `redis:7.2.16-alpine` (2026-09-21, BSD-3; 7.4+ is not BSD), `qdrant/qdrant:v1.12.6` (Apache-2.0, matches TokenLean's compose), `langfuse/langfuse:2.95.11` (2025-11-07, MIT core; v3 needs ClickHouse).

## Global constraints

- Generic, config-driven, backward compatible. New behaviour is off by default or purely additive. No agentic names in core code; only the profile file and `deploy/lite` mention agentic.
- Provider-agnostic. The profile names no provider. Smoke mock mode uses a generic OpenAI-compatible provider. Live smoke picks its model from whichever key is set.
- Scripts: PowerShell 5.1+ (ASCII only, `-UseBasicParsing`) and bash 3.2+ (macOS default; no `jq`, no GNU-only flags; `shasum -a 256` / `sha256sum`). `.sh` files are LF via `deploy/lite/.gitattributes`.
- TDD: each test fails before its code. Run the full suite (`python -m pytest tests/ -q`, check the exit code) before each commit. Run `python scripts/ci/leak_scan.py` before each commit.
- No `# fmt: skip` / `# fmt: off`. Never print or commit secrets. Never push.

## Review focus

1. Streams stay byte-identical with the knob off. With it on, the cost frame is priced exactly once and never breaks an SDK parser.
2. Overlay merge semantics, and failing loudly on a missing overlay.
3. The pass-through test really compares the body the provider receives with the body the client sent.
4. The scripts on macOS bash 3.2 / zsh and PowerShell 5.1, and the podman path.

---

## Task 1 — `x-tokenlean-cost-usd` on non-streamed responses (M1-65)

**Files:** `src/proxy/main.py` (`_savings_headers`), `tests/unit/test_cost_headers.py` (new).

**Failing tests:**
- `test_savings_headers_carry_actual_cost`: a ctx with `savings.cost_actual_usd = 0.0012345` → `headers["x-tokenlean-cost-usd"] == "0.001234"` (or `"0.001235"`, matching `f"{v:.6f}"`).
- `test_savings_headers_cost_zero_when_no_provider_call`: cost 0 → `"0.000000"` (cache hit/bypass: nothing was paid).
- `test_chat_completion_response_has_cost_header` (app `client` fixture, non-stream): status 200, `float(r.headers["x-tokenlean-cost-usd"]) >= 0` and the header equals `_token_opt.cost_actual_usd` from the body to 6 dp.

**Verify:** `python -m pytest tests/unit/test_cost_headers.py -q`, then the full suite.

## Task 2 — cost frame on streams, behind `observability.stream_cost_event` (M1-65)

**Files:** `src/proxy/main.py` (`_stream_response.event_gen`, `_translate_stream`, `_as_requested_stream`/`_openai_one_shot_stream`, `_translate_response` one-shot), `config/config.yaml.template` (`observability.stream_cost_event: false` + comment), `docs/config-reference.md`, `tests/unit/test_stream_cost_event.py` (new).

**Interface:** `_stream_cost_enabled(cfg) -> bool` (reads `observability.stream_cost_event`, strict `is True`); `_cost_comment(request_id, cost_usd, usage_source=None) -> str` returns `': x-tokenlean-cost {json}\n\n'`.

**Failing tests** (live stream driven like `tests/unit/test_streaming_passthrough.py`, with `fake_acompletion` + `_record_outcome` stubbed):
- `test_stream_without_flag_is_unchanged`: config `{}` → no line starts with `:`; body ends `data: [DONE]\n\n`.
- `test_stream_with_flag_ends_with_cost_comment`: `observability.stream_cost_event: true` → the second-to-last frame starts with `: x-tokenlean-cost `; its JSON has `request_id == "req-1"`, `cost_usd == f"{ctx.savings.cost_actual_usd:.6f}"`, `float(cost_usd) > 0`, `usage == "reported"`; the last frame is `data: [DONE]`.
- `test_stream_cost_priced_once`: patch `main._price_stream` with a counter → called exactly once with the flag on (and once with it off).
- `test_stream_cost_estimated_when_no_usage_chunk`: no usage chunk → `usage == "estimated"`.
- `test_failed_stream_has_no_cost_comment`: acompletion raises → no `x-tokenlean-cost` frame; status recorded 502.
- `test_translate_stream_passes_cost_comment_last`: `_translate_stream(ANTHROPIC translator, iter([chunk, comment, [DONE]]))` → the output's last frame is the comment, and the `message_stop` event comes before it.
- `test_one_shot_stream_has_cost_comment_when_enabled`: `_as_requested_stream(JSONResponse(body, headers={"x-tokenlean-cost-usd": "0.000000", "x-tokenlean-request-id": "r"}), withhold_usage=False)` with the flag on → the comment comes before `data: [DONE]`; with the flag off → no comment.

## Task 3 — config overlays `CONFIG_OVERLAYS` (M1-66)

**Files:** `src/proxy/config_loader.py` (`deep_merge`, `apply_overlays`, call in `_load_once`), `docs/config-reference.md`, `tests/unit/test_config_overlays.py` (new).

**Failing tests** (`tmp_path` YAML files, `monkeypatch.setenv`):
- `test_no_overlays_env_leaves_config_unchanged`: the loaded config equals the base file.
- `test_overlay_deep_merges_maps_and_replaces_lists`: base `groups.G5_cache {enabled: true, l1_ttl_seconds: 3600}` + overlay `groups.G5_cache {enabled: false}` → `{enabled: false, l1_ttl_seconds: 3600}`; a list in the overlay replaces the list.
- `test_overlays_apply_in_order`: two overlays set the same key → the last one wins.
- `test_overlay_values_get_env_expansion`: overlay `"${LITE_X:-dflt}"` → `"dflt"`.
- `test_missing_overlay_fails_required_load`: `load_config(required=True)` raises `ConfigLoadError` naming the path (`_sleep` patched).
- `test_missing_overlay_on_reload_keeps_last_good`.
- `test_overlay_root_must_be_mapping`: overlay `- a` → load fails.

## Task 4 — `G5_cache.l2_enabled` (M1-66)

**Files:** `src/proxy/middleware/g05_cache.py`, `src/proxy/main.py` (warm-up skip), `config/config.yaml.template` (`l2_enabled: true` + comment), `docs/config-reference.md`, `tests/unit/middleware/test_g05_l2_switch.py` (new).

**Failing tests:** with `l2_enabled: false`, the semantic lookup is never called (patched embed/search spy has 0 calls) and the store writes no L2 entry; an L1 hit is still served; the default (key absent) still calls L2.

## Task 5 — LITE profile `config/profiles/agentic.yaml` (M1-66)

**Files:** `config/profiles/agentic.yaml` (new), `tests/unit/test_agentic_profile.py` (new).

**G-number mapping:**

| Row term | TokenLean key | LITE |
|---|---|---|
| compression | `G1_compression` | off |
| G05 step cache | `G5_cache.step_cache_enabled` | off (`X-Step-Name` replays) |
| G05 semantic (L2) | `G5_cache.l2_enabled` | off (an embedding match can serve another question's answer) |
| G10 | `G10_memory` | off |
| G11 | `G11_output` | off |
| G12 | `G12_reasoning` | off |
| also changes the model, the prompt or the answer | G04 bypass, G06 routing, G07, G08, G13, G14, G15, G16, G17, G19, G22, G23, G25, G26/G27/G28/G09/g20 (already off), G02/G03 (inert without templates or RAG) | off |
| gateway | G00 rate limit/spend caps, G05 L1, G18, G21 (provider prompt caching), G24, G29–G32 (template default `flag`) | on |

**Failing tests:**
- `test_profile_effective_config` (template + profile via `CONFIG_OVERLAYS`): the off/on table above, `observability.stream_cost_event is True`, `G18_observability.langfuse_enabled is False`.
- `test_only_approved_groups_enabled`: `{k for k, v in groups.items() if v.get("enabled")} <= APPROVED`. This makes a new enabled-by-default template group a deliberate decision.
- `test_profile_names_no_provider`: the profile text contains none of the provider names in the template's `providers[].name`.
- `test_lite_pass_through_is_byte_faithful` (app client, LITE config, rich agentic request: system + user + assistant tool_calls + tool result + tools + tool_choice + temperature + max_tokens + an email address in the text): the `messages` litellm receives `==` the request's messages, `model ==` requested, and each client param is forwarded unchanged. The only extra keys allowed in the call are transport/caching ones (`api_key`, `api_base`, timeouts/retries, `prompt_cache_key`, `stream_options`, `metadata`-style call limits; the exact allow-list is written in the test).
- `test_lite_stream_pass_through_is_byte_faithful`: the same for `stream: true`, and the response chunks reach the client unchanged, plus the cost comment.

## Task 6 — `deploy/lite/` local stack (M1-67)

**Files:** `deploy/lite/README.md`, `deploy/lite/.gitattributes`, `deploy/lite/config/agentic.yaml` (byte copy of the profile), `deploy/lite/config/langfuse.yaml` (overlay: `langfuse_enabled: true`), `deploy/lite/local/docker-compose.lite.yml`, `deploy/lite/local/.env.lite.template`, `deploy/lite/local/{up,down,smoke}.{sh,ps1}`, `deploy/lite/local/mock/mock_llm.py`, `deploy/lite/local/mock/mock-provider.yaml`, `.gitignore` (`!deploy/lite/local/.env.lite.template`, `deploy/lite/local/.state/`), `tests/unit/test_lite_deploy.py` (new).

**Behaviour:**
- `up`: create `.env.lite` from the template if it is missing. Fill every empty generated secret (DB/Redis/Qdrant passwords, proxy user key `tok-LITE-LOC-01-<hex>` + admin key, metrics token, Langfuse secrets). Write `.state/local-keys.json` (sha256 → tenant). Pick the compose profiles (`--qdrant`, `--langfuse`, `--mock`) and the overlays. Then `<engine> compose up -d --build` and wait for `/health` == ok (timeout configurable).
- `down`: stop (volumes kept); `--purge` / `-Purge` removes the volumes.
- `smoke`: `/health`; a non-stream chat completion (checks x-tokenlean-request-id + x-tokenlean-cost-usd); a streamed chat completion (checks the cost comment + `[DONE]`); a non-stream `/v1/messages` (checks `"type":"message"` + x-tokenlean headers); then poll `/admin/usage-export` (own tenant) until all three request ids are present. Exit 0/1 with one line per check.

**Failing tests (`test_lite_deploy.py`):**
- deploy copy == `config/profiles/agentic.yaml` byte for byte;
- every compose `image:` has an exact tag (no `latest`, no bare major) and the proxy uses `${TOKENLEAN_IMAGE:-…}` + `build` with `src/proxy`;
- the proxy `depends_on` holds only `postgres` and `redis`; `qdrant`, `langfuse` and `mock-llm` are in their named profiles;
- only the proxy publishes a port, and it is bound to `${LITE_BIND:-127.0.0.1}`;
- every script exists as a `.sh`/`.ps1` pair, the `.ps1` files are ASCII, and both use `CONTAINER_ENGINE`;
- `.env.lite.template` is not git-ignored and `.env.lite` is (`git check-ignore`);
- the mock overlay + profile overlay produce a config whose only provider is the mock one.

## Task 7 — docs + release notes

`README.md` (LITE section + new header + knob), `release-notes.md` (`## 2026-10-20`, one `###` item, Enhancement (OSS)), `docs/config-reference.md` (three knobs + `CONFIG_OVERLAYS`), `docs/deployment-local.md` (pointer to `deploy/lite`).

## Task 8 — verification

1. `python -m pytest tests/ -q` → exit 0; record the counts against the baseline (base `b7db81e`).
2. Windows: `powershell -ExecutionPolicy Bypass -File deploy\lite\local\up.ps1 -Mock`, then `smoke.ps1`, then `down.ps1 -Purge`.
3. Linux (WSL Ubuntu, real bash): `./up.sh --mock && ./smoke.sh && ./down.sh --purge`.
4. Also boot once with `--qdrant --langfuse` (they start; the proxy is still healthy), then `down --purge`.
5. `<engine> ps` shows no `tokenlean-lite` containers.

**Not runnable here (owner):** a macOS (Apple Silicon) run, podman, and a live-provider smoke (SPEND, cents).

---

## Outcome (2026-10-21)

Branch `feat/agentic-lite-profile` at `D:\token-optimisation-wt\sp19-lite`, base `b7db81e`. One commit per row: `d012a9d` (ST-3, tasks 1–2), `4c05550` (ST-6, tasks 3–5), `b276174` (ST-8, tasks 6–7). Each commit is a snapshot that passed the full suite in a separate test worktree before it was committed.

- Suite: base 5481 passed / 98 skipped → 5495 → 5525 → **5543 passed / 98 skipped**, exit 0.
- Smoke (`--mock`): **Windows** (PowerShell 5.1, Docker Desktop) pass; **Linux** (Ubuntu under WSL2, bash, fresh `git archive` on ext4, umask 077) pass; `--qdrant --langfuse` boot + smoke pass, and Langfuse received the traces. `down --purge` leaves nothing.
- Changes from the plan: shared `_lib.sh`/`_lib.ps1`. The mock provider needs a placeholder `LLM_KEY_LITEMOCK`, because the provider client will not send an empty key. Scripts are committed 100755. `up` makes `.state/` 755, because the proxy runs as uid 1000. `smoke.sh` helpers are safe under `set -e`.
- Owner actions: macOS (Apple Silicon) run, Podman run, live-provider smoke (SPEND), `verify-oss-gates.sh` / `test-tiers.sh`, and push/PR via `checkin-push.sh`.
