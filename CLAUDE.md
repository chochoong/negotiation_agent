# 커뮤니케이션·협상 에이전트 (negotiation_agent) — 1인 무역 5번 `negotiation`

공급처와의 커뮤니케이션·협상을 돕는 에이전트. trade_hub 공용 DB에 연결돼 있고 화면은 trade_hub 대시보드 5번 페이지.

파이프라인: 1 시장조사 → 2 공급처 발굴 (2-R 구매대행) → 3 통관·규제 체크 → 4 원가·수익 계산 → **5 커뮤니케이션·협상** → 6 무역서류 → 7 판매 → 8 재무

## 절대 원칙
1. 메시지는 절대 자동 발송하지 않는다. 생성물은 messages.status='draft' 로만 저장하고, 사람이 검토 후 직접 발송한다.
2. 내부 상한가(private.py, data/private.db)는 공용 DB·LLM 입력·초안 어디에도 들어가면 안 된다.
   새 초안 기능을 만들면 반드시 hub.save_draft 로 저장한다(leaks_max_price 검사 포함).
3. LLM이 모르는 사실(가격, 인증, 회사 정보, 주문량, 경쟁사 견적)을 지어내지 않게 프롬프트에서 막는다.
4. trade_hub 연결 규칙: 실행마다 agent_runs에 'negotiation' 1행(hub._run), 쓰기는 `with conn:`으로 짧게 커밋,
   자기 담당 테이블(messages, quotes, tasks, product_suppliers.status)만 쓴다. 스키마 변경은 trade_hub에서만.
5. Gmail: gmail.py 는 drafts.create(임시보관함)와 읽기만 한다. drafts.send·messages.send 를 절대 부르지 않는다
   (tests/test_gmail.py::test_source_never_calls_send 가 검사). 토큰·OAuth 파일(data/gmail_token.json, secrets/)은 git 제외.

## 구조
- negotiation_agent/__main__.py — CLI (`python -m negotiation_agent ...`). 대시보드 서버·슈퍼바이저가 이 명령을 실행
- negotiation_agent/hub.py — rfq, add_reply/analyze_replies, counter, followups, auto, mark (공용 DB 기록)
- negotiation_agent/private.py — 내부 목표가·상한가 (로컬 전용)
- negotiation_agent/prompts.py — 프롬프트
- negotiation_agent/llm.py — LLM 호출 (모델 교체는 여기서만, .env 는 이 폴더)
- negotiation_agent/gmail.py — Gmail: 초안 → 임시보관함(push_draft), 발송 확인·답장 가져오기(sync). messages.external_* 칸(trade_hub v9)
- 화면: ../trade_hub/trade_hub/dashboard.py (negotiation_view), 실행 경로: ../trade_hub/trade_hub/server.py (/api/neg)
- 자동 실행: ../trade_hub/supervisor.json `negotiation` (auto, Gmail 때문에 every_hours 2도). when_sql 은 hub.FOLLOWUP_DUE_SQL 과 같은 조건 — 같이 바꿀 것

## 로드맵
- [x] 1단계: RFQ 초안, 답장 분석 → 견적 저장
- [x] 2단계: 협상 역제안 초안 (부드럽게/보통/강하게), 무응답 후속 메일
- [x] trade_hub 공용 DB·대시보드·슈퍼바이저 연결 (2026-11-17)
- [ ] 3단계: 샘플 요청 템플릿 (messages.kind='sample'), 공급처 선정/제외 버튼
- [x] 4단계: Gmail 연동 — 임시보관함 초안 넣기, 발송 확인, 답장 자동 가져오기 (발송 X) (2026-11-18)
