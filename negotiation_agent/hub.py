"""5번 커뮤니케이션·협상 에이전트 — 공용 DB(trade_hub) 연동.

2번이 찾은 공급처(product_suppliers)에 보낼 메일 초안을 만들고, 사람이 붙여넣은 답장을 분석해 견적(quotes)을 남긴다.
메일은 절대 보내지 않는다: 초안(messages.status='draft')과 승인 대기함 할 일(tasks)만 만든다.

이 에이전트가 쓰는 곳 (trade_hub/schema.sql 머리말):
  messages, quotes, tasks(send_message·review_quote),
  product_suppliers.status (candidate→contacted→quoted, 앞으로만),
  products.stage sourcing→negotiating (사람이 첫 메일을 '발송 완료'로 표시할 때만)
내부 상한가는 공용 DB에 두지 않는다 → private.py
"""
import json
import sys
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
  AND julianday('now', 'localtime') - julianday(COALESCE(
        (SELECT MAX(f.sent_at) FROM messages f WHERE f.reply_to_id = m.id AND f.kind = 'follow_up' AND f.status = 'sent'),
        m.sent_at)) >= :days
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


def _run(conn, args):
    return db.agent_run(conn, AGENT, provider=llm.PROVIDER, model=llm.MODEL, args=args)


# ---------------------------------------------------------------------------
# RFQ (첫 문의)
# ---------------------------------------------------------------------------
def rfq(ps_id, quantity=None, incoterm=None, language="en", note=None, force=False, conn=None):
    conn = conn or db.connect()
    ps = get_ps(conn, ps_id)
    if ps["stage"] in db.TERMINAL_STAGES:
        raise ValueError(f"품목 #{ps['product_id']}은(는) {ps['stage']} 상태입니다")
    pending = conn.execute("SELECT id FROM messages WHERE product_supplier_id = ? AND kind = 'rfq' "
                           "AND status IN ('draft', 'approved', 'sent')", (ps_id,)).fetchone()
    if pending and not force:
        raise ValueError(f"이 공급처에는 이미 견적 요청 메일(#{pending['id']})이 있습니다. 새로 만들려면 --force")
    qty = quantity or (f"{ps['max_order_qty']} pcs (first order)" if ps["max_order_qty"] else None)
    user = json.dumps({
        "supplier_name": ps["company"], "supplier_country": ps["country"], "contact_name": ps["contact_name"],
        "product_ko": ps["product"], "product_en": ps["search_keyword_en"] or ps["product"],
        "category": ps["category"], "quantity": qty, "incoterms": incoterm or None,
        "their_listed_moq": ps["moq"], "their_listed_price": ps["price_note"],
        "questions_to_ask": _loads(ps["first_questions_json"], []),
        "unverified_points": _loads(ps["unverified_json"], []),
        "required_certifications": ps["regulations"], "extra_notes": note or None,
    }, ensure_ascii=False)
    with _run(conn, {"action": "rfq", "ps_id": ps_id, "language": language}) as run:
        log(f"▶ 견적 요청 초안: [{ps['product_id']}] {ps['product']} → {ps['company']}")
        out = llm.complete_json(prompts.RFQ_SYSTEM.format(language=LANGS.get(language, language)), user)
        return save_draft(conn, run.id, ps, "rfq", language, out)


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
        user = json.dumps({"product": ps["search_keyword_en"] or ps["product"], "supplier": ps["company"],
                           "our_previous_message": (f"{prev['subject']}\n\n{prev['body']}"[:3000] if prev else None),
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
        done.append(_save_analysis(conn, run.id, ps, m, out))
    if errors:
        raise RuntimeError(f"답장 {len(errors)}건 분석 실패: {errors} — `python -m negotiation_agent analyze {' '.join(map(str, errors))}`로 다시")
    return done


def _save_analysis(conn, run_id, ps, m, out):
    keys = ("unit_price", "currency", "min_qty", "price_tiers", "incoterm", "lead_time_days", "payment_terms",
            "sample_cost", "sample_shipping", "valid_until")
    analysis = {"summary_ko": out.get("summary_ko"), "open_questions_ko": out.get("open_questions_ko") or [],
                "extracted": {k: out.get(k) for k in keys}}
    price, cur = _num(out.get("unit_price")), (out.get("currency") or "").upper() or None
    quote_id = None
    with conn:
        conn.execute("UPDATE messages SET translation = ?, language = COALESCE(language, ?), analysis_json = ? WHERE id = ?",
                     (out.get("translation_ko"), out.get("language"), db.dumps(analysis), m["id"]))
        if price is not None and cur == "USD":
            tiers = [{"qty": _int(t.get("qty")), "usd": _num(t.get("price"))} for t in out.get("price_tiers") or []
                     if isinstance(t, dict) and _num(t.get("price")) is not None]
            inco = (out.get("incoterm") or "").upper()
            quote_id = conn.execute(
                "INSERT INTO quotes (product_supplier_id, message_id, run_id, unit_price_usd, min_qty, price_tiers_json,"
                " incoterm, lead_time_days, payment_terms, sample_cost_usd, sample_shipping_usd, valid_until)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ps["id"], m["id"], run_id, price, _int(out.get("min_qty")), db.dumps(tiers or None),
                 inco if inco in INCOTERMS else None, _int(out.get("lead_time_days")), out.get("payment_terms"),
                 _num(out.get("sample_cost")), _num(out.get("sample_shipping")),
                 out.get("valid_until"))).lastrowid
            advance_ps_status(conn, ps["id"], "quoted")
            moq = f" / MOQ {_int(out.get('min_qty'))}" if out.get("min_qty") else ""
            db.add_task(conn, "review_quote", f"[{ps['product']}] {ps['company']} 견적 검토 — ${price:g}{moq}",
                        product_id=ps["product_id"], run_id=run_id, ref_table="quotes", ref_id=quote_id,
                        detail=analysis["summary_ko"], priority=2)
            log(f"  💾 견적 #{quote_id}: ${price:g}{moq}")
        elif price is not None:
            db.add_task(conn, "review_quote", f"[{ps['product']}] {ps['company']} 견적 확인 — {price:g} {cur or '통화 미상'}",
                        product_id=ps["product_id"], run_id=run_id, ref_table="messages", ref_id=m["id"],
                        detail="USD가 아니어서 견적표에 넣지 않았습니다. " + (analysis["summary_ko"] or ""), priority=2)
            log(f"  · 가격 {price:g} {cur or '통화 미상'} — USD가 아니어서 견적표 대신 할 일로 남김")
        else:
            log("  · 가격 정보 없음 (요약만 저장)")
    return {"message_id": m["id"], "quote_id": quote_id}


# ---------------------------------------------------------------------------
# 후속 메일
# ---------------------------------------------------------------------------
def due_followups(conn, days=FOLLOWUP_DAYS, max_followups=MAX_FOLLOWUPS):
    return conn.execute(FOLLOWUP_DUE_SQL, {"days": days, "max": max_followups}).fetchall()


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
        return _followups(conn, run, rows, note)


def _followups(conn, run, rows, note=None):
    out_ids, errors = [], []
    for m in rows:
        ps = get_ps(conn, m["product_supplier_id"])
        count = conn.execute("SELECT COUNT(*) FROM messages WHERE reply_to_id = ? AND kind = 'follow_up' "
                             "AND status = 'sent'", (m["id"],)).fetchone()[0]
        days = (datetime.now() - datetime.fromisoformat(m["sent_at"])).days
        log(f"▶ 후속 메일 초안: {ps['company']} — 메일 #{m['id']} {days}일째 답 없음 (후속 {count}회 보냄)")
        language = m["language"] or thread_language(conn, ps["id"])
        user = json.dumps({"supplier_name": ps["company"], "contact_name": ps["contact_name"],
                           "previous_message": f"{m['subject'] or ''}\n\n{m['body']}"[:3000],
                           "days_since_sent": days, "follow_up_count": count, "extra_notes": note or None},
                          ensure_ascii=False)
        try:
            out = llm.complete_json(prompts.FOLLOWUP_SYSTEM.format(language=LANGS.get(language, language)), user)
            out_ids.append(save_draft(conn, run.id, ps, "follow_up", language, out, reply_to=m["id"]))
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
            advance_ps_status(conn, ps["id"], "contacted")
            if ps["stage"] == "sourcing":      # 첫 메일 발송 = 협상 시작 (사람이 보냈으므로 run_id 없음)
                db.advance_stage(conn, ps["product_id"], "negotiating", None,
                                 f"{ps['company']}에 {KIND_LABEL.get(m['kind'], '메일')} 발송 ({how})")
    log(f"#{message_id} → {status} ({ps['company']})")
    print(f"RESULT_ANCHOR=ps-{ps['id']}", flush=True)
