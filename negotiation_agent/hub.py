"""5번 커뮤니케이션·협상 에이전트 — 공용 DB(trade_hub) 연동.

2번이 찾은 공급처(product_suppliers)에 보낼 메일 초안을 만들고, 사람이 붙여넣은 답장을 분석해 견적(quotes)을 남긴다.
메일은 절대 보내지 않는다: 초안(messages.status='draft')과 승인 대기함 할 일(tasks)만 만든다.

이 에이전트가 쓰는 곳 (trade_hub/schema.sql 머리말):
  messages, quotes, tasks(send_message·review_quote),
  product_suppliers.status (candidate→contacted→quoted→sampling, 앞으로만 / 선정·제외·되돌리기는 사람 버튼),
  product_suppliers.reject_reason (제외 사유), product_suppliers.supplier_specs_json (공급처가 답한 사양 → 3번이 읽음,
    db.merge_supplier_specs 로만),
  products.stage sourcing→negotiating (사람이 첫 메일을 '발송 완료'로 표시할 때만)
내부 상한가는 공용 DB에 두지 않는다 → private.py
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

from trade_hub import db

from . import llm, private, prompts

AGENT = "negotiation"
LANGS = {"en": "English", "zh": "Simplified Chinese"}
TONE_LABELS = {"soft": "부드럽게", "normal": "보통", "firm": "강하게"}
KIND_LABEL = {"rfq": "견적 요청", "follow_up": "후속 메일", "negotiation": "역제안", "sample": "샘플 요청",
              "order": "발주", "other": "메일"}
INCOTERMS = {"EXW", "FOB", "CFR", "CIF", "DAP", "DDP"}
PS_FLOW = ["candidate", "contacted", "quoted", "sampling", "selected"]   # rejected는 건드리지 않음

FOLLOWUP_DAYS = 3      # 보낸 뒤 며칠 답이 없으면 후속 메일 초안
MAX_FOLLOWUPS = 2      # 한 메일에 후속 메일은 최대 몇 번까지

# 후속 메일이 필요한 '보낸 메일'. supervisor.json 의 negotiation.when_sql 도 같은 조건(3일·2회)을 쓴다 — 바꾸면 같이 바꿀 것.
FOLLOWUP_DUE_SQL = """
SELECT m.* FROM messages m
JOIN product_suppliers ps ON ps.id = m.product_supplier_id
JOIN products p ON p.id = ps.product_id
WHERE m.direction = 'outbound' AND m.status = 'sent' AND m.kind != 'follow_up' AND m.sent_at IS NOT NULL
  AND ps.status != 'rejected' AND p.stage NOT IN ('closed', 'dropped')
  -- 그 공급처에서 이후 답장이 없음
  AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.product_supplier_id = m.product_supplier_id
                  AND r.direction = 'inbound' AND COALESCE(r.sent_at, r.created_at) >= m.sent_at)
  -- 그 공급처에 보낸 마지막 메일만 (이후 다른 메일을 보냈으면 그쪽 기준)
  AND NOT EXISTS (SELECT 1 FROM messages n WHERE n.product_supplier_id = m.product_supplier_id
                  AND n.direction = 'outbound' AND n.kind != 'follow_up' AND n.status = 'sent' AND n.id > m.id)
  -- 이미 만든 후속 초안이 대기 중이거나, 사람이 후속 초안을 버렸으면(=후속 안 함) 제외
  AND NOT EXISTS (SELECT 1 FROM messages f WHERE f.reply_to_id = m.id AND f.kind = 'follow_up'
                  AND f.status IN ('draft', 'approved', 'discarded'))
  AND (SELECT COUNT(*) FROM messages f WHERE f.reply_to_id = m.id AND f.kind = 'follow_up' AND f.status = 'sent') < :max
  -- 예정일: 사람이 캘린더에서 옮긴 날짜(followup_due_on, trade_hub v15 calendar_sync가 씀)가 마지막 후속 발송보다
  --   뒤면 그 날부터, 아니면 마지막 발송(원래 메일 또는 후속) 뒤 :days 일
  AND (CASE WHEN m.followup_due_on IS NOT NULL AND m.followup_due_on > COALESCE(date((SELECT MAX(f.sent_at) FROM messages f
                 WHERE f.reply_to_id = m.id AND f.kind = 'follow_up' AND f.status = 'sent')), '')
            THEN date('now', 'localtime') >= m.followup_due_on
            ELSE julianday('now', 'localtime') - julianday(COALESCE((SELECT MAX(f.sent_at) FROM messages f
                 WHERE f.reply_to_id = m.id AND f.kind = 'follow_up' AND f.status = 'sent'), m.sent_at)) >= :days END)
ORDER BY m.id"""

PENDING_REPLY_SQL = "SELECT * FROM messages WHERE direction = 'inbound' AND analysis_json IS NULL ORDER BY id"


class DraftRejected(ValueError):
    """초안이 안전 검사(상한가 노출)를 통과하지 못함 — 저장하지 않는다."""


def log(msg):
    """진행 출력. 콘솔 인코딩(cp949 등)이 못 쓰는 글자가 있어도 작업은 실패하지 않게 한다."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(str(msg).encode(enc, "replace").decode(enc), flush=True)


def _loads(v, default=None):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return None if n is None else int(n)


# ---------------------------------------------------------------------------
# 읽기 도우미
# ---------------------------------------------------------------------------
PS_SQL = """
SELECT ps.*, p.id AS product_id, p.name AS product, p.stage, p.search_keyword_en, p.category, p.regulations,
       s.company, s.country, s.url AS supplier_url, s.contact_name, s.contact_email, s.contact_other
FROM product_suppliers ps JOIN products p ON p.id = ps.product_id JOIN suppliers s ON s.id = ps.supplier_id
WHERE ps.id = ?"""


