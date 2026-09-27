# magicpin Vera Challenge — Submission

## Approach

The bot is a **deterministic decision engine**, not an LLM wrapper. `composer.py`
holds one small handler per trigger `kind` (research digest, perf spike/dip,
recall due, competitor opened, IPL match, etc. — 25 kinds total, covering
every kind seen in the dataset). Each handler:

1. Reads only real fields out of the `category` / `merchant` / `trigger` /
   `customer` context dicts it's given.
2. Renders a WhatsApp-style message anchored on the most specific, verifiable
   fact available (a number, a date, a source citation, a real offer price).
3. Picks `send_as` (`vera` vs `merchant_on_behalf`) based on whether a
   `customer` context is present.
4. Returns `{body, cta, send_as, suppression_key, rationale}` — the exact
   `compose()` contract from the brief.

A trigger kind we've never seen (or one that only carries a `placeholder`
payload, as some generated ones do) falls through to `_generic_fallback`,
which still grounds itself in real merchant/customer data rather than
inventing anything — this is what should let the bot survive the
post-submission context injection described in §8 without hallucinating.

`bot.py` is a thin FastAPI layer implementing all 5 endpoints
(`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) per
`challenge-testing-brief.md`. It keeps contexts and conversation state in
memory, dedupes on `suppression_key` so a trigger is never sent twice, and
never re-sends the same body verbatim in one conversation.

`conversation_handlers.py` implements the optional multi-turn `respond()`:
- **Auto-reply detection**: regex bank + "same message 3× verbatim" hint from
  the brief. First occurrence gets one gentle human-check; a repeat exits
  gracefully instead of burning turns (brief §3 pain point #1).
- **Intent transition**: an explicit "yes / let's do it / go ahead" routes
  straight to `action`, never back into another qualifying question
  (avoids the brief's Pattern-D anti-pattern).
- **Hard decline / hostile**: exits politely on "not interested" / "stop";
  de-escalates on abuse without mirroring tone, without dropping the mission.

## Tradeoffs

- Chose a rule-based engine over an LLM-prompt approach so the bot is fully
  deterministic, has zero external-API latency/cost risk during the judge's
  60-minute test window, and can never fabricate a fact (every clause in
  every message traces back to a specific context field). The tradeoff is
  less linguistic variety than an LLM would produce — a real deployment
  would likely use this engine as the *retrieval + fact-selection* layer and
  hand the final phrasing to an LLM constrained to those facts.
- Hindi-English code-mixing is applied for customer-facing messages template-
  by-template (checked against `languages` / `language_pref`) rather than
  via a general translator, to keep it predictable and on-voice.

## What additional context would have helped most

- An explicit **merchant reply-language history** (not just a static
  `languages` list) so code-mix ratio could track how the specific person
  actually writes, turn to turn.
- A **content-library ↔ digest cross-reference** so "draft a patient-ed
  WhatsApp" follow-ups could pull a ready template instead of just offering
  to draft one.

## Files

- `composer.py` — the decision engine (`compose()`)
- `bot.py` — FastAPI server, the 5-endpoint contract
- `conversation_handlers.py` — multi-turn `respond()`
- `make_submission.py` — regenerates `submission.jsonl` from the dataset's
  canonical `test_pairs.json`
- `submission.jsonl` — 30 composed messages, one per canonical test pair
- `dataset_expanded/` — the full expanded dataset (50 merchants, 200
  customers, 100 triggers, 5 categories, `test_pairs.json`), generated
  deterministically from the provided seeds via `generate_dataset.py`
- `judge_simulator.py` — provided harness, unmodified

## Run it

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Regenerate the submission file:

```bash
python make_submission.py --dataset dataset_expanded --out submission.jsonl
```

Self-test against the provided harness (needs an LLM API key for scoring;
set `BOT_URL=http://localhost:8080` and the relevant `LLM_*` env vars per
`judge_simulator.py`'s own header):

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

Deploy `bot.py` anywhere that gives a public HTTPS URL (Render, Fly,
Railway, etc.) and submit that URL per the portal's Step 3.
