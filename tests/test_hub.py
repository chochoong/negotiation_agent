"""3번 협상 에이전트 테스트 — 임시 공용 DB + 임시 private DB, LLM은 가짜 응답 (API 호출 없음)."""
import json

import pytest

from comm_agent import __main__ as cli
from comm_agent import hub, llm, private
from trade_hub import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB", str(tmp_path / "trade.db"))
    monkeypatch.setenv("COMM_PRIVATE_DB", str(tmp_path / "private.db"))
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def fake_llm(monkeypatch):
    """calls[-1] = (system, user dict). reply 를 바꾸면 다음 응답이 바뀐다."""
    state = {"calls": [], "reply": None}

    def complete_json(system, user, max_tokens=3000):
        state["calls"].append((system, json.loads(user)))
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return dict(state["reply"] or {"subject": "Inquiry: yoga mat", "body": "Dear Lily, could you quote USD 2.80?",
                                       "korean_translation": "2.80달러 가능?", "strategy_ko": "목표가 제시"})

    monkeypatch.setattr(llm, "complete_json", complete_json)
    return state


@pytest.fixture
def ps(conn):
    """sourcing 단계 품목 1개 + 2번이 찾은 공급처 1곳."""
    with conn:
        pid = db.upsert_product(conn, "요가매트", stage="sourcing", search_keyword_en="yoga mat",
                                regulations="KC 안전확인")
        sid = db.upsert_supplier(conn, "ABC Co", "China", contact_name="Lily", contact_email="lily@abc.cn")
        link = db.link_supplier(conn, pid, sid, is_top_pick=1, moq="500 pcs", max_order_qty=50,
                                first_questions_json=["Can you print our logo?"], unverified_json=["factory audit"])
    return conn.execute(hub.PS_SQL, (link,)).fetchone()


def test_rfq_draft_uses_sourcing_data_and_creates_task(conn, ps, fake_llm):
    mid = hub.rfq(ps["id"], conn=conn)
    _, user = fake_llm["calls"][-1]
    assert user["product_en"] == "yoga mat" and user["quantity"] == "50 pcs (first order)"
    assert user["questions_to_ask"] == ["Can you print our logo?"] and user["required_certifications"] == "KC 안전확인"
    m = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    assert (m["direction"], m["kind"], m["status"], m["channel"]) == ("outbound", "rfq", "draft", "email")
    assert m["translation"] == "2.80달러 가능?" and m["run_id"]
    run = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (m["run_id"],)).fetchone()
    assert (run["agent"], run["status"]) == ("negotiation", "succeeded")
    t = conn.execute("SELECT * FROM tasks WHERE ref_table = 'messages' AND ref_id = ?", (mid,)).fetchone()
    assert t["kind"] == "send_message" and t["status"] == "pending"
    with pytest.raises(ValueError, match="이미 견적 요청"):       # 중복 방지
        hub.rfq(ps["id"], conn=conn)