def get_ps(conn, ps_id):
    row = conn.execute(PS_SQL, (ps_id,)).fetchone()
    if row is None:
        raise ValueError(f"품목-공급처 번호 {ps_id}가 없습니다")
    return row


def thread_language(conn, ps_id, default="en"):
    row = conn.execute("SELECT language FROM messages WHERE product_supplier_id = ? AND language IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1", (ps_id,)).fetchone()
    return row["language"] if row else default


def thread_channel(conn, ps):
    row = conn.execute("SELECT channel FROM messages WHERE product_supplier_id = ? ORDER BY id DESC LIMIT 1",
                       (ps["id"],)).fetchone()
    if row:
        return row["channel"]
    if ps["contact_email"]:
        return "email"
    if "alibaba" in (ps["supplier_url"] or "").lower():
        return "alibaba"
    return "other"


def last_sent(conn, ps_id):
    return conn.execute("SELECT * FROM messages WHERE product_supplier_id = ? AND direction = 'outbound' "
                        "AND status = 'sent' ORDER BY id DESC LIMIT 1", (ps_id,)).fetchone()


def _spec_values(flag):
    rule = db.SPEC_FLAG_KEYS[flag]
    if rule is bool:
        return "true|false"
    if rule is str:
        return "ISO 3166-1 alpha-2 country code, e.g. CN"
    return "|".join(rule)


def spec_questions(conn, ps, unanswered_only=True):
    """3번(통관·규제)이 최신 판정에서 '공급처에 물어볼 것'으로 남긴 사양 질문 [{"flag", "question_ko"}].
    unanswered_only: 이 공급처가 이미 답한(supplier_specs_json.flags 에 값이 있는) 항목은 뺀다."""
    check = db.latest_compliance_check(conn, ps["product_id"])
    if check is None or "supplier_questions_json" not in check.keys():
        return []
    qs = _loads(check["supplier_questions_json"], [])
    specs = _loads(ps["supplier_specs_json"], {}) if "supplier_specs_json" in ps.keys() else {}
    answered = (specs or {}).get("flags") or {} if isinstance(specs, dict) else {}
    out, seen = [], set()
    for q in qs if isinstance(qs, list) else []:
        if not isinstance(q, dict):
            continue
        flag, text = q.get("flag"), str(q.get("question_ko") or "").strip()
        if flag not in db.SPEC_FLAG_KEYS or not text or flag in seen:
            continue
        if unanswered_only and answered.get(flag) is not None:
            continue
        seen.add(flag)
        out.append({"flag": flag, "question_ko": text})
    return out


# ---------------------------------------------------------------------------
# 쓰기 도우미
# ---------------------------------------------------------------------------
def advance_ps_status(conn, ps_id, status):
    """product_suppliers.status 를 앞으로만 옮긴다 (rejected·이미 더 진행된 건 그대로)."""
    cur = conn.execute("SELECT status FROM product_suppliers WHERE id = ?", (ps_id,)).fetchone()["status"]
    if cur in PS_FLOW and PS_FLOW.index(cur) < PS_FLOW.index(status):
        conn.execute("UPDATE product_suppliers SET status = ?, updated_at = ? WHERE id = ?", (status, db.now(), ps_id))
        return True
    return False


def save_draft(conn, run_id, ps, kind, language, out, reply_to=None, analysis=None):
    """초안 저장 + 승인 대기함(send_message). 모든 초안은 여기를 거친다 (상한가 노출 검사)."""
    texts = [out.get("subject"), out.get("body"), out.get("korean_translation"), (analysis or {}).get("strategy_ko")]
    if any(private.leaks_max_price(t or "", ps["product_id"]) for t in texts):
        raise DraftRejected("초안에 내부 상한가와 같은 숫자가 들어 있어 저장하지 않았습니다. 다시 생성해 주세요.")
    if not (out.get("body") or "").strip():
        raise ValueError("LLM이 빈 초안을 돌려줬습니다. 다시 생성해 주세요.")
    with conn:
        mid = conn.execute(
            "INSERT INTO messages (product_supplier_id, run_id, direction, kind, channel, subject, body, status,"
            " reply_to_id, language, translation, analysis_json) VALUES (?, ?, 'outbound', ?, ?, ?, ?, 'draft', ?, ?, ?, ?)",
            (ps["id"], run_id, kind, thread_channel(conn, ps), out.get("subject"), out["body"], reply_to, language,
             out.get("korean_translation"), db.dumps(analysis))).lastrowid
        db.add_task(conn, "send_message", f"[{ps['product']}] {ps['company']} {KIND_LABEL[kind]} 초안 검토·발송",
                    product_id=ps["product_id"], run_id=run_id, ref_table="messages", ref_id=mid,
                    detail=out.get("subject"), priority=2)
    log(f"  ✉ 초안 #{mid} 저장 ({KIND_LABEL[kind]}) — {ps['company']} · {out.get('subject')}")
    print(f"RESULT_ANCHOR=ps-{ps['id']}", flush=True)
    return mid


# ---------------------------------------------------------------------------
# 환율 (USD가 아닌 견적 → 견적표에는 USD로 환산해 넣고, 원래 금액·환율은 quotes.notes 에)
#   .env FX_RATES="CNY=7.1,EUR=0.86" (1 USD 당 그 통화) 가 있으면 그 값, 없으면 공개 환율 API (통화 코드만 보냄)
# ---------------------------------------------------------------------------
FX_URL = "https://open.er-api.com/v6/latest/USD"
CURRENCY_ALIAS = {"RMB": "CNY", "YUAN": "CNY", "元": "CNY", "¥": "CNY", "€": "EUR", "£": "GBP", "￥": "CNY"}
_fx_cache = {}


