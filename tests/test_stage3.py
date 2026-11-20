"""3번 사양 질문·답 연동, 샘플 요청, 공급처 선정/제외, 상한가 검사 오탐·재시도, 역제안 상태 검사."""
import json

import pytest

from negotiation_agent import __main__ as cli
from negotiation_agent import hub, llm, private
from trade_hub import db

QUESTIONS = [{"flag": "chemical_product", "question_ko": "별도 접착제가 동봉되나요?"},
             {"flag": "food_contact", "question_ko": "트레이·양념통이 함께 오나요?"},
             {"flag": "origin_country", "question_ko": "생산국은 어디인가요?"},
             {"flag": "children_target", "question_ko": "어린이용인가요?"}]     # 사실 항목이 아님 → 무시


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB", str(tmp_path / "trade.db"))
    monkeypatch.setenv("COMM_PRIVATE_DB", str(tmp_path / "private.db"))
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def fake_llm(monkeypatch):
    state = {"calls": [], "replies": []}

    def complete_json(system, user, max_tokens=3000):
        state["calls"].append((system, json.loads(user)))
        if state["replies"]:
            r = state["replies"].pop(0)
            if isinstance(r, Exception):
                raise r
            return dict(r)
        return {"subject": "Inquiry: wall shelf", "body": "Dear Lily, a few questions.", "korean_translation": "질문"}

    monkeypatch.setattr(llm, "complete_json", complete_json)
    return state


@pytest.fixture
def ps(conn):
    with conn:
        pid = db.upsert_product(conn, "벽선반", stage="sourcing", search_keyword_en="wall shelf")
        sid = db.upsert_supplier(conn, "ABC Co", "China", contact_name="Lily", contact_email="lily@abc.cn")
        link = db.link_supplier(conn, pid, sid, is_top_pick=1, first_questions_json=["Is it 3M adhesive?"])
        conn.execute("INSERT INTO compliance_checks (product_id, verdict, supplier_questions_json, checked_on) "
                     "VALUES (?, 'conditional', ?, '2026-11-19')", (pid, json.dumps(QUESTIONS, ensure_ascii=False)))
    return conn.execute(hub.PS_SQL, (link,)).fetchone()


def _ps(conn, ps_id):
    return conn.execute(hub.PS_SQL, (ps_id,)).fetchone()


def _sent_rfq(conn, ps):
    mid = hub.rfq(ps["id"], conn=conn)
    hub.mark(mid, "sent", conn=conn)
    return mid


def test_rfq_asks_compliance_questions_except_answered(conn, ps, fake_llm):
    with conn:
        db.merge_supplier_specs(conn, ps["id"], {"food_contact": False}, [{"claim": "트레이 없음", "message_id": None}])
    mid = hub.rfq(ps["id"], conn=conn)
    system, user = fake_llm["calls"][-1]
    assert user["product_spec_questions_ko"] == ["별도 접착제가 동봉되나요?", "생산국은 어디인가요?"]
    assert "Translate each into English" in system and user["questions_to_ask"] == ["Is it 3M adhesive?"]
    m = conn.execute("SELECT analysis_json FROM messages WHERE id = ?", (mid,)).fetchone()
    assert json.loads(m[0])["spec_flags_asked"] == ["chemical_product", "origin_country"]


def test_no_compliance_check_means_no_questions(conn, fake_llm):
    with conn:
        pid = db.upsert_product(conn, "컵", stage="sourcing")
        link = db.link_supplier(conn, pid, db.upsert_supplier(conn, "Cup Co", "China"))
    hub.rfq(link, conn=conn)
    assert fake_llm["calls"][-1][1]["product_spec_questions_ko"] == []


def test_reply_records_only_clear_answers(conn, ps, fake_llm):
    _sent_rfq(conn, ps)
    fake_llm["replies"] = [{"summary_ko": "접착제 없음, 중국산", "spec_answers": [
        {"flag": "chemical_product", "value": False, "claim_ko": "별도 접착제 없음, 3M 양면테이프 부착"},
        {"flag": "origin_country", "value": "cn", "claim_ko": "중국 광둥 공장 생산"},
        {"flag": "food_contact", "value": "maybe", "claim_ko": "애매"},          # 형식 틀림 → 버림
        {"flag": "children_target", "value": False, "claim_ko": "-"},           # 물어본 항목 아님 → 버림
        {"flag": "wireless", "value": False, "claim_ko": "-"}]}]               # 물어본 항목 아님 → 버림
    rid = hub.add_reply(ps["id"], "No glue included, 3M tape attached. Made in Guangdong, China.", conn=conn)
    _, user = fake_llm["calls"][-1]
    assert {q["flag"] for q in user["spec_questions"]} == {"chemical_product", "food_contact", "origin_country"}
    specs = json.loads(_ps(conn, ps["id"])["supplier_specs_json"])
    assert specs["flags"] == {"chemical_product": False, "origin_country": "CN"}
    assert {"claim": "chemical_product: 별도 접착제 없음, 3M 양면테이프 부착", "message_id": rid} in specs["evidence"]
    assert specs["updated_at"]
    # 답한 항목은 다음 초안에서 다시 묻지 않음
    hub.followups([1], conn=conn)
    assert fake_llm["calls"][-1][1]["product_spec_questions_ko"] == ["트레이·양념통이 함께 오나요?"]


def test_reply_without_spec_answers_leaves_specs_alone(conn, ps, fake_llm):
    fake_llm["replies"] = [{"summary_ko": "가격만", "spec_answers": []}]
    hub.add_reply(ps["id"], "price later", conn=conn)
    assert _ps(conn, ps["id"])["supplier_specs_json"] is None


