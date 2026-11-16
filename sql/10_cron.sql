-- ============================================================
-- 크론: Supabase 가 Vercel 의 /api/cron/tick 을 주기적으로 깨운다.
-- (Vercel Hobby 크론은 하루 한 번뿐이라, 깨우는 일은 Supabase 가 맡는다)
--
--   biseo-due   1분마다  ?only=due   시각이 된 알림만 (AI 안 부름, 싸고 빠름)
--   biseo-tick  2분마다             과제 진행·아침 브리핑·성장 (열린 과제가 없으면 AI 안 부름)
--
-- 실행 전: Database > Extensions 에서 pg_cron, pg_net 을 켠다.
-- 아래 declare 의 두 값만 바꾸고 통째로 실행한다. 몇 번 다시 실행해도 안전하다.
-- ============================================================
do $$
declare
  app    text := 'biseojin.vercel.app';   -- ← Vercel 앱 주소 (https:// 와 끝의 / 없이)
  secret text := '여기에_CRON_SECRET';     -- ← Vercel 환경변수 CRON_SECRET 과 똑같은 값
  sid    uuid;
begin
  if app like '%<%' or app like 'http%' or app like '%/%' then
    raise exception '앱 주소를 다시 확인해 주세요: % (예: biseojin.vercel.app)', app;
  end if;
  if secret like '%<%' or secret = '여기에_CRON_SECRET' then
    raise exception 'CRON_SECRET 값을 넣어 주세요';
  end if;

  -- 비밀값: Vault 에 두고 꺼내 쓴다 (SQL 기록·cron.job 에 평문이 남지 않게)
  select id into sid from vault.secrets where name = 'biseo_cron_secret';
  if sid is null then
    perform vault.create_secret(secret, 'biseo_cron_secret');
  else
    perform vault.update_secret(sid, secret);
  end if;

  -- 예전에 자리표시자(<YOUR_APP>)째로 등록됐거나 같은 이름인 작업은 지우고 다시 건다
  perform cron.unschedule(jobid) from cron.job
   where command like '%<YOUR_APP>%' or jobname in ('biseo-tick', 'biseo-due');

  perform cron.schedule('biseo-due', '* * * * *', format($job$
    select net.http_get(
      url := 'https://%s/api/cron/tick?only=due',
      headers := jsonb_build_object('Authorization', 'Bearer ' ||
        (select decrypted_secret from vault.decrypted_secrets where name = 'biseo_cron_secret')),
      timeout_milliseconds := 20000);
  $job$, app));

  perform cron.schedule('biseo-tick', '*/2 * * * *', format($job$
    select net.http_get(
      url := 'https://%s/api/cron/tick',
      headers := jsonb_build_object('Authorization', 'Bearer ' ||
        (select decrypted_secret from vault.decrypted_secrets where name = 'biseo_cron_secret')),
      timeout_milliseconds := 55000);
  $job$, app));
end $$;

-- 확인 (1~2분 뒤): status_code 가 200 이면 정상
--   select jobname, schedule, active from cron.job;
--   select status_code, left(content::text, 200), created from net._http_response order by created desc limit 5;
--   401 → 위 secret 이 Vercel 의 CRON_SECRET 과 다름 / 404 → 앱 주소가 틀림
