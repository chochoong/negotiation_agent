# 3번 커뮤니케이션·협상 에이전트 (negotiation_agent)

1인 무역 파이프라인의 3번 에이전트. 2번(sourcing)이 찾은 공급처에 보낼 **메일 초안**을 영어/중국어로 만들고,
받은 답장을 한국어로 요약·번역해 **견적표**에 정리합니다. 데이터는 공용 DB(`../trade_hub/data/trade.db`)에 기록하고,
화면은 trade_hub 대시보드(3번 페이지)를 씁니다.

**메일은 절대 자동 발송되지 않습니다.** 초안을 검토한 뒤 직접 보내고 대시보드에서 '발송 완료로 표시'를 누르세요.

## 설치 (Windows PowerShell)
```powershell
cd C:\workspace\negotiation_agent
pip install -r requirements.txt     # anthropic, python-dotenv, trade_hub(편집 모드)
copy .env.example .env              # 메모장으로 열어 ANTHROPIC_API_KEY 입력
```
슈퍼바이저·대시보드 서버를 돌리는 파이썬에서 실행되므로(`supervisor.json`의 `{python}`) 그 파이썬에 설치하세요.

## 쓰는 법 — 대시보드 (`trade_hub serve` → 3번 페이지)
1. 2번이 찾은 공급처 옆 **[견적 요청 초안]** → 수량·조건·언어 입력 → 초안 생성
   (2번이 정리한 첫 문의 질문·확인 못 한 항목·필요 인증을 함께 물음)
2. **[원문 복사]** → 메일·알리바바·위챗으로 직접 발송 → **[발송 완료로 표시]**
   - 첫 발송 때 품목이 '공급처 발굴' → '협상' 단계로, 공급처는 '연락함'으로 바뀝니다.
3. 답장이 오면 **[받은 답장 붙여넣기]** → 요약·번역, USD 가격이 있으면 견적표에 저장('견적 받음')
4. 견적 옆 **[역제안]** → 톤(부드럽게/보통/강하게)·단가 → 역제안 초안
5. 3일 동안 답이 없으면 슈퍼바이저가 **후속 메일 초안**을 자동으로 만듭니다(메일당 최대 2회).
   기다리지 않고 만들려면 **[후속 메일 초안]**. 후속 초안을 '버림'하면 그 메일은 더 이상 후속하지 않습니다.

## 내부 기준가 (공용 DB에 저장하지 않음)
```powershell
python -m comm_agent target 16 --target 2.8 --max 3.5 --payment "T/T 30% 선금 / 70% 선적 전"
```
- 목표가: 역제안 단가를 비우면 이 값을 씀 (공급처에 말해도 되는 값)
- 상한가: **LLM 입력에도, 초안에도 들어가지 않음.** 초안에 같은 숫자가 나오면 저장하지 않음
- 저장 위치: `data/private.db` (git 제외). 슈퍼바이저 검토관이 읽는 공용 DB에는 없음

## 터미널 명령
```
python -m comm_agent status                     # 공급처별 메일·견적 현황 (연결번호 확인)
python -m comm_agent rfq <연결번호> [--qty "50 pcs"] [--incoterm FOB] [--lang zh]
python -m comm_agent reply <연결번호> --file 답장.txt
python -m comm_agent counter <견적번호> --tone firm [--price 2.8]
python -m comm_agent followup [메일번호]
python -m comm_agent auto                        # 슈퍼바이저가 실행: 답장 분석 + 후속 메일
python -m comm_agent mark <메일번호> sent         # approved / sent / discarded / draft
```

## 테스트
```powershell
python -m pytest -q      # 임시 DB + 가짜 LLM (API 비용 없음)
```
