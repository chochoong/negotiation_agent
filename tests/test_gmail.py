"""Gmail 연동 테스트 — 가짜 Gmail 서비스 (실제 계정·API 호출 없음)."""
import base64
import email
import re
from datetime import datetime, timedelta
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

from negotiation_agent import gmail, hub, llm, private
from trade_hub import db

ME = "me@example.com"
SUPPLIER = "lily@abc.cn"


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGmail:
    """users().drafts()/threads()/messages()/getProfile() 만 흉내. send 계열은 일부러 없음."""

    def __init__(self):
        self.dr, self.msgs, self.n = {}, {}, 0

    def _id(self, p):
        self.n += 1
        return f"{p}{self.n}"

    def add_msg(self, thread_id, frm, body, labels, when=None, subject="Re: Inquiry", attach=None):
        mid = self._id("m")
        parts = [{"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}]
        if attach:
            parts.append({"mimeType": "application/pdf", "filename": attach, "body": {"attachmentId": "a1"}})
        self.msgs[mid] = {"id": mid, "threadId": thread_id or mid, "labelIds": labels,
                          "internalDate": str(int((when or datetime.now()).timestamp() * 1000)),
                          "payload": {"mimeType": "multipart/mixed", "parts": parts,
                                      "headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject},
                                                  {"name": "Message-ID", "value": f"<{mid}@mail>"}]}}
        return mid

    def send_from_gmail(self, draft_id, when=None):
        """사람이 Gmail 화면에서 [보내기]를 누른 상황."""
        d = self.dr.pop(draft_id)
        m = self.msgs[d["message"]["id"]]
        m["labelIds"] = ["SENT"]
        m["internalDate"] = str(int((when or datetime.now()).timestamp() * 1000))
        return m["id"]

    # --- API 흉내 ---
    def users(self):
        return self

    def getProfile(self, userId):
        return _Call(lambda: {"emailAddress": ME})

    def drafts(self):
        svc = self

        class D:
            def create(self, userId, body):
                def run():
                    raw = email.message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]))
                    svc.last_raw = raw
                    tid = body["message"].get("threadId")
                    mid = svc.add_msg(tid, ME, raw.get_payload(decode=True).decode(), ["DRAFT"],
                                      subject=raw["Subject"])
                    did = svc._id("d")
                    svc.dr[did] = {"id": did, "message": {"id": mid, "threadId": svc.msgs[mid]["threadId"]}}
                    return svc.dr[did]
                return _Call(run)

            def get(self, userId, id, format=None):
                def run():
                    if id not in svc.dr:
                        raise HttpError(httplib2.Response({"status": 404}), b"not found")
                    return svc.dr[id]
                return _Call(run)

            def list(self, userId, maxResults=None, pageToken=None):
                return _Call(lambda: {"drafts": [{"id": k} for k in svc.dr]})
        return D()

    def threads(self):
        svc = self

        class T:
            def get(self, userId, id, format=None, metadataHeaders=None):
                return _Call(lambda: {"id": id, "messages": sorted(
                    [m for m in svc.msgs.values() if m["threadId"] == id], key=lambda m: int(m["internalDate"]))})
        return T()

    def messages(self):
        svc = self

        class M:
            def get(self, userId, id, format=None):
                return _Call(lambda: svc.msgs[id])

            def list(self, userId, q, maxResults=None):
                frm = re.search(r"from:(\S+)", q)[1]
                return _Call(lambda: {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in svc.msgs.values()
                                                   if frm in m["payload"]["headers"][0]["value"]]})
        return M()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB", str(tmp_path / "trade.db"))
    monkeypatch.setenv("COMM_PRIVATE_DB", str(tmp_path / "private.db"))
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def svc(monkeypatch):
    fake = FakeGmail()
    monkeypatch.setattr(gmail, "service", lambda: fake)
    return fake


@pytest.fixture
def fake_llm(monkeypatch):
    state = {"reply": None}

    def complete_json(system, user, max_tokens=3000):
        return dict(state["reply"] or {"subject": "Inquiry: yoga mat", "body": "Dear Lily, could you quote?",
                                       "korean_translation": "견적 주세요"})
    monkeypatch.setattr(llm, "complete_json", complete_json)
    return state


def _ps(conn, email_addr=SUPPLIER, company="ABC Co"):
    with conn:
        pid = db.upsert_product(conn, "요가매트", stage="sourcing", search_keyword_en="yoga mat")
        sid = db.upsert_supplier(conn, company, "China", contact_name="Lily", contact_email=email_addr)
        return db.link_supplier(conn, pid, sid, is_top_pick=1)


def _msg(conn, mid):
    return conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()


def test_source_never_calls_send():
    """절대 원칙 1: Gmail 모듈에 발송 호출이 없어야 한다."""
    src = (Path(gmail.__file__)).read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert not re.search(r"\.send\s*\(", code)


