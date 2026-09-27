"""
conversation_handlers.py — multi-turn conversation logic.

Implements the optional §7.4 contract:

    def respond(state: ConversationState, merchant_message: str) -> dict

`state` here is a plain dict (kept by bot.py) with at least:
    {
      "conversation_id": str,
      "merchant": dict, "category": dict, "customer": dict|None,
      "turns": [{"from": "vera"|"merchant"|"customer", "body": str}, ...],
      "last_bot_body": str,
      "send_as": "vera" | "merchant_on_behalf",
    }

This module targets the three replay-test scenarios called out in
challenge-testing-brief.md §4 Phase 4:
  1. Auto-reply hell        -> detect + exit after one retry
  2. Intent transition      -> stop qualifying, go to action immediately
  3. Hostile / off-topic    -> stay polite, stay on-mission
"""

from __future__ import annotations
import re
from typing import Optional

from composer import _digest_item, _active_offer, _catalog_offer


# --- Auto-reply detection --------------------------------------------------

_AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"we (will|shall) get back to you",
    r"automated (assistant|response|reply)",
    r"aapki jaankari ke liye.*shukriya",
    r"hamari team tak pahuncha",
    r"currently (unavailable|away|closed)",
    r"business hours",
    r"out of office",
]
_AUTO_REPLY_RE = re.compile("|".join(_AUTO_REPLY_PATTERNS), re.IGNORECASE)


def _looks_like_auto_reply(message: str) -> bool:
    return bool(_AUTO_REPLY_RE.search(message or ""))


def _is_repeat_verbatim(state: dict, message: str, min_prior_repeats: int = 2) -> bool:
    """True if this exact message has already appeared >= min_prior_repeats
    times from the other party earlier in this conversation (hint from the
    brief: 'same message verbatim 3+ times = auto-reply').

    IMPORTANT: `respond()` is called BEFORE the current incoming message is
    appended to state["turns"] (see bot.py), so `state["turns"]` only holds
    *prior* occurrences — it does not yet include the message we're
    currently evaluating. To trigger on exactly the 3rd identical message,
    we only need 2 *prior* occurrences already recorded (this call being the
    3rd). Counting the current call would require 3 prior occurrences,
    which means the 3rd repeat is silently under-counted as if it were only
    the 2nd — that under-count was the bug; callers should pass
    min_prior_repeats = (target_total_repeats - 1)."""
    message = (message or "").strip()
    count = sum(
        1 for t in state.get("turns", [])
        if t.get("from") in ("merchant", "customer") and (t.get("body", "") or "").strip() == message
    )
    return count >= min_prior_repeats


# --- Intent detection --------------------------------------------------

_POSITIVE_INTENT_RE = re.compile(
    r"\b(yes|yep|yeah|sure|ok(ay)?|go ahead|let'?s do it|lets do it|"
    r"i want to join|join karna hai|karo|kar do|chaliye|badhiya|theek hai|start karo|"
    r"send (it|me)|book (it|kar))\b", re.IGNORECASE)

_HARD_DECLINE_RE = re.compile(
    r"\b(not interested|stop|no thanks|nahi chahiye|band karo|unsubscribe|"
    r"leave me alone|don'?t (contact|message) me)\b", re.IGNORECASE)

_ABUSE_RE = re.compile(
    r"\b(idiot|stupid|shut up|useless|bkwas|bakwas||bevakoof)\b", re.IGNORECASE)


def _has_positive_intent(message: str) -> bool:
    return bool(_POSITIVE_INTENT_RE.search(message or ""))


def _has_hard_decline(message: str) -> bool:
    return bool(_HARD_DECLINE_RE.search(message or ""))


def _is_abusive(message: str) -> bool:
    return bool(_ABUSE_RE.search(message or ""))


def _bot_already_asked_qualifying(state: dict) -> bool:
    return any(t.get("from") == "vera" and "?" in t.get("body", "") for t in state.get("turns", []))


# --- Delivering the promised artifact on explicit "yes" -------------------

