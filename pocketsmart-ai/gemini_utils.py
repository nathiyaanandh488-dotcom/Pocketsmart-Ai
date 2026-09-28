"""gemini_utils.py - prompt orchestration, Gemini calls and budget post-processing.

Every planner (home, party, jewelry) follows the same pipeline:
  1. build a prompt from the user's inputs
  2. ask Gemini for structured JSON (optionally with an outfit image)
  3. retry once if the plan is over budget
  4. fall back to rule-based estimates if Gemini is unavailable or returns junk
  5. normalise the result: store links, subtotals, allocation percentages, warnings
"""
import json
import logging
import os
import re
from typing import Callable, Optional, Tuple
from urllib.parse import quote_plus

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("pocketsmart")

try:
    from google import genai
    from google.genai import types
except ImportError:  # the app still runs with rule-based fallbacks
    genai = None
    types = None

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_client = None

# ---------------------------------------------------------------------------
# Store links. These are search / landing links, not live product APIs.
# ---------------------------------------------------------------------------
PLATFORM_LINKS = {
    "Amazon": "https://www.amazon.in/s?k={q}",
    "Flipkart": "https://www.flipkart.com/search?q={q}",
    "IKEA": "https://www.ikea.com/in/en/search/?q={q}",
    "Swiggy": "https://www.swiggy.com/search?query={q}",
    "Zomato": "https://www.zomato.com/",
    "OYO": "https://www.oyorooms.com/search?location={q}",
    "Web": "https://www.google.com/search?q={q}",
}
HOME_PLATFORMS = ["IKEA", "Amazon", "Flipkart"]
PARTY_PLATFORMS = ["Swiggy", "Zomato", "OYO", "Amazon"]
JEWELRY_PLATFORMS = ["Amazon", "Flipkart"]


def platform_link(platform: str, query: str) -> str:
    return PLATFORM_LINKS.get(platform, PLATFORM_LINKS["Web"]).format(q=quote_plus(query))


# ---------------------------------------------------------------------------
# Gemini access
# ---------------------------------------------------------------------------
def get_client():
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key or genai is None:
            raise RuntimeError("Gemini is not configured (missing GEMINI_API_KEY or google-genai).")
        _client = genai.Client(api_key=key)
    return _client


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def _ask(prompt: str, image: Optional[Tuple[bytes, str]] = None) -> dict:
    """Send a prompt (plus optional (bytes, mime_type) image) and return parsed JSON."""
    client = get_client()
    contents = [prompt]
    if image:
        contents = [types.Part.from_bytes(data=image[0], mime_type=image[1]), prompt]
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.5
        ),
    )
    return _parse_json(response.text)


def gemini_ready() -> bool:
    return bool(os.getenv("GEMINI_API_KEY")) and genai is not None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
COMMON = """You are PocketSmart AI, a budget planning assistant for shoppers in India.
All prices are in Indian rupees (INR) and must be realistic for Indian online stores.
HARD RULE: the sum of (estimated_price * quantity) across all items must not exceed the budget.
Recommend product types and price ranges that exist on the named platform; do not invent brand
model numbers. Give a short "search_query" for each item that would find it on the platform."""

SCHEMA = """Return ONLY valid JSON, no markdown, in exactly this shape:
{
  "summary": "one or two sentences describing the plan",
  "allocation": [{"category": "string", "amount": 0}],
  "items": [{
    "category": "string",
    "name": "string",
    "platform": "one of the allowed platforms",
    "search_query": "string",
    "estimated_price": 0,
    "quantity": 1,
    "why": "one short sentence"
  }],
  "tips": ["short money-saving tip"]
}
"allocation" is how the budget is divided across categories (amounts in INR)."""


