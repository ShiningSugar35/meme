from __future__ import annotations
import argparse, asyncio, gzip, json, time
from dotenv import dotenv_values
from backend.app.collector import ApiKeyRoles, AsyncRateLimiter, CollectorEndpoints, GMGNDataClient, GMGNEnrichmentProvider, HttpxTransport
from backend.app.config import PROJECT_ROOT, get_settings
from backend.app.database import Database
from backend.app.services.platform_configuration import PlatformConfigurationService

async def main():
    p=argparse.ArgumentParser(); p.add_argument('--rps',type=float,default=1.0); p.add_argument('--concurrency',type=int,default=4); p.add_argument('--out',default='artifacts/research/kline_cache_v3_2h.json.gz'); a=p.parse_args()
    db=Database(get_settings().sqlite_path)
    rows=[dict(r) for r in db.fetch_all("""SELECT id,address,entry_time,entry_price,tag FROM samples WHERE label_status='mature' AND tag IN (0,1) AND token_type IN ('new_creation','near_completion') AND feature_schema_version='event1m_regime_v3' ORDER BY entry_time,id""")]
    cfg=PlatformConfigurationService(db); roles=ApiKeyRoles.from_secrets(cfg.provider_credentials('gmgn')); env=dotenv_values(PROJECT_ROOT/'.env')
    ep=CollectorEndpoints(trenches=env.get('GMGN_TRENCHES_PATH','/v1/trenches'),token_info=env.get('GMGN_TOKEN_INFO_PATH','/v1/token/info'),token_security=env.get('GMGN_TOKEN_SECURITY_PATH','/v1/token/security'),token_pool_info=env.get('GMGN_TOKEN_POOL_INFO_PATH','/v1/token/pool_info'),top_holders=env.get('GMGN_TOKEN_HOLDERS_PATH','/v1/market/token_top_holders'),kline=env.get('GMGN_KLINE_PATH','/v1/market/token_kline'),trending=env.get('GMGN_TRENDING_PATH','/v1/market/rank'),signal=env.get('GMGN_SIGNAL_PATH','/v1/market/token_signal'),hot_searches=env.get('GMGN_HOT_SEARCHES_PATH','/v1/market/hot_searches'),created_tokens=env.get('GMGN_PORTFOLIO_CREATED_TOKENS_PATH','/v1/user/created_tokens'))
    tr=HttpxTransport(); client=GMGNDataClient(base_url=env.get('GMGN_API_BASE_URL',''),transport=tr,limiter=AsyncRateLimiter(a.rps),endpoints=ep); provider=GMGNEnrichmentProvider(client,roles,primary_attempts=2,primary_retry_seconds=2.0,fallback_delay_seconds=2.0)
    sem=asyncio.Semaphore(a.concurrency); lock=asyncio.Lock(); out={}; errors=[]; done=0; started=time.time()
    async def one(row):
        nonlocal done
        async with sem:
            try:
                et=int(row['entry_time']); vals=await provider.klines(str(row['address']),et,et+7200); bars=[[int(k.timestamp),k.open,k.high,k.low,k.close] for k in vals]
                async with lock: out[str(row['id'])]={'row':row,'bars':bars}
            except Exception as exc:
                async with lock: errors.append({'id':row['id'],'error':f'{type(exc).__name__}: {exc}'[:300]})
            finally:
                async with lock:
                    done+=1
                    if done%50==0 or done==len(rows): print(json.dumps({'progress':done,'total':len(rows),'ok':len(out),'errors':len(errors),'elapsed_s':round(time.time()-started,1)}),flush=True)
    await asyncio.gather(*(one(r) for r in rows)); await tr.close()
    if errors: raise RuntimeError(f'kline errors={len(errors)} first={errors[:3]}')
    path=PROJECT_ROOT/a.out; path.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(path,'wt',encoding='utf-8',compresslevel=6) as f: json.dump({'sample_count':len(rows),'items':out},f,ensure_ascii=False,separators=(',',':'))
    print(json.dumps({'saved':str(path),'samples':len(out)},ensure_ascii=False),flush=True)
if __name__=='__main__': asyncio.run(main())