def _deliver_promised_action(state: dict) -> tuple[str, str]:
    """When a merchant says 'yes' to something Vera offered to draft/send,
    actually produce that artifact now (grounded only in real trigger/
    category/merchant data), rather than promising it will arrive "in a
    moment" — this response IS the only chance to deliver it. If we don't
    have enough real data to build the specific artifact, ask for the one
    smallest missing input instead of promising unfounded work.

    Returns (body, rationale).
    """
    trigger = state.get("trigger", {}) or {}
    kind = trigger.get("kind", "")
    category = state.get("category", {}) or {}
    merchant = state.get("merchant", {}) or {}
    mname = merchant.get("identity", {}).get("name", "")
    p = trigger.get("payload", {}) or {}

    if kind == "research_digest":
        item = _digest_item(category, p.get("top_item_id"))
        if item:
            body = (f"Here's the draft — a patient-ed WhatsApp message:\n\n"
                     f"\"Hi! A quick health note from {mname}: {item.get('title','')}. "
                     f"{item.get('actionable','')}\"\n\n(Source: {item.get('source','')})")
            return body, ("Delivering the actual patient-ed WhatsApp draft immediately, built only from the real "
                           "digest item's title/actionable/source, instead of promising it 'in a moment'.")

    if kind == "active_planning_intent":
        topic = (p.get("intent_topic") or "").replace("_", " ") or "this"
        offer = _active_offer(merchant) or _catalog_offer(category)
        if offer:
            body = (f"Here's the starting draft for the {topic}:\n"
                     f"• Anchor: \"{offer['title']}\"\n"
                     f"• Sample WhatsApp blast: \"{mname} is launching {topic}! {offer['title']} — reply to know more.\"\n"
                     f"Want any tweaks before we finalize?")
            return body, ("Delivering the promised draft immediately, anchored on the merchant's real active "
                           "offer/catalog pricing, instead of promising unfounded future work.")
        return (f"To draft the {topic} properly — what price point or package should I anchor it to?"), \
               ("No active offer/catalog pricing on file to anchor the draft in without inventing a number, so "
                "asking for the one missing input instead of promising a draft we can't yet ground.")

    # No specific artifact was offered (or we don't have a handler for this
    # kind's promised action yet) — acknowledge and confirm the action is
    # underway without inventing further in-chat content.
    body = "Done — moving to the next step now."
    rationale = (
    "Merchant explicitly confirmed the proposed action; "
    "acknowledging the confirmation and progressing without inventing details."
)
    return body, rationale


# --- Main entrypoint --------------------------------------------------

def respond(state: dict, merchant_message: str) -> dict:
    """Given the conversation so far + the latest reply, produce the next move.

    Returns one of:
        {"action": "send", "body": str, "cta": str, "rationale": str}
        {"action": "wait", "wait_seconds": int, "rationale": str}
        {"action": "end", "rationale": str}
    """
    merchant_message = merchant_message or ""
    merchant = state.get("merchant", {}) or {}
    customer = state.get("customer")
    send_as = state.get("send_as", "vera")
    who = "customer" if customer else "merchant"
    name = (customer.get("identity", {}).get("name") if customer
            else merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name"))

    # 1) Auto-reply detection — reply once to double-check with a human,
    #    then exit gracefully if it repeats.
    if _looks_like_auto_reply(merchant_message) or _is_repeat_verbatim(state, merchant_message, min_prior_repeats=2):
        prior_auto_replies = sum(
            1 for t in state.get("turns", [])
            if t.get("from") in ("merchant", "customer") and _looks_like_auto_reply(t.get("body", ""))
        )
        if prior_auto_replies == 0:
            body = ("Understood — before this goes to your team, is there 2 minutes for you to take a quick look "
                    "yourself? Happy to keep it short.")
            return {"action": "send", "body": body, "cta": "open_ended",
                    "rationale": "First occurrence of an auto-reply pattern; one lightweight human-check before "
                                  "burning further turns (per brief §3 pain point #1: faster auto-reply routing wins)."}
        return {"action": "end",
                "rationale": "Auto-reply confirmed a second time — this is WhatsApp Business's canned response, "
                              "not the owner. Exiting to avoid wasting turns; will retry via a fresh trigger later."}

    # 2) Hard decline — exit immediately and gracefully.
    if _has_hard_decline(merchant_message):
        return {
            "action": "end",
            "rationale": "Explicit stop/not-interested signal; ending immediately with no further messaging."
        }
    # after sending the graceful exit once, a repeat should just end silently
    if state.get("_declined") and _has_hard_decline(merchant_message):
        return {"action": "end", "rationale": "Already acknowledged decline; ending conversation."}

    # 3) Hostile / off-topic — stay polite, redirect to the mission, don't
    #    get derailed into unrelated requests we can't fulfill.
    if _is_abusive(merchant_message):
        return {"action": "send",
                "body": "No worries, I'll leave you be for now — happy to help whenever's useful.",
                "cta": "none",
                "rationale": "Abusive message detected; de-escalate politely rather than mirroring tone, then step back."}

    # 4) Explicit positive intent — route straight to action, don't re-qualify.
    #    Deliver the actual promised artifact/next-step right now if we have
    #    enough context to do so; otherwise ask only for the smallest missing
    #    piece. Never promise a draft "in a moment" that this response won't
    #    actually contain.
    if _has_positive_intent(merchant_message):
        if send_as == "merchant_on_behalf":
            body = (
                f"Got it, {name} — we'll proceed with that request. "
                f"Let us know if you'd like to change the timing or option."
            )
            rationale = (
                f"Customer gave explicit positive intent ('{merchant_message[:60]}'); "
                "acknowledging the request without claiming an external booking/order "
                "was completed or inventing a confirmation."
            )
        else:
            body, rationale = _deliver_promised_action(state)
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": rationale}

    # 5) Otherwise: a real, engaged, on-topic reply — acknowledge specifically
    #    and offer the next concrete step. Keep it short, single CTA.
    body = ("Got it — thanks for the reply. " +
            ("Want me to go ahead with the next step?" if send_as == "vera"
             else "Let us know if that works for you, or suggest another time."))
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": "Genuine engaged reply with no clear yes/no signal; acknowledging and offering one "
                          "concrete next step to keep momentum without re-pitching from scratch."}