"""Groq-powered discussion that helps choose a target (country, city, areas, business type)."""
import json
import re
import threading
import time

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
OWNER_MODEL = "openai/gpt-oss-20b"
MIN_SECONDS_BETWEEN_OWNER_CALLS = 2.1  # stay under the free tier's 30 requests/minute

SYSTEM_PROMPT = """You are a lead-generation strategist helping a freelance web developer find local businesses to pitch websites to.
Your job is to help them choose ONE concrete search target: a country, a city, 1-5 specific areas/sectors/neighbourhoods in that city, and a business type.

How to run the conversation:
- Ask at most 2 short questions per message. First understand: what they sell (new websites, redesigns, booking/ordering systems...), their rough price range, languages they can work in, local vs foreign clients, and timezone limits.
- Then suggest 2-3 options at each step (country -> city -> areas -> business type), each with a one-line reason: many businesses with weak online presence, ability to pay, language fit, competition level.
- Be honest that your knowledge of specific neighbourhoods may be out of date; tell the user to sanity-check on Google Maps.
- Keep replies short and practical, using bullet points.
- When the user has agreed on all four parts, do NOT ask for confirmation again. Immediately reply with a one-line summary followed by a fenced ```json block (always include the ``` fence) in exactly this shape:
{"country": "...", "city": "...", "areas": ["...", "..."], "domain": "...", "rationale": "..."}
"domain" must be a short Google Maps category of 1-2 words (e.g. "cafe", "dentist", "grocery store"), never a description like "independent specialty coffee shops".
Only output the json block once everything is agreed, or when the user says /done (then choose the best options discussed so far)."""

OWNER_PROMPT = """Below are snippets from the website of a business called "{business}".
If they clearly name the owner or founder, reply with ONLY that person's full name.
If no owner/founder name is clearly stated, reply with exactly: NONE

Snippets:
{snippets}"""

JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class GroqError(Exception):
    pass


def chat(api_key, messages, model, temperature=0.6, max_tokens=2000):
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if model.startswith("openai/gpt-oss"):
        # Reasoning models spend tokens thinking before answering; keep that short and out of the reply.
        payload.update(reasoning_effort="low", include_reasoning=False)
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise GroqError(f"Network error talking to Groq: {exc}") from exc
    if resp.status_code == 429:
        raise GroqError("Groq free-tier rate limit hit. Wait a minute and try again.")
    if resp.status_code == 404 and "model" in resp.text:
        raise GroqError(
            f"Groq model '{model}' isn't available anymore. Set GROQ_MODEL in .env to one listed at "
            "https://console.groq.com/docs/models"
        )
    if resp.status_code != 200:
        raise GroqError(f"Groq error {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"].get("content") or ""
    if not content.strip():
        raise GroqError("Groq returned an empty reply. Please send your message again.")
    return content


class Discussion:
    def __init__(self, api_key, model=DEFAULT_MODEL):
        self.api_key = api_key
        self.model = model
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def send(self, text):
        self.messages.append({"role": "user", "content": text})
        try:
            reply = chat(self.api_key, self.messages, self.model)
        except GroqError:
            self.messages.pop()  # let the user retry the same message
            raise
        self.messages.append({"role": "assistant", "content": reply})
        return reply


def _json_objects(reply):
    """Yield every JSON object in the reply, fenced in ``` or not (models don't always use the fence)."""
    match = JSON_BLOCK_RE.search(reply)
    if match:
        try:
            yield json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(reply) if ch == "{"):
        try:
            obj, _ = decoder.raw_decode(reply, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def extract_target(reply):
    """Return the agreed target dict from a reply containing the target JSON, or None."""
    data = next((obj for obj in _json_objects(reply) if "country" in obj and "city" in obj), None)
    if data is None:
        return None
    areas = data.get("areas")
    if isinstance(areas, str):
        areas = [a.strip() for a in areas.split(",")]
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in ("country", "city", "domain")) or not areas:
        return None
    return {
        "country": data["country"].strip(),
        "city": data["city"].strip(),
        "areas": [str(a).strip() for a in areas if str(a).strip()],
        "domain": data["domain"].strip(),
        "rationale": str(data.get("rationale", "")).strip(),
    }


_owner_lock = threading.Lock()
_last_owner_call = 0.0


def extract_owner_name(api_key, business, snippets):
    """Ask a small model to pull an owner/founder name out of website text. Returns '' if none."""
    global _last_owner_call
    with _owner_lock:
        wait = MIN_SECONDS_BETWEEN_OWNER_CALLS - (time.monotonic() - _last_owner_call)
        if wait > 0:
            time.sleep(wait)
        _last_owner_call = time.monotonic()
    prompt = OWNER_PROMPT.format(business=business, snippets=snippets[:1500])
    answer = chat(api_key, [{"role": "user", "content": prompt}], OWNER_MODEL, temperature=0, max_tokens=300).strip()
    if answer.upper().startswith("NONE") or len(answer.split()) > 5:
        return ""
    return answer.strip(" .\"'")
