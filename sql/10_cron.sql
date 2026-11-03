-- ============================================================
-- 10분마다 Vercel 의 /api/cron/tick 을 깨운다.
-- Vercel Hobby 크론은 하루 한 번뿐이라, 깨우는 일은 Supabase 가 맡는다.
--
-- 실행 전: Supabase 대시보드 > Database > Extensions 에서
--          pg_cron, pg_net 두 개를 켠다.
-- 아래 두 값을 바꿔서 실행:
--   <YOUR_APP>     Vercel 주소 (예: biseojin.vercel.app)
--   <CRON_SECRET>  Vercel 환경변수 CRON_SECRET 과 같은 값
-- ============================================================

-- 비밀값은 Vault 에 넣어 두고 꺼내 쓴다 (SQL 기록에 평문이 남지 않게)
select vault.create_secret('<CRON_SECRET>', 'biseo_cron_secret');

select cron.unschedule('biseo-tick') where exists (
  select 1 from cron.job where jobname = 'biseo-tick');

select cron.schedule(
  'biseo-tick',
  '*/10 * * * *',
  $$
  select net.http_get(
    url := 'https://<YOUR_APP>/api/cron/tick',
    headers := jsonb_build_object(
      'Authorization',
      'Bearer ' || (select decrypted_secret from vault.decrypted_secrets
                    where name = 'biseo_cron_secret')),
    timeout_milliseconds := 55000
  );
  $$
);