def fx_rate(currency):
    """1 USD = ? currency. (rate, 출처) 또는 (None, 이유)."""
    cur = CURRENCY_ALIAS.get(currency, currency)
    for part in (os.getenv("FX_RATES") or "").split(","):
        k, _, v = part.partition("=")
        if k.strip().upper() == cur and _num(v.strip()):
            return _num(v.strip()), ".env FX_RATES"
    if "rates" not in _fx_cache:
        try:
            with urllib.request.urlopen(FX_URL, timeout=8) as r:
                data = json.loads(r.read().decode())
            _fx_cache["rates"] = data.get("rates") or {}
            _fx_cache["date"] = (data.get("time_last_update_utc") or "")[:16]
        except Exception as err:
            return None, f"환율을 가져오지 못함 ({type(err).__name__})"
    rate = _num(_fx_cache["rates"].get(cur))
    return (rate, f"open.er-api.com {_fx_cache.get('date', '')}".strip()) if rate else (None, f"{cur} 환율 없음")


def _run(conn, args):
    return db.agent_run(conn, AGENT, provider=llm.PROVIDER, model=llm.MODEL, args=args)


def _check_active(ps):
    """끝난 품목·제외한 공급처에는 새 초안을 만들지 않는다."""
    if ps["stage"] in db.TERMINAL_STAGES:
        raise ValueError(f"품목 #{ps['product_id']}은(는) {ps['stage']} 상태입니다")
    if ps["status"] == "rejected":
        raise ValueError(f"{ps['company']}은(는) 제외한 공급처입니다 (되돌리려면 supplier {ps['id']} restore)")


# ---------------------------------------------------------------------------
# RFQ (첫 문의)
# ---------------------------------------------------------------------------
def rfq(ps_id, quantity=None, incoterm=None, language="en", note=None, force=False, conn=None):
    conn = conn or db.connect()
    ps = get_ps(conn, ps_id)
    _check_active(ps)
    pending = conn.execute("SELECT id FROM messages WHERE product_supplier_id = ? AND kind = 'rfq' "
                           "AND status IN ('draft', 'approved', 'sent')", (ps_id,)).fetchone()
    if pending and not force:
        raise ValueError(f"이 공급처에는 이미 견적 요청 메일(#{pending['id']})이 있습니다. 새로 만들려면 --force")
    qty = quantity or (f"{ps['max_order_qty']} pcs (first order)" if ps["max_order_qty"] else None)
    specs = spec_questions(conn, ps)
    user = json.dumps({
        "supplier_name": ps["company"], "supplier_country": ps["country"], "contact_name": ps["contact_name"],
        "product_ko": ps["product"], "product_en": ps["search_keyword_en"] or ps["product"],
        "category": ps["category"], "quantity": qty, "incoterms": incoterm or None,
        "their_listed_moq": ps["moq"], "their_listed_price": ps["price_note"],
        "questions_to_ask": _loads(ps["first_questions_json"], []),
        "unverified_points": _loads(ps["unverified_json"], []),
        "required_certifications": ps["regulations"], "extra_notes": note or None,
        "product_spec_questions_ko": [q["question_ko"] for q in specs],
    }, ensure_ascii=False)
    with _run(conn, {"action": "rfq", "ps_id": ps_id, "language": language}) as run:
        log(f"▶ 견적 요청 초안: [{ps['product_id']}] {ps['product']} → {ps['company']}"
            + (f" · 3번 사양 질문 {len(specs)}개" if specs else ""))
        out = llm.complete_json(prompts.RFQ_SYSTEM.format(language=LANGS.get(language, language)), user)
        return save_draft(conn, run.id, ps, "rfq", language, out,
                          analysis={"spec_flags_asked": [q["flag"] for q in specs]} if specs else None)


# ---------------------------------------------------------------------------
# 역제안
# ---------------------------------------------------------------------------
def counter(quote_id, tone="normal", price=None, note=None, other_quotes_note=None, language=None, conn=None):
    if tone not in prompts.TONES:
        raise ValueError(f"톤은 {', '.join(prompts.TONES)} 중 하나")
    conn = conn or db.connect()
    q = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if q is None:
        raise ValueError(f"견적 번호 {quote_id}가 없습니다")
    ps = get_ps(conn, q["product_supplier_id"])
    _check_active(ps)
    cfg = private.get(ps["product_id"])
    price = _num(price) or cfg.get("target_price_usd")
    if price and cfg.get("max_price_usd") and price > cfg["max_price_usd"]:
        log("  ⚠ 역제안가가 내부 상한가보다 높습니다. 확인하세요.")
    if price and price >= q["unit_price_usd"]:
        log("  ⚠ 역제안가가 상대 견적가 이상입니다.")
    language = language or thread_language(conn, ps["id"])
    user = json.dumps({
        "supplier_name": ps["company"], "contact_name": ps["contact_name"],
        "product_en": ps["search_keyword_en"] or ps["product"],
        "their_quote": {"unit_price_usd": q["unit_price_usd"], "min_qty": q["min_qty"],
                        "price_tiers": _loads(q["price_tiers_json"], []), "incoterm": q["incoterm"],
                        "lead_time_days": q["lead_time_days"], "payment_terms": q["payment_terms"],
                        "sample_cost_usd": q["sample_cost_usd"]},
        "our_counter_price_usd": price,
        "our_requested_payment_terms": cfg.get("preferred_payment_terms"),
        "other_quotes_note": other_quotes_note or None, "extra_notes": note or None,
    }, ensure_ascii=False)
    with _run(conn, {"action": "counter", "quote_id": quote_id, "tone": tone}) as run:
        log(f"▶ 역제안 초안 ({TONE_LABELS[tone]}): {ps['company']} 견적 #{quote_id} ${q['unit_price_usd']:g} → "
            + (f"${price:g}" if price else "가격 미제시"))
        system = prompts.COUNTER_SYSTEM.format(language=LANGS.get(language, language), tone_desc=prompts.TONES[tone])
        out = llm.complete_json(system, user)
        analysis = {"tone": tone, "counter_price_usd": price, "strategy_ko": out.get("strategy_ko")}
        return save_draft(conn, run.id, ps, "negotiation", language, out, reply_to=q["message_id"], analysis=analysis)


