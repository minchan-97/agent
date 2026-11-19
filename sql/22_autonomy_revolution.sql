-- 관문핵 구조: 혁명과 재해석 (종류 규칙 자체가 바뀌는 단계). 21번 다음에 실행한다.
-- 돌리기 전에도 앱은 동작한다 (혁명 압력이 저장되지 않아 종류 규칙은 그대로 유지된다).
alter table action_context add column if not exists rival int not null default 0;
alter table action_context add column if not exists rival_day text;
