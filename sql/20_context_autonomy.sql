-- 이미 배포된 DB 에 상황별 자율을 더한다 (00_schema.sql 을 처음부터 돌리는 경우엔 필요 없음)
create table if not exists action_context (
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  ctx        text not null,
  kind       text not null,
  label      text,
  acc        real not null default 0,
  rej        real not null default 0,
  n          int  not null default 0,
  pi         real not null default 0,
  mode       text check (mode in ('auto','ask')),
  changed_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key (user_id, ctx)
);
alter table action_context enable row level security;
drop policy if exists own_rows on action_context;
create policy own_rows on action_context for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
alter table actions add column if not exists auto_basis text;
