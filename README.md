# 휴가 및 출장 처리 에이전트

직원의 휴가·출장 신청을 받아 부서 출타율 30% 규칙으로 자동 승인하거나 책임자에게 넘기는 로컬 시연 시스템입니다. 신청은 **웹 폼**과 **자유 형식 메일** 두 경로로 받습니다. 메일은 LLM 에이전트가 읽고, 빠진 정보는 되묻고, 이해한 내용을 직원에게 확인받은 뒤 접수합니다.

Python 표준 라이브러리와 로컬 LM Studio(`qwen/qwen3-8b`)만 사용합니다. 외부 패키지 설치가 없습니다.

## 무엇을 하나

- **신청은 하루 또는 기간(시작일~종료일) 단위입니다.** 사용일은 기간 안의 평일(월~금)만 세고, 반차는 하루짜리 신청만 가능합니다.
- **승인 규칙은 코드가 정합니다.** 일반 신청은 `pending`(자동 승인 대기)으로 접수되고, 하루 뒤 같은 부서의 대기 신청을 한꺼번에 계산해 기간의 모든 날이 30% 이하면 자동 승인, 하루라도 넘으면 그 신청 전체를 책임자 확인으로 넘깁니다. 사용일이 내일 이전이면 바로 판정합니다.
- **예외는 접수 즉시 처리합니다.** 친인척 경조사와 증빙 있는 중요 출장은 책임자 우선 검토, 사유가 모호하면 책임자 확인, 잔여 휴가일이 부족하면 반려입니다. 사유 분류만 LLM이 하고 승인은 하지 않습니다.
- **메일 에이전트**는 도구(`ask`, `leave_balance`, `capacity`, `propose`, `reply`)를 스스로 골라 씁니다. LLM은 **제안만** 할 수 있고, 직원이 "확인"이라고 답해야 코드가 접수합니다. 날짜는 LLM이 아니라 코드가 메일 원문에서 찾습니다.
- 승인된 출타자는 `출타자 현황.csv`(Excel용 UTF-8)에 `기간`(예: `2026-12-21 ~ 2026-12-23`)과 `사용일`로 내보냅니다. 알림은 DB에 기록하며 실제 메신저 연동은 없습니다.

## 준비

```powershell
lms server start
lms load qwen/qwen3-8b
```

LM Studio 앱에서 모델을 로드하고 **Developer → Start server**를 켜도 됩니다. 기본 주소는 `http://127.0.0.1:1234/v1`이며 `LM_STUDIO_URL`, `LM_STUDIO_MODEL`로 바꿀 수 있습니다.

## 웹

```powershell
python web.py
```

`http://127.0.0.1:8765`를 엽니다. 신청 `/apply`, 담당자 `/manager` 화면이 있습니다. 담당자 화면에서 부서와 직원을 먼저 등록합니다. 웹 서버는 1분마다 자동 승인 판정을 실행합니다. 로컬 접속만 허용하며 로그인·권한 관리는 없습니다.

## 메일

```powershell
python mail_agent.py register 김하나 hana@example.com
python mail_agent.py try hana@example.com "12월 21일부터 23일까지 연차, 가족 여행"
python mail_agent.py try hana@example.com "확인"
python mail_agent.py run
```

`try`는 메일 계정 없이 한 통을 처리해 답장을 화면에 보여줍니다. `run`은 IMAP 메일함을 1분마다 확인하고 SMTP로 답장합니다. 환경 변수 `MAIL_USER`, `MAIL_PASSWORD`, `IMAP_HOST`, `SMTP_HOST`(기본 Gmail)가 필요합니다. 등록되지 않은 주소, 자동 발송 메일, 발신자 인증(`dmarc=pass`)이 없는 메일에는 답하지 않습니다. DMARC가 없는 사내 메일 서버는 `MAIL_TRUST_SENDER=1`을 설정하고 내부 전용 메일함을 사용하세요.

메일로 접수한 신청도 웹 담당자 화면에 그대로 표시됩니다.

## CLI

```powershell
python agent.py department 개발 10
python agent.py employee 김하나 개발 15
python agent.py submit sample_requests.json
python agent.py settle
python agent.py list
python agent.py approve 1
python agent.py cancel 1
python agent.py respond 2 yes
python agent.py notices
```

신청 파일은 JSON 배열입니다. `day`는 시작일, `end_day`는 종료일(생략하면 하루)입니다. `kind`는 `leave`/`trip`, `period`는 `full`/`am`/`pm`이고, 증빙 있는 출장은 `evidence: true`와 `evidence_note`가 필요합니다. 승인 취소로 자리가 나면 대기 신청에 `offered` 알림이 가고 `yes`를 받아야 승인됩니다.

## 테스트와 평가

```powershell
python test_agent.py                              # 업무 규칙·메일 흐름 단위 테스트 (LLM 없이)
python evaluate_model.py                          # 사유 분류 샘플 (LM Studio 필요)
python evaluate_mail.py eval_mail/test_v3.json    # 메일 에이전트 평가 (LM Studio 필요)
```

메일 에이전트는 개발셋과 고정된 테스트셋을 분리해 평가했습니다. 최종 테스트셋 v3(38건)에서 잘못 접수된 신청은 0건입니다. 절차와 수치는 [EVALUATION.md](EVALUATION.md)에 있습니다.

## 문서

- [ARCHITECTURE.md](ARCHITECTURE.md): 구성과 데이터 흐름
- [EVALUATION.md](EVALUATION.md): 평가 절차, 결과, 오류 분석
- [ISSUES_AND_FIXES.md](ISSUES_AND_FIXES.md): 개발 중 발견한 문제와 수정 기록

## 한계

- 메일 한 통에 신청 한 건(하루 또는 연속 기간 하나)만 받습니다. 떨어진 날짜 여러 개는 따로 보내야 합니다.
- 사용일에서 주말만 빼고 공휴일은 빼지 않습니다.
- 지난 날짜 소급 신청은 메일로 받지 않고 되묻습니다.
- 평가셋은 개발자(에이전트)가 작성했습니다. 실제 직원 메일로 다시 평가해야 합니다.
- 증빙 파일의 진위는 자동 판별하지 않습니다. 책임자가 확인합니다.