def test_max_price_never_saved_or_sent_to_llm(conn, ps, fake_llm):
    private.set_(ps["product_id"], target=2.8, max_price=1000)
    fake_llm["reply"] = {"subject": "Order", "body": "We can go up to 1,000 USD.", "korean_translation": "-"}
    with pytest.raises(hub.DraftRejected):
        hub.rfq(ps["id"], conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM agent_runs ORDER BY id DESC").fetchone()[0] == "failed"
    assert "1000" not in json.dumps(fake_llm["calls"][-1][1])
    for text in ("max 1000.00", "1,000.", "USD1000"):
        assert private.leaks_max_price(text, ps["product_id"])
    assert not private.leaks_max_price("2.80 / 10000 pcs", ps["product_id"])
    assert private.leaks_max_price("1000", None)            # 품목 미지정 → 전체 상한가로 검사


def test_mark_sent_moves_stage_and_closes_task(conn, ps, fake_llm):
    mid = hub.rfq(ps["id"], conn=conn)
    hub.mark(mid, "sent", conn=conn)
    assert conn.execute("SELECT stage FROM products WHERE id = ?", (ps["product_id"],)).fetchone()[0] == "negotiating"
    ev = conn.execute("SELECT * FROM product_events ORDER BY id DESC").fetchone()
    assert ev["run_id"] is None and "발송" in ev["note"]
    assert conn.execute("SELECT status FROM product_suppliers WHERE id = ?", (ps["id"],)).fetchone()[0] == "contacted"
    assert conn.execute("SELECT status FROM tasks WHERE ref_id = ?", (mid,)).fetchone()[0] == "done"
    assert conn.execute("SELECT sent_at FROM messages WHERE id = ?", (mid,)).fetchone()[0]
    with pytest.raises(ValueError, match="이미 'sent'"):
        hub.mark(mid, "discarded", conn=conn)


REPLY = {"summary_ko": "단가 3.2달러, MOQ 300", "translation_ko": "번역", "language": "en", "unit_price": 3.2,
         "currency": "USD", "min_qty": 300, "price_tiers": [{"qty": 1000, "price": 2.9}], "incoterm": "fob",
         "lead_time_days": 20, "payment_terms": "T/T 30/70", "sample_cost": 30, "sample_shipping": None,
         "valid_until": None, "open_questions_ko": ["로고 인쇄 비용"]}


def test_reply_analysis_saves_quote_and_counter(conn, ps, fake_llm):
    sent = hub.rfq(ps["id"], conn=conn)
    hub.mark(sent, "sent", conn=conn)
    fake_llm["reply"] = REPLY
    mid = hub.add_reply(ps["id"], "Price is $3.2 FOB, MOQ 300 ...", conn=conn)
    m = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    assert (m["direction"], m["status"], m["reply_to_id"]) == ("inbound", "received", sent)
    assert json.loads(m["analysis_json"])["summary_ko"] == "단가 3.2달러, MOQ 300"
    q = conn.execute("SELECT * FROM quotes WHERE message_id = ?", (mid,)).fetchone()
    assert (q["unit_price_usd"], q["min_qty"], q["incoterm"], q["lead_time_days"]) == (3.2, 300, "FOB", 20)
    assert json.loads(q["price_tiers_json"]) == [{"qty": 1000, "usd": 2.9}]
    assert conn.execute("SELECT status FROM product_suppliers WHERE id = ?", (ps["id"],)).fetchone()[0] == "quoted"
    assert conn.execute("SELECT kind FROM tasks WHERE ref_table = 'quotes'").fetchone()[0] == "review_quote"

    private.set_(ps["product_id"], target=2.7, max_price=3.0, payment="T/T 30/70")
    fake_llm["reply"] = None
    cid = hub.counter(q["id"], "firm", conn=conn)
    system, user = fake_llm["calls"][-1]
    assert user["our_counter_price_usd"] == 2.7 and "3.0" not in json.dumps(user) and "Firm" in system
    c = conn.execute("SELECT * FROM messages WHERE id = ?", (cid,)).fetchone()
    assert (c["kind"], c["reply_to_id"], json.loads(c["analysis_json"])["tone"]) == ("negotiation", mid, "firm")


def test_non_usd_reply_becomes_task_not_quote(conn, ps, fake_llm):
    fake_llm["reply"] = {**REPLY, "currency": "CNY", "unit_price": 22}
    hub.add_reply(ps["id"], "单价 22 元", conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    assert "CNY" in conn.execute("SELECT title FROM tasks WHERE kind = 'review_quote'").fetchone()[0]


def test_failed_analysis_is_recorded_not_retried(conn, ps, fake_llm):
    fake_llm["reply"] = RuntimeError("API down")
    with pytest.raises(RuntimeError, match="분석 실패"):
        hub.add_reply(ps["id"], "hello", conn=conn)
    m = conn.execute("SELECT * FROM messages WHERE direction = 'inbound'").fetchone()
    assert m and "API down" in json.loads(m["analysis_json"])["error"]       # 답장은 남고 실패 표시
    assert conn.execute(hub.PENDING_REPLY_SQL).fetchall() == []               # auto가 다시 돌리지 않음


def _age(conn, mid, days):
    with conn:
        conn.execute("UPDATE messages SET sent_at = strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime', ?) WHERE id = ?",
                     (f"-{days} days", mid))


def test_followup_due_rules(conn, ps, fake_llm):
    sent = hub.rfq(ps["id"], conn=conn)
    hub.mark(sent, "sent", conn=conn)
    assert hub.due_followups(conn) == []                       # 방금 보냄
    _age(conn, sent, 4)
    assert [r["id"] for r in hub.due_followups(conn)] == [sent]
    out = hub.auto(conn=conn)
    f1 = out["followups"][0]
    assert conn.execute("SELECT reply_to_id, kind FROM messages WHERE id = ?", (f1,)).fetchone()[:] == (sent, "follow_up")
    assert hub.due_followups(conn) == []                       # 후속 초안 대기 중
    hub.mark(f1, "sent", conn=conn)
    _age(conn, f1, 4)
    assert [r["id"] for r in hub.due_followups(conn)] == [sent]   # 두 번째 후속
    assert fake_llm["calls"] and hub.auto(conn=conn)
    assert fake_llm["calls"][-1][1]["follow_up_count"] == 1
    f2 = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    hub.mark(f2, "sent", conn=conn)
    _age(conn, f2, 10)
    assert hub.due_followups(conn) == []                       # 최대 2회

    # 답장이 오면 대상 아님 / 후속 초안을 버리면 대상 아님
    sid = db.upsert_supplier(conn, "XYZ", "China")
    with conn:
        link = db.link_supplier(conn, ps["product_id"], sid)
    m2 = hub.rfq(link, conn=conn)
    hub.mark(m2, "sent", conn=conn)
    _age(conn, m2, 5)
    assert [r["id"] for r in hub.due_followups(conn)] == [m2]
    fake_llm["reply"] = {"summary_ko": "s"}
    hub.add_reply(link, "thanks, will reply", conn=conn)
    assert hub.due_followups(conn) == []


def test_supervisor_when_sql_matches_hub():
    """supervisor.json 의 when_sql 이 hub 조건(3일·2회)과 같은 결과를 내는지."""
    cfg = json.loads((cli.Path(__file__).resolve().parents[2] / "trade_hub" / "supervisor.json").read_text(encoding="utf-8"))
    neg = cfg["agents"]["negotiation"]
    assert neg["enabled"] and neg["command"][-1] == "auto" and neg["cwd"] == "../negotiation_agent"
    assert "analysis_json IS NULL" in neg["when_sql"] and "follow_up" in neg["when_sql"]


def test_when_sql_counts(conn, ps, fake_llm):
    cfg = json.loads((cli.Path(__file__).resolve().parents[2] / "trade_hub" / "supervisor.json").read_text(encoding="utf-8"))
    when = cfg["agents"]["negotiation"]["when_sql"]
    count = lambda: conn.execute(when).fetchone()[0]           # noqa: E731
    assert count() == 0
    sent = hub.rfq(ps["id"], conn=conn)
    hub.mark(sent, "sent", conn=conn)
    _age(conn, sent, 4)
    assert count() == len(hub.due_followups(conn)) == 1
    with conn:
        conn.execute("INSERT INTO messages (product_supplier_id, direction, body, status, sent_at) "
                     "VALUES (?, 'inbound', 'hi', 'received', strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))", (ps["id"],))
    assert count() == 1        # 답장 왔으니 후속 0 + 분석 대기 1


def test_cli_reply_file_and_mark(conn, ps, fake_llm, tmp_path, capsys):
    f = tmp_path / "r.txt"
    f.write_text("Price $3.2", encoding="utf-8")
    fake_llm["reply"] = REPLY
    assert cli.main(["reply", str(ps["id"]), "--file", str(f), "--delete-file"]) == 0
    assert not f.exists() and f"RESULT_ANCHOR=ps-{ps['id']}" in capsys.readouterr().out
    assert cli.main(["mark", "999", "sent"]) == 1
    assert cli.main(["status"]) == 0