def home_prompt(budget: float, inputs: dict) -> str:
    rooms = ", ".join(inputs["rooms"]) or "not specified"
    quantities = "; ".join(
        f"{k.replace('_', ' ')}: {v}" for k, v in inputs["quantities"].items() if v
    ) or "none specified"
    return f"""{COMMON}

Task: plan home interior purchases.
Budget: INR {budget:,.0f}
Style: {inputs['style']}
Rooms: {rooms}
Requested items and quantities: {quantities}
Notes: {inputs.get('notes') or 'none'}

Recommend one product per requested item and set quantity to the number requested. If no
quantities were given, suggest sensible essentials for the rooms. Balance function, style and
price. Use room names or item groups as categories.
Allowed platforms: {', '.join(HOME_PLATFORMS)}.

{SCHEMA}"""


def party_prompt(budget: float, inputs: dict) -> str:
    return f"""{COMMON}

Task: plan a party or event.
Total budget: INR {budget:,.0f}
Event type: {inputs['event_type']}
Guests: {inputs['guests']}
Venue details: {inputs.get('venue') or 'not specified'}
Guests need accommodation: {'yes' if inputs['needs_stay'] else 'no'}
Notes: {inputs.get('notes') or 'none'}

Split the budget proportionally across catering, decoration and entertainment (plus stay if
needed), tuned to the event type. Catering items use price per guest as estimated_price and the
guest count as quantity. Use Swiggy or Zomato for food, OYO for stays, Amazon for decor and
entertainment supplies.
Allowed platforms: {', '.join(PARTY_PLATFORMS)}.

{SCHEMA}"""


def jewelry_prompt(budget: float, inputs: dict, has_image: bool) -> str:
    image_note = (
        "An outfit photo is attached. Match jewelry to its colours, neckline and formality."
        if has_image
        else "No outfit photo was provided."
    )
    return f"""{COMMON}

Task: recommend jewelry.
Budget: INR {budget:,.0f}
Occasion: {inputs['occasion']}
Style preference: {inputs['style']}
Metal preference: {inputs['metal']}
Notes: {inputs.get('notes') or 'none'}
{image_note}

Suggest a matching set (for example necklace, earrings, bangles or ring). Use the piece type as
the category. Mention colour coordination in "why".
Allowed platforms: {', '.join(JEWELRY_PLATFORMS)}.

{SCHEMA}"""


# ---------------------------------------------------------------------------
# Rule-based fallbacks (used when Gemini fails or is not configured)
# ---------------------------------------------------------------------------
def _pick(platforms: list, index: int) -> str:
    return platforms[index % len(platforms)]


def _round10(value: float) -> int:
    return max(10, int(round(value / 10.0)) * 10)


HOME_CATALOG = {
    "lights": ("Lighting", "Ceiling light fitting", 1200),
    "ceiling_fans": ("Fans", "Energy-saving ceiling fan", 3200),
    "dining_tables": ("Furniture", "Dining table set", 14000),
    "sofas": ("Furniture", "Three-seater sofa", 18000),
    "beds": ("Furniture", "Bed with storage", 16000),
    "wall_art": ("Decor", "Framed wall art", 1500),
}
HOME_DEFAULTS = {"lights": 2, "ceiling_fans": 1, "wall_art": 2}


def home_fallback(budget: float, inputs: dict) -> dict:
    wanted = {k: v for k, v in inputs["quantities"].items() if v and k in HOME_CATALOG}
    wanted = wanted or HOME_DEFAULTS
    base_total = sum(HOME_CATALOG[k][2] * q for k, q in wanted.items())
    scale = min(1.0, budget * 0.95 / base_total)
    items = []
    for i, (key, qty) in enumerate(wanted.items()):
        category, name, base = HOME_CATALOG[key]
        style = inputs["style"].lower()
        items.append({
            "category": category,
            "name": f"{style.capitalize()} {name.lower()}",
            "platform": _pick(HOME_PLATFORMS, i),
            "search_query": f"{style} {name.lower()}",
            "estimated_price": _round10(base * scale),
            "quantity": qty,
            "why": "Estimated from typical prices and scaled to fit your budget.",
        })
    return {
        "summary": "Rule-based estimate: essentials scaled to fit your budget.",
        "allocation": [],
        "items": items,
        "tips": ["Compare the same item across IKEA, Amazon and Flipkart before buying."],
    }