def test_leak_check_ignores_quantities_but_blocks_prices(conn, ps):
    private.set_(ps["product_id"], max_price=50)
    pid = ps["product_id"]
    for ok in ("MOQ 50 pcs", "within 50 days", "50% deposit", "50cm wide", "1. a\n50. b", "50个"):
        assert not private.leaks_max_price(ok, pid), ok
    for bad in ("$50", "USD 50.00", "50 USD", "price 50", "50 元", "50"):
        assert private.leaks_max_price(bad, pid), bad


def test_followup_blocked_twice_becomes_task_and_stops_auto(conn, ps, fake_llm):
    private.set_(ps["product_id"], max_price=4.5)
    sent = _sent_rfq(conn, ps)
    with conn:
        conn.execute("UPDATE messages SET sent_at = datetime('now', 'localtime', '-5 days') WHERE id = ?", (sent,))
    leak = {"subject": "Following up", "body": "Could you do 4.5?", "korean_translation": "-"}
    fake_llm["replies"] = [leak, leak]
    with pytest.raises(RuntimeError):
        hub.auto(conn=conn)
    assert "Do not write any numbers" in fake_llm["calls"][-1][0]           # 두 번째는 숫자 없이
    t = conn.execute("SELECT * FROM tasks WHERE kind = 'other'").fetchone()
    assert hub.BLOCKED_TITLE in t["title"] and t["ref_id"] == sent
    assert hub.due_followups(conn) == []                                     # 자동으로 다시 안 함
    n = len(fake_llm["calls"])
    hub.auto(conn=conn)
    assert len(fake_llm["calls"]) == n
    assert hub.followups([sent], conn=conn)                                  # 사람이 버튼으로는 만들 수 있음


def test_followup_retry_without_numbers_succeeds(conn, ps, fake_llm):
    private.set_(ps["product_id"], max_price=4.5)
    sent = _sent_rfq(conn, ps)
    fake_llm["replies"] = [{"subject": "F", "body": "Maybe 4.5?", "korean_translation": "-"}]
    assert len(hub.followups([sent], conn=conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'other'").fetchone()[0] == 0


def test_counter_refuses_rejected_supplier_or_closed_product(conn, ps, fake_llm):
    fake_llm["replies"] = [{"summary_ko": "s", "unit_price": 3.2, "currency": "USD"}]
    hub.add_reply(ps["id"], "$3.2", conn=conn)
    qid = conn.execute("SELECT id FROM quotes").fetchone()[0]
    hub.supplier(ps["id"], "reject", "너무 비쌈", conn=conn)
    with pytest.raises(ValueError, match="제외한 공급처"):
        hub.counter(qid, conn=conn)
    hub.supplier(ps["id"], "restore", conn=conn)
    with conn:
        conn.execute("UPDATE products SET stage = 'dropped' WHERE id = ?", (ps["product_id"],))
    with pytest.raises(ValueError, match="dropped"):
        hub.counter(qid, conn=conn)


def test_supplier_select_reject_restore(conn, ps, fake_llm):
    mid = hub.rfq(ps["id"], conn=conn)
    assert hub.supplier(ps["id"], "reject", "  인증 서류 없음  ", conn=conn) == "rejected"
    row = _ps(conn, ps["id"])
    assert (row["status"], row["reject_reason"]) == ("rejected", "인증 서류 없음")
    assert conn.execute("SELECT status FROM tasks WHERE ref_id = ?", (mid,)).fetchone()[0] == "dismissed"
    with pytest.raises(ValueError, match="제외한 공급처"):
        hub.sample(ps["id"], conn=conn)
    assert hub.supplier(ps["id"], "restore", conn=conn) == "candidate"
    assert _ps(conn, ps["id"])["reject_reason"] is None
    assert hub.supplier(ps["id"], "select", conn=conn) == "selected"
    fake_llm["replies"] = [{"summary_ko": "s", "unit_price": 3.2, "currency": "USD"}]
    hub.add_reply(ps["id"], "$3.2", conn=conn)
    assert _ps(conn, ps["id"])["status"] == "selected"                      # 선정은 견적으로 뒤로 가지 않음
    assert hub.supplier(ps["id"], "restore", conn=conn) == "quoted"


def test_sample_request_and_mark_sent_moves_to_sampling(conn, ps, fake_llm):
    fake_llm["replies"] = [{"summary_ko": "s", "unit_price": 3.2, "currency": "USD", "sample_cost": 20}]
    hub.add_reply(ps["id"], "$3.2, sample $20", conn=conn)
    sid = hub.sample(ps["id"], "2 pcs", note="빨간색", conn=conn)
    system, user = fake_llm["calls"][-1]
    assert "sample request" in system and user["sample_quantity"] == "2 pcs"
    assert user["their_quote"]["sample_cost_usd"] == 20 and user["product_spec_questions_ko"]
    m = conn.execute("SELECT * FROM messages WHERE id = ?", (sid,)).fetchone()
    assert (m["kind"], m["status"]) == ("sample", "draft")
    with pytest.raises(ValueError, match="샘플 요청 초안"):
        hub.sample(ps["id"], conn=conn)
    hub.mark(sid, "sent", conn=conn)
    assert _ps(conn, ps["id"])["status"] == "sampling"


def test_cli_sample_and_supplier(conn, ps, fake_llm, capsys):
    assert cli.main(["supplier", str(ps["id"]), "reject", "--reason", "응답 없음"]) == 0
    assert cli.main(["sample", str(ps["id"])]) == 1                          # 제외한 공급처
    assert "제외한 공급처" in capsys.readouterr().err
    assert cli.main(["supplier", str(ps["id"]), "restore"]) == 0
    assert cli.main(["sample", str(ps["id"]), "--qty", "3 pcs"]) == 0
    assert "RESULT_ANCHOR=ps-" in capsys.readouterr().out
