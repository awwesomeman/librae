#!/usr/bin/env python3
import os, json
import numpy as np
import pandas as pd
import shioaji as sj


def resample(df, rule):
    x = pd.DataFrame()
    x['open'] = df['open'].resample(rule).first()
    x['high'] = df['high'].resample(rule).max()
    x['low'] = df['low'].resample(rule).min()
    x['close'] = df['close'].resample(rule).last()
    x['volume'] = df['volume'].resample(rule).sum()
    return x.dropna()


def add_h1_features(h1):
    o = h1.copy()
    o['ema20'] = o['close'].ewm(span=20, adjust=False).mean()
    o['ema60'] = o['close'].ewm(span=60, adjust=False).mean()
    tr = pd.concat([
        o['high'] - o['low'],
        (o['high'] - o['close'].shift(1)).abs(),
        (o['low'] - o['close'].shift(1)).abs(),
    ], axis=1).max(axis=1)
    o['atr14'] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    o['vol_sma20'] = o['volume'].rolling(20).mean()
    o['ret20'] = o['close'].pct_change(20)
    o['ret60'] = o['close'].pct_change(60)
    o['atrp'] = o['atr14'] / o['close']
    roll_max = o['close'].rolling(40).max()
    o['dd40'] = (roll_max - o['close']) / roll_max
    o['hh'] = (o['high'] > o['high'].shift(1)).astype(float)
    o['hl'] = (o['low'] > o['low'].shift(1)).astype(float)
    return o


def factor_score(row):
    trend = 100.0 if row['close'] > row['ema20'] > row['ema60'] else (60.0 if row['close'] > row['ema20'] else 20.0)
    mom = 50.0
    if not np.isnan(row['ret20']) and not np.isnan(row['ret60']):
        mom = np.clip(50 + (0.6*row['ret20'] + 0.4*row['ret60']) * 1000, 0, 100)
    dd = np.clip(row['dd40'] if not np.isnan(row['dd40']) else 0.2, 0, 0.2)
    dd_score = 100*(1-dd/0.2)
    structure = np.clip(0.7*dd_score + 30*row['hh'] + 30*row['hl'], 0, 100)
    volq = 40.0 if np.isnan(row['vol_sma20']) or row['vol_sma20']<=0 else np.clip((row['volume']/row['vol_sma20'])*70,0,100)
    vp = 50.0 if np.isnan(row['atrp']) else np.clip(100-row['atrp']*5000,0,100)
    return 0.30*trend + 0.25*mom + 0.20*structure + 0.15*volq + 0.10*vp


def run_bt(m1, h1f, d1, start, end, cost_points=2.0, threshold=75, bn=3, en=10):
    h = h1f[(h1f.index >= start) & (h1f.index <= end)]
    rets=[]
    pos=None
    for i in range(70, len(h)-1):
        cur=h.iloc[i]; prev=h.iloc[i-1]; t=h.index[i]; nt=h.index[i+1]
        if pos is not None:
            w = m1[(m1.index>pos['last']) & (m1.index<=t)]
            for _,r in w.iterrows():
                if r['low']<=pos['stop']:
                    rets.append((pos['stop']-pos['entry'])/pos['entry'] - cost_points/pos['entry']); pos=None; break
                if (not pos['t1d']) and r['high']>=pos['t1']:
                    pos['t1d']=True; pos['part']=0.5*((pos['t1']-pos['entry'])/pos['entry'])
                if r['high']>=pos['t2']:
                    rem=0.5 if pos['t1d'] else 1.0
                    rets.append(pos['part'] + rem*((pos['t2']-pos['entry'])/pos['entry']) - cost_points/pos['entry']); pos=None; break
            if pos is not None:
                pos['bars'] += 1
                if pos['bars']>=6 or cur['close']<cur['ema20']:
                    rets.append((cur['close']-pos['entry'])/pos['entry'] - cost_points/pos['entry']); pos=None
                else:
                    pos['last']=t
        if pos is not None: continue

        day=t.floor('D')-pd.Timedelta(days=1)
        if day not in d1.index: continue
        d=d1.loc[day]
        trend_gate=(d['close']>d['ema20']) and (d['ema20']>d['ema20_prev'])
        setup = trend_gate and (abs(cur['low']-cur['ema20'])<=0.3*cur['atr14']) and (cur['close']>cur['open']) and (cur['close']>prev['high'])
        if not setup: continue
        if factor_score(cur) < threshold: continue

        ew = m1[(m1.index>t)&(m1.index<=nt)].copy()
        if len(ew) < max(en+2,bn+2): continue
        ew['ema']=ew['close'].ewm(span=en, adjust=False).mean()
        ew['hh']=ew['high'].rolling(bn).max().shift(1)
        trg=None
        for ts,r in ew.iterrows():
            if np.isnan(r['ema']) or np.isnan(r['hh']): continue
            if r['close']>r['hh'] and r['close']>r['ema']:
                trg=(ts,float(r['close'])); break
        if trg is None: continue
        ts,entry=trg
        stop=float(cur['low']-0.2*cur['atr14'])
        risk=entry-stop
        if risk<=0: continue
        pos={'entry':entry,'stop':stop,'t1':entry+1.5*risk,'t2':entry+2.2*risk,'bars':0,'t1d':False,'part':0.0,'last':ts}

    if not rets: return {'trades':0}
    arr=np.array(rets)
    wins=(arr>0).sum(); gp=arr[arr>0].sum(); gl=-arr[arr<0].sum(); pf=gp/gl if gl>0 else np.nan
    eq=np.cumprod(1+arr); peak=np.maximum.accumulate(eq); mdd=np.max((peak-eq)/peak)
    years=max((pd.Timestamp(end)-pd.Timestamp(start)).days/365.25, 1e-6)
    ann=eq[-1]**(1/years)-1
    tpy=len(arr)/years
    vol=arr.std(ddof=1)*np.sqrt(tpy) if len(arr)>1 else np.nan
    sharpe=(arr.mean()/arr.std(ddof=1))*np.sqrt(tpy) if len(arr)>1 and arr.std(ddof=1)>0 else np.nan
    return {'trades':int(len(arr)),'win_rate':float(wins/len(arr)),'avg_ret':float(arr.mean()),'pf':float(pf) if not np.isnan(pf) else None,'mdd':float(mdd),'equity':float(eq[-1]),'ann_return':float(ann),'ann_sharpe':float(sharpe) if not np.isnan(sharpe) else None,'ann_vol':float(vol) if not np.isnan(vol) else None}


