# 비서진 (v0)

몰랑이를 모체로 한 개인·가족 비서. 이름은 사용자가 짓는다.
GitHub → Vercel 자동 배포, 상태는 Supabase, 주기 실행은 Supabase pg_cron.

## 구조

```
api/index.py        Vercel 에서 도는 FastAPI 창구
biseo/
  identity.py       정체성 주입 (몰랑이 identity_core 원리)
  absorb.py         사용자 사실 흡수 관문 (몰랑이 molang_store 원리)
  norm.py           정규화·에코 판정 (몰랑이 molang_norm)
  search.py         Brave 검색, 결과 격리 (몰랑이 molang_search)
  notify.py         ntfy 알림, 계정별 주제 (몰랑이 notify)
  privacy.py        번호류 가리기 + 알림 민감어 필터
  actions.py        자율성 등급·승격·강등
  brain.py          대화 한 턴
  tick.py           크론 한 걸음 (규칙만, LLM 없음)
  db.py             사용자용(RLS 적용) / 크론용(service role) 연결
public/index.html   화면 (로그인 → 이름 짓기 → 대화·확인함)
sql/00_schema.sql   테이블·제약·RLS
sql/10_cron.sql     10분마다 tick 호출
```

## 설치 순서

1. **Supabase**
   - 새 프로젝트를 만들고 SQL Editor 에서 `sql/00_schema.sql` 실행
   - Authentication > Providers 에서 Email 켜기
     (혼자 쓸 땐 "Confirm email" 을 꺼 두면 가입 즉시 로그인된다)
2. **GitHub** 에 이 폴더를 올린다. `.env` 파일은 올리지 않는다.
3. **Vercel** 에서 그 저장소를 Import
   - Environment Variables 에 `.env.example` 의 값들을 넣는다
   - 배포되면 주소(예: `biseojin.vercel.app`)를 확인
4. **pg_cron**
   - Supabase > Database > Extensions 에서 `pg_cron`, `pg_net` 켜기
   - `sql/10_cron.sql` 의 `<YOUR_APP>`, `<CRON_SECRET>` 을 바꿔서 실행
5. 폰에 **ntfy** 앱을 깔고, 앱의 설정 화면에 나오는 주제를 구독

## 자율성 등급

| 등급 | 뜻 | 처리 | 지금 있는 종류 |
|---|---|---|---|
| 1 | 보기·찾기 | 바로 실행 | 검색 |
| 2 | 제안·초안 | 만들어서 보여 줌 | 초안 |
| 3 | 나에게만, 되돌릴 수 있음 | 처음엔 묻고, 수정 없이 5번 승인되면 묻지 않음 | 메모, 알림 예약 |
| 4 | 남에게 닿음, 되돌릴 수 없음 | 언제나 묻는다 (승격 없음) | 메시지 보내기 (아직 꺼져 있음) |

- 자동 실행한 일을 되돌리면 그 종류는 다시 묻는 단계로 내려간다.
- 바깥(메일·문자)에서 들어온 내용이 계기면 무조건 4등급. DB 제약으로도 막혀 있다.
- 승격 기록은 계정마다 따로 쌓인다.

## 몰랑이에서 지킨 것, 바꾼 것

**지킨 것**
- 신뢰 구조를 DB 제약으로 강제: 사용자 본인 말만 사실이 되고, 사람 승인 없이 human 신뢰는 없다
- 검색 결과는 답변 문맥으로만 쓰고 기억으로 흡수하지 않는다
- 비서 자기 말이 사실로 굳는 것(assistant echo)을 검역소로 보낸다
- 먼저 말을 거는 것은 규칙이 정한다. 계기가 없으면 아무 말도 안 한다

**바꾼 것**
- 이름과 목적: 몰랑이가 아니라 사용자가 이름 짓는 비서, 목적은 바깥(사용자의 일)
- 업무 기억(`task_memory`)과 사용자 사실(`facts`)을 테이블부터 분리
- 모든 행에 `user_id` + RLS. 지금 한 명이어도 가족 계정을 붙일 수 있는 바닥

## 아직 안 넣은 것 (다음 단계)

- 사고 트리·마르코프·SOM: 넣을 때는 반드시 `agent_state` 에 **계정별로** 둔다
- 메일·캘린더 연결 (들어오는 내용은 `origin='external'`)
- 사기 메시지 감지, 가족 공유 일정, 관리자 운영 화면
- 검역소·미확신 사실을 사람이 승인하는 화면 (몰랑이 `/pending`, `/approve` 에 해당)

## 알아 둘 점

- Vercel Hobby 는 비상업적 개인 용도 전용이다.
- 브라우저는 Supabase 에 직접 쿼리하지 않고 API 만 부른다. 다만 로그인 토큰으로
  자기 행은 직접 고칠 수 있다 (RLS 는 남의 행만 막는다). 가족 규모에서는 괜찮지만,
  외부 사용자를 받게 되면 쓰기는 서버 전용 함수로 옮길 것.
