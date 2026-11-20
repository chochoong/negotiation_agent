"""Gmail 연동 — 초안을 Gmail 임시보관함에 넣고, 사람이 Gmail에서 보낸 것과 공급처 답장을 가져온다.

절대 원칙: 이 모듈은 메일을 보내지 않는다. Gmail API의 send 계열(drafts.send, messages.send)을 부르지 않는다.
  (tests/test_gmail.py 가 이 파일에 send 호출이 없는지 검사한다.) 발송 버튼은 사람이 Gmail 화면에서 직접 누른다.

권한: gmail.compose(임시보관함 초안 만들기) + gmail.readonly(보낸 메일·받은 답장 읽기).
  gmail.compose 는 구글 권한상 발송도 가능한 범위라서, 코드에서 send 를 쓰지 않는 것으로 막는다.

파일 (둘 다 git 제외):
  secrets/gmail_credentials.json — Google Cloud에서 받은 OAuth 클라이언트(데스크톱 앱) 파일
  data/gmail_token.json          — `python -m negotiation_agent gmail-login` 으로 만들어지는 로그인 토큰
  테스트에서는 환경변수 GMAIL_CREDENTIALS, GMAIL_TOKEN 으로 바꿀 수 있다.

공용 DB (trade_hub v9) messages 칸:
  external_draft_id  — 임시보관함 초안 id (넣었음 표시, 발송 확인에 사용)
  external_thread_id — Gmail 대화 id (답장이 어느 공급처 줄인지 판단)
  external_id        — Gmail 메일 id (보낸 메일·받은 답장, 중복 저장 방지)
"""
import base64
import html
import os
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from trade_hub import db

from . import hub, private

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]
MAX_BODY = 20_000            # 대시보드 붙여넣기 한도와 같게
SEARCH_LIMIT = 50            # 공급처 메일 주소당 한 번에 확인할 최근 메일 수
LOGIN_HINT = "python -m negotiation_agent gmail-login"


def credentials_path() -> Path:
    return Path(os.environ.get("GMAIL_CREDENTIALS") or ROOT / "secrets" / "gmail_credentials.json")


def token_path() -> Path:
    return Path(os.environ.get("GMAIL_TOKEN") or ROOT / "data" / "gmail_token.json")


def configured() -> bool:
    """로그인 토큰이 있으면 Gmail 연동을 쓴다 (없으면 auto 는 Gmail 단계를 건너뜀)."""
    return token_path().exists()


# ---------------------------------------------------------------------------
# 로그인·서비스
# ---------------------------------------------------------------------------
def login():
    """브라우저로 구글 로그인 → 토큰 저장. 사람이 터미널에서 한 번 실행한다."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    cred = credentials_path()
    if not cred.exists():
        raise RuntimeError(f"OAuth 클라이언트 파일이 없습니다: {cred}\n"
                           "  Google Cloud 콘솔 → API 및 서비스 → 사용자 인증 정보 → OAuth 클라이언트 ID(데스크톱 앱) → JSON 다운로드")
    flow = InstalledAppFlow.from_client_secrets_file(str(cred), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    me = _build(creds).users().getProfile(userId="me").execute().get("emailAddress")
    hub.log(f"Gmail 로그인 완료: {me}  (토큰: {token_path()})")
    return me


def _save_token(creds):
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(creds.to_json(), encoding="utf-8")


def _build(creds):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def service():
    """Gmail API 클라이언트. 테스트는 이 함수를 가짜로 바꾼다."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not configured():
        raise RuntimeError(f"Gmail에 로그인하지 않았습니다 → {LOGIN_HINT}")
    creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError(f"Gmail 로그인이 만료됐습니다 → {LOGIN_HINT}")
        try:
            creds.refresh(Request())
        except RefreshError as err:
            raise RuntimeError(f"Gmail 로그인이 만료됐습니다 (구글 앱이 '테스트' 상태면 7일마다 만료) → {LOGIN_HINT}") from err
        _save_token(creds)
    return _build(creds)


def _is_404(err):
    return getattr(getattr(err, "resp", None), "status", None) == 404