def main():
    key=os.getenv('SINO_API_KEY'); sec=os.getenv('SINO_SECRET_KEY')
    api=sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)
    kb=api.kbars(api.Contracts.Futures.MXF.MXFR1,start='2024-01-01',end='2026-03-02')
    api.logout()

    df=pd.DataFrame({**kb})
    df['ts']=pd.to_datetime(df['ts'])
    df=df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume'}).set_index('ts').sort_index()
    m1=df[['open','high','low','close','volume']]
    h1f=add_h1_features(resample(m1,'60min'))
    d1=resample(m1,'1D'); d1['ema20']=d1['close'].ewm(span=20,adjust=False).mean(); d1['ema20_prev']=d1['ema20'].shift(1)

    # walk-forward windows
    wf = [
        ('2024-01-01','2024-09-30','2024-10-01','2024-12-31'),
        ('2024-04-01','2024-12-31','2025-01-01','2025-03-31'),
        ('2024-07-01','2025-03-31','2025-04-01','2025-06-30'),
        ('2024-10-01','2025-06-30','2025-07-01','2025-09-30'),
        ('2025-01-01','2025-09-30','2025-10-01','2026-03-02'),
    ]

    param_grid=[]
    for th in [65,70,75]:
        for bn in [3,5]:
            for en in [10,20]:
                param_grid.append((th,bn,en))

    wf_results=[]
    for tr_s,tr_e,te_s,te_e in wf:
        best=None
        for th,bn,en in param_grid:
            m=run_bt(m1,h1f,d1,tr_s,tr_e,2.0,th,bn,en)
            if m.get('trades',0)<10: continue
            score=(m.get('ann_sharpe') or -9) - 2.0*m.get('mdd',0)
            if best is None or score>best[0]: best=(score,th,bn,en,m)
        if best is None:
            wf_results.append({'test_period':f'{te_s}~{te_e}','status':'no_valid_param'})
            continue
        _,th,bn,en,trainm = best
        testm=run_bt(m1,h1f,d1,te_s,te_e,2.0,th,bn,en)
        wf_results.append({'test_period':f'{te_s}~{te_e}','params':{'th':th,'bn':bn,'en':en},'test':testm})

    # stability around anchor params
    anchor=(75,3,10)
    stab=[]
    for th in [70,75,80]:
        for bn in [3,5]:
            for en in [10,15]:
                m=run_bt(m1,h1f,d1,'2025-07-01','2026-03-02',2.0,th,bn,en)
                stab.append({'th':th,'bn':bn,'en':en,'ann':m.get('ann_return'),'mdd':m.get('mdd'),'trades':m.get('trades')})

    # cost stress on anchor
    cost_stress={}
    for c in [2.0,3.0,4.0]:
        cost_stress[str(c)] = run_bt(m1,h1f,d1,'2025-07-01','2026-03-02',c,*anchor)

    out={'strategy':'MultiFactorScore_v1.0-H1-L-TSI','wf':wf_results,'stability':stab,'cost_stress':cost_stress}
    print(json.dumps(out,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
