"""5번 커뮤니케이션·협상 에이전트 명령.

  python -m negotiation_agent status                              # 공급처별 메일·견적 현황, 처리 대기
  python -m negotiation_agent rfq <연결번호> [--qty "200 pcs"] [--incoterm FOB] [--lang en|zh] [--note ...]
  python -m negotiation_agent reply <연결번호> (--file 답장.txt | --text "...") [--no-analyze]
  python -m negotiation_agent analyze [메일번호 ...]               # 분석 안 된 받은 답장 (번호를 주면 다시 분석)
  python -m negotiation_agent counter <견적번호> --tone soft|normal|firm [--price 2.8] [--note ...] [--other ...]
  python -m negotiation_agent followup [메일번호 ...] [--days 3]   # 답 없는 보낸 메일에 후속 메일 초안
  python -m negotiation_agent auto [--days 3]                      # 슈퍼바이저용: 답장 분석 + 후속 메일
  python -m negotiation_agent mark <메일번호> approved|sent|discarded|draft   # 사람이 검토·발송·버림 표시
  python -m negotiation_agent sample <연결번호> [--qty "2 pcs"] [--note ...] [--lang en|zh]   # 샘플 요청 초안
  python -m negotiation_agent supplier <연결번호> select|reject|restore [--reason ...]   # 공급처 선정·제외·되돌리기
  python -m negotiation_agent target <품목번호> [--target 2.8] [--max 3.5] [--payment "T/T 30/70"]  # 내부 전용 기준가
  python -m negotiation_agent gmail-login                         # 구글 로그인 (한 번, 브라우저가 열림)
  python -m negotiation_agent gmail-draft <메일번호> [--to a@b.com]  # 초안을 Gmail 임시보관함에 넣기 (발송 X)
  python -m negotiation_agent gmail-sync                          # Gmail에서 발송 확인 + 새 답장 가져와 분석

<연결번호> = 공용 DB product_suppliers.id (대시보드 5번 페이지에 표시). 메일은 절대 보내지 않습니다.
공용 DB 위치: 환경변수 TRADE_DB, 없으면 ../trade_hub/data/trade.db
"""
import argparse
import sys
from pathlib import Path


