-- ============================================================
-- 앱 구조: 일정·알림을 정식 항목으로. 메모·초안은 task_memory 를 그대로 쓴다.
-- 연동: 알림과 메모는 일정에 붙을 수 있다.
-- 30_skills.sql 다음에 실행. 여러 번 실행해도 안전하다.
-- ============================================================

-- ---------- 일정 ----------
create table if not exists events (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  title       text not null,
  starts_at   timestamptz not null,
  ends_at     timestamptz,
  all_day     boolean not null default false,
  place       text,
  created_by  text not null default 'user' check (created_by in ('user','agent')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint event_title_len check (char_length(title) between 1 and 100),
  constraint event_order check (ends_at is null or ends_at >= starts_at)
);
create index if not exists events_time on events(user_id, starts_at);

-- ---------- 알림 ----------
-- offset_min: 일정에 붙은 알림이면 "일정 몇 분 전"인지. 일정 시각이 바뀌면 따라 움직인다.
create table if not exists reminders (
  id          bigserial primary key,
  user_id     uuid not null default auth.uid() references auth.users(id) on delete cascade,
  text        text not null,
  due_at      timestamptz not null,
  status      text not null default 'scheduled'
              check (status in ('scheduled','sending','sent','cancelled')),
  event_id    bigint references events(id) on delete cascade,
  offset_min  int check (offset_min is null or offset_min between 0 and 10080),
  created_by  text not null default 'user' check (created_by in ('user','agent')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  sent_at     timestamptz,
  constraint reminder_text_len check (char_length(text) between 1 and 300),
  constraint offset_needs_event check (offset_min is null or event_id is not null)
);
create index if not exists reminders_due on reminders(status, due_at) where status = 'scheduled';

-- ---------- 메모·초안: 기존 task_memory 에 연결과 수정 시각 추가 ----------
alter table task_memory add column if not exists event_id bigint references events(id) on delete set null;
alter table task_memory add column if not exists title text;
alter table task_memory add column if not exists updated_at timestamptz not null default now();

-- ---------- RLS ----------
do $$
declare t text;
begin
  foreach t in array array['events','reminders'] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists own_rows on %I', t);
    execute format('create policy own_rows on %I for all to authenticated
                    using (user_id = auth.uid()) with check (user_id = auth.uid())', t);
  end loop;
end $$;

-- ---------- 기능이 일정 도구도 쓸 수 있게 ----------
alter table skills drop constraint if exists skill_tools_ok;
alter table skills add constraint skill_tools_ok
  check (tools <@ array['search','note','reminder','draft','event']::text[]);

-- ---------- 예전 방식(actions 에 걸려 있던) 알림을 새 테이블로 옮긴다 ----------
insert into reminders (user_id, text, due_at, created_by)
select user_id, coalesce(args->>'text', '알림'), due_at, 'agent'
from actions where kind = 'reminder' and status = 'scheduled' and due_at is not null;
update actions set status = 'done', done_at = now()
where kind = 'reminder' and status = 'scheduled';
