#!/usr/bin/env node
import fs from 'fs';

const csv4h = fs.readFileSync('data/binance/BTCUSDT_4h.csv', 'utf8').trim().split('\n');
const csv1d = fs.readFileSync('data/binance/BTCUSDT_1d.csv', 'utf8').trim().split('\n');

function parse(lines){
  const h = lines[0].split(',');
  const idx = Object.fromEntries(h.map((k,i)=>[k,i]));
  return lines.slice(1).map(l=>{
    const p=l.split(',');
    return {
      t:+p[idx.open_time],
      o:+p[idx.open],h:+p[idx.high],l:+p[idx.low],c:+p[idx.close],v:+p[idx.volume]
    }
  });
}

const d4 = parse(csv4h);
const d1 = parse(csv1d);

function ema(arr, period, key='c'){
  const k = 2/(period+1);
  let out = new Array(arr.length).fill(null);
  let prev = arr[0][key];
  out[0]=prev;
  for(let i=1;i<arr.length;i++) { prev = arr[i][key]*k + prev*(1-k); out[i]=prev; }
  return out;
}
function sma(arr, period, key='v'){
  const out = new Array(arr.length).fill(null); let s=0;
  for(let i=0;i<arr.length;i++){ s += arr[i][key]; if(i>=period) s -= arr[i-period][key]; if(i>=period-1) out[i]=s/period; }
  return out;
}
function atr(arr, period=14){
  const tr = arr.map((x,i)=> i===0? x.h-x.l : Math.max(x.h-x.l, Math.abs(x.h-arr[i-1].c), Math.abs(x.l-arr[i-1].c)));
  const out = new Array(arr.length).fill(null); let prev=tr[0]; out[0]=prev; const k=1/period;
  for(let i=1;i<arr.length;i++){ prev = tr[i]*k + prev*(1-k); out[i]=prev; }
  return out;
}

const ema20_4h = ema(d4,20);
const atr14_4h = atr(d4,14);
const volSma20 = sma(d4,20,'v');
const ema20_1d = ema(d1,20);

// map daily trend to 4h bars by UTC date
const dailyMap = new Map();
for(let i=0;i<d1.length;i++) dailyMap.set(new Date(d1[i].t).toISOString().slice(0,10), {c:d1[i].c, ema:ema20_1d[i], emaPrev:i>0?ema20_1d[i-1]:ema20_1d[i]});

let trades=[];
let pos=null;
for(let i=30;i<d4.length;i++){
  const b=d4[i], p=d4[i-1];
  const day = new Date(b.t).toISOString().slice(0,10);
  const dt = dailyMap.get(day);
  if(!dt) continue;
  const trendLong = dt.c>dt.ema && dt.ema>dt.emaPrev;

  if(pos){
    pos.barsHeld++;
    const stopHit = b.l <= pos.stop;
    const t1Hit = !pos.t1Done && b.h >= pos.t1;
    const t2Hit = b.h >= pos.t2;
    const emaBreak = b.c < ema20_4h[i];
    if(stopHit){
      const loss = (pos.stop-pos.entry)/pos.entry;
      trades.push({...pos, exit:pos.stop, ret:loss, reason:'stop'}); pos=null; continue;
    }
    if(t1Hit){ pos.t1Done=true; pos.partialRet = (pos.t1-pos.entry)/pos.entry * 0.5; }
    if(t2Hit){
      const r2=(pos.t2-pos.entry)/pos.entry*(pos.t1Done?0.5:1);
      const total=(pos.partialRet||0)+r2;
      trades.push({...pos, exit:pos.t2, ret:total, reason:'target'}); pos=null; continue;
    }
    if(pos.barsHeld>=6 || emaBreak){
      const remainW = pos.t1Done?0.5:1;
      const total=(pos.partialRet||0)+((b.c-pos.entry)/pos.entry)*remainW;
      trades.push({...pos, exit:b.c, ret:total, reason:pos.barsHeld>=6?'time':'ema_break'}); pos=null; continue;
    }
  }

  if(!pos && trendLong){
    const nearEma = Math.abs(b.l-ema20_4h[i]) <= 0.3*atr14_4h[i];
    const bullish = b.c>b.o && b.c>p.h;
    const volOk = (volSma20[i]??0)>0 ? b.v>=0.9*volSma20[i] : true;
    if(nearEma && bullish && volOk){
      const stop = b.l - 0.2*atr14_4h[i];
      const risk = b.c-stop;
      if(risk<=0) continue;
      pos={entry:b.c, stop, t1:b.c+1.5*risk, t2:b.c+2.2*risk, i, barsHeld:0, t1Done:false, partialRet:0};
    }
  }
}

const n=trades.length;
const wins=trades.filter(t=>t.ret>0).length;
const avg= n? trades.reduce((a,t)=>a+t.ret,0)/n :0;
const grossP= trades.filter(t=>t.ret>0).reduce((a,t)=>a+t.ret,0);
const grossL= Math.abs(trades.filter(t=>t.ret<0).reduce((a,t)=>a+t.ret,0));
const pf = grossL>0? grossP/grossL : null;

let eq=1, peak=1, mdd=0;
for(const t of trades){ eq*= (1+t.ret); if(eq>peak) peak=eq; const dd=(peak-eq)/peak; if(dd>mdd)mdd=dd; }

console.log(JSON.stringify({
  bars4h:d4.length,
  trades:n,
  winRate:n?wins/n:0,
  avgRetPerTrade:avg,
  profitFactor:pf,
  mdd,
  equityMultiple:eq,
  sample:trades.slice(-5)
},null,2));