def _draft_exists(svc, draft_id):
    from googleapiclient.errors import HttpError
    try:
        svc.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
        return True
    except HttpError as err:
        if _is_404(err):
            return False
        raise


# ---------------------------------------------------------------------------
# 초안 → Gmail 임시보관함 (발송 X)
# ---------------------------------------------------------------------------
def _thread_info(svc, thread_id, me=""):
    """대화의 마지막 메일 Message-ID·제목 (답장 형식으로 이어 붙이기용) + 상대 주소 (받는 사람 기본값)."""
    t = svc.users().threads().get(userId="me", id=thread_id, format="metadata",
                                  metadataHeaders=["Message-ID", "Subject", "References", "From", "To"]).execute()
    msgs = [x for x in t.get("messages") or [] if "DRAFT" not in (x.get("labelIds") or [])]
    if not msgs:
        return None
    last = msgs[-1]
    h = _headers(last)
    peer = None                                # 상대가 마지막으로 보낸 주소, 없으면 우리가 마지막으로 보낸 받는 사람
    for x in reversed(msgs):
        hx = _headers(x)
        frm = parseaddr(hx.get("from", ""))[1].lower()
        if "SENT" not in (x.get("labelIds") or []) and frm and frm != me:
            peer = frm
            break
        if "SENT" in (x.get("labelIds") or []) and hx.get("to"):
            peer = hx["to"]
            break
    return {"message_id": h.get("message-id"), "references": h.get("references"),
            "subject": _headers(msgs[0]).get("subject"), "peer": peer}


EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
MAX_TO = 5


FIND_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def find_emails(*texts):
    """공급처 연락처 칸들(contact_email, contact_other)에서 메일 주소를 모두 뽑는다 (중복 제거, 소문자).
    'info@a.com / fiona@a.com' 처럼 여러 개가 한 칸에 있어도, 메모 속에 섞여 있어도 찾는다."""
    out = []
    for t in texts:
        for addr in FIND_EMAIL_RE.findall(t or ""):
            addr = addr.lower().rstrip(".")
            if addr not in out:
                out.append(addr)
    return out[:MAX_TO]


def supplier_emails(ps):
    keys = ps.keys()
    return find_emails(*(ps[k] for k in ("contact_email", "contact_other") if k in keys))


def parse_recipients(text):
    """'a@x.com, b@y.com' → ['a@x.com', 'b@y.com']. 형식이 틀리면 ValueError (대시보드에 한 줄로 표시)."""
    out = []
    for part in re.split(r"[,;]+", (text or "").strip()):
        part = part.strip()
        if not part:
            continue
        addr = parseaddr(part)[1] if "<" in part else part
        if not EMAIL_RE.match(addr):
            raise ValueError(f"메일 주소 형식이 아닙니다: {part}")
        if addr.lower() not in (x.lower() for x in out):
            out.append(addr)
    if len(out) > MAX_TO:
        raise ValueError(f"받는 사람은 {MAX_TO}명까지 넣을 수 있습니다.")
    return out


def _reply_subject(subject):
    s = (subject or "").strip()
    return s if re.match(r"(?i)^re\s*:", s) else f"Re: {s}"


