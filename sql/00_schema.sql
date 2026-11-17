-- ============================================================
-- 비서진 스키마 (합본: 기존 00 + 20 tasks + 30 skills + 40 apps + 71 heir)
--
-- 새 프로젝트: 이 파일 하나만 SQL Editor 에서 실행하면 된다.
-- 이미 쓰던 DB: 그대로 다시 실행해도 된다. 있는 테이블은 건너뛰고,
--               빠진 컬럼만 alter ... add column if not exists 로 채운다.
-- 그다음 10_cron.sql 을 실행한다.
--
-- 바닥 원칙 두 가지
--   1. 모든 행에는 주인(user_id)이 있고, RLS가 주인만 보게 한다.
--      사용자가 지금 한 명이어도 처음부터 이렇게 둔다.
--   2. 몰랑이처럼 "주장"이 아니라 DB 제약으로 강제한다.
--      - 검색·자기 답변·외부 메시지는 사실(facts)이 될 수 없다
--      - 사람 승인 없이 human 신뢰는 없다 (cogito anchor)
--      - 남에게 닿는 행동(tier 4)은 사람 승인 없이 완료될 수 없다
--      - 외부에서 들어온 내용이 계기인 행동은 최소 tier 4
--      - 추가 기능은 기본 도구 밖의 도구를 쓸 수 없다
--      - 검색 결과를 보고 나온 행동(tainted)은 승격돼 있어도 사람에게 묻는다
--      - 과제는 걸음 한도를 넘을 수 없다
--      - 몰랑이의 자식(heir)은 전체에서 한 명뿐이다
-- ============================================================

