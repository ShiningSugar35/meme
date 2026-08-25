from __future__ import annotations
import gzip,json,statistics
from collections import Counter,deque
from pathlib import Path
from backend.app.config import PROJECT_ROOT

CACHE=PROJECT_ROOT/'artifacts/research/kline_cache_v3_2h.json.gz'
OUT=PROJECT_ROOT/'artifacts/research/label_policy_grid.json'
TPS=(1.6,1.8,2.0); SLS=(0.90,0.80,0.75,0.70); WINDOWS=(60,90,120); DD=(0.20,0.25,0.30); LBS=(5,10,15)

def vals(b):
    ts=int(b[0]); c=float(b[4] or 0); return ts,float(b[2] if b[2] is not None else c),float(b[3] if b[3] is not None else c)

def replay(item,tp,sl,window,dd=None,lb=None):
    row=item['row']; entry=float(row['entry_price']); et=int(row['entry_time']); end=et+window*60
    bars=[b for b in item['bars'] if et<=int(b[0])<=end]
    if not bars:return None
    prior=deque(); mx=0.0; mn=float('inf'); conflict=False
    for i,b in enumerate(bars):
        ts,hi,lo=vals(b)
        if hi<=0 or lo<=0:continue
        trailing=False; peak=None
        if dd is not None:
            cutoff=ts-lb*60
            while prior and prior[0][0]<cutoff:prior.popleft()
            if prior:
                peak=max(v for _,v in prior); trailing=lo<=peak*(1-dd)
        hard=lo<=entry*sl; hit=hi>=entry*tp
        if hit and (hard or trailing):conflict=True
        mx=max(mx,hi/entry); mn=min(mn,lo/entry)
        if hard or trailing:
            future=max((vals(x)[1] for x in bars[i+1:]),default=0.0)
            proxy_ret=(sl-1.0) if hard else (peak*(1-dd)/entry-1.0)
            return {'tag':0,'reason':'hard_sl' if hard else 'trailing_sl','exit_min':(ts-et)/60,'mfe':mx-1,'mae':mn-1,'future_tp':future>=entry*tp,'conflict':conflict,'peak_ratio':peak/entry if peak else None,'proxy_ret':proxy_ret}
        if hit:return {'tag':1,'reason':'tp','exit_min':(ts-et)/60,'mfe':mx-1,'mae':mn-1,'future_tp':False,'conflict':conflict,'peak_ratio':peak/entry if peak else None,'proxy_ret':tp-1.0}
        if dd is not None:prior.append((ts,hi))
    last_close=float(bars[-1][4] or 0.0)
    return {'tag':0,'reason':'timeout','exit_min':(int(bars[-1][0])-et)/60,'mfe':mx-1,'mae':mn-1,'future_tp':False,'conflict':conflict,'peak_ratio':None,'proxy_ret':last_close/entry-1.0 if last_close>0 else None}