# ---------------------------------------------------------------------------
# 받은 답장
# ---------------------------------------------------------------------------
def add_reply(ps_id, text, analyze=True, conn=None, subject=None, sent_at=None, external_id=None, thread_id=None):
    """사람이 붙여넣은(또는 Gmail에서 가져온) 답장을 저장하고 (기본) 바로 분석한다. 저장은 분석이 실패해도 남는다."""
    conn = conn or db.connect()
    ps = get_ps(conn, ps_id)
    text = (text or "").strip()
    if not text:
        raise ValueError("답장 내용이 비어 있습니다")
    prev = None
    if thread_id:      # 같은 Gmail 대화에서 마지막으로 보낸 메일에 대한 답장
        prev = conn.execute("SELECT * FROM messages WHERE product_supplier_id = ? AND direction = 'outbound' "
                            "AND status = 'sent' AND external_thread_id = ? ORDER BY id DESC LIMIT 1",
                            (ps_id, thread_id)).fetchone()
    prev = prev or last_sent(conn, ps_id)
    with conn:
        mid = conn.execute(
            "INSERT INTO messages (product_supplier_id, direction, kind, channel, subject, body, status, reply_to_id,"
            " sent_at, external_id, external_thread_id) VALUES (?, 'inbound', 'other', ?, ?, ?, 'received', ?, ?, ?, ?)",
            (ps_id, "email" if external_id else thread_channel(conn, ps), subject, text, prev["id"] if prev else None,
             sent_at or db.now(), external_id, thread_id)).lastrowid
    log(f"📥 답장 #{mid} 저장 — {ps['company']}" + (f" · {subject}" if subject else ""))
    hits = bank_change_evidence(f"{subject or ''}\n{text}")
    if hits:
        with conn:
            _bank_task(conn, ps, mid, hits)
    print(f"RESULT_ANCHOR=ps-{ps_id}", flush=True)
    if analyze:
        analyze_replies([mid], conn=conn)
    return mid


def analyze_replies(ids=None, conn=None):
    """분석 안 된 받은 답장(analysis_json IS NULL) → 요약·번역·견적 추출. ids를 주면 그것만 (다시) 분석."""
    conn = conn or db.connect()
    if ids:
        rows = conn.execute(f"SELECT * FROM messages WHERE direction = 'inbound' AND id IN ({', '.join('?' * len(ids))})",
                            list(ids)).fetchall()
    else:
        rows = conn.execute(PENDING_REPLY_SQL).fetchall()
    if not rows:
        log("분석할 답장이 없습니다.")
        return []
    with _run(conn, {"action": "analyze", "ids": [r["id"] for r in rows]}) as run:
        return _analyze(conn, run, rows)


def _analyze(conn, run, rows):
    done, errors = [], []
    for m in rows:
        ps = get_ps(conn, m["product_supplier_id"])
        log(f"▶ 답장 분석 #{m['id']} — {ps['company']}")
        prev = conn.execute("SELECT subject, body FROM messages WHERE id = ?", (m["reply_to_id"],)).fetchone() \
            if m["reply_to_id"] else None
        asked = spec_questions(conn, ps, unanswered_only=False)    # 이미 답한 항목도 — 공급처가 고쳐 말할 수 있음
        user = json.dumps({"product": ps["search_keyword_en"] or ps["product"], "supplier": ps["company"],
                           "our_previous_message": (f"{prev['subject']}\n\n{prev['body']}"[:3000] if prev else None),
                           "spec_questions": [{"flag": q["flag"], "question_ko": q["question_ko"],
                                               "allowed_values": _spec_values(q["flag"])} for q in asked],
                           "reply": m["body"]}, ensure_ascii=False)
        try:
            out = llm.complete_json(prompts.REPLY_SYSTEM, user)
        except Exception as err:      # 이 답장만 실패로 표시하고 다음 답장 계속 (자동 재시도 안 함)
            errors.append(m["id"])
            with conn:
                conn.execute("UPDATE messages SET analysis_json = ? WHERE id = ?",
                             (db.dumps({"error": f"{type(err).__name__}: {err}"[:500]}), m["id"]))
            log(f"  ✗ 분석 실패: {err}")
            continue
        done.append(_save_analysis(conn, run.id, ps, m, out, {q["flag"] for q in asked}))
    if errors:
        raise RuntimeError(f"답장 {len(errors)}건 분석 실패: {errors} — `python -m negotiation_agent analyze {' '.join(map(str, errors))}`로 다시")
    return done


def _place(out, incoterm):
    """인코텀즈 지정장소: 공급처가 쓴 그대로 (없으면 None — 국가·도시로 추정하지 않음). 'FOB Ningbo'가 통째로 오면 규칙 글자는 뺀다."""
    place = " ".join(str(out.get("incoterm_place") or "").split())
    if incoterm and place.upper().startswith(incoterm + " "):
        place = place[len(incoterm) + 1:].strip()
    if not place or place.lower() in ("null", "none", "n/a", "-", "unknown", "tbd"):
        return None
    return place[:100]