-- ---------- 0. 비서 (계정당 하나) ----------
create table if not exists agents (
  user_id     uuid primary key default auth.uid()
              references auth.users(id) on delete cascade,
  name        text,                         -- 사용자가 정한다. 정하기 전엔 null
  ntfy_topic  text,                         -- 계정별 알림 주제 (= 비밀번호)
  heir        boolean not null default false,  -- 몰랑이의 자식 (전체에서 한 명)
  persona     text,                         -- heir 계정에서 자라는 나다움
  think_at    text default '07:30',         -- 매일 비서가 먼저 생각하는 시각 (한국, HH:MM). null 이면 끔
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint name_len check (name is null or char_length(name) between 1 and 12)
);
alter table agents add column if not exists heir    boolean not null default false;
alter table agents add column if not exists persona text;
alter table agents add column if not exists think_at text default '07:30';
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'think_at_ok') then
    alter table agents add constraint think_at_ok
      check (think_at is null or think_at ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$');
  end if;
end $$;

-- heir 는 한 명만: 동시에 켜도 유니크 인덱스가 하나만 통과시킨다
create unique index if not exists agents_one_heir on agents ((true)) where heir;

-- 아무도 heir 가 아닐 때만 uid 를 올린다. 성공하면 uid, 아니면 null.
-- 예전 71_heir_unique.sql 의 함수는 반환 형식이 달라서 덮어쓸 수 없으므로 먼저 지운다.
-- (서버는 결과가 비었는지만 보므로 반환 형식이 바뀌어도 그대로 작동한다)
drop function if exists claim_heir(uuid);
create function claim_heir(uid uuid) returns uuid
language plpgsql security definer set search_path = public as $$
begin
  -- 이미 내가 heir 면 그대로 성공 (다시 켜도 거절되지 않게)
  if exists (select 1 from agents where heir and user_id = uid) then
    return uid;
  end if;
  if exists (select 1 from agents where heir) then
    return null;
  end if;
  update agents set heir = true, updated_at = now() where user_id = uid;
  return case when found then uid else null end;
exception when unique_violation then
  return null;
end $$;
revoke all on function claim_heir(uuid) from public, anon, authenticated;

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

-- ---------- 4. 일정 (앱) ----------
-- task_memory·reminders 가 일정을 가리키므로 먼저 만든다.
create table if not exists events (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  title       text not null,
  starts_at   timestamptz not null,
  ends_at     timestamptz,
  place       text,
  all_day     boolean not null default false,
  created_by  text not null default 'user' check (created_by in ('user','agent')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint ends_after_start check (ends_at is null or ends_at >= starts_at)
);
alter table events add column if not exists repeat text;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'events_repeat_ok') then
    alter table events add constraint events_repeat_ok
      check (repeat is null or repeat in ('daily','weekdays','weekly','monthly'));
  end if;
end $$;
create index if not exists events_when on events(user_id, starts_at);

-- ---------- 5. 업무 기억 (정체성과 분리) ----------
-- 여기 있는 것은 절대 facts 로 흡수되지 않는다.
-- 비서가 "처리"하는 것이지 "자기"로 소화하는 것이 아니다.
create table if not exists task_memory (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind        text not null check (kind in ('note','draft')),
  title       text,
  content     text not null,
  ref_action  bigint,
  -- 일정을 지우면 메모는 남고 연결만 풀린다 (메모는 사용자의 글이니까)
  event_id    bigint references events(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
alter table task_memory add column if not exists title      text;
alter table task_memory add column if not exists event_id   bigint references events(id) on delete set null;
alter table task_memory add column if not exists updated_at timestamptz not null default now();
create index if not exists task_memory_kind on task_memory(user_id, kind, updated_at desc);

-- ---------- 6. 추가 기능 ----------
-- 코드가 아니라 "지시문 + 기본 도구 목록". 도구 범위는 DB 가 막는다.
create table if not exists skills (
  id           bigserial primary key,
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name         text not null,
  description  text not null default '',
  instructions text not null,
  tools        text[] not null default '{}',
  origin       text not null default 'user' check (origin in ('user','agent')),
  app          text check (app is null or app in ('events','notes','reminders','drafts','search','tasks')),
  uses         int not null default 0,
  pinned       boolean not null default false,
  icon         text,
  tint         text,
  created_at   timestamptz not null default now(),
  constraint skill_name_len check (char_length(name) between 1 and 20),
  -- 기능이 늘어도 비서가 할 수 있는 행동의 범위는 넓어지지 않는다
  constraint skill_tools_ok check (tools <@ array['search','note','reminder','draft','event']::text[])
);
create unique index if not exists skills_name on skills(user_id, name);

-- ---------- 7. 과제 ----------
create table if not exists tasks (
  id            bigserial primary key,
  user_id       uuid not null default auth.uid() references auth.users(id) on delete cascade,
  goal          text not null,
  status        text not null default 'open'
                check (status in ('open','waiting_user','waiting_approval','done','stopped','cancelled')),
  next_step     text,
  question      text,
  result        text,
  reflection    text,                 -- 끝낼 때 스스로 적은 한계
  quality       smallint check (quality is null or quality between 1 and 5),
  log           jsonb not null default '[]',   -- 업무 기억. facts 로 흡수되지 않는다
  steps         int not null default 0,
  max_steps     int not null default 12,
  skill_id      bigint references skills(id) on delete set null,
  icon          text,
  tint          text,
  last_step_at  timestamptz,
  -- 지금 누가 이 과제를 진행 중인지 (크론·"지금 한 걸음"이 겹쳐도 한쪽만 진행)
  lease_until   timestamptz not null default 'epoch',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint steps_cap check (steps <= max_steps),
  constraint max_steps_range check (max_steps between 1 and 60)
);
alter table tasks add column if not exists lease_until timestamptz not null default 'epoch';
alter table tasks add column if not exists shortcut_id bigint;      -- 버튼(단축)으로 시작한 과제 → 홈 위젯이 '최근 결과'를 보여 준다
create index if not exists tasks_open on tasks(user_id, status, last_step_at nulls first);

-- ---------- 8. 행동 ----------
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
  task_id           bigint references tasks(id) on delete set null,
  -- 검색 결과(바깥 글)를 본 뒤에 나온 행동. tier 3 이상이면 자동 실행할 수 없다.
  tainted           boolean not null default false,
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
alter table actions add column if not exists task_id bigint references tasks(id) on delete set null;
alter table actions add column if not exists tainted boolean not null default false;
-- 오염된 행동은 승격돼 있어도 묻는다 (보기·초안인 tier 1·2 는 일어나는 일이 없으므로 예외)
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'tainted_never_auto') then
    alter table actions add constraint tainted_never_auto
      check (not (tainted and auto_executed and tier >= 3));
  end if;
end $$;
create index if not exists actions_open on actions(user_id, status);
create index if not exists actions_due on actions(status, due_at) where status = 'scheduled';

-- ---------- 9. 행동 신뢰 (승격·강등) ----------
create table if not exists action_trust (
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind       text not null,
  clean      int not null default 0,         -- 수정 없이 승인된 횟수
  promoted   boolean not null default false, -- true 면 tier 3 행동을 묻지 않고 실행
  updated_at timestamptz not null default now(),
  primary key (user_id, kind)
);

-- ---------- 10. 알림 (앱) ----------
-- 일정에 붙은 알림은 일정을 지우면 같이 지워진다.
-- offset_min 이 있으면 "일정 몇 분 전"이라 일정 시각을 따라 움직인다.
create table if not exists reminders (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  text        text not null,
  due_at      timestamptz not null,
  event_id    bigint references events(id) on delete cascade,
  offset_min  int check (offset_min is null or offset_min between 0 and 10080),
  -- scheduled → sending(가져감) → sent. 틱이 겹쳐도 한 번만 간다.
  status      text not null default 'scheduled'
              check (status in ('scheduled','sending','sent','cancelled')),
  created_by  text not null default 'user' check (created_by in ('user','agent')),
  sent_at     timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
-- 반복 알림: 보내면 '보냄'으로 끝나지 않고 다음 회차로 다시 걸린다. repeat_anchor 는 첫 회차(매월 31일 같은 날을 기억)
alter table reminders add column if not exists repeat text;
alter table reminders add column if not exists repeat_anchor timestamptz;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'reminders_repeat_ok') then
    alter table reminders add constraint reminders_repeat_ok
      check (repeat is null or repeat in ('daily','weekdays','weekly','monthly'));
  end if;
end $$;
create index if not exists reminders_due on reminders(status, due_at) where status = 'scheduled';
create index if not exists reminders_event on reminders(event_id);

-- ---------- 11. 명령 버튼 ----------
create table if not exists shortcuts (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  app         text not null check (app in ('search','tasks')),
  label       text not null,
  kind        text not null check (kind in ('search','task')),
  text        text not null,
  icon        text,
  uses        int not null default 0,
  pinned      boolean not null default false,
  -- 'agent' = 비서가 사용 패턴을 보고 만든 것 (홈에서 표시가 다르다)
  origin      text not null default 'user' check (origin in ('user','agent')),
  created_at  timestamptz not null default now()
);
alter table shortcuts add column if not exists origin text not null default 'user';

-- ---------- 12. 사용 패턴 (버튼 제안용 관찰) ----------
create table if not exists usage_patterns (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  app         text not null check (app in ('search','tasks')),
  norm_key    text not null,
  sample      text not null,
  count       int not null default 1,
  suggested   boolean not null default false,
  dismissed   boolean not null default false,
  created_at  timestamptz not null default now()
);
create unique index if not exists usage_patterns_key on usage_patterns(user_id, app, norm_key);

-- ---------- 13. 먼저 거는 말 ----------
create table if not exists outbox (
  id         bigserial primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  rule       text not null,
  body       text not null,
  notified   boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists outbox_day on outbox(user_id, rule, created_at desc);

-- ---------- 14. 앱 자체 알림 구독 (기기마다 하나) ----------
create table if not exists push_subscriptions (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  endpoint    text not null unique,
  p256dh      text not null,
  auth        text not null,
  device      text,
  created_at  timestamptz not null default now()
);

-- ---------- 14-1. 판단 기록 (몰랑이 판단 노드 · 판단 회상) ----------
-- 비서가 제안·행동·과제 마무리를 할 때마다 어떤 길로(path, at_probs) 무엇을 근거로(evidence)
-- 어떤 지난 판단을 떠올려(recalled) 판단했는지 남긴다. '왜?' 화면이 이것을 보여 준다.
create table if not exists judgments (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  tree        text not null,
  path        jsonb not null default '[]',
  at_probs    jsonb not null default '[]',
  steps       jsonb not null default '[]',
  evidence    jsonb not null default '[]',
  recalled    jsonb not null default '[]',
  summary     text,
  context     text,
  ref_kind    text check (ref_kind in ('daily','action','task')),
  ref_id      text,
  tainted     boolean not null default false,
  feedback    real check (feedback is null or feedback between -1 and 1),
  decided_at  timestamptz,
  created_at  timestamptz not null default now()
);
create index if not exists judgments_ref on judgments(user_id, ref_kind, ref_id);
create index if not exists judgments_tree on judgments(user_id, tree, id desc);

-- ---------- 14-2. 문서함 (올린 문서 · 고친 버전) ----------
-- 파일 자체는 Storage 의 docs 버킷({user_id}/무작위.확장자)에, 뽑은 글과 구조는 여기에.
-- 고친 파일은 원본을 덮지 않고 parent_id = 원본 id 인 새 행(버전)으로 남는다.
create table if not exists docs (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name        text not null,
  kind        text not null check (kind in ('xlsx','docx','pdf','hwp','hwpx','image')),
  mime        text,
  size        bigint not null default 0,
  path        text not null,
  text        text,
  outline     jsonb not null default '{}',
  pages       int,
  note        text,
  summary     text,
  parent_id   bigint references docs(id) on delete cascade,
  version     int not null default 1,
  changes     jsonb,
  created_at  timestamptz not null default now(),
  constraint docs_path_own check (path like (user_id::text || '/%'))
);
create index if not exists docs_user on docs(user_id, id desc);
create index if not exists docs_parent on docs(parent_id);

-- 양식함: 문서를 양식으로 저장. path = {{칸}} 이 박힌 양식 원본, src_path = 처음 문서 사본
create table if not exists templates (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name        text not null,
  kind        text not null check (kind in ('xlsx','docx','hwpx','pdf')),
  ext         text not null,
  src_path    text not null,
  path        text not null,
  fields      jsonb not null default '[]',
  last_values jsonb not null default '{}',
  uses        int not null default 0,
  source_doc  bigint references docs(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint templates_path_own check (path like (user_id::text || '/%') and src_path like (user_id::text || '/%')),
  constraint templates_fields_ok check (jsonb_typeof(fields) = 'array' and jsonb_array_length(fields) <= 40)
);
create index if not exists templates_user on templates(user_id, id desc);
alter table docs add column if not exists template_id bigint references templates(id) on delete set null;

-- 저장소 버킷과 정책: 자기 폴더({auth.uid()}/...)의 파일만 읽고 쓰고 지운다.
-- (Supabase 가 아닌 Postgres 에서 돌리면 storage 스키마가 없으므로 건너뛴다)
do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'storage')
     and exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = 'storage' and c.relname = 'buckets') then
    execute $q$insert into storage.buckets (id, name, public, file_size_limit)
               values ('docs', 'docs', false, 20971520)
               on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit$q$;
    execute 'drop policy if exists docs_own_select on storage.objects';
    execute 'drop policy if exists docs_own_insert on storage.objects';
    execute 'drop policy if exists docs_own_update on storage.objects';
    execute 'drop policy if exists docs_own_delete on storage.objects';
    execute $q$create policy docs_own_select on storage.objects for select to authenticated
               using (bucket_id = 'docs' and (storage.foldername(name))[1] = auth.uid()::text)$q$;
    execute $q$create policy docs_own_insert on storage.objects for insert to authenticated
               with check (bucket_id = 'docs' and (storage.foldername(name))[1] = auth.uid()::text)$q$;
    execute $q$create policy docs_own_update on storage.objects for update to authenticated
               using (bucket_id = 'docs' and (storage.foldername(name))[1] = auth.uid()::text)
               with check (bucket_id = 'docs' and (storage.foldername(name))[1] = auth.uid()::text)$q$;
    execute $q$create policy docs_own_delete on storage.objects for delete to authenticated
               using (bucket_id = 'docs' and (storage.foldername(name))[1] = auth.uid()::text)$q$;
  end if;
end $$;

-- ---------- 15. 계정별로 자라는 상태 ----------
-- 마르코프·SOM·사고 트리처럼 입력으로 자라는 것은 반드시 여기, 계정별로.
-- 공용으로 자라면 한 사람의 말투·단어가 다른 사람 비서로 샌다.
create table if not exists agent_state (
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  key        text not null,
  value      jsonb not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, key)
);

-- ---------- 16. 감사 로그 ----------
create table if not exists audit (
  id         bigserial primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  kind       text not null,
  detail     jsonb,
  created_at timestamptz not null default now()
);

-- ---------- 17. 서버 비밀 (VAPID 열쇠 등) ----------
-- 주인이 없는 서버 전용 값. RLS 는 켜되 정책을 두지 않아 service role 만 읽는다.
create table if not exists app_secrets (
  key        text primary key,
  value      jsonb not null,
  created_at timestamptz not null default now()
);
alter table app_secrets enable row level security;

-- ============================================================
-- RLS: 주인만 본다
-- (크론은 service role 로 돌고 RLS를 우회하므로, 코드에서 Scoped 로 user_id 를 건다)
-- ============================================================
do $$
declare t text;
begin
  foreach t in array array['agents','facts','quarantine','episodes','events','task_memory',
                           'skills','tasks','actions','action_trust','reminders','shortcuts',
                           'usage_patterns','outbox','push_subscriptions','agent_state','audit',
                           'judgments','docs','templates']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists own_rows on %I', t);
    execute format(
      'create policy own_rows on %I for all to authenticated
         using (user_id = auth.uid()) with check (user_id = auth.uid())', t);
  end loop;
end $$;
