"""
bot.py — HTTP server exposing the 5-endpoint contract magicpin's judge harness
expects (see challenge-testing-brief.md §2).

Run locally:
    uvicorn bot:app --host 0.0.0.0 --port 8080

Then point judge_simulator.py / your own curl calls at it, or deploy
anywhere that gives you a public HTTPS URL (Render, Fly, Railway, ...).
"""

from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from composer import compose
from conversation_handlers import respond as conv_respond, _has_hard_decline

app = FastAPI(title="magicpin Vera Challenge Bot")
START = time.time()

# ---------------------------------------------------------------------------
# In-memory state (fine for the challenge; swap for Redis/SQLite in prod)
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
contexts: dict[tuple[str, str], dict] = {}

# conversation_id -> conversation state (see conversation_handlers.py header)
conversations: dict[str, dict] = {}

# suppression_key -> True, once an action with that key has actually been sent
sent_suppression_keys: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = contexts.get((scope, context_id))
    return entry["payload"] if entry else None


# ---------------------------------------------------------------------------
# 2.4 GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in contexts.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


# ---------------------------------------------------------------------------
# 2.5 GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Team Vera-Better",
        "team_members": ["Candidate"],
        "model": "rule-based-composer (no external LLM calls; deterministic)",
        "approach": "Deterministic decision engine keyed on trigger kind, grounded strictly in "
                    "pushed context (category/merchant/customer/trigger). No fact is ever generated "
                    "that isn't present in a context payload. Multi-turn handling covers auto-reply "
                    "detection, explicit-intent routing, and graceful exit.",
        "version": "1.0.0",
        "submitted_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# 2.1 POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        # idempotent / stale — no-op per spec
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}


# ---------------------------------------------------------------------------
# 2.2 POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        try:
            if not trg_id:
                continue  # missing trigger id -> skip safely

            trigger = _get("trigger", trg_id)
            if not trigger:
                continue  # missing trigger context -> skip safely

            suppression_key = trigger.get("suppression_key", "") or ""
            if suppression_key and suppression_key in sent_suppression_keys:
                continue  # anti-repetition / dedup — already sent this exact thing

            merchant_id = trigger.get("merchant_id")
            merchant = _get("merchant", merchant_id) if merchant_id else None
            if not merchant:
                continue  # missing merchant -> skip safely

            category = _get("category", merchant.get("category_slug"))
            if not category:
                continue  # missing category -> skip safely

            # customer is optional per spec: a missing customer_id, or no
            # context pushed for it yet, is a perfectly valid merchant-only
            # trigger and must not be skipped.
            customer_id = trigger.get("customer_id")
            customer = _get("customer", customer_id) if customer_id else None

            conversation_id = f"conv_{merchant_id}_{trg_id}"
            if conversation_id in conversations:
                continue  # don't restart an existing conversation from /v1/tick

            try:
                composed = compose(category, merchant, trigger, customer)
            except Exception:
                # compose() already has its own internal safety net, but a
                # failure here must still never take down the /v1/tick
                # endpoint or the batch of triggers after it.
                continue

            action = {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": composed.get("send_as", "vera"),
                "trigger_id": trg_id,
                "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
                "template_params": [merchant.get("identity", {}).get("name", "")],
                "body": composed.get("body", ""),
                "cta": composed.get("cta", "open_ended"),
                "suppression_key": suppression_key,
                "rationale": composed.get("rationale", ""),
            }

            # Only now — after the action is fully built and about to be
            # emitted — do we record the conversation and mark the
            # suppression key as sent. Never mark a key as sent for an
            # action that didn't actually go out.
            conversations[conversation_id] = {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "merchant": merchant,
                "category": category,
                "customer": customer,
                "trigger": trigger,
                "send_as": action["send_as"],
                "turns": [{"from": "vera", "body": action["body"]}],
                "last_bot_body": action["body"],
                "_declined": False,
            }
            if suppression_key:
                sent_suppression_keys.add(suppression_key)

            actions.append(action)
            if len(actions) >= 20:  # cap per §5 of testing brief
                break
        except Exception:
            # Absolute last-resort backstop: one malformed trigger must
            # never crash the whole /v1/tick batch.
            continue

    return {"actions": actions}


# ---------------------------------------------------------------------------
# 2.3 POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.get(body.conversation_id)
    if not state:
        # Judge replied to a conversation we don't have (e.g. after a restart,
        # or one we never actually started) — build a minimal state from
        # whatever context we do have rather than erroring.
        merchant = _get("merchant", body.merchant_id) or {}
        customer = _get("customer", body.customer_id) if body.customer_id else None
        state = conversations[body.conversation_id] = {
            "conversation_id": body.conversation_id,
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "merchant": merchant,
            "category": _get("category", merchant.get("category_slug")) or {},
            "customer": customer,
            "trigger": {},
            "send_as": "merchant_on_behalf" if customer else "vera",
            "turns": [],
            "last_bot_body": "",
            "_declined": False,
        }

    # Empty/whitespace/None message must never crash the endpoint.
    message = body.message if isinstance(body.message, str) else ""

    # NOTE: respond() looks at state["turns"] to decide things like "has this
    # auto-reply pattern already occurred once before in this conversation?"
    # so we call it BEFORE recording the current incoming message, then
    # record both sides afterwards.
    try:
        outcome = conv_respond(state, message)
    except Exception:
        # A conversation-logic failure must never bring down /v1/reply.
        outcome = {"action": "end", "rationale": "Internal error handling this reply; ending conversation safely."}
    state["turns"].append({"from": body.from_role, "body": message})

    try:
        declined_this_turn = _has_hard_decline(message)
    except Exception:
        declined_this_turn = False
    if declined_this_turn:
        state["_declined"] = True

    if outcome["action"] == "send":
        # anti-repetition: never resend the exact same body verbatim
        if outcome.get("body", "").strip() == state.get("last_bot_body", "").strip():
            outcome = {"action": "end", "rationale": "Would have repeated the last message verbatim; ending instead."}
        else:
            state["turns"].append({"from": "vera", "body": outcome["body"]})
            state["last_bot_body"] = outcome["body"]
            return {"action": "send", "body": outcome["body"], "cta": outcome.get("cta", "open_ended"),
                    "rationale": outcome.get("rationale", "")}

    if outcome["action"] == "wait":
        return {"action": "wait", "wait_seconds": outcome.get("wait_seconds", 1800), "rationale": outcome.get("rationale", "")}

    return {"action": "end", "rationale": outcome.get("rationale", "")}


# ---------------------------------------------------------------------------
# Optional teardown (privacy §11)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_suppression_keys.clear()
    return {"status": "wiped"}