# ---------------------------------------------------------------------------
# 계좌 변경 사기 경고: 받은 메일에 송금 계좌 안내·변경이 있으면 결제 할 일(최우선)로. 자동 답장·계좌 정보 저장은 하지 않는다.
# ---------------------------------------------------------------------------
BANK_TITLE = "⚠ 계좌 변경 요청 확인"
BANK_RE = re.compile(
    r"(?:new|changed?|change\s+of|updated?|different|another|alternative)\s+(?:our\s+)?"
    r"(?:bank|banking|beneficiary|receiving|company)?\s*(?:account|bank\s+details|bank\s+info\w*|beneficiary)"
    r"|(?:bank|account|banking|payment)\s+(?:details|information|info)\s+(?:has|have|had)\s+(?:been\s+)?(?:changed|updated)"
    r"|changed\s+(?:our\s+)?bank"
    r"|\b(?:swift(?:\s*code)?|iban|bic\s*code|beneficiary\s+(?:name|bank|account)|account\s+(?:no|number)\b)"
    r"|(?:pay|remit|transfer|send)\w*\s+(?:the\s+\w+\s+)?to\s+(?:our\s+)?(?:new|another|this|the\s+following)\s+account"
    r"|新(?:的)?(?:银行)?(?:账户|账号|帐户|帐号)|(?:更换|变更|更改|换了)\S{0,8}(?:账户|账号|帐户|帐号|银行)"
    r"|收款(?:账户|账号|人|银行)|银行(?:账户|账号)?信息|开户行"
    r"|(?:계좌|입금처)\S{0,6}(?:변경|바뀌|바꾸)|새\s*계좌|변경된\s*계좌|입금\s*계좌|수취인",
    re.I)


def bank_change_evidence(text):
    """송금 계좌 안내·변경으로 보이는 문장들 (최대 3개, 각 200자). 없으면 []. 놓치지 않는 쪽으로 넓게 잡는다."""
    out = []
    for sent in re.split(r"(?<=[.!?。！？])\s+|\n+", text or ""):
        if BANK_RE.search(sent) and sent.strip() not in out:
            out.append(sent.strip())
        if len(out) >= 3:
            break
    return [s[:200] for s in out]


