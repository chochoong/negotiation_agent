"""6번 연결: 인코텀즈 지정장소 저장 / 계좌 변경 사기 경고 할 일."""
import json

import pytest

from negotiation_agent import hub, llm
from trade_hub import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB", str(tmp_path / "trade.db"))
    monkeypatch.setenv("COMM_PRIVATE_DB", str(tmp_path / "private.db"))
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def ps(conn):
    with conn:
        pid = db.upsert_product(conn, "키보드 케이스", stage="sourcing", search_keyword_en="EVA keyboard case")
        sid = db.upsert_supplier(conn, "Ao Ye Mei", "China", contact_email="m@aoyemei.cn")
        return db.link_supplier(conn, pid, sid)


@pytest.fixture
def reply(monkeypatch):
    box = {}
    monkeypatch.setattr(llm, "complete_json", lambda system, user, max_tokens=0: dict(box["out"]))
    return box


def _quote(conn):
    return conn.execute("SELECT * FROM quotes ORDER BY id DESC").fetchone()


def _bank_tasks(conn):
    return conn.execute("SELECT * FROM tasks WHERE kind = 'payment'").fetchall()


@pytest.mark.parametrize("place,inco,want", [("Ningbo", "FOB", "Ningbo"), ("FOB  Shenzhen port", "FOB", "Shenzhen port"),
                                             (None, "FOB", None), ("N/A", "EXW", None), ("Ningbo", None, "Ningbo")])
def test_incoterm_place_saved_as_stated(conn, ps, reply, place, inco, want):
    reply["out"] = {"summary_ko": "s", "unit_price": 2.5, "currency": "USD", "incoterm": inco, "incoterm_place": place,
                    "bank_change": {"detected": False}}
    hub.add_reply(ps, "USD 2.5 " + (inco or ""), conn=conn)
    q = _quote(conn)
    assert (q["incoterm"], q["incoterm_place"]) == (inco, want)


def test_reanalysis_updates_place(conn, ps, reply):
    reply["out"] = {"summary_ko": "s", "unit_price": 2.5, "currency": "USD", "incoterm": "FOB", "incoterm_place": None}
    mid = hub.add_reply(ps, "USD 2.5 FOB", conn=conn)
    reply["out"] = {**reply["out"], "incoterm_place": "Ningbo"}
    hub.analyze_replies([mid], conn=conn)
    assert [r["incoterm_place"] for r in conn.execute("SELECT incoterm_place FROM quotes")] == ["Ningbo"]


@pytest.mark.parametrize("text", [
    "Please note we have a new bank account, kindly remit the balance there.",
    "Our bank details have been changed.",
    "Beneficiary bank: Bank of China, SWIFT: BKCHCNBJ480, Account No: 6217 0000 1234 5678",
    "我们公司更换了银行账户，请付款到新账户。",
    "입금 계좌가 변경되었습니다."])
def test_bank_change_warns_even_without_analysis(conn, ps, text):
    mid = hub.add_reply(ps, text, analyze=False, conn=conn)
    t = _bank_tasks(conn)
    assert len(t) == 1 and t[0]["priority"] == 1 and (t[0]["ref_table"], t[0]["ref_id"]) == ("messages", mid)
    assert t[0]["title"] == f"{hub.BANK_TITLE}: Ao Ye Mei" and "송금 금지" in t[0]["detail"]
    assert "1234 5678" not in t[0]["detail"] and "BKCHCNBJ480" not in t[0]["detail"]     # 계좌 정보는 가림


def test_normal_reply_no_bank_task_and_llm_flag_adds_once(conn, ps, reply):
    reply["out"] = {"summary_ko": "s", "unit_price": 2.5, "currency": "USD", "bank_change": {"detected": False}}
    hub.add_reply(ps, "USD 2.5 FOB Ningbo, MOQ 500 pcs, payment 30% deposit by T/T.", conn=conn)
    assert _bank_tasks(conn) == []
    reply["out"] = {"summary_ko": "s", "bank_change": {"detected": True, "evidence": "please use the account below"}}
    mid = hub.add_reply(ps, "From now please use the account below.", conn=conn)        # 정규식은 놓쳤지만 LLM이 잡음
    hub.analyze_replies([mid], conn=conn)                                                  # 다시 분석해도 하나
    t = _bank_tasks(conn)
    assert len(t) == 1 and t[0]["ref_id"] == mid and "account below" in t[0]["detail"]


def test_prompt_asks_place_and_bank():
    from negotiation_agent import prompts
    assert "incoterm_place" in prompts.REPLY_SYSTEM and "bank_change" in prompts.REPLY_SYSTEM