def push_draft(message_id, conn=None, svc=None, to=None):
    """DB 초안(messages) 하나를 Gmail 임시보관함에 넣는다. 같은 공급처와의 Gmail 대화가 있으면 그 대화의 답장으로.

    받는 사람: to(직접 입력) → 공급처 연락처(contact_email·contact_other)에 있는 메일 주소 전부 → 같은 대화의 상대 주소.
      셋 다 없으면 비워 둠 (Gmail에서 입력).
    이미 임시보관함에 있는 초안에 to 를 주면 그 초안의 받는 사람만 바꾼다(drafts.update — 발송 아님).
    """
    conn = conn or db.connect()
    m = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if m is None or m["direction"] != "outbound":
        raise ValueError(f"보낼 메일 초안 #{message_id}이(가) 없습니다")
    if m["status"] not in ("draft", "approved"):
        raise ValueError(f"#{message_id}은(는) '{m['status']}' 상태라 임시보관함에 넣지 않습니다 (초안·검토 완료만)")
    typed = parse_recipients(to)
    ps = hub.get_ps(conn, m["product_supplier_id"])
    if any(private.leaks_max_price(t or "", ps["product_id"]) for t in (m["subject"], m["body"])):
        raise hub.DraftRejected("초안에 내부 상한가와 같은 숫자가 있어 Gmail에 넣지 않았습니다.")
    svc = svc or service()
    existing = m["external_draft_id"] if m["external_draft_id"] and _draft_exists(svc, m["external_draft_id"]) else None
    if existing and not typed:
        raise ValueError(f"#{message_id}은(는) 이미 Gmail 임시보관함에 있습니다. Gmail에서 확인 후 직접 보내세요. "
                         "(받는 사람을 바꾸려면 주소를 입력하세요)")

    if existing and m["external_thread_id"]:   # 이미 넣은 초안: 그 초안이 속한 대화 기준
        thread_id = m["external_thread_id"]
    else:
        prev = conn.execute("SELECT external_thread_id FROM messages WHERE product_supplier_id = ? AND id != ? "
                            "AND external_thread_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                            (ps["id"], message_id)).fetchone()
        thread_id = prev["external_thread_id"] if prev else None
    me = (svc.users().getProfile(userId="me").execute().get("emailAddress") or "").lower() if thread_id else ""
    info = _thread_info(svc, thread_id, me) if thread_id else None

    msg = EmailMessage()
    recipients = ", ".join(typed or supplier_emails(ps)) or (info or {}).get("peer") or ""
    if recipients:
        msg["To"] = recipients
    subject = m["subject"] or ""
    if info:                                   # 같은 대화로 묶이려면 제목·In-Reply-To·References 가 맞아야 함
        subject = _reply_subject(info["subject"] or subject)
        if info["message_id"]:
            msg["In-Reply-To"] = info["message_id"]
            msg["References"] = " ".join(x for x in (info["references"], info["message_id"]) if x)
    msg["Subject"] = subject
    msg.set_content(m["body"])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"message": {"raw": raw, **({"threadId": thread_id} if info else {})}}

    with hub._run(conn, {"action": "gmail_draft", "message_id": message_id, "update": bool(existing)}):
        if existing:                           # 받는 사람만 바꿈 — Gmail에서 고친 본문은 DB 원문으로 되돌아감
            d = svc.users().drafts().update(userId="me", id=existing, body={"id": existing, **body}).execute()
        else:
            d = svc.users().drafts().create(userId="me", body=body).execute()
        with conn:
            conn.execute("UPDATE messages SET external_draft_id = ?, external_thread_id = ? WHERE id = ?",
                         (d["id"], d["message"].get("threadId"), message_id))
    shown = recipients or "받는 사람 비어 있음 — Gmail에서 입력"
    hub.log(f"📨 Gmail 임시보관함에 {'받는 사람 변경' if existing else '넣음'}: #{message_id} → {shown} · {subject}"
            + (" (기존 대화에 답장)" if info else ""))
    hub.log("   발송은 Gmail에서 직접 하세요. 보내면 다음 동기화 때 '보냄'으로 자동 표시됩니다.")
    print(f"RESULT_ANCHOR=ps-{ps['id']}", flush=True)
    return d["id"]


# ---------------------------------------------------------------------------
# 동기화: 발송 확인 + 답장 가져오기
# ---------------------------------------------------------------------------
def _headers(msg):
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers") or []}


def _local(ms):
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%dT%H:%M:%S")


def _decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace") if data else ""


def _walk(part):
    yield part
    for p in part.get("parts") or []:
        yield from _walk(p)


QUOTE_CUT = re.compile(r"^(On .{5,200}wrote:|-{2,}\s*Original Message\s*-{2,}|-{3,}\s*原始邮件\s*-{3,}|"
                       r"发件人[:：].*|在.{2,100}写道[:：]|From:\s.+)\s*$", re.I | re.M)


