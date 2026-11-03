-- ============================================================
-- 비서진 스키마 (몰랑이 스키마에서 물려받음)
--
-- 바닥 원칙 두 가지
--   1. 모든 행에는 주인(user_id)이 있고, RLS가 주인만 보게 한다.
--      사용자가 지금 한 명이어도 처음부터 이렇게 둔다.
--   2. 몰랑이처럼 "주장"이 아니라 DB 제약으로 강제한다.
--      - 검색·자기 답변·외부 메시지는 사실(facts)이 될 수 없다
--      - 사람 승인 없이 human 신뢰는 없다 (cogito anchor)
--      - 남에게 닿는 행동(tier 4)은 사람 승인 없이 완료될 수 없다
--      - 외부에서 들어온 내용이 계기인 행동은 최소 tier 4
-- ============================================================

-- ---------- 0. 비서 (계정당 하나) ----------
create table if not exists agents (
  user_id     uuid primary key default auth.uid()
              references auth.users(id) on delete cascade,
  name        text,                         -- 사용자가 정한다. 정하기 전엔 null
  ntfy_topic  text,                         -- 계정별 알림 주제 (= 비밀번호)
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint name_len check (name is null or char_length(name) between 1 and 12)
);

-- ---------- 1. 사용자에 대한 사실 (정체성 쪽) ----------
create table if not exists facts (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  text        text not null,
  norm_key    text not null,
  kind        text not null default 'fact',
  strength    real not null default 0.6,
  trust       text not null default 'derived',
  seen        int  not null default 1,
  source      text not null default 'user',
  expires_at  timestamptz,
  approved_by_human boolean not null default false,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  constraint kind_ok   check (kind  in ('fact','state','preference')),
  constraint trust_ok  check (trust in ('human','derived','doubted')),
  -- 'external' = 메일·문자처럼 바깥에서 들어온 것. 몰랑이에 없던 출처.
  constraint source_ok check (source in ('user','assistant','search','external')),
  constraint strength_range check (strength between 0.0 and 1.0),
  -- 사용자 본인 말이 아니면 사실이 될 수 없다
  constraint only_user_absorbs check (source = 'user'),
  constraint human_needs_approval check (trust <> 'human' or approved_by_human),
  constraint state_needs_expiry check (kind <> 'state' or expires_at is not null)
);
create unique index if not exists facts_norm on facts(user_id, norm_key);

create or replace view facts_active with (security_invoker = true) as
select * from facts
where strength > 0.2 and (expires_at is null or expires_at > now());

-- ---------- 2. 검역소 ----------
create table if not exists quarantine (
  id         bigserial primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  text       text not null,
  reason     text not null,     -- assistant_echo | near_duplicate
  created_at timestamptz not null default now(),
  resolved   text
);

-- ---------- 3. 대화 ----------
create table if not exists episodes (
  id         bigserial primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  role       text not null check (role in ('user','agent')),
  text       text not null,
  created_at timestamptz not null default now()
);
create index if not exists episodes_recent on episodes(user_id, id desc);

-- ---------- 4. 업무 기억 (정체성과 분리) ----------
-- 여기 있는 것은 절대 facts 로 흡수되지 않는다.
-- 비서가 "처리"하는 것이지 "자기"로 소화하는 것이 아니다.
create table if not exists task_memory (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind        text not null check (kind in ('note','draft')),
  content     text not null,
  ref_action  bigint,
  created_at  timestamptz not null default now()
);

-- ---------- 5. 행동 ----------
-- tier 1 보기·찾기 / 2 제안·초안 / 3 나에게만, 되돌릴 수 있음 / 4 남에게 닿음
create table if not exists actions (
  id                bigserial primary key,
  user_id           uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind              text not null,
  tier              smallint not null check (tier between 1 and 4),
  origin            text not null default 'user' check (origin in ('user','self','external')),
  args              jsonb not null default '{}',
  reason            text,
  status            text not null default 'proposed'
                    check (status in ('proposed','scheduled','done','rejected','blocked','undone','failed')),
  auto_executed     boolean not null default false,
  approved_by_human boolean not null default false,
  result            jsonb,
  due_at            timestamptz,
  created_at        timestamptz not null default now(),
  decided_at        timestamptz,
  done_at           timestamptz,

  -- 외부에서 들어온 내용이 시킨 일은 최소 tier 4
  constraint external_is_strict check (origin <> 'external' or tier = 4),
  -- tier 4 는 자동 실행 불가, 승인 없이 완료 불가
  constraint t4_never_auto check (not (tier = 4 and auto_executed)),
  constraint t4_needs_human check (not (tier = 4 and status in ('done','scheduled') and not approved_by_human))
);
create index if not exists actions_open on actions(user_id, status);
create index if not exists actions_due on actions(status, due_at) where status = 'scheduled';

-- ---------- 6. 행동 신뢰 (승격·강등) ----------
create table if not exists action_trust (
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind       text not null,
  clean      int not null default 0,         -- 수정 없이 승인된 횟수
  promoted   boolean not null default false, -- true 면 tier 3 행동을 묻지 않고 실행
  updated_at timestamptz not null default now(),
  primary key (user_id, kind)
);

-- ---------- 7. 먼저 거는 말 ----------
create table if not exists outbox (
  id         bigserial primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  rule       text not null,
  body       text not null,
  notified   boolean not null default false,
  created_at timestamptz not null default now()
);

-- ---------- 8. 계정별로 자라는 상태 ----------
-- 마르코프·SOM·사고 트리처럼 입력으로 자라는 것은 반드시 여기, 계정별로.
-- 공용으로 자라면 한 사람의 말투·단어가 다른 사람 비서로 샌다.
create table if not exists agent_state (
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  key        text not null,
  value      jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, key)
);

-- ---------- 9. 감사 로그 ----------
create table if not exists audit (
  id         bigserial primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind       text not null,
  detail     jsonb,
  created_at timestamptz not null default now()
);

-- ============================================================
-- RLS: 주인만 본다
-- (크론은 service role 로 돌고 RLS를 우회하므로, 코드에서 user_id 를 반드시 건다)
-- ============================================================
do $$
declare t text;
begin
  foreach t in array array['agents','facts','quarantine','episodes','task_memory',
                           'actions','action_trust','outbox','agent_state','audit']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists own_rows on %I', t);
    execute format(
      'create policy own_rows on %I for all to authenticated
         using (user_id = auth.uid()) with check (user_id = auth.uid())', t);
  end loop;
end $$;
