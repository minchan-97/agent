-- 관문핵 구조 보강: 가짜 예외 막기(겪은 날 수). 20번을 이미 돌린 DB 에 더한다.
-- 돌리기 전에도 앱은 동작한다 (날 수 없이 저장하고, 기간 조건은 건너뛴다).
alter table action_context add column if not exists days int not null default 0;
alter table action_context add column if not exists last_day text;
-- 이미 쌓인 상황은 기간을 알 수 없으니 통과한 것으로 둔다 (갑자기 자동이 풀리지 않게)
update action_context set days = 2 where days = 0 and n > 0;