def build_parser():
    ap = argparse.ArgumentParser(prog="negotiation_agent", description="5번 커뮤니케이션·협상 에이전트 (초안만 만듦)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="공급처별 메일·견적 현황")

    s = sub.add_parser("rfq", help="견적 요청(첫 문의) 초안")
    s.add_argument("ps_id", type=int)
    s.add_argument("--qty")
    s.add_argument("--incoterm")
    s.add_argument("--lang", default="en")
    s.add_argument("--note")
    s.add_argument("--force", action="store_true", help="이미 견적 요청 메일이 있어도 새로 만들기")

    s = sub.add_parser("reply", help="받은 답장 저장 + 분석")
    s.add_argument("ps_id", type=int)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--text")
    s.add_argument("--no-analyze", action="store_true")
    s.add_argument("--delete-file", action="store_true", help="저장한 뒤 --file 을 지움 (대시보드용)")

    s = sub.add_parser("analyze", help="분석 안 된 받은 답장 분석")
    s.add_argument("ids", type=int, nargs="*")

    s = sub.add_parser("counter", help="역제안 초안")
    s.add_argument("quote_id", type=int)
    s.add_argument("--tone", default="normal", choices=["soft", "normal", "firm"])
    s.add_argument("--price", type=float)
    s.add_argument("--note")
    s.add_argument("--other", help="타 업체 비교 언급 (사실만)")
    s.add_argument("--lang")

    s = sub.add_parser("followup", help="답 없는 메일에 후속 메일 초안")
    s.add_argument("ids", type=int, nargs="*")
    s.add_argument("--days", type=int)
    s.add_argument("--note")

    s = sub.add_parser("auto", help="슈퍼바이저용: 답장 분석 + 후속 메일")
    s.add_argument("--days", type=int)

    s = sub.add_parser("mark", help="초안 검토·발송·버림 표시 (사람)")
    s.add_argument("message_id", type=int)
    s.add_argument("status", choices=["approved", "sent", "discarded", "draft"])

    s = sub.add_parser("sample", help="샘플 요청 초안")
    s.add_argument("ps_id", type=int)
    s.add_argument("--qty")
    s.add_argument("--note")
    s.add_argument("--lang")

    s = sub.add_parser("supplier", help="공급처 선정·제외·되돌리기 (사람)")
    s.add_argument("ps_id", type=int)
    s.add_argument("action", choices=list(("select", "reject", "restore")))
    s.add_argument("--reason", help="제외 사유")

    s = sub.add_parser("target", help="내부 전용 목표가·상한가 (공용 DB에 저장하지 않음)")
    s.add_argument("product_id", type=int, nargs="?")
    s.add_argument("--target", type=float)
    s.add_argument("--max", type=float)
    s.add_argument("--payment")

    sub.add_parser("gmail-login", help="Gmail 로그인 (브라우저) — 토큰을 data/gmail_token.json 에 저장")
    s = sub.add_parser("gmail-draft", help="초안을 Gmail 임시보관함에 넣기 (발송하지 않음)")
    s.add_argument("message_id", type=int)
    s.add_argument("--to", help="받는 사람 (쉼표로 여러 명). 이미 넣은 초안이면 받는 사람만 바꿈")
    sub.add_parser("gmail-sync", help="Gmail에서 발송 확인 + 새 답장 가져와 분석")
    return ap


def status(conn):
    from . import hub
    rows = conn.execute("""
        SELECT ps.id, p.id AS pid, p.name, p.stage, s.company, ps.status, ps.is_top_pick,
               (SELECT COUNT(*) FROM messages m WHERE m.product_supplier_id = ps.id AND m.status IN ('draft','approved')) AS drafts,
               (SELECT COUNT(*) FROM messages m WHERE m.product_supplier_id = ps.id AND m.status = 'sent') AS sent,
               (SELECT COUNT(*) FROM messages m WHERE m.product_supplier_id = ps.id AND m.direction = 'inbound') AS inbound,
               (SELECT MIN(unit_price_usd) FROM quotes q WHERE q.product_supplier_id = ps.id) AS best
        FROM product_suppliers ps JOIN products p ON p.id = ps.product_id JOIN suppliers s ON s.id = ps.supplier_id
        WHERE p.stage NOT IN ('candidate', 'closed', 'dropped')
        ORDER BY p.id, ps.is_top_pick DESC, ps.id""").fetchall()
    if not rows:
        print("진행 중인 품목의 공급처가 없습니다 (2번이 공급처를 찾은 뒤에 사용).")
    pid = None
    for r in rows:
        if r["pid"] != pid:
            pid = r["pid"]
            print(f"\n[{r['pid']}] {r['name']} ({r['stage']})")
        best = f" · 최저 견적 ${r['best']:g}" if r["best"] else ""
        print(f"  연결 {r['id']:>3} {'★' if r['is_top_pick'] else ' '} {r['company'][:40]:<40} {r['status']:<9} "
              f"초안 {r['drafts']} · 보냄 {r['sent']} · 받음 {r['inbound']}{best}")
    pending = conn.execute(hub.PENDING_REPLY_SQL).fetchall()
    due = hub.due_followups(conn)
    print(f"\n분석 안 된 답장 {len(pending)}건 · 후속 메일 필요 {len(due)}건 → python -m negotiation_agent auto")


def main(argv=None):
    a = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from trade_hub import db   # 공용 DB 패키지 (pip install -e ../trade_hub)

    from . import gmail, hub, private
    if a.cmd == "gmail-login":        # 공용 DB 필요 없음
        try:
            gmail.login()
        except (ValueError, RuntimeError) as err:
            print(f"오류: {err}", file=sys.stderr, flush=True)
            return 1
        return 0
    conn = db.connect()
    try:
        if a.cmd == "gmail-draft":
            gmail.push_draft(a.message_id, conn=conn, to=a.to)
        elif a.cmd == "gmail-sync":
            gmail.sync_and_analyze(conn=conn)
        elif a.cmd == "status":
            status(conn)
        elif a.cmd == "rfq":
            hub.rfq(a.ps_id, a.qty, a.incoterm, a.lang, a.note, a.force, conn=conn)
        elif a.cmd == "reply":
            text = Path(a.file).read_text(encoding="utf-8") if a.file else a.text
            hub.add_reply(a.ps_id, text, analyze=False, conn=conn)
            if a.file and a.delete_file:
                Path(a.file).unlink(missing_ok=True)
            if not a.no_analyze:
                hub.analyze_replies(conn=conn)
        elif a.cmd == "analyze":
            hub.analyze_replies(a.ids, conn=conn)
        elif a.cmd == "counter":
            hub.counter(a.quote_id, a.tone, a.price, a.note, a.other, a.lang, conn=conn)
        elif a.cmd == "followup":
            hub.followups(a.ids, a.days or hub.FOLLOWUP_DAYS, a.note, conn=conn)
        elif a.cmd == "auto":
            hub.auto(a.days or hub.FOLLOWUP_DAYS, conn=conn)
        elif a.cmd == "mark":
            hub.mark(a.message_id, a.status, conn=conn)
        elif a.cmd == "sample":
            hub.sample(a.ps_id, a.qty, a.note, a.lang, conn=conn)
        elif a.cmd == "supplier":
            hub.supplier(a.ps_id, a.action, a.reason, conn=conn)
        elif a.cmd == "target":
            if a.product_id is None:
                for r in private.all_rows():
                    print(r)
            else:
                print(private.set_(a.product_id, a.target, a.max, a.payment)
                      if any(v is not None for v in (a.target, a.max, a.payment)) else private.get(a.product_id))
    except (ValueError, RuntimeError) as err:
        print(f"오류: {err}", file=sys.stderr, flush=True)
        return 1
    except Exception as err:      # API 키 없음·네트워크 등 — 실패는 agent_runs에 이미 기록됨, 대시보드엔 한 줄로
        print(f"오류: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
        if not a.cmd.startswith("gmail") and ("auth" in str(err).lower() or "api_key" in str(err).lower()):
            print("  → negotiation_agent/.env 에 ANTHROPIC_API_KEY 를 넣어 주세요 (.env.example 참고)", file=sys.stderr, flush=True)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
