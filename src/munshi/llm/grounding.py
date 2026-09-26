"""Grounding a real model's turn: the model chooses WHAT to do; code says what is TRUE.

A model turn's reply is never the model's own account of the books or of what happened:

  * it ran tools   -> the reply is code's rendering of the tool results (llm/answers.py, in the user's language), the
                      same sentences the rules engine shows. The model's prose is discarded (kept in the chat meta).
                      A write that ran without a card (the OTP-gated close_stop) is said from its result too.
  * it raised a card -> the card's own code-built text (platform._open_card), never model prose.
  * no tool at all -> its text is shown only when it is a clarifying QUESTION: every sentence a question or a plain
                      request for a detail, no digits (so no amount, quantity, date, balance or ID), no claim that
                      anything was done, recorded, noted, sent or is waiting for approval, and never a request for an
                      ID / SKU / code. Anything else is replaced by the rules' "didn't understand" reply.

The model therefore cannot state a number, a balance, a stock level, a date, or a state of the system that code didn't
read from the books this turn. Pure functions; the platform decides when to call them."""
from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from munshi.llm import answers
from munshi.llm.text import fold

# Name lookups: shown only when nothing else was read (the khata a model read after find_customer says it better).
LOOKUPS = ("find_customer", "find_supplier", "search_products")

_Q_END = re.compile(r"[?؟]\s*[*_)\]\"'»]*$")
# a sentence that asks for a detail without a question mark ('naam batayein', 'please tell me the amount')
_REQUEST = re.compile(r"\b(batayein|bataen|bataiye|batain|bata dein|bata den|bata do|batao|likhein|likh dein|bhej dein|bhejein|confirm karein|"
                      r"please (tell|say|send|share|confirm|let me know)|tell me|let me know|which one|kaun sa|kaunsa|konsa)\b"
                      r"|بتائیں|بتا دیں|لکھیں|بھیجیں", re.I)
_DIGIT = re.compile(r"[0-9۰-۹٠-٩]")
# never ask the user for an internal identifier
_ASKS_ID = re.compile(r"\b(id|ids|sku|skus|code|codes|otp|pin|order number|order no|plan number|stop number|receipt number|entry number)\b|آئی ڈی|کوڈ", re.I)
# the state of the system, which only code may state: done / recorded / noted / sent / waiting / approval
_STATE = re.compile(r"\b(approval|approve|approved|manzoori|manzoor|pending|waiting|intezar|intezaar|darkar|noted?|note kar|record(ed)?|darj|saved?|"
                    r"created|posted|added|done|sent|bhej diya|bhej di|kar diya|kar di|kar diye|ho gaya|ho gayi|ho gaye|ho chuka|bana diya|bana di|"
                    r"tayyar|tayar|jama ho|shamil kar diya)\b|منظوری|درج|ہو گیا|کر دیا", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?؟۔\n])\s+", str(text or "")) if s.strip()]


def ok_question(text: str) -> bool:
    """A model reply that may be shown as it is: only a clarifying question (see the module docstring)."""
    t = str(text or "").strip()
    if not t or len(t) > 320 or _DIGIT.search(t):
        return False
    f = fold(t)
    if _ASKS_ID.search(f) or _STATE.search(f):
        return False
    parts = _sentences(t)
    return bool(parts) and all(_Q_END.search(s) or _REQUEST.search(fold(s)) for s in parts)


def last_question(text: str) -> str:
    """The model's closing question (its last sentence), when it passes ok_question on its own; else ''."""
    parts = _sentences(text)
    q = parts[-1] if parts else ""
    return q if ok_question(q) else ""


_RAW_IDS = re.compile(r"\((?:C|S)-\d{3,}\)|\b(?:C|S)-\d{3,}\b|\b(?:ORD|DSP|STP|REM|PRM|TRF|DEP|NTF)-[A-Z0-9]{6,}\b")


def strip_ids(text: str, repo) -> str:
    """Raw record codes out of text that is shown to a person: customers / suppliers / godowns / routes / products by
    name, and internal record ids dropped."""
    s = str(text or "")
    try:
        for w in repo.list_warehouses():
            s = re.sub(rf"\b{re.escape(w.warehouse_id)}\b", w.name, s)
        for r in repo.list_routes():
            s = re.sub(rf"\b{re.escape(r.route_id)}\b", r.name, s)
        for p in repo.list_products(include_inactive=True):
            s = re.sub(rf"(?<![\w-]){re.escape(p.sku)}(?![\w-])", p.name, s)
        for c in repo.list_customers(include_inactive=True):
            s = re.sub(rf"\s*\({re.escape(c.customer_id)}\)", "", s)
            s = re.sub(rf"\b{re.escape(c.customer_id)}\b", c.name, s)
        for sp in repo.list_suppliers():
            s = re.sub(rf"\s*\({re.escape(sp.supplier_id)}\)", "", s)
            s = re.sub(rf"\b{re.escape(sp.supplier_id)}\b", sp.name, s)
    except Exception:
        pass
    s = _RAW_IDS.sub("", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def turn_results(msgs: list, h: int) -> list[tuple[str, dict, ToolMessage]]:
    """(tool, args, result) for every tool that ran after the message at index h, in order."""
    args_of = {tc.get("id"): tc.get("args") or {} for m in msgs[h + 1:] if isinstance(m, AIMessage) for tc in (m.tool_calls or [])}
    return [(str(m.name or ""), args_of.get(m.tool_call_id, {}), m) for m in msgs[h + 1:] if isinstance(m, ToolMessage)]


def _error_of(content: str) -> str | None:
    try:
        d = json.loads(content)
    except (ValueError, TypeError):
        return None
    return str(d["error"]) if isinstance(d, dict) and d.get("error") else None


def render_results(results: list[tuple[str, dict, ToolMessage]], repo, text: str, is_write) -> tuple[list[str], str]:
    """The sentences code says for this turn's tool results (and the raw result of the last one shown), in the
    language of `text`. Lookups only when nothing else rendered; a failed write says why it failed; a rejected call
    says nothing was done."""
    lang = answers.lang_of(text)
    said: list[tuple[str, str, str]] = []           # (tool, sentence, raw)
    seen: set[str] = set()
    for tool, args, tm in results:
        content = str(tm.content)
        key = f"{tool}|{json.dumps(args, sort_keys=True, default=str)}|{content[:200]}"
        if key in seen:
            continue
        seen.add(key)
        err = _error_of(content)
        if err is not None or tm.status == "error":
            if is_write(tool):
                why = err or content[:160]
                said.append((tool, {"ur": "یہ نہیں ہو سکا: {w}", "ru": "Ye nahi ho saka: {w}"}.get(lang, "Couldn't do that: {w}").format(w=why.rstrip(".")) + ".", content))
            continue
        s = answers.render(tool, content, repo, text, args)
        if s:
            said.append((tool, s, content))
    main = [x for x in said if x[0] not in LOOKUPS] or said
    main = main[-3:]
    out = list(dict.fromkeys(s for _, s, _ in main))
    return out, (main[-1][2] if main else "")


def human_index(msgs: list) -> int:
    return max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1)