def _strip_quote(text):
    """답장 아래 붙은 이전 메일 인용을 잘라낸다 (첫 줄부터 인용이면 그대로 둠)."""
    cut = QUOTE_CUT.search(text)
    if cut and text[:cut.start()].strip():
        text = text[:cut.start()]
    return "\n".join(line for line in text.splitlines() if not line.startswith(">")).strip()


def message_text(msg):
    """본문(텍스트 우선, 없으면 HTML을 글자로) + 첨부 파일 이름."""
    plain, rich, files = [], [], []
    for p in _walk(msg.get("payload") or {}):
        mime, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if p.get("filename"):
            files.append(p["filename"])
        elif mime == "text/plain" and data:
            plain.append(_decode(data))
        elif mime == "text/html" and data:
            t = re.sub(r"(?is)<(script|style).*?</\1>", "", _decode(data))
            t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
            rich.append(html.unescape(re.sub(r"<[^>]+>", "", t)))
    text = _strip_quote("\n".join(plain) if plain else "\n".join(rich))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if files:     # 가격표가 첨부로 오는 경우가 많다 — 분석에 안 들어가니 사람이 열어 보도록 표시
        text += "\n\n[첨부 파일 — Gmail에서 직접 확인: " + ", ".join(files) + "]"
    return text[:MAX_BODY] or "(본문 없음)"


def _known_ids(conn):
    return {r[0] for r in conn.execute("SELECT external_id FROM messages WHERE external_id IS NOT NULL")}


def _detect_sent(conn, svc):
    """Gmail 임시보관함에서 사라진 초안 → 그 대화에 '보낸 메일'이 있으면 '보냄'으로 표시, 없으면 넣음 표시만 지움."""
    rows = conn.execute("SELECT * FROM messages WHERE direction = 'outbound' AND status IN ('draft', 'approved') "
                        "AND external_draft_id IS NOT NULL ORDER BY id").fetchall()
    if not rows:
        return []
    live, token = set(), None
    while True:
        page = svc.users().drafts().list(userId="me", maxResults=500, pageToken=token).execute()
        live |= {d["id"] for d in page.get("drafts") or []}
        token = page.get("nextPageToken")
        if not token:
            break
    sent = []
    for m in rows:
        if m["external_draft_id"] in live:
            continue
        known = _known_ids(conn)
        since = datetime.fromisoformat(m["created_at"]) - timedelta(minutes=10)
        found = None
        if m["external_thread_id"]:
            t = svc.users().threads().get(userId="me", id=m["external_thread_id"], format="minimal").execute()
            cands = [x for x in t.get("messages") or [] if "SENT" in (x.get("labelIds") or [])
                     and "DRAFT" not in (x.get("labelIds") or []) and x["id"] not in known
                     and datetime.fromtimestamp(int(x["internalDate"]) / 1000) >= since]
            found = min(cands, key=lambda x: int(x["internalDate"])) if cands else None
        if found:
            hub.mark(m["id"], "sent", conn=conn, sent_at=_local(found["internalDate"]), external_id=found["id"],
                     how="Gmail에서 발송 확인")
            sent.append(m["id"])
        else:
            with conn:
                conn.execute("UPDATE messages SET external_draft_id = NULL WHERE id = ?", (m["id"],))
            hub.log(f"  · #{m['id']} Gmail 초안이 보내지 않고 삭제됨 → 임시보관함 표시만 지움 (초안은 그대로)")
    return sent


def _import(conn, svc, ps_id, msg_id, me):
    full = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    labels = full.get("labelIds") or []
    h = _headers(full)
    sender = parseaddr(h.get("from", ""))[1].lower()
    if "SENT" in labels or "DRAFT" in labels or (me and sender == me):
        return None
    return hub.add_reply(ps_id, message_text(full), analyze=False, conn=conn, subject=h.get("subject"),
                         sent_at=_local(full["internalDate"]), external_id=full["id"], thread_id=full.get("threadId"))