PARTY_SPLITS = {  # catering, decoration, entertainment
    "birthday": (0.50, 0.28, 0.22),
    "corporate": (0.55, 0.15, 0.30),
    "wedding": (0.55, 0.25, 0.20),
}


def party_fallback(budget: float, inputs: dict) -> dict:
    event = inputs["event_type"].lower()
    catering, decor, fun = PARTY_SPLITS.get(event, PARTY_SPLITS["birthday"])
    stay_share = 0.15 if inputs["needs_stay"] else 0.0
    keep = 1 - stay_share
    guests = max(1, inputs["guests"])
    catering_amt, decor_amt, fun_amt = (budget * s * keep for s in (catering, decor, fun))
    items = [
        {
            "category": "Catering", "name": f"{event.capitalize()} menu for {guests} guests",
            "platform": "Swiggy" if event == "birthday" else "Zomato",
            "search_query": f"party catering {event}",
            "estimated_price": _round10(catering_amt / guests), "quantity": guests,
            "why": "Per-guest food cost from your catering share.",
        },
        {
            "category": "Decoration", "name": f"{event.capitalize()} decoration kit",
            "platform": "Amazon", "search_query": f"{event} party decoration kit",
            "estimated_price": _round10(decor_amt), "quantity": 1,
            "why": "Balloons, banners and lights sized to your budget.",
        },
        {
            "category": "Entertainment", "name": "Speaker and party games",
            "platform": "Amazon", "search_query": "party speaker games",
            "estimated_price": _round10(fun_amt), "quantity": 1,
            "why": "Music and activities for guests.",
        },
    ]
    if stay_share:
        items.append({
            "category": "Stay", "name": "Guest rooms",
            "platform": "OYO", "search_query": inputs.get("venue") or "hotel",
            "estimated_price": _round10(budget * stay_share), "quantity": 1,
            "why": "Accommodation share of your budget.",
        })
    return {
        "summary": f"Rule-based split tuned for a {event} with {guests} guests.",
        "allocation": [],
        "items": items,
        "tips": ["Ask caterers for a per-plate quote and compare at least two."],
    }


JEWELRY_SETS = {  # occasion -> [(piece, share)]
    "wedding": [("Necklace set", 0.5), ("Earrings", 0.2), ("Bangles", 0.3)],
    "party": [("Statement earrings", 0.4), ("Necklace", 0.35), ("Bracelet", 0.25)],
    "festival": [("Jhumkas", 0.35), ("Necklace", 0.4), ("Bangles", 0.25)],
}
JEWELRY_DEFAULT = [("Pendant", 0.45), ("Stud earrings", 0.35), ("Bracelet", 0.2)]


