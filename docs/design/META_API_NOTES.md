# META_API_NOTES: read before writing any model-calling code

Status of every fact here: reported by third-party integrations and Meta's docs index, NOT yet verified against the live API. M0 confirms the basics with one cheap call per check; the rest is confirmed from the usage and errors of real calls during the build. Where real calls disagree with this file, trust the real calls and record the difference in DEVLOG.md.

## 1. Endpoint, auth, models

- Base URL `https://api.meta.ai/v1`. OpenAI-compatible chat completions (Responses and Messages formats also exist). Auth: `Authorization: Bearer $META_API_KEY`.
- `muse-spark-1.3`: Standard tier. $1.25 per million input, $0.15 per million cached input, $4.25 per million output. Not used to improve Meta products. Use for everything in the shipped product and for anything involving real user text.
- `muse-spark-1.3-contributor`: $0.10 input, $0.002 cached input, $0.20 output. Inputs and outputs may be used to train Meta models, token rate limited, select countries. Use only for development with fake data (`META_MODEL_DEV`).
- `sam-3.1`: segmentation. Request shape to be discovered in M0 (see HANDOFF M0). Fallback: send the whole photo to Muse Spark vision.
- Keep the model client behind one `ModelClient` interface so the base URL and model are configuration, not code.

## 2. Behaviours that will bite you

1. `tool_choice` accepts only `"auto"`. `required`, `none` and named functions reportedly return 400. Never depend on forcing a tool. Instruct in the prompt; if the model answers without calling `finish_interview`, retry once with a reminder, then run a structured-output extraction call.
2. Thinking is always on. Reasoning tokens are spent first and billed as output. A small `max_tokens` can return empty `content` with `finish_reason: "length"`. Use 4,000 or more for interviews and more for review synthesis. `reasoning_effort` reportedly does nothing.
3. No assistant prefill. Use Markdown scaffolding in prompts instead of XML tags.
4. Native structured output (JSON) and image input are supported.
5. Rate limits (RPM, TPM, TPD) return 429. Retry with exponential backoff plus jitter; cap parallel interviews with a semaphore (default 4).
6. The cost meter must count reasoning tokens as output, otherwise it under-reports.

## 3. Prompt caching playbook (highest priority after correctness)

Why it matters: cached input is $0.15 vs $1.25 per million, 8x cheaper. Pam sends the same system prompt and tool list on every interview turn, so most input tokens should be cacheable. Caching is also what makes frequent check-ins affordable, which is part of the submission story.

What is known: repeated prefixes reportedly return `usage.prompt_tokens_details.cached_tokens`. Caching appears automatic (no cache markers). What is NOT known: minimum prefix size, TTL, and whether cached tokens depend on exact byte equality. Read them from the usage of real calls; do not run dedicated tests.

### Rules for the code (implement from day one, not as a later optimization)

1. One function builds every request: `build_messages(role, project_context, conversation, volatile)` in `src/pam_pm/agents/prompts/builder.py`. No other code assembles messages.
2. Order is fixed: system prompt, tool definitions, project and team context (stable for the sprint), then the conversation in order, then the volatile block LAST.
3. The volatile block (current time, today's date, the person's name, board snapshot diff) goes in the final user message only. Never in the system prompt or tool definitions.
4. Tool definitions are generated once at import, serialized with sorted keys, and reused byte for byte. No docstring-derived schemas that change with imports.
5. Conversation is append-only. Never edit, reorder or re-summarize earlier turns mid-interview. Compact only between interviews.
6. All parallel interviewers share the exact same system prompt and tools. Per-person detail lives in the first user message.
7. Do not change the system prompt wording during a run (no A/B tweaks, no version stamps).
8. Warm the cache once before launching parallel interviews (send one cheap call with the shared prefix, wait for it to finish, then fan out). Parallel first calls all miss.
9. Keep calls within a conversation close together in time. If cached share drops between turns that are minutes apart (TTL looks short), schedule follow-ups accordingly or accept a cold start.
10. Log per call: prompt_tokens, cached_tokens, completion_tokens (including reasoning), dollars. Write to SQLite and show cached share in the console footer and in `pam doctor`.

### Tests that must exist

- `tests/test_prompt_prefix_stability.py`: call `build_messages` twice for two different people and two different times; assert the system prompt and tool definitions are byte-identical and that all differences live in the last user message. Fails if anyone puts a timestamp in the prefix.
- `tests/test_cost_meter.py`: feed a fake usage payload with cached_tokens and reasoning tokens; assert dollars and cached share.
- `pam doctor` cache check (for users, optional): sends a ~3,000 token prefix twice and reports HIT or MISS. Do not run it repeatedly during the build.

### How to measure (no dedicated test runs)

Do not spend tokens on a caching test. Read `usage.prompt_tokens_details.cached_tokens` from the real calls Pam makes during normal development and testing, log it through `cost.py`, and record the cached share in DEVLOG.md after the first interview runs. If it is near zero on repeated turns of the same conversation, check prefix stability first (the stability test), then note it in MORNING.md. `pam doctor` has an optional one-click cache check for later.

### Build-agent caching (Hermes itself)

Hermes (pam-builder) and Muse Code both run on Meta models, so the same rules apply to them: keep sessions long instead of starting fresh ones, do not edit instruction files mid-session, keep tool output small. After the first hour compute the cached share from the Meta console usage. Below 50 percent: write it in MORNING.md and reduce context churn.

## 4. Pricing sanity check (from the rates above)

| Item | Estimate |
|---|---|
| Interview turn: 6k input tokens (5k cached) and 1.5k output tokens including reasoning | 1k x $1.25 + 5k x $0.15 + 1.5k x $4.25 per million = about $0.0084 |
| Same turn with no caching | 6k x $1.25 + 1.5k x $4.25 per million = about $0.0139 |
| Full sprint run (3 people, daily, review, retro) | well under $2 |

The estimates are rough; the Meta usage table in DEVLOG.md replaces them with measured numbers.

## 5. Sources

- Meta Model API console and docs: https://dev.meta.ai
- LiteLLM Meta provider documentation (behaviours above)
- harn-vm Meta capability file (tool_choice and thinking behaviours)
- Third-party pricing pages for Muse Spark 1.3 and Contributor tier

## 6. SAM 3.1 discovery (phase 2)

Source: dev.meta.ai docs pages for media segmentation and SAM
(overview, segmenting with prompts, reading segmentation output).

- Endpoint: POST https://api.meta.ai/v1/responses, same base URL and
  Bearer auth (META_API_KEY) as Muse Spark. Model id sam-3.1.
- Request shape: Responses API, one user message, content holds one
  input_text concept prompt (short noun phrase naming one thing, for
  example red square) plus one input_image part with image_url as a
  public URL or base64 data URL. One concept per request. Streaming
  with stream true is recommended for video; non-streaming returns the
  full result in one response.
- Response shape: segmentation output arrives as special-token text in
  the output_text lane, one line per frame. Each record is an object id
  plus one box token then one mask token, for example
  <0f> plus box and mask tokens with w and h in source pixels and mask
  raster as height,width plus base85 payload. Zero matches is valid:
  the stream completes with empty output_text.
- Probe: scripts/check_sam.py keeps GET /models (call 1) and adds one
  POST /responses call (call 2 max) on a synthetic 48x48 PNG made in
  code (white field, red square center) with prompt red square. On any
  400 or 404 it prints FALLBACK with the error detail truncated to 200
  chars and exits 0. Live result: not yet run in this phase; run the
  script once with real credentials to confirm masks come back.