def one_policy(items,tp,sl,window,dd=None,lb=None):
    pairs=[]
    ordered_items=sorted(items.values(),key=lambda x:(int(x['row']['entry_time']),int(x['row']['id'])))
    for item in ordered_items:
        o=replay(item,tp,sl,window,dd,lb)
        if o:pairs.append((item['row'],o))
    n=len(pairs); oldpos=sum(int(r['tag'])==1 for r,_ in pairs); oldneg=n-oldpos; pos=sum(o['tag']==1 for _,o in pairs)
    retain=sum(int(r['tag'])==1 and o['tag']==1 for r,o in pairs); convert=sum(int(r['tag'])==0 and o['tag']==1 for r,o in pairs)
    reasons=Counter(o['reason'] for _,o in pairs); trail=[o for _,o in pairs if o['reason']=='trailing_sl']; stops=[o for _,o in pairs if o['reason'] in ('trailing_sl','hard_sl')]; positives=[o for _,o in pairs if o['tag']==1]
    proxy=[o['proxy_ret'] for _,o in pairs if o.get('proxy_ret') is not None]
    recent_start=3*n//4; recent=pairs[recent_start:]; recent_proxy=[o['proxy_ret'] for _,o in recent if o.get('proxy_ret') is not None]
    recent_rate=sum(o['tag']==1 for _,o in recent)/len(recent)
    be=(1.0-sl)/((tp-1.0)+(1.0-sl))
    return {'tp':tp,'sl':sl,'window':window,'dd':dd,'lb':lb,'n':n,'pos':pos,'rate':pos/n,'recent_n':len(recent),'recent_rate':recent_rate,'recent_rate_minus_be':recent_rate-be,'barrier_be_precision':be,'base_rate_minus_be':pos/n-be,'retain_n':retain,'retain_rate':retain/oldpos,'convert_n':convert,'convert_rate':convert/oldneg,'old1_to_0':oldpos-retain,'tp_exit':reasons['tp'],'hard_exit':reasons['hard_sl'],'trail_exit':reasons['trailing_sl'],'timeout_exit':reasons['timeout'],'trail_future_tp_n':sum(o['future_tp'] for o in trail),'trail_future_tp_rate':sum(o['future_tp'] for o in trail)/len(trail) if trail else 0.0,'stop_future_tp_n':sum(o['future_tp'] for o in stops),'conflict_n':sum(o['conflict'] for _,o in pairs),'pos_med_exit_min':statistics.median(o['exit_min'] for o in positives) if positives else None,'pos_med_mae':statistics.median(o['mae'] for o in positives) if positives else None,'pos_p25_mae':sorted(o['mae'] for o in positives)[int(.25*(len(positives)-1))] if positives else None,'stop_med_mfe':statistics.median(o['mfe'] for o in stops) if stops else None,'proxy_mean_ret':statistics.fmean(proxy) if proxy else None,'proxy_median_ret':statistics.median(proxy) if proxy else None,'recent_proxy_mean_ret':statistics.fmean(recent_proxy) if recent_proxy else None}

def main():
    with gzip.open(CACHE,'rt',encoding='utf-8') as f:payload=json.load(f)
    items=payload['items']; results=[]
    for tp in TPS:
      for sl in SLS:
       for w in WINDOWS:
        results.append(one_policy(items,tp,sl,w))
        for dd in DD:
         for lb in LBS:results.append(one_policy(items,tp,sl,w,dd,lb))
    baseline=next(x for x in results if x['tp']==1.6 and x['sl']==.9 and x['window']==60 and x['dd'] is None)
    no_trail=sorted((x for x in results if x['dd'] is None),key=lambda x:(x['rate'],x['retain_rate']),reverse=True)
    with_trail=sorted((x for x in results if x['dd'] is not None),key=lambda x:(x['rate'],x['retain_rate'],-x['trail_future_tp_rate']),reverse=True)
    viable=sorted((x for x in results if .05<=x['rate']<=.25),key=lambda x:(x['retain_rate'],-x['trail_future_tp_rate'],x['rate']),reverse=True)
    # Pareto: maximize rate/retention, minimize trailing false-kill.
    pareto=[]
    for a in results:
        dominated=False
        for b in results:
            if a is b:continue
            if b['rate']>=a['rate'] and b['retain_rate']>=a['retain_rate'] and b['trail_future_tp_rate']<=a['trail_future_tp_rate'] and (b['rate']>a['rate'] or b['retain_rate']>a['retain_rate'] or b['trail_future_tp_rate']<a['trail_future_tp_rate']): dominated=True; break
        if not dominated:pareto.append(a)
    out={'sample_count':payload['sample_count'],'policy_count':len(results),'baseline':baseline,'best_no_trail':no_trail[:20],'best_with_trail':with_trail[:20],'viable_5_25pct':viable[:50],'pareto':pareto,'all':results}
    OUT.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'samples':payload['sample_count'],'policies':len(results),'baseline':baseline,'top_no_trail':no_trail[:8],'top_with_trail':with_trail[:8],'viable':viable[:12]},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