def _mask_numbers(s):
    """근거 문장의 계좌번호·SWIFT·IBAN 같은 긴 숫자·코드는 가린다 (할 일에 송금 정보를 남기지 않음)."""
    s = re.sub(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{10,30}\b", "[IBAN 가림]", s)
    s = re.sub(r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", "[코드 가림]", s)
    return re.sub(r"\d[\d\s-]{5,}\d", "[숫자 가림]", s)


def _bank_task(conn, ps, message_id, evidence, run_id=None):
    """with conn: 안에서 부른다. 같은 메일에는 하나만 (db.add_task 가 ref 로 중복을 막음)."""
    ev = " / ".join(_mask_numbers(e) for e in evidence if e and e.strip())[:600]
    tid = db.add_task(conn, "payment", f"{BANK_TITLE}: {ps['company']}", product_id=ps["product_id"], run_id=run_id,
                      ref_table="messages", ref_id=message_id, priority=1,
                      detail="전화 등 다른 경로로 공급처에 직접 확인 전까지 송금 금지 (메일에 적힌 연락처로 확인하지 말 것). "
                             + (f"근거 문장: {ev}" if ev else "답장 분석에서 계좌 안내가 감지됨"))
    log(f"  ⚠ 계좌 안내·변경 문구 감지 → 결제 할 일 #{tid} (다른 경로로 확인 전 송금 금지)")
    return tid


def _spec_answers(out, asked, message_id):
    """LLM이 뽑은 사양 답 → (flags, evidence). 물어본 항목만, 값 형식이 맞는 것만 (애매하면 LLM이 빼도록 프롬프트에서 막음)."""
    flags, evidence = {}, []
    for a in out.get("spec_answers") or []:
        if not isinstance(a, dict) or a.get("flag") not in asked or a["flag"] in flags:
            continue
        flag, value = a["flag"], a.get("value")
        if db.SPEC_FLAG_KEYS[flag] is str and isinstance(value, str):
            value = value.strip().upper()
        if not db._spec_value_ok(flag, value):
            continue
        flags[flag] = value
        claim = str(a.get("claim_ko") or "").strip() or f"{flag} = {value}"
        evidence.append({"claim": f"{flag}: {claim}"[:300], "message_id": message_id})
    return flags, evidence


def _save_analysis(conn, run_id, ps, m, out, asked=frozenset()):
    """분석 결과 저장. 같은 답장을 다시 분석하면 그 답장의 견적을 고치고(새로 만들지 않음) 대기 중인 검토 할 일을 바꾼다."""
    keys = ("unit_price", "currency", "min_qty", "price_tiers", "incoterm", "incoterm_place", "lead_time_days",
            "payment_terms", "sample_cost", "sample_shipping", "valid_until")
    analysis = {"summary_ko": out.get("summary_ko"), "open_questions_ko": out.get("open_questions_ko") or [],
                "extracted": {k: out.get(k) for k in keys}}
    price, cur = _num(out.get("unit_price")), (out.get("currency") or "").strip().upper() or None
    cur = CURRENCY_ALIAS.get(cur, cur)
    flags, evidence = _spec_answers(out, asked, m["id"])
    if flags:
        analysis["spec_answers"] = flags

    rate, fx_note = None, None       # 1 USD = rate cur. USD가 아니면 환산, 환율을 못 구하면 견적표 대신 할 일로
    if price is not None and cur == "USD":
        rate = 1.0
    elif price is not None and cur:
        rate, src = fx_rate(cur)
        if rate:
            fx_note = f"원래 견적 {price:g} {cur} — 환율 1 USD = {rate:g} {cur} ({src}) 적용, 확인 필요"
            analysis["fx"] = {"currency": cur, "unit_price": price, "rate": rate, "source": src}
        else:
            fx_note = src

    def usd(v):
        return None if v is None or not rate else round(v / rate, 4)

    old = conn.execute("SELECT id FROM quotes WHERE message_id = ? ORDER BY id", (m["id"],)).fetchall()
    quote_id = None
    with conn:
        conn.execute("UPDATE messages SET translation = ?, language = COALESCE(language, ?), analysis_json = ? WHERE id = ?",
                     (out.get("translation_ko"), out.get("language"), db.dumps(analysis), m["id"]))
        # 다시 분석: 이 답장으로 만든 대기 중 검토 할 일은 닫고 새로 만든다 (중복 방지)
        for table, rid in [("messages", m["id"])] + [("quotes", q["id"]) for q in old]:
            conn.execute("UPDATE tasks SET status = 'dismissed', done_at = ? WHERE kind = 'review_quote' "
                         "AND ref_table = ? AND ref_id = ? AND status = 'pending'", (db.now(), table, rid))
        bc = out.get("bank_change") if isinstance(out.get("bank_change"), dict) else {}
        if bc.get("detected") is True:
            _bank_task(conn, ps, m["id"], [str(bc.get("evidence") or "")], run_id)
        if flags:
            db.merge_supplier_specs(conn, ps["id"], flags, evidence)
            log("  🔎 공급처가 답한 사양 기록 (3번 참고): " + ", ".join(f"{k}={v}" for k, v in flags.items()))
        if price is not None and rate:
            price_usd = usd(price)
            tiers = [{"qty": _int(t.get("qty")), "usd": usd(_num(t.get("price")))} for t in out.get("price_tiers") or []
                     if isinstance(t, dict) and _num(t.get("price")) is not None]
            inco = (out.get("incoterm") or "").upper()
            inco = inco if inco in INCOTERMS else None
            vals = (price_usd, _int(out.get("min_qty")), db.dumps(tiers or None), inco, _place(out, inco),
                    _int(out.get("lead_time_days")), out.get("payment_terms"), usd(_num(out.get("sample_cost"))),
                    usd(_num(out.get("sample_shipping"))), out.get("valid_until"), fx_note)
            cols = ("unit_price_usd, min_qty, price_tiers_json, incoterm, incoterm_place, lead_time_days, payment_terms, "
                    "sample_cost_usd, sample_shipping_usd, valid_until, notes")
            marks = ", ".join("?" * len(vals))
            if old:
                quote_id = old[0]["id"]
                conn.execute(f"UPDATE quotes SET ({cols}) = ({marks}), run_id = ? WHERE id = ?",
                             (*vals, run_id, quote_id))
            else:
                quote_id = conn.execute(
                    f"INSERT INTO quotes (product_supplier_id, message_id, run_id, {cols})"
                    f" VALUES (?, ?, ?, {marks})", (ps["id"], m["id"], run_id, *vals)).lastrowid
            advance_ps_status(conn, ps["id"], "quoted")
            moq = f" / MOQ {_int(out.get('min_qty'))}" if out.get("min_qty") else ""
            orig = f" (원래 {price:g} {cur})" if fx_note else ""
            db.add_task(conn, "review_quote", f"[{ps['product']}] {ps['company']} 견적 검토 — ${price_usd:g}{moq}{orig}",
                        product_id=ps["product_id"], run_id=run_id, ref_table="quotes", ref_id=quote_id,
                        detail=" ".join(x for x in (fx_note, analysis["summary_ko"]) if x), priority=2)
            log(f"  💾 견적 #{quote_id}{' (다시 분석 → 고침)' if old else ''}: ${price_usd:g}{moq}{orig}")
        elif price is not None:
            why = fx_note or "통화 미상"
            db.add_task(conn, "review_quote", f"[{ps['product']}] {ps['company']} 견적 확인 — {price:g} {cur or '통화 미상'}",
                        product_id=ps["product_id"], run_id=run_id, ref_table="messages", ref_id=m["id"],
                        detail=f"USD로 바꾸지 못해 견적표에 넣지 않았습니다 ({why}). " + (analysis["summary_ko"] or ""),
                        priority=2)
            log(f"  · 가격 {price:g} {cur or '통화 미상'} — USD로 바꾸지 못해 견적표 대신 할 일로 남김 ({why})")
        else:
            log("  · 가격 정보 없음 (요약만 저장)")
    return {"message_id": m["id"], "quote_id": quote_id}


# ---------------------------------------------------------------------------
# 후속 메일
# ---------------------------------------------------------------------------
BLOCKED_TITLE = "후속 메일 자동 생성 막힘"     # tasks.title 머리 — 이 할 일이 있는 메일은 자동 후속에서 뺀다


def due_followups(conn, days=FOLLOWUP_DAYS, max_followups=MAX_FOLLOWUPS):
    """후속 메일이 필요한 보낸 메일. 상한가 검사에 두 번 막혀 할 일로 넘긴 메일은 빼고(사람이 버튼으로 직접),
    슈퍼바이저 when_sql 은 이 차이를 모른다 — 그 경우 auto 가 실행돼도 할 일이 없어 그냥 끝난다."""
    rows = conn.execute(FOLLOWUP_DUE_SQL, {"days": days, "max": max_followups}).fetchall()
    blocked = {r[0] for r in conn.execute("SELECT ref_id FROM tasks WHERE ref_table = 'messages' AND kind = 'other' "
                                          "AND title LIKE ?", (f"%{BLOCKED_TITLE}%",))}
    return [r for r in rows if r["id"] not in blocked]


def followups(ids=None, days=FOLLOWUP_DAYS, note=None, conn=None):
    """답이 없는 보낸 메일에 후속 메일 초안. ids를 주면 그 메일만 (기한과 상관없이)."""
    conn = conn or db.connect()
    if ids:
        rows = conn.execute(f"SELECT * FROM messages WHERE direction = 'outbound' AND status = 'sent' "
                            f"AND id IN ({', '.join('?' * len(ids))})", list(ids)).fetchall()
    else:
        rows = due_followups(conn, days)
    if not rows:
        log("후속 메일이 필요한 메일이 없습니다.")
        return []
    with _run(conn, {"action": "followup", "ids": [r["id"] for r in rows]}) as run:
        return _followups(conn, run, rows, note, manual=bool(ids))


def _followups(conn, run, rows, note=None, manual=False):
    out_ids, errors = [], []
    for m in rows:
        ps = get_ps(conn, m["product_supplier_id"])
        if ps["stage"] in db.TERMINAL_STAGES or ps["status"] == "rejected":
            log(f"  · 건너뜀: {ps['company']} (끝난 품목이거나 제외한 공급처)")
            continue
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE reply_to_id = ? AND kind = 'follow_up' "
                             "AND status = 'sent'", (m["id"],)).fetchone()[0]
        days = (datetime.now() - datetime.fromisoformat(m["sent_at"])).days
        log(f"▶ 후속 메일 초안: {ps['company']} — 메일 #{m['id']} {days}일째 답 없음 (후속 {count}회 보냄)")
        language = m["language"] or thread_language(conn, ps["id"])
        specs = spec_questions(conn, ps)
        user = json.dumps({"supplier_name": ps["company"], "contact_name": ps["contact_name"],
                           "previous_message": f"{m['subject'] or ''}\n\n{m['body']}"[:3000],
                           "days_since_sent": days, "follow_up_count": count, "extra_notes": note or None,
                           "product_spec_questions_ko": [q["question_ko"] for q in specs]},
                          ensure_ascii=False)
        system = prompts.FOLLOWUP_SYSTEM.format(language=LANGS.get(language, language))
        analysis = {"spec_flags_asked": [q["flag"] for q in specs]} if specs else None
        try:
            try:
                out = llm.complete_json(system, user)
                out_ids.append(save_draft(conn, run.id, ps, "follow_up", language, out, reply_to=m["id"], analysis=analysis))
            except DraftRejected:            # 우연히 상한가와 같은 숫자 → 숫자 없이 한 번 더
                log("  · 상한가와 같은 숫자가 나와 숫자 없이 다시 생성")
                out = llm.complete_json(system + prompts.NO_NUMBERS_RETRY, user)
                out_ids.append(save_draft(conn, run.id, ps, "follow_up", language, out, reply_to=m["id"], analysis=analysis))
        except DraftRejected as err:         # 두 번 막힘 → 자동으로는 다시 하지 않고 사람에게 (버튼으로 직접 만들 수 있음)
            log(f"  ✗ {err}")
            if not manual:
                with conn:
                    db.add_task(conn, "other", f"[{ps['product']}] {ps['company']} {BLOCKED_TITLE} — 메일 #{m['id']}",
                                product_id=ps["product_id"], run_id=run.id, ref_table="messages", ref_id=m["id"],
                                detail="초안에 내부 상한가와 같은 숫자가 두 번 나와 저장하지 않았습니다. "
                                       "필요하면 5번 페이지 [후속 메일 초안]으로 직접 만드세요. 자동 후속은 이 메일에 더 하지 않습니다.",
                                priority=2)
            errors.append(m["id"])
        except Exception as err:      # 이 메일만 건너뛰고 다음 메일 계속
            errors.append(m["id"])
            log(f"  ✗ 실패: {err}")
    if errors:
        raise RuntimeError(f"후속 메일 {len(errors)}건 실패 (메일 {errors}) — 다음 실행 때 다시 시도합니다")
    return out_ids


# ---------------------------------------------------------------------------
# 슈퍼바이저용: 분석 안 된 답장 + 후속 메일 (RFQ·역제안은 사람이 버튼으로)
# ---------------------------------------------------------------------------
def auto(days=FOLLOWUP_DAYS, conn=None):
    from . import gmail
    conn = conn or db.connect()
    use_gmail = gmail.configured()
    replies, due = conn.execute(PENDING_REPLY_SQL).fetchall(), due_followups(conn, days)
    log(f"공용 DB: {db.default_path()}")
    log(f"분석할 답장 {len(replies)}건 · 후속 메일 필요 {len(due)}건" + (" · Gmail 확인" if use_gmail else ""))
    if not replies and not due and not use_gmail:
        return {}
    result, errors = {}, []
    with _run(conn, {"action": "auto", "days": days, "gmail": use_gmail}) as run:
        if use_gmail:       # Gmail에서 발송 확인·새 답장 가져오기 → 그 결과로 할 일을 다시 계산 (답장 온 곳엔 후속 X)
            try:
                result["gmail"] = gmail.sync(conn)
            except Exception as err:
                errors.append(f"Gmail: {err}")
            replies, due = conn.execute(PENDING_REPLY_SQL).fetchall(), due_followups(conn, days)
        for key, fn, rows in (("followups", _followups, due), ("analyzed", _analyze, replies)):
            if not rows:
                continue
            try:                                  # 한쪽이 실패해도 다른 쪽은 처리 (저장한 건 이미 커밋됨)
                result[key] = fn(conn, run, rows)
            except Exception as err:
                errors.append(str(err))
        if errors:
            raise RuntimeError(" / ".join(errors))
    return result


# ---------------------------------------------------------------------------
# 사람의 처리 표시 (대시보드 버튼 / CLI) — LLM 없음, agent_runs에 남기지 않음 (run_id NULL = 사람)
# ---------------------------------------------------------------------------
MARKS = {"approved": {"draft"}, "sent": {"draft", "approved"}, "discarded": {"draft", "approved"},
         "draft": {"approved"}}


def mark(message_id, status, conn=None, sent_at=None, external_id=None, how="사람"):
    """sent_at·external_id·how 는 Gmail 동기화가 '사람이 Gmail에서 보낸 것'을 확인했을 때 쓴다."""
    conn = conn or db.connect()
    m = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if m is None or m["direction"] != "outbound":
        raise ValueError(f"보낸 메일 초안 #{message_id}이(가) 없습니다")
    if status not in MARKS:
        raise ValueError(f"상태는 {', '.join(MARKS)} 중 하나")
    if m["status"] not in MARKS[status]:
        raise ValueError(f"#{message_id}은(는) 이미 '{m['status']}' 상태라 '{status}'(으)로 바꿀 수 없습니다")
    ps = get_ps(conn, m["product_supplier_id"])
    with conn:
        conn.execute("UPDATE messages SET status = ?, sent_at = ?, external_id = COALESCE(?, external_id) WHERE id = ?",
                     (status, (sent_at or db.now()) if status == "sent" else None, external_id, message_id))
        task_status = {"sent": "done", "discarded": "dismissed"}.get(status)
        if task_status:
            conn.execute("UPDATE tasks SET status = ?, done_at = ? WHERE kind = 'send_message' AND ref_table = 'messages'"
                         " AND ref_id = ? AND status = 'pending'", (task_status, db.now(), message_id))
        if status == "sent":
            advance_ps_status(conn, ps["id"], "sampling" if m["kind"] == "sample" else "contacted")
            if ps["stage"] == "sourcing":      # 첫 메일 발송 = 협상 시작 (사람이 보냈으므로 run_id 없음)
                db.advance_stage(conn, ps["product_id"], "negotiating", None,
                                 f"{ps['company']}에 {KIND_LABEL.get(m['kind'], '메일')} 발송 ({how})")
    log(f"#{message_id} → {status} ({ps['company']})")
    print(f"RESULT_ANCHOR=ps-{ps['id']}", flush=True)


# ---------------------------------------------------------------------------
# 샘플 요청 (로드맵 3단계)
# ---------------------------------------------------------------------------
def sample(ps_id, quantity=None, note=None, language=None, address_note=None, conn=None):
    """샘플 요청 초안 (messages.kind='sample'). 사람이 보내면 공급처 상태가 '샘플'(sampling)로."""
    conn = conn or db.connect()
    ps = get_ps(conn, ps_id)
    _check_active(ps)
    pending = conn.execute("SELECT id FROM messages WHERE product_supplier_id = ? AND kind = 'sample' "
                           "AND status IN ('draft', 'approved')", (ps_id,)).fetchone()
    if pending:
        raise ValueError(f"이 공급처에는 검토 대기 중인 샘플 요청 초안(#{pending['id']})이 있습니다")
    q = conn.execute("SELECT * FROM quotes WHERE product_supplier_id = ? ORDER BY id DESC LIMIT 1", (ps_id,)).fetchone()
    language = language or thread_language(conn, ps_id)
    specs = spec_questions(conn, ps)
    user = json.dumps({
        "supplier_name": ps["company"], "contact_name": ps["contact_name"],
        "product_en": ps["search_keyword_en"] or ps["product"],
        "sample_quantity": quantity or "1-2 pcs",
        "their_quote": ({"unit_price_usd": q["unit_price_usd"], "min_qty": q["min_qty"],
                         "sample_cost_usd": q["sample_cost_usd"], "sample_shipping_usd": q["sample_shipping_usd"]}
                        if q else None),
        "shipping_destination": "South Korea", "shipping_note": address_note or None,
        "extra_notes": note or None,
        "product_spec_questions_ko": [x["question_ko"] for x in specs],
    }, ensure_ascii=False)
    with _run(conn, {"action": "sample", "ps_id": ps_id, "language": language}) as run:
        log(f"▶ 샘플 요청 초안: [{ps['product_id']}] {ps['product']} → {ps['company']}")
        out = llm.complete_json(prompts.SAMPLE_SYSTEM.format(language=LANGS.get(language, language)), user)
        analysis = {"spec_flags_asked": [x["flag"] for x in specs]} if specs else None
        return save_draft(conn, run.id, ps, "sample", language, out,
                          reply_to=q["message_id"] if q else None, analysis=analysis)


# ---------------------------------------------------------------------------
# 공급처 선정 / 제외 / 되돌리기 (사람 버튼 — LLM 없음, run_id 없음)
# ---------------------------------------------------------------------------
SUPPLIER_ACTIONS = {"select": "선정", "reject": "제외", "restore": "되돌리기"}


def _natural_status(conn, ps_id):
    """메일·견적 기록으로 본 원래 진행 상태 (되돌리기용)."""
    sent = {r[0] for r in conn.execute("SELECT kind FROM messages WHERE product_supplier_id = ? AND direction = 'outbound' "
                                       "AND status = 'sent'", (ps_id,))}
    if "sample" in sent:
        return "sampling"
    if conn.execute("SELECT 1 FROM quotes WHERE product_supplier_id = ?", (ps_id,)).fetchone():
        return "quoted"
    return "contacted" if sent else "candidate"


def supplier(ps_id, action, reason=None, conn=None):
    """select: 이 공급처로 진행 (status='selected') / reject: 제외 (+사유, 이후 초안·자동 후속 안 함) /
    restore: 선정·제외 취소 → 메일·견적 기록대로. 다른 공급처는 건드리지 않는다."""
    if action not in SUPPLIER_ACTIONS:
        raise ValueError(f"동작은 {', '.join(SUPPLIER_ACTIONS)} 중 하나")
    conn = conn or db.connect()
    ps = get_ps(conn, ps_id)
    reason = " ".join((reason or "").split())[:300] or None
    status = {"select": "selected", "reject": "rejected"}.get(action) or _natural_status(conn, ps_id)
    with conn:
        conn.execute("UPDATE product_suppliers SET status = ?, reject_reason = ?, updated_at = ? WHERE id = ?",
                     (status, reason if action == "reject" else None, db.now(), ps_id))
        if action == "reject":       # 제외한 공급처의 대기 중 발송 할 일은 닫음 (초안 자체는 남김 — 사람이 버림 처리)
            conn.execute("UPDATE tasks SET status = 'dismissed', done_at = ? WHERE kind = 'send_message' AND status = 'pending'"
                         " AND ref_table = 'messages' AND ref_id IN (SELECT id FROM messages WHERE product_supplier_id = ?)",
                         (db.now(), ps_id))
    log(f"{ps['company']} → {SUPPLIER_ACTIONS[action]} ({ps['status']} → {status})" + (f" · 사유: {reason}" if reason else ""))
    print(f"RESULT_ANCHOR=ps-{ps_id}", flush=True)
    return status