def test_push_draft_then_detect_sent_and_import_reply(conn, svc, fake_llm):
    link = _ps(conn)
    mid = hub.rfq(link, conn=conn)
    did = gmail.push_draft(mid, conn=conn)
    m = _msg(conn, mid)
    assert m["external_draft_id"] == did and m["external_thread_id"] and m["status"] == "draft"
    assert svc.last_raw["To"] == SUPPLIER and svc.last_raw["Subject"] == "Inquiry: yoga mat"
    with pytest.raises(ValueError, match="이미 Gmail"):        # 두 번 넣지 않음
        gmail.push_draft(mid, conn=conn)

    assert gmail.sync(conn)["sent"] == []                      # 아직 안 보냄
    gid = svc.send_from_gmail(did)                              # 사람이 Gmail에서 보냄
    out = gmail.sync(conn)
    m = _msg(conn, mid)
    assert out["sent"] == [mid] and m["status"] == "sent" and m["external_id"] == gid
    assert conn.execute("SELECT stage FROM products").fetchone()[0] == "negotiating"
    ev = conn.execute("SELECT note FROM product_events ORDER BY id DESC").fetchone()[0]
    assert "Gmail에서 발송 확인" in ev

    # 공급처가 같은 대화로 답장 (인용·첨부 포함)
    svc.add_msg(m["external_thread_id"], f"Lily <{SUPPLIER}>",
                "Price is $3.2 FOB.\n\nOn Mon, me wrote:\n> old text", ["INBOX", "UNREAD"],
                when=datetime.now() + timedelta(minutes=5), attach="pricelist.pdf")
    fake_llm["reply"] = {"summary_ko": "3.2달러", "unit_price": 3.2, "currency": "USD"}
    out = gmail.sync_and_analyze(conn)
    r = _msg(conn, out["replies"][0])
    assert (r["direction"], r["channel"], r["reply_to_id"]) == ("inbound", "email", mid)
    assert "Price is $3.2" in r["body"] and "old text" not in r["body"] and "pricelist.pdf" in r["body"]
    assert conn.execute("SELECT unit_price_usd FROM quotes").fetchone()[0] == 3.2
    assert gmail.sync(conn)["replies"] == []                    # 중복으로 가져오지 않음
    run = conn.execute("SELECT * FROM agent_runs ORDER BY id DESC").fetchone()
    assert (run["agent"], run["status"]) == ("negotiation", "succeeded")


def test_followup_draft_goes_into_same_thread(conn, svc, fake_llm):
    link = _ps(conn)
    mid = hub.rfq(link, conn=conn)
    did = gmail.push_draft(mid, conn=conn)
    svc.send_from_gmail(did)
    gmail.sync(conn)
    fake_llm["reply"] = {"subject": "Following up", "body": "Any update?", "korean_translation": "-"}
    fid = hub.followups([mid], conn=conn)[0]
    gmail.push_draft(fid, conn=conn)
    assert _msg(conn, fid)["external_thread_id"] == _msg(conn, mid)["external_thread_id"]
    assert svc.last_raw["Subject"] == "Re: Inquiry: yoga mat" and svc.last_raw["In-Reply-To"]


def test_deleted_draft_is_unflagged_not_marked_sent(conn, svc, fake_llm):
    mid = hub.rfq(_ps(conn), conn=conn)
    did = gmail.push_draft(mid, conn=conn)
    svc.dr.pop(did)                                         # Gmail에서 보내지 않고 삭제
    gmail.sync(conn)
    m = _msg(conn, mid)
    assert m["status"] == "draft" and m["external_draft_id"] is None
    gmail.push_draft(mid, conn=conn)                            # 다시 넣을 수 있음


def test_reply_from_supplier_address_in_new_thread(conn, svc, fake_llm):
    link = _ps(conn)
    mid = hub.rfq(link, conn=conn)
    hub.mark(mid, "sent", conn=conn)                             # Gmail 밖(직접)에서 보낸 경우
    svc.add_msg(None, SUPPLIER, "Hello, our catalog attached", ["INBOX"], when=datetime.now() + timedelta(minutes=1))
    svc.add_msg(None, "spam@else.com", "unrelated", ["INBOX"])
    out = gmail.sync(conn)
    assert len(out["replies"]) == 1 and _msg(conn, out["replies"][0])["product_supplier_id"] == link
    # 우리가 연락하지 않은 공급처의 메일은 가져오지 않음
    _ps(conn, "other@xyz.cn", "XYZ")
    svc.add_msg(None, "other@xyz.cn", "hi", ["INBOX"])
    assert gmail.sync(conn)["replies"] == []


def test_push_draft_blocks_max_price(conn, svc, fake_llm):
    link = _ps(conn)
    mid = hub.rfq(link, conn=conn)
    pid = conn.execute("SELECT product_id FROM product_suppliers WHERE id = ?", (link,)).fetchone()[0]
    with conn:
        conn.execute("UPDATE messages SET body = 'we can pay 4.5' WHERE id = ?", (mid,))
    private.set_(pid, max_price=4.5)
    with pytest.raises(hub.DraftRejected):
        gmail.push_draft(mid, conn=conn)
    assert svc.dr == {}


def test_auto_runs_gmail_sync_when_logged_in(conn, svc, fake_llm, tmp_path, monkeypatch):
    token = tmp_path / "tok.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GMAIL_TOKEN", str(token))
    out = hub.auto(conn=conn)
    assert out["gmail"] == {"sent": [], "replies": []}
    assert conn.execute("SELECT COUNT(*) FROM agent_runs WHERE agent = 'negotiation'").fetchone()[0] == 1


def test_not_logged_in_message(conn):
    with pytest.raises(RuntimeError, match="gmail-login"):
        gmail.service()
