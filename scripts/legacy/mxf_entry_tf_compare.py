#!/usr/bin/env python3
import os, json
import numpy as np, pandas as pd, shioaji as sj

def resample(df, rule):
    x=pd.DataFrame()
    x['open']=df['open'].resample(rule).first();x['high']=df['high'].resample(rule).max();x['low']=df['low'].resample(rule).min();x['close']=df['close'].resample(rule).last();x['volume']=df['volume'].resample(rule).sum();
    return x.dropna()

def ind(df):
    o=df.copy();o['ema20']=o['close'].ewm(span=20,adjust=False).mean();
    tr=pd.concat([o['high']-o['low'],(o['high']-o['close'].shift(1)).abs(),(o['low']-o['close'].shift(1)).abs()],axis=1).max(axis=1)
    o['atr14']=tr.ewm(alpha=1/14,adjust=False).mean();o['vol_sma20']=o['volume'].rolling(20).mean();return o

def long_setup(h, p, d):
    trend=(d['close']>d['ema20']) and (d['ema20']>d['ema20_prev'])
    near=abs(h['low']-h['ema20'])<=0.3*h['atr14']
    bull=(h['close']>h['open']) and (h['close']>p['high'])
    vol=(h['volume']>=0.9*h['vol_sma20']) if not np.isnan(h['vol_sma20']) else False
    return trend and near and bull and vol

def run(h1,d1,m1,entry_rule):
    trades=[]; pos=None
    for i in range(30,len(h1)-1):
        cur=h1.iloc[i]; prev=h1.iloc[i-1]; t=h1.index[i]; n=h1.index[i+1]
        day=t.floor('D')
        if day not in d1.index: continue
        d=d1.loc[day]
        if pos is not None:
            win=m1[(m1.index>pos['last'])&(m1.index<=t)]
            for ts,r in win.iterrows():
                if r['low']<=pos['stop']:
                    trades.append((pos['entry'],pos['stop'])); pos=None; break
                if (not pos['t1d']) and r['high']>=pos['t1']:
                    pos['t1d']=True; pos['part']=0.5*((pos['t1']-pos['entry'])/pos['entry'])
                if r['high']>=pos['t2']:
                    rem=0.5 if pos['t1d'] else 1.0; trades.append(('ret',pos['part']+rem*((pos['t2']-pos['entry'])/pos['entry']))); pos=None; break
            if pos is not None:
                pos['bars']+=1
                if pos['bars']>=6 or cur['close']<cur['ema20']:
                    rem=0.5 if pos['t1d'] else 1.0; trades.append(('ret',pos['part']+rem*((cur['close']-pos['entry'])/pos['entry']))); pos=None
                else: pos['last']=t
        if pos is not None: continue
        if not long_setup(cur,prev,d) or cur['atr14']<=0: continue
        w=m1[(m1.index>t)&(m1.index<=n)]
        if w.empty: continue
        e=entry_rule(w)
        if e is None: continue
        ts,entry=e
        stop=float(cur['low']-0.2*cur['atr14']); risk=entry-stop
        if risk<=0: continue
        pos={'entry':entry,'stop':stop,'t1':entry+1.5*risk,'t2':entry+2.2*risk,'bars':0,'t1d':False,'part':0.0,'last':ts}

    # normalize trades
    rets=[]
    for t in trades:
      if t[0]=='ret': rets.append(t[1])
      else: rets.append((t[1]-t[0])/t[0])
    if not rets: return {'trades':0}
    arr=np.array(rets); wins=(arr>0).sum(); gp=arr[arr>0].sum(); gl=-arr[arr<0].sum(); pf=gp/gl if gl>0 else None
    eq=1;pk=1;mdd=0
    for r in arr: eq*=1+r; pk=max(pk,eq); mdd=max(mdd,(pk-eq)/pk)
    return {'trades':len(arr),'win_rate':float(wins/len(arr)),'avg_ret':float(arr.mean()),'pf':float(pf) if pf else None,'mdd':float(mdd),'eq':float(eq)}

def rule_1m(w):
    x=w.copy(); x['ema20']=x['close'].ewm(span=20,adjust=False).mean(); x['hh5']=x['high'].rolling(5).max().shift(1)
    for ts,r in x.iterrows():
        if np.isnan(r['ema20']) or np.isnan(r['hh5']): continue
        if r['close']>r['hh5'] and r['close']>r['ema20']: return ts,float(r['close'])

def mk_rule(tf):
    def f(w):
      t=resample(w,tf)
      if len(t)<6: return None
      t['ema20']=t['close'].ewm(span=20,adjust=False).mean(); t['hh3']=t['high'].rolling(3).max().shift(1)
      for ts,r in t.iterrows():
        if np.isnan(r['ema20']) or np.isnan(r['hh3']): continue
        if r['close']>r['hh3'] and r['close']>r['ema20']: return ts,float(r['close'])
      return None
    return f

key=os.environ['SINO_API_KEY'];sec=os.environ['SINO_SECRET_KEY']
api=sj.Shioaji(simulation=True);api.login(api_key=key,secret_key=sec)
kb=api.kbars(api.Contracts.Futures.MXF.MXFR1,start='2024-01-01',end='2026-03-02');api.logout()
df=pd.DataFrame({**kb});df['ts']=pd.to_datetime(df['ts']);df=df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume'}).set_index('ts').sort_index()
m1=df[['open','high','low','close','volume']]
h1=ind(resample(m1,'60min'));d1=resample(m1,'1D');d1['ema20']=d1['close'].ewm(span=20,adjust=False).mean();d1['ema20_prev']=d1['ema20'].shift(1)
res={'1m':run(h1,d1,m1,rule_1m),'15m':run(h1,d1,m1,mk_rule('15min')),'30m':run(h1,d1,m1,mk_rule('30min'))}
print(json.dumps(res,ensure_ascii=False,indent=2))