def _import_replies(conn, svc):
    me = (svc.users().getProfile(userId="me").execute().get("emailAddress") or "").lower()
    known = _known_ids(conn)
    new = []
    # 1) 우리가 아는 Gmail 대화(임시보관함에 넣었던 초안의 대화)에 온 메일
    threads = {}
    for r in conn.execute("SELECT external_thread_id, product_supplier_id FROM messages "
                          "WHERE external_thread_id IS NOT NULL ORDER BY id"):
        threads[r["external_thread_id"]] = r["product_supplier_id"]      # 같은 대화면 마지막 줄 기준
    for tid, ps_id in threads.items():
        t = svc.users().threads().get(userId="me", id=tid, format="minimal").execute()
        for x in t.get("messages") or []:
            labels = x.get("labelIds") or []
            if x["id"] in known or "SENT" in labels or "DRAFT" in labels:
                continue
            mid = _import(conn, svc, ps_id, x["id"], me)
            known.add(x["id"])
            if mid:
                new.append(mid)
    # 2) 공급처 메일 주소에서 온 메일 (다른 대화로 답장한 경우). 우리가 메일을 보낸 뒤에 온 것만.
    rows = conn.execute("""
        SELECT ps.id, s.contact_email, s.contact_other,
               MIN(m.sent_at) AS first_sent, MAX(m.sent_at) AS last_sent
        FROM product_suppliers ps JOIN suppliers s ON s.id = ps.supplier_id JOIN products p ON p.id = ps.product_id
        JOIN messages m ON m.product_supplier_id = ps.id AND m.direction = 'outbound' AND m.status = 'sent'
        WHERE (s.contact_email LIKE '%@%' OR s.contact_other LIKE '%@%') AND ps.status != 'rejected' AND p.stage NOT IN ('closed', 'dropped')
          AND m.sent_at IS NOT NULL
        GROUP BY ps.id""").fetchall()
    by_email = {}
    for r in rows:
        for addr in find_emails(r["contact_email"], r["contact_other"]):
            by_email.setdefault(addr, []).append(r)
    for email, links in by_email.items():
        after = int((datetime.fromisoformat(min(r["first_sent"] for r in links)) - timedelta(days=1)).timestamp())
        page = svc.users().messages().list(userId="me", q=f"from:{email} after:{after}", maxResults=SEARCH_LIMIT).execute()
        # 같은 주소가 여러 품목에 연결돼 있으면: 대화로 알 수 있으면 그 줄, 아니면 가장 최근에 메일을 보낸 줄
        default = max(links, key=lambda r: r["last_sent"])["id"]
        if len(links) > 1:
            hub.log(f"  · {email} 은(는) 공급처 연결 {len(links)}곳에 있음 → 대화로 모르면 최근에 보낸 연결 #{default}에 붙임")
        for x in sorted(page.get("messages") or [], key=lambda x: x["id"]):
            if x["id"] in known:
                continue
            mid = _import(conn, svc, threads.get(x.get("threadId"), default), x["id"], me)
            known.add(x["id"])
            if mid:
                new.append(mid)
    return new


def sync(conn=None, svc=None):
    """발송 확인 + 새 답장 가져오기 (분석은 하지 않음 — 호출한 쪽이 분석). 실행 기록은 호출한 쪽의 run 에 포함."""
    conn = conn or db.connect()
    svc = svc or service()
    sent = _detect_sent(conn, svc)
    new = _import_replies(conn, svc)
    hub.log(f"Gmail: 발송 확인 {len(sent)}건 · 새 답장 {len(new)}건")
    return {"sent": sent, "replies": new}


def sync_and_analyze(conn=None, svc=None):
    """대시보드 [Gmail 확인] / CLI gmail-sync: 동기화하고 새 답장은 바로 분석."""
    conn = conn or db.connect()
    with hub._run(conn, {"action": "gmail_sync"}) as run:
        out = sync(conn, svc)
        if out["replies"]:
            rows = conn.execute(f"SELECT * FROM messages WHERE id IN ({', '.join('?' * len(out['replies']))})",
                                out["replies"]).fetchall()
            out["analyzed"] = hub._analyze(conn, run, rows)
    return out
