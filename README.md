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

## Gmail 연동 (선택) — 초안을 임시보관함에 넣고, 답장을 자동으로 가져옴
**발송은 하지 않습니다.** 코드에 Gmail 발송 호출이 없고(테스트로 검사), [보내기]는 Gmail 화면에서 직접 누릅니다.

### 한 번만 설정
1. [Google Cloud 콘솔](https://console.cloud.google.com/) → 새 프로젝트 → **API 및 서비스 → 라이브러리 → Gmail API 사용**
2. **OAuth 동의 화면**: 사용자 유형 '외부' → 앱 이름·내 메일 입력 → **테스트 사용자에 내 Gmail 주소 추가**
3. **사용자 인증 정보 → OAuth 클라이언트 ID → 애플리케이션 유형 '데스크톱 앱'** → JSON 다운로드
4. 받은 파일을 `secrets\gmail_credentials.json` 으로 저장 (git 제외 폴더)
5. 로그인 (브라우저가 열림 — '확인되지 않은 앱' 경고는 '고급 → 이동'):
   ```powershell
   python -m negotiation_agent gmail-login     # 토큰 → data\gmail_token.json (git 제외)
   ```
   - 동의 화면이 '테스트' 상태면 **구글이 7일마다 로그인을 만료**시킵니다. 만료되면 대시보드에 안내가 뜨고, 위 명령을 다시 실행하면 됩니다.
     (동의 화면을 '프로덕션으로 게시'하면 만료되지 않음 — 확인 안 된 앱 경고는 계속 뜸)

### 쓰는 법
- 초안 옆 **[Gmail 임시보관함에 넣기]** → Gmail 임시보관함에서 확인·수정 후 직접 보내기
  - 받는 사람: 공급처 메일 주소(없으면 비워 둠 → Gmail에서 입력). 같은 공급처와 Gmail 대화가 있으면 그 대화의 답장('Re: …')으로 들어감
- Gmail에서 보내면 → 다음 동기화 때 자동으로 **'보냄'** 표시 (발송 완료 버튼을 누르지 않아도 됨)
- 공급처 답장 → 자동으로 가져와 요약·번역·견적 저장 (붙여넣기 불필요)
  - 가져오는 메일: ① 임시보관함에 넣었던 초안의 대화에 온 메일 ② 우리가 메일을 보낸 공급처 주소에서 온 메일
  - 첨부(가격표 PDF·엑셀)는 분석에 들어가지 않습니다 — 본문 끝 `[첨부 파일 …]` 표시를 보고 Gmail에서 열어 보세요
- 동기화: 슈퍼바이저 `auto`가 2시간마다, 또는 3번 페이지 위 **[Gmail 확인]** (`python -m negotiation_agent gmail-sync`)
- 권한: `gmail.compose`(초안 만들기) + `gmail.readonly`(읽기). compose 권한은 구글 기준으로 발송도 가능한 범위라, 발송은 코드에서 막습니다.

## 내부 기준가 (공용 DB에 저장하지 않음)
```powershell
python -m negotiation_agent target 16 --target 2.8 --max 3.5 --payment "T/T 30% 선금 / 70% 선적 전"
```
- 목표가: 역제안 단가를 비우면 이 값을 씀 (공급처에 말해도 되는 값)
- 상한가: **LLM 입력에도, 초안에도 들어가지 않음.** 초안에 같은 숫자가 나오면 저장하지 않음
- 저장 위치: `data/private.db` (git 제외). 슈퍼바이저 검토관이 읽는 공용 DB에는 없음

## 터미널 명령
```
python -m negotiation_agent status                     # 공급처별 메일·견적 현황 (연결번호 확인)
python -m negotiation_agent rfq <연결번호> [--qty "50 pcs"] [--incoterm FOB] [--lang zh]
python -m negotiation_agent reply <연결번호> --file 답장.txt
python -m negotiation_agent counter <견적번호> --tone firm [--price 2.8]
python -m negotiation_agent followup [메일번호]
python -m negotiation_agent auto                        # 슈퍼바이저가 실행: 답장 분석 + 후속 메일
python -m negotiation_agent mark <메일번호> sent         # approved / sent / discarded / draft
python -m negotiation_agent gmail-login                 # Gmail 로그인 (한 번)
python -m negotiation_agent gmail-draft <메일번호>       # 초안 → Gmail 임시보관함 (발송 X)
python -m negotiation_agent gmail-sync                  # Gmail 발송 확인 + 새 답장 가져와 분석
```

## 테스트
```powershell
python -m pytest -q      # 임시 DB + 가짜 LLM (API 비용 없음)
```
