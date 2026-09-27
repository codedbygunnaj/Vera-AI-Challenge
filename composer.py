"""
composer.py — the decision engine ("brain") for the Vera-style merchant bot.

Implements:
    compose(category, merchant, trigger, customer=None) -> dict

Design principle: this is NOT a hardcoded per-category message bank. It is a
small set of handlers keyed by *trigger kind* (the reason we're messaging),
each of which pulls concrete facts out of the category/merchant/customer
context dicts and renders a message. If a trigger kind we've never seen
arrives, `_generic_fallback` still produces a grounded, non-fabricated
message using whatever real fields are present — this is what lets the bot
survive new trigger kinds injected after submission (per challenge-brief §8
"twist").

No fact is ever invented: every number/date/name in a rendered message is
read from one of the four context dicts. If a needed fact is missing, the
handler degrades to a more generic (but still true) statement instead of
making something up.

Hardening principle: each handler is responsible for checking that the
facts it actually needs are present (via .get(), not bracket indexing) and
degrading itself gracefully. compose() keeps a top-level catch-all as a
last-resort backstop only — handlers should not rely on it to survive
missing OPTIONAL fields.
"""

from __future__ import annotations
from typing import Optional


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _txt(value, default: str = "") -> str:
    """Coerce a context field into something safe to drop into merchant/
    customer-facing text. A missing OR explicitly-null field must never
    render as the literal string 'None', and a dict/list value should never
    leak Python repr into a message — both count as accidental internal
    jargon/debugging text."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return default
    return str(value)


def _salutation(merchant: dict) -> str:
    merchant = merchant or {}
    ident = merchant.get("identity", {}) or {}
    first = ident.get("owner_first_name")
    name = _txt(ident.get("name"), "there")
    cat = merchant.get("category_slug", "")
    if first and cat in ("dentists",):
        return f"Dr. {_txt(first)}"
    if first:
        return _txt(first)
    return name


def _hi_ok(merchant: dict, customer: Optional[dict]) -> bool:
    """Customer-facing copy is intentionally English-only for this submission."""
    return False


def _digest_item(category: dict, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in (category or {}).get("digest", []) or []:
        if item.get("id") == item_id:
            return item
    return None


def _active_offer(merchant: dict, prefer_keywords: Optional[list] = None) -> Optional[dict]:
    merchant = merchant or {}
    offers = [o for o in (merchant.get("offers", []) or []) if o.get("status") == "active"]
    if not offers:
        return None
    if prefer_keywords:
        for o in offers:
            title = (o.get("title") or "").lower()
            if any(k in title for k in prefer_keywords):
                return o
    return offers[0]


def _catalog_offer(category: dict, prefer_keywords: Optional[list] = None) -> Optional[dict]:
    catalog = (category or {}).get("offer_catalog", []) or []
    if not catalog:
        return None
    if prefer_keywords:
        for o in catalog:
            title = (o.get("title") or "").lower()
            if any(k in title for k in prefer_keywords):
                return o
    return catalog[0]


def _offer_title(offer: Optional[dict], default: str = "a current offer") -> str:
    if not offer:
        return default
    return _txt(offer.get("title"), default)


def _pct(x) -> str:
    try:
        return f"{abs(float(x)) * 100:.0f}%"
    except (TypeError, ValueError):
        return "a notable amount"


def _fmt_ctr(x) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _first_name(customer: Optional[dict]) -> str:
    if not customer:
        return "there"
    return _txt((customer.get("identity", {}) or {}).get("name"), "there")


def _singularize(word: str) -> str:
    """Best-effort singularization so segment/signal names that differ only
    by pluralization (e.g. 'high_risk_adults' vs 'high_risk_adult_cohort')
    still match. Deliberately simple — only strips a trailing 's'/'ies'."""
    word = word or ""
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _humanize_or_options(s: str) -> str:
    """Turn a snake_case value whose options are joined with a literal
    '_or_' (e.g. 'postcard_or_phone_call') into readable text ('postcard or
    phone call') without ever duplicating the word 'or'. A blind
    `.replace('_', ' or ')` breaks whenever a value already contains the
    token 'or' as one of its '_'-separated parts."""
    s = s or ""
    options = [opt.replace("_", " ").strip() for opt in s.split("_or_") if opt.strip()]
    return " or ".join(options)


def _merchant_fact_snapshot(merchant: dict) -> str:
    """A short, honest snapshot of real merchant performance numbers to
    ground a message in when the trigger's own payload doesn't carry the
    specific figures (e.g. a placeholder-only payload). Never invents a
    trend/delta — only states current, verifiable counts."""
    merchant = merchant or {}
    perf = merchant.get("performance", {}) or {}
    bits = []
    if perf.get("views") is not None:
        bits.append(f"{perf['views']} views")
    if perf.get("calls") is not None:
        bits.append(f"{perf['calls']} calls")
    if not bits:
        return ""
    window = perf.get("window_days")
    return f"{', '.join(bits)} over the last {window} days" if window else ", ".join(bits)


def _result(body: str, cta: str, trigger: dict, rationale: str,
            send_as: str = "vera") -> dict:
    trigger = trigger or {}
    body = (body or "").strip()
    # Belt-and-suspenders: collapse accidental double spaces left behind by
    # conditional text fragments, and never ship a literally empty body.
    body = " ".join(body.split())
    if not body:
        body = "Hi — quick update on your account. Want me to share details?"
    return {
        "body": body,
        "cta": cta or "open_ended",
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": (rationale or "").strip(),
    }


# ---------------------------------------------------------------------------
# Humanizing internal trigger-kind identifiers
# ---------------------------------------------------------------------------

# Never surface a raw snake_case trigger kind (e.g. "customer_lapsed_hard",
# "active_planning_intent") to a merchant or customer — it reads as internal
# jargon. This maps every kind we know about to a natural phrase; a kind we
# don't recognize (e.g. one injected after submission) intentionally has no
# entry, and callers must treat that as "no safe description available"
# rather than falling back to a naive `.replace("_", " ")`.
_KIND_HUMANIZE = {
    "research_digest": "a relevant research update",
    "regulation_change": "a compliance update",
    "cde_opportunity": "a CDE opportunity",
    "competitor_opened": "some local market activity",
    "perf_spike": "your account performance",
    "perf_dip": "your account performance",
    "seasonal_perf_dip": "your account performance",
    "milestone_reached": "a recent milestone",
    "review_theme_emerged": "recent reviews",
    "renewal_due": "your renewal",
    "dormant_with_vera": "our last conversation",
    "gbp_unverified": "your Google Business Profile",
    "festival_upcoming": "an upcoming festival",
    "ipl_match_today": "tonight's match",
    "supply_alert": "a supply alert",
    "category_seasonal": "seasonal trends",
    "curious_ask_due": "a quick question",
    "active_planning_intent": "your plans",
    "winback_eligible": "your account",
    "recall_due": "your upcoming visit",
    "appointment_tomorrow": "your appointment",
    "customer_lapsed_soft": "reconnecting",
    "customer_lapsed_hard": "reconnecting",
    "trial_followup": "your trial session",
    "wedding_package_followup": "your wedding planning",
    "chronic_refill_due": "your refill",
}


def _humanize_kind(kind: str) -> Optional[str]:
    """Return a safe, natural phrase for a trigger kind, or None if none
    exists — callers must NOT fall back to exposing the raw kind string."""
    if not kind:
        return None
    return _KIND_HUMANIZE.get(kind)


# ---------------------------------------------------------------------------
# Merchant-facing handlers (send_as = "vera")
# ---------------------------------------------------------------------------

def h_research_digest(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    item = _digest_item(category, p.get("top_item_id"))
    sal = _salutation(merchant)
    if not item:
        body = (f"{sal}, a new item landed in this week's {_txt((category or {}).get('display_name'), 'category')} "
                 f"digest. Want me to send the summary?")
        return _result(body, "open_ended", trigger,
                        "Research digest fired but no matching digest item found; degraded to a safe generic nudge rather than inventing content.")
    segment = item.get("patient_segment", "") or ""
    signals = merchant.get("signals", []) or []
    aggregate = merchant.get("customer_aggregate", {}) or {}
    relevance = ""
    if segment:
        # Segment names in research items and merchant signals/aggregates are
        # not always written in the same grammatical number (e.g. research
        # says "high_risk_adults", the merchant's own signal/aggregate keys
        # say "high_risk_adult_cohort" / "high_risk_adult_count"). Check both
        # the literal and singularized forms before concluding there's no
        # match, so a real cohort match isn't missed over a plural/singular
        # mismatch.
        candidates = {segment, _singularize(segment)}
        matched_signal = any(f"{c}_cohort" in signals for c in candidates)
        cnt = next((aggregate.get(f"{c}_count") for c in candidates if aggregate.get(f"{c}_count")), None)
        if matched_signal or cnt:
            seg_readable = _singularize(segment).replace("_", " ")
            relevance = f"This one is relevant to your {seg_readable} cohort" + (f" — you have {cnt} such patients" if cnt else "") + ". "
        else:
            relevance = "Worth a skim: "
    else:
        relevance = "Worth a skim: "
    trial = f"{item.get('trial_n')}-patient trial " if item.get("trial_n") else ""
    source = _txt(item.get("source"), "this week\u2019s digest")
    body = (f"{sal}, {source} landed. {relevance}{trial}"
            f"{_txt(item.get('title'))}. {_txt(item.get('actionable'))} "
            f"— {_txt(item.get('source'))}").strip()
    cta = "Want me to pull it + draft a patient-ed WhatsApp you can share?"
    body = f"{body} {cta}"
    return _result(body, "open_ended", trigger,
                    f"External research digest anchored on '{item.get('title')}' ({item.get('source')}); "
                    f"tied to merchant's own patient cohort where the signal matches.")


def h_regulation_change(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    sal = _salutation(merchant)
    deadline = p.get("deadline_iso", "") or ""
    item = _digest_item(category, p.get("top_item_id"))
    if not item:
        body = f"{sal}, a compliance update affecting {_txt((category or {}).get('display_name'), 'your category')} is in effect{f' from {deadline}' if deadline else ''}. Want the details?"
        return _result(body, "open_ended", trigger, "Regulation trigger with no matching digest item; kept generic to avoid fabricating specifics.")
    deadline_txt = f" Deadline: {deadline}." if deadline else ""
    body = (f"{sal}, heads up — {_txt(item.get('title'), 'a compliance change')} ({_txt(item.get('source'))}). "
            f"{_txt(item.get('summary'))}{deadline_txt} "
            f"{_txt(item.get('actionable'), 'Want me to check if this applies to your setup?')}")
    return _result(body, "open_ended", trigger,
                    f"Regulatory deadline ({deadline or 'unspecified'}) tied to a real digest item; urgency={trigger.get('urgency')} warrants a direct compliance nudge.")


def h_cde_opportunity(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    item = _digest_item(category, p.get("top_item_id"))
    sal = _salutation(merchant)
    credits = p.get("credits")
    fee = (p.get("fee") or "").replace("_", " ")
    credits_txt = f"{credits} credits" if credits is not None else "CME/CDE credits"
    if not item:
        body = f"{sal}, a CDE session worth {credits_txt} is coming up — want the details?"
        return _result(body, "open_ended", trigger, "CDE trigger without matching digest item; kept minimal.")
    fee_txt = f", {fee}" if fee else ""
    body = (f"{sal}, {_txt(item.get('title'), 'a CDE session')} — {_txt(item.get('summary'))} "
            f"{credits_txt}{fee_txt}. Want me to send you the registration link?")
    return _result(body, "open_ended", trigger,
                    "CDE opportunity is low-pressure, informational; single open CTA to register interest.")


def h_competitor_opened(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    name = p.get("competitor_name")
    if not name:
        # Naming a competitor is the entire point of this trigger — without
        # a real name we must never invent one, so degrade to the generic
        # handler (which stays anchored on the merchant's own real data).
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    perf = merchant.get("performance", {}) or {}
    dist = p.get("distance_km")
    dist_txt = f"{dist}km from you" if dist is not None else "near you"
    opened_txt = f" on {p['opened_date']}" if p.get("opened_date") else ""
    offer_txt = f", running {p['their_offer']}" if p.get("their_offer") else ""
    perf_txt = f"Your listing is doing {perf.get('views', 0)} views/30d. " if perf.get("views") else ""
    body = (f"{sal}, {name} opened {dist_txt}{opened_txt}{offer_txt}. "
            f"{perf_txt}Want me to show how your listing compares to theirs on the map right now?")
    return _result(body, "open_ended", trigger,
                    "Loss-aversion framing anchored on a real, nearby, named competitor and their actual offer.")


def h_perf_spike(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    metric = p.get("metric")
    delta_pct = p.get("delta_pct")
    if not metric or delta_pct is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    driver = (p.get("likely_driver") or "").replace("_", " ")
    window = p.get("window", "recent period")
    vs_baseline = p.get("vs_baseline")
    baseline_txt = f" (now ~{vs_baseline}/day)" if vs_baseline is not None else ""
    body = (f"{sal}, good news — your {metric} are up {_pct(delta_pct)} over the last {window}"
            f"{baseline_txt}" + (f", likely from {driver}" if driver else "") + ". "
            f"Want me to repeat whatever's working, or push it a bit further this week?")
    return _result(body, "open_ended", trigger,
                    f"Positive, verifiable perf delta ({_pct(delta_pct)}); framed as reinforcement, not a pitch.")


def h_perf_dip(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    metric = p.get("metric")
    delta_pct = p.get("delta_pct")
    if not metric or delta_pct is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    window = p.get("window", "recent period")
    vs_baseline = p.get("vs_baseline")
    baseline_txt = f" (baseline was ~{vs_baseline}/day)" if vs_baseline is not None else ""
    body = (f"{sal}, your {metric} dropped {_pct(delta_pct)} over the last {window}"
            f"{baseline_txt}. Want me to pull up what changed — profile, offers, or reviews — "
            f"and suggest a fix?")
    return _result(body, "open_ended", trigger,
                    f"Loss-aversion, concrete delta ({_pct(delta_pct)}); urgency={trigger.get('urgency')} justifies a direct nudge.")


def h_seasonal_perf_dip(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    metric = p.get("metric")
    delta_pct = p.get("delta_pct")
    if not metric or delta_pct is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    note = (p.get("season_note") or "").replace("_", " ")
    note_txt = f"the usual {note} pattern" if note else "a seasonal pattern"
    body = (f"{sal}, your {metric} are down {_pct(delta_pct)} this week — this lines up with {note_txt} we see "
            f"across the category, not something specific to your listing. "
            f"No action needed, just flagging so it doesn't surprise you.")
    return _result(body, "none", trigger,
                    "Expected seasonal dip (is_expected_seasonal=true); informational only, no CTA to avoid false alarm/spam.")


def h_milestone_reached(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    metric = p.get("metric")
    value_now = p.get("value_now")
    milestone_value = p.get("milestone_value")
    if not metric or value_now is None or milestone_value is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    metric_readable = metric.replace("_", " ")
    if p.get("is_imminent"):
        try:
            remaining = milestone_value - value_now
        except TypeError:
            return _generic_fallback(category, merchant, trigger, customer)
        body = (f"{sal}, you're {remaining} {metric_readable} away from "
                f"{milestone_value} — want me to draft a Google post for when you cross it?")
    else:
        body = f"{sal}, you just crossed {value_now} {metric_readable} \U0001F389 Want me to turn this into a post?"
    return _result(body, "open_ended", trigger,
                    "Social-proof / celebratory milestone, verifiable count from merchant's own data; low-friction content offer.")


def h_review_theme_emerged(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    theme = p.get("theme")
    occurrences = p.get("occurrences_30d")
    if not theme or occurrences is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    quote = p.get("common_quote")
    quote_txt = f" (one says: \"{quote}\")" if quote else ""
    trend = p.get("trend")
    trend_txt = f" and it's {trend}" if trend else ""
    body = (f"{sal}, {occurrences} reviews this month mention \"{theme.replace('_', ' ')}\"{quote_txt}{trend_txt}. "
            f"Want me to draft a response template, or flag an ops fix?")
    return _result(body, "open_ended", trigger,
                    f"Recurring review theme ({occurrences} occurrences" +
                    (f", trend={trend}" if trend else "") + ") is concrete and actionable.")


def h_renewal_due(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    plan = p.get("plan")
    days_remaining = p.get("days_remaining")
    if not plan or days_remaining is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    amount = p.get("renewal_amount")
    amount_txt = f" (\u20b9{amount})" if amount is not None else ""
    body = (f"{sal}, your {plan} plan renews in {days_remaining} days{amount_txt}. "
            f"Reply YES to renew now, or STOP if you'd rather I check back later.")
    return _result(body, "binary_yes_stop", trigger,
                    "Time-boxed subscription action; binary CTA per constraint #3 (action trigger).")


def h_dormant_with_vera(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    days = p.get("days_since_last_merchant_message")
    if days is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    topic = (p.get("last_topic") or "").replace("_", " ")
    topic_txt = f" about {topic}" if topic else ""
    body = f"{sal}, it's been {days} days since we spoke{topic_txt}. Still relevant, or should I drop it for now?"
    return _result(body, "open_ended", trigger,
                    "Re-engagement after silence; references the real last topic rather than a generic 'checking in'.")


def h_gbp_unverified(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    uplift = p.get("estimated_uplift_pct")
    if uplift is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    path = p.get("verification_path")
    path_txt = f" Verification is via {_humanize_or_options(path)}." if path else ""
    body = (f"{sal}, your Google Business Profile isn't verified yet — that alone is costing you an estimated "
            f"{_pct(uplift)} in visibility.{path_txt} Want me to start it for you?")
    return _result(body, "open_ended", trigger,
                    f"Concrete estimated uplift ({_pct(uplift)}) makes this loss-aversion, not generic advice.")


def h_festival_upcoming(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    festival = p.get("festival")
    days_until = p.get("days_until")
    if not festival or days_until is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    relevant = merchant.get("category_slug") in (p.get("category_relevance") or [])
    offer = _active_offer(merchant) or _catalog_offer(category)
    if relevant and offer:
        body = (f"{sal}, {festival} is {days_until} days out. Want me to build a {festival} post "
                f"around your \"{_offer_title(offer)}\" offer before the rush?")
    else:
        body = f"{sal}, {festival} is {days_until} days out — heads up in case you want to plan anything around it."
    return _result(body, "open_ended" if relevant else "none", trigger,
                    f"Festival is {'directly' if relevant else 'loosely'} relevant to {merchant.get('category_slug')}; "
                    f"{'anchored on an existing offer' if (relevant and offer) else 'kept low-key since no obvious tie-in exists'}.")


def h_ipl_match_today(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    match = p.get("match")
    venue = p.get("venue")
    if not match or not venue:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    offer = _active_offer(merchant) or _catalog_offer(category)
    match_time_iso = p.get("match_time_iso") or ""
    time_txt = f" ({match_time_iso[11:16]})" if len(match_time_iso) >= 16 else ""
    city = p.get("city")
    city_txt = f" — {city} will be watching" if city else ""
    cta_txt = (f"Want me to push a match-night post with your \"{_offer_title(offer)}\"?" if offer
               else "Want me to draft a quick match-night post?")
    body = f"{sal}, {match} at {venue} tonight{time_txt}{city_txt}. {cta_txt}"
    return _result(body, "open_ended", trigger,
                    "Same-day local event with a hard time anchor; framed as a same-day demand opportunity for a restaurant-type business.")


def h_supply_alert(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    molecule = p.get("molecule")
    if not molecule:
        # A recall/safety alert with no molecule to name is not something we
        # can safely fabricate — degrade rather than invent what's affected.
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    batches = ", ".join(p.get("affected_batches") or [])
    batches_txt = f" batches {batches}" if batches else ""
    manufacturer = p.get("manufacturer")
    manu_txt = f" ({manufacturer})" if manufacturer else ""
    body = (f"{sal}, recall alert: {molecule}{batches_txt}{manu_txt} flagged. "
            f"Please check your shelf stock and pull if matched.")
    return _result(body, "none", trigger,
                    f"Urgency={trigger.get('urgency')} (max) safety alert; factual, no CTA needed beyond the instruction itself.")


def h_category_seasonal(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    season = p.get("season")
    trends = p.get("trends") or []
    if not season and not trends:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    top = trends[:2]
    readable = ", ".join(t.replace("_", " ") for t in top) if top else "a few seasonal shifts"
    season_txt = f"{season.replace('_', ' ')} shift: " if season else ""
    action_txt = " Might be worth a shelf reset." if p.get("shelf_action_recommended") else ""
    body = f"{sal}, {season_txt}{readable}.{action_txt} Want the full list?"
    return _result(body, "open_ended", trigger,
                    "Category-wide seasonal demand shift with concrete trend deltas; suggests action without pushing a specific SKU we don't have.")


def h_curious_ask_due(category, merchant, trigger, customer):
    sal = _salutation(merchant)
    p = trigger.get("payload", {}) or {}
    template = p.get("ask_template", "")
    if template == "what_service_in_demand_this_week":
        body = f"{sal}, quick one — what's been your most-asked-about service this week?"
    else:
        body = f"{sal}, quick question — anything new customers have been asking about lately?"
    return _result(body, "open_ended", trigger,
                    "Pure curiosity/asking-the-merchant lever (§10 #7) — cadence engagement, not a pitch, no data needed to fabricate.")


def h_active_planning_intent(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    sal = _salutation(merchant)
    topic = (p.get("intent_topic") or "").replace("_", " ") or "this"
    last_msg = p.get("merchant_last_message", "")
    quote_txt = f" ('{last_msg}')" if last_msg else ""
    body = (f"{sal}, on it — here's a starting draft for the {topic}: I'll put together the outline, pricing slabs, "
            f"and a sample WhatsApp blast copy. Reply YES and I'll send the draft in the next message.")
    return _result(body, "binary_yes_stop", trigger,
                    f"Merchant already showed explicit intent{quote_txt} — routing straight to action instead of re-qualifying (avoids Pattern-D anti-pattern).")


def h_winback_eligible(category, merchant, trigger, customer):
    p = trigger.get("payload", {}) or {}
    days = p.get("days_since_expiry")
    if days is None:
        return _generic_fallback(category, merchant, trigger, customer)
    sal = _salutation(merchant)
    dip_pct = p.get("perf_dip_pct")
    lapsed = p.get("lapsed_customers_added_since_expiry")
    facts = []
    if dip_pct is not None:
        facts.append(f"your listing's visibility is down {_pct(dip_pct)}")
    if lapsed is not None:
        facts.append(f"you've had {lapsed} more customers go quiet since then")
    facts_txt = f" — {' and '.join(facts)}" if facts else ""
    body = (f"{sal}, it's been {days} days since your plan lapsed{facts_txt}. "
            f"Reply YES to reactivate, or STOP and I won't follow up again.")
    return _result(body, "binary_yes_stop", trigger,
                    "Subscription winback; loss-aversion with real perf-dip and lapsed-customer numbers tied to the lapse date.")


# ---------------------------------------------------------------------------
# Customer-facing handlers (send_as = "merchant_on_behalf")
# ---------------------------------------------------------------------------

def h_recall_due(category, merchant, trigger, customer):
    if not customer:
        return _generic_fallback(category, merchant, trigger, customer)
    p = trigger.get("payload", {}) or {}
    name = _first_name(customer)
    mname = merchant.get("identity", {}).get("name", "")
    hi = _hi_ok(merchant, customer)
    slots = p.get("available_slots") or []
    service = (p.get("service_due") or "").replace("_", " ")
    offer = _active_offer(merchant, ["clean"]) or _active_offer(merchant) or _catalog_offer(category)
    price_txt = f" {_offer_title(offer)}." if offer else ""

    if slots and service:
        labels = [s.get("label", "") for s in slots if s.get("label")]
        if not labels:
            slots = []
        else:
            slot_txt = " ya ".join(labels) if hi else " or ".join(labels)
            first_label = labels[0].split(",")[0]
            last_label = labels[-1].split(",")[0]
            if hi:
                body = (f"Hi {name}, {mname} here 🦷 Your {service} is due. "
                        f"Available slots: {slot_txt}." + price_txt +
                        f" Reply 1 for {first_label}, 2 for {last_label}, "
                        f"or tell us a time that works.")
            else:
                body = (f"Hi {name}, {mname} here. Your {service} is due. "
                        f"Available slots: {slot_txt}." + price_txt +
                        f" Reply 1 for {first_label}, 2 for {last_label}, "
                        f"or tell us a time that works.")
            return _result(body, "open_ended", trigger,
                            "Recall trigger with real available slots + real active offer, honoring language/time preferences.",
                            send_as="merchant_on_behalf")

    # Placeholder payload — no service/slot data provided. Ground the message
    # in the customer's real visit history and the merchant's real active
    # offer (if any) instead of inventing a due-service, a date, or slot
    # times, and ask for the smallest useful next step (a convenient time).
    rel = (customer.get("relationship", {}) or {})
    last_visit = rel.get("last_visit", "")
    last_service = (rel.get("services_received") or [None])[-1]
    visit_txt = (f" (last visit {last_visit}" + (f", {last_service}" if last_service else "") + ")") if last_visit else ""
    if hi:
        body = (f"Hi {name}, this is {mname} — you may be due for your next visit{visit_txt}." + price_txt +
                " Let us know a time that works and we’ll line up a slot.")
    else:
        body = (f"Hi {name}, this is {mname} — you may be due for your next visit{visit_txt}." + price_txt +
                " Let us know a time that works and we'll line up a slot.")
    return _result(body, "open_ended", trigger,
                    "Recall trigger arrived as a placeholder with no service/slot data; grounded in the customer's "
                    "real visit history and merchant's real active offer instead of inventing a due-service or slot "
                    "times, and asked for the one missing input (a convenient time) rather than a vague 'want details?'.",
                    send_as="merchant_on_behalf")


def h_appointment_tomorrow(category, merchant, trigger, customer):
    if not customer:
        return _generic_fallback(category, merchant, trigger, customer)
    name = _first_name(customer)
    mname = merchant.get("identity", {}).get("name", "")
    hi = _hi_ok(merchant, customer)
    rel = customer.get("relationship", {}) or {}
    last_service = (rel.get("services_received") or [None])[-1]
    svc_txt = f" for your {last_service}" if last_service else ""
    if hi:
        body = f"Hi {name}, this is a reminder from {mname} — you have an appointment tomorrow{svc_txt}. Reply YES to confirm, or let us know if you’d like to reschedule."
    else:
        body = f"Hi {name}, this is a reminder from {mname} — you have an appointment tomorrow{svc_txt}. Reply YES to confirm, or let us know if you'd like to reschedule."
    return _result(body, "binary_yes_stop", trigger,
                    "Appointment reminder; trigger payload was placeholder so we grounded the message in the customer's real service history instead of inventing a time.",
                    send_as="merchant_on_behalf")


def h_customer_lapsed(category, merchant, trigger, customer):
    if not customer:
        return _generic_fallback(category, merchant, trigger, customer)
    name = _first_name(customer)
    mname = merchant.get("identity", {}).get("name", "")
    hi = _hi_ok(merchant, customer)
    rel = customer.get("relationship", {}) or {}
    last_visit = rel.get("last_visit", "")
    p = trigger.get("payload", {}) or {}
    days = p.get("days_since_last_visit")
    offer = _active_offer(merchant) or _catalog_offer(category)
    offer_txt = f" We've got {_offer_title(offer)} on right now." if offer else ""
    days_txt = f"{days} days" if days else "a while"
    if hi:
        body = f"Hi {name}, it’s {mname} — it’s been {days_txt} since your last visit ({last_visit or 'a while back'}).{offer_txt} Would you like to come back in?"
    else:
        body = f"Hi {name}, it's {mname} — it's been {days_txt} since your last visit ({last_visit or 'a while back'}).{offer_txt} Want to come back in?"
    return _result(body, "open_ended", trigger,
                    "Winback for a lapsed customer; anchored on their real last-visit date and merchant's real active offer, not a generic discount.",
                    send_as="merchant_on_behalf")


def h_trial_followup(category, merchant, trigger, customer):
    if not customer:
        return _generic_fallback(category, merchant, trigger, customer)
    p = trigger.get("payload", {}) or {}
    name = _first_name(customer)
    mname = merchant.get("identity", {}).get("name", "")
    hi = _hi_ok(merchant, customer)
    options = p.get("next_session_options") or []
    slot_txt = options[0].get("label", "your preferred slot") if options and isinstance(options[0], dict) else "your preferred slot"
    trial_date = p.get("trial_date", "")
    if hi:
        body = f"Hi {name}, this is {mname} — how was your trial on {trial_date}? Your next session is available: {slot_txt}. Would you like to continue?"
    else:
        body = f"Hi {name}, this is {mname} — how was your trial on {trial_date}? Next session is open: {slot_txt}. Want to continue?"
    return _result(body, "open_ended", trigger,
                    "Trial-to-paid follow-up; uses the real trial date and a real next slot from the trigger payload.",
                    send_as="merchant_on_behalf")


def h_wedding_package_followup(category, merchant, trigger, customer):
    if not customer:
        return _generic_fallback(category, merchant, trigger, customer)
    p = trigger.get("payload", {}) or {}
    days = p.get("days_to_wedding")
    if days is None:
        return _generic_fallback(category, merchant, trigger, customer)
    name = _first_name(customer)
    mname = merchant.get("identity", {}).get("name", "")
    hi = _hi_ok(merchant, customer)
    step = (p.get("next_step_window_open") or "").replace("_", " ") or "the next step"
    if hi:
        body = f"Hi {name}, this is {mname} — your wedding is {days} days away. Your trial is complete, and the {step} window is now open. Would you like us to book it?"
    else:
        body = f"Hi {name}, this is {mname} — your wedding is {days} days out. Since your trial's done, the {step} window is open now. Want us to book it in?"
    return _result(body, "open_ended", trigger,
                    "Bridal follow-up sequenced on the real wedding date and completed trial; timed to the next logical step.",
                    send_as="merchant_on_behalf")


def h_chronic_refill_due(category, merchant, trigger, customer):
    if not customer:
        return _generic_fallback(category, merchant, trigger, customer)
    p = trigger.get("payload", {}) or {}
    name = _first_name(customer)
    mname = merchant.get("identity", {}).get("name", "")
    hi = _hi_ok(merchant, customer)
    meds_list = p.get("molecule_list") or []
    stock_out = p.get("stock_runs_out_iso")
    delivery = " We can deliver to your saved address." if p.get("delivery_address_saved") else (
        " We can deliver to your saved address." if p.get("delivery_address_saved") else "")

    if meds_list and stock_out:
        meds = ", ".join(meds_list)
        stock_out_txt = stock_out[:10] if isinstance(stock_out, str) else str(stock_out)
        if hi:
            body = f"Hi {name}, this is {mname} — your {meds} supply may run out {stock_out_txt}. Would you like us to arrange a refill?{delivery}"
        else:
            body = f"Hi {name}, this is {mname} — your {meds} refill runs out around {stock_out_txt}. Should we send a refill?{delivery}"
        return _result(body, "binary_yes_stop", trigger,
                        "Chronic-medication refill reminder; molecule list and stock-out date are both read from the real trigger payload.",
                        send_as="merchant_on_behalf")

    # Placeholder payload — no molecule list or stock-out date. Ground in the
    # customer's real visit history instead of inventing a medicine name or
    # a date.
    rel = (customer.get("relationship", {}) or {})
    last_visit = rel.get("last_visit", "")
    visit_txt = f" (last order around {last_visit})" if last_visit else ""
    if hi:
        body = f"Hi {name}, this is {mname} — your regular refill may be due{visit_txt}. Would you like us to arrange it?{delivery}"
    else:
        body = f"Hi {name}, this is {mname} — your regular refill may be due{visit_txt}. Want us to send one over?{delivery}"
    return _result(body, "open_ended", trigger,
                    "Chronic-refill trigger arrived as a placeholder with no molecule/date data; grounded in the "
                    "customer's real visit history instead of inventing a medicine name or stock-out date.",
                    send_as="merchant_on_behalf")


# ---------------------------------------------------------------------------
# Fallback for unseen / placeholder-only trigger kinds
# ---------------------------------------------------------------------------

def _generic_fallback(category, merchant, trigger, customer):
    """Used for trigger kinds we don't have a specific handler for, and for
    trigger kinds we DO recognize but whose payload is placeholder-only (no
    real fields to render from) — the latter is what causes most of the
    _HANDLERS entries above to defer here. Either way, this never invents a
    fact: it grounds itself in whatever is verifiably true (merchant/
    customer identity, real performance numbers, real active offers, real
    signals, real relationship history) and always ends on a concrete next
    step rather than a bare 'want details?'. It never surfaces the raw
    internal trigger-kind string to the merchant/customer."""
    merchant = merchant or {}
    trigger = trigger or {}
    kind = trigger.get("kind", "") or ""
    kind_readable = _humanize_kind(kind)

    if customer:
        name = _first_name(customer)
        mname = merchant.get("identity", {}).get("name", "")
        rel = customer.get("relationship", {}) or {}
        last_visit = rel.get("last_visit", "")
        visit_txt = f" (last visit {last_visit})" if last_visit else ""
        offer = _active_offer(merchant) or _catalog_offer(category)
        offer_txt = f" We've also got {_offer_title(offer)} on right now." if offer else ""
        if kind in ("recall_due", "chronic_refill_due", "appointment_tomorrow", "trial_followup"):
            cta = "Let us know a time that works and we'll line up a slot."
        else:
            cta = "Let us know if you'd like to hear more."
        topic_txt = f" — wanted to reach out about {kind_readable}." if kind_readable else " — wanted to check in."
        body = f"Hi {name}, this is {mname}{visit_txt}{topic_txt}{offer_txt} {cta}"
        return _result(body, "open_ended", trigger,
                        (f"Trigger kind '{kind}' arrived with a placeholder/unrecognized payload; grounded in the "
                         f"customer's real visit history and merchant's real active offer instead of inventing "
                         f"specifics, with a concrete next step rather than a vague 'want details?'."),
                        send_as="merchant_on_behalf")

    sal = _salutation(merchant)
    cat_name = _txt((category or {}).get("display_name")) or merchant.get("category_slug", "your category")
    snapshot = _merchant_fact_snapshot(merchant)
    offer = _active_offer(merchant) or _catalog_offer(category)
    signals = merchant.get("signals", []) or []

    if kind == "perf_dip":
        body = (f"{sal}, we've flagged a dip in your account performance this week." +
                (f" You're currently at {snapshot}." if snapshot else "") +
                " Want me to pull up what changed — profile, offers, or reviews — and suggest a fix?")
        rationale = ("perf_dip trigger with a placeholder payload (no metric/delta given); grounded in the "
                     "merchant's real current performance snapshot instead of a fabricated percentage.")
    elif kind == "competitor_opened":
        offer_part = f", with \"{_offer_title(offer)}\" currently live" if offer else ", with no offer currently live"
        body = (f"{sal}, we've picked up signs of new competitive activity near you." +
                (f" Your own listing is at {snapshot}{offer_part}." if snapshot else f"Your listing{offer_part}.") +
                f" Want me to check how you compare to nearby listings in {cat_name}?")
        rationale = ("competitor_opened trigger with a placeholder payload (no competitor name/offer given); we "
                     "never invent a competitor, so the message stays anchored on the merchant's own real "
                     "performance/offer and offers a concrete comparison as the next step.")
    elif kind == "festival_upcoming":
        if offer:
            body = f"{sal}, festival season is coming up for {cat_name} — want me to build a post around your \"{_offer_title(offer)}\" offer before the rush?"
        else:
            body = f"{sal}, festival season is coming up for {cat_name} — want me to put together a seasonal post once we lock in an offer to anchor it to?"
        rationale = ("festival_upcoming trigger with a placeholder payload (no specific festival/date given); we "
                     "never invent a festival name, so this stays generic about timing but still anchors on the "
                     "merchant's real offer where one exists and proposes a concrete action either way.")
    elif signals:
        signal_txt = signals[0].replace(":", " ").replace("_", " ")
        topic_txt = f"flagging {kind_readable}" if kind_readable else "flagging something on your account"
        body = f"{sal}, {topic_txt} — noticed: {signal_txt}. Want me to dig into it and suggest next steps?"
        rationale = (f"Unrecognized/placeholder trigger kind '{kind}'; grounded in a real merchant signal instead "
                     f"of a bare 'want details?' ask, with the internal kind name kept out of the merchant-facing text.")
    elif snapshot:
        topic_txt = f"flagging {kind_readable}" if kind_readable else "flagging something on your account"
        body = f"{sal}, {topic_txt}. You're currently at {snapshot} — want me to take a closer look and suggest next steps?"
        rationale = (f"Unrecognized/placeholder trigger kind '{kind}'; no signals available, so grounded in the "
                     f"merchant's real current performance snapshot instead, with the internal kind name kept "
                     f"out of the merchant-facing text.")
    else:
        topic_txt = f"flagging {kind_readable}" if kind_readable else "wanted to flag something on your account"
        body = f"{sal}, {topic_txt}. Want me to look into the specifics and suggest a next step?"
        rationale = (f"Unrecognized/placeholder trigger kind '{kind}' with no other real merchant data available "
                     f"to ground on; kept minimal and non-fabricated, still ending on a concrete next step, with "
                     f"the internal kind name kept out of the merchant-facing text.")
    return _result(body, "open_ended", trigger, rationale)


# ---------------------------------------------------------------------------
# Kind -> handler registry
# ---------------------------------------------------------------------------

_HANDLERS = {
    "research_digest": h_research_digest,
    "regulation_change": h_regulation_change,
    "cde_opportunity": h_cde_opportunity,
    "competitor_opened": h_competitor_opened,
    "perf_spike": h_perf_spike,
    "perf_dip": h_perf_dip,
    "seasonal_perf_dip": h_seasonal_perf_dip,
    "milestone_reached": h_milestone_reached,
    "review_theme_emerged": h_review_theme_emerged,
    "renewal_due": h_renewal_due,
    "dormant_with_vera": h_dormant_with_vera,
    "gbp_unverified": h_gbp_unverified,
    "festival_upcoming": h_festival_upcoming,
    "ipl_match_today": h_ipl_match_today,
    "supply_alert": h_supply_alert,
    "category_seasonal": h_category_seasonal,
    "curious_ask_due": h_curious_ask_due,
    "active_planning_intent": h_active_planning_intent,
    "winback_eligible": h_winback_eligible,
    "recall_due": h_recall_due,
    "appointment_tomorrow": h_appointment_tomorrow,
    "customer_lapsed_soft": h_customer_lapsed,
    "customer_lapsed_hard": h_customer_lapsed,
    "trial_followup": h_trial_followup,
    "wedding_package_followup": h_wedding_package_followup,
    "chronic_refill_due": h_chronic_refill_due,
}


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """The composition function required by the challenge brief §5.

    Inputs are plain dicts loaded from the dataset JSON (or pushed via
    /v1/context). Returns a dict with keys: body, cta, send_as,
    suppression_key, rationale. Deterministic — no randomness, no calls out.

    Handlers are written to degrade gracefully on their own (see each
    handler's own None/`.get()` guards). This try/except is a last-resort
    backstop only, in case a payload shape slips through that no handler
    anticipated — it must never be the primary mechanism for correctness.
    """
    category = category or {}
    merchant = merchant or {}
    trigger = trigger or {}
    kind = trigger.get("kind", "")
    handler = _HANDLERS.get(kind)
    try:
        if handler:
            result = handler(category, merchant, trigger, customer)
        else:
            result = _generic_fallback(category, merchant, trigger, customer)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, ZeroDivisionError):
        # A payload didn't have a field we expected, or had one of an
        # unexpected shape -> never crash, never guess. Fall back to the
        # generic, still-grounded handler.
        try:
            result = _generic_fallback(category, merchant, trigger, customer)
        except Exception:
            # Even the generic fallback needs a floor: guarantee the
            # required response shape no matter how malformed the inputs.
            result = _result(
                "Hi — we have an update on your account. Want me to share the details?",
                "open_ended", trigger,
                "All handlers (including the generic fallback) failed on a malformed payload; "
                "returned the minimal safe, non-fabricated response shape.",
            )

    # Guardrail: single primary CTA only (constraint #3) — if the handler's
    # body accidentally contains multiple "reply X / reply Y" style asks
    # beyond the intended binary choice, we leave it (handlers are already
    # authored to respect this), but we always ensure a suppression_key and
    # the exact required shape.
    result.setdefault("suppression_key", trigger.get("suppression_key", ""))
    result.setdefault("cta", "open_ended")
    result.setdefault("send_as", "vera")
    result.setdefault("rationale", "")
    result.setdefault("body", "")
    return result