def jewelry_fallback(budget: float, inputs: dict) -> dict:
    pieces = JEWELRY_SETS.get(inputs["occasion"].lower(), JEWELRY_DEFAULT)
    items = []
    for i, (piece, share) in enumerate(pieces):
        items.append({
            "category": piece, "name": f"{inputs['style']} {piece.lower()}",
            "platform": _pick(JEWELRY_PLATFORMS, i),
            "search_query": f"{inputs['style']} {piece.lower()} {inputs['metal']}",
            "estimated_price": _round10(budget * 0.95 * share), "quantity": 1,
            "why": f"Suits a {inputs['occasion'].lower()} look.",
        })
    return {
        "summary": "Rule-based set split across pieces for your occasion.",
        "allocation": [],
        "items": items,
        "tips": ["Check the return policy and metal purity certificate before ordering."],
    }


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------
def _num(value, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _plan_total(data: dict) -> float:
    total = 0.0
    for item in data.get("items", []) or []:
        if isinstance(item, dict):
            total += _num(item.get("estimated_price")) * max(1, int(_num(item.get("quantity"), 1)))
    return total


def _finalize(data: dict, budget: float, platforms: list, fallback_used: bool) -> dict:
    items, groups = [], {}
    for raw in data.get("items", []) or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        price = max(0.0, _num(raw.get("estimated_price")))
        qty = max(1, int(_num(raw.get("quantity"), 1)))
        platform = raw.get("platform") if raw.get("platform") in PLATFORM_LINKS else platforms[0]
        name = str(raw["name"]).strip()
        item = {
            "category": str(raw.get("category") or "Other").strip(),
            "name": name,
            "platform": platform,
            "estimated_price": price,
            "quantity": qty,
            "line_total": price * qty,
            "why": str(raw.get("why") or "").strip(),
            "url": platform_link(platform, str(raw.get("search_query") or name)),
        }
        items.append(item)
        groups.setdefault(item["category"], []).append(item)

    total = sum(i["line_total"] for i in items)
    group_list = [
        {"category": cat, "items": its, "subtotal": sum(i["line_total"] for i in its)}
        for cat, its in groups.items()
    ]

    allocation = []
    for entry in data.get("allocation", []) or []:
        if isinstance(entry, dict) and _num(entry.get("amount")) > 0:
            allocation.append({"category": str(entry.get("category") or "Other"),
                               "amount": _num(entry["amount"])})
    if not allocation:
        allocation = [{"category": g["category"], "amount": g["subtotal"]} for g in group_list]
    alloc_sum = sum(a["amount"] for a in allocation) or 1.0
    for a in allocation:
        a["percent"] = round(a["amount"] / alloc_sum * 100, 1)

    warning = None
    if total > budget:
        warning = (f"This plan is about INR {total - budget:,.0f} over your budget. "
                   "Raise the budget or remove an item to bring it down.")

    return {
        "summary": str(data.get("summary") or "").strip(),
        "allocation": allocation,
        "groups": group_list,
        "items": items,
        "tips": [str(t) for t in (data.get("tips") or []) if t][:5],
        "total": total,
        "remaining": budget - total,
        "warning": warning,
        "fallback_used": fallback_used,
    }


def _plan(budget: float, prompt: str, platforms: list, inputs: dict,
          fallback: Callable[[float, dict], dict],
          image: Optional[Tuple[bytes, str]] = None) -> dict:
    fallback_used = False
    try:
        data = _ask(prompt, image)
        total = _plan_total(data)
        if total > budget * 1.02:  # one retry to enforce budget adherence
            data = _ask(
                prompt + f"\n\nYour previous plan cost INR {total:,.0f}, above the budget of "
                f"INR {budget:,.0f}. Return a corrected plan that stays within the budget.",
                image,
            )
        if not data.get("items"):
            raise ValueError("Gemini returned no items")
    except Exception as exc:  # network, quota, bad JSON, missing key...
        log.warning("Gemini unavailable, using fallback: %s", exc)
        data = fallback(budget, inputs)
        fallback_used = True
    return _finalize(data, budget, platforms, fallback_used)


# ---------------------------------------------------------------------------
# Public API used by main.py
# ---------------------------------------------------------------------------
def generate_home_plan(budget: float, inputs: dict) -> dict:
    return _plan(budget, home_prompt(budget, inputs), HOME_PLATFORMS, inputs, home_fallback)


def generate_party_plan(budget: float, inputs: dict) -> dict:
    return _plan(budget, party_prompt(budget, inputs), PARTY_PLATFORMS, inputs, party_fallback)


def generate_jewelry_plan(budget: float, inputs: dict,
                          image: Optional[Tuple[bytes, str]] = None) -> dict:
    return _plan(budget, jewelry_prompt(budget, inputs, image is not None),
                 JEWELRY_PLATFORMS, inputs, jewelry_fallback, image)
