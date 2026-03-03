#!/usr/bin/env python3
import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib.pyplot as plt
from pathlib import Path

base = Path('/home/jasonpan_subscribe/.openclaw/workspace/data/shioaji')
trades = pd.read_csv(base/'MXF_v2_trades.csv')
trades['entry_time'] = pd.to_datetime(trades['entry_time'])
trades['exit_time'] = pd.to_datetime(trades['exit_time'])

# rebuild 60m from cached csv
h1 = pd.read_csv(base/'MXF_60m.csv')
h1['ts'] = pd.to_datetime(h1['ts'])
h1 = h1.set_index('ts')
h1 = h1.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'})

# 1) recent 7d cleaner chart
cut = h1.index.max() - pd.Timedelta(days=7)
plot_df = h1[h1.index>=cut].copy()
tr = trades[trades['entry_time']>=cut].copy()
entry_mark = pd.Series(np.nan,index=plot_df.index)
exit_mark = pd.Series(np.nan,index=plot_df.index)
for _,t in tr.iterrows():
    et=t['entry_time'].floor('60min'); xt=t['exit_time'].floor('60min')
    if et in entry_mark.index: entry_mark.loc[et]=t['entry']
    if xt in exit_mark.index: exit_mark.loc[xt]=t['exit']
aps=[
 mpf.make_addplot(entry_mark,type='scatter',marker='^',markersize=120,color='lime'),
 mpf.make_addplot(exit_mark,type='scatter',marker='v',markersize=120,color='red')
]
fig,ax=mpf.plot(plot_df,type='candle',style='yahoo',addplot=aps,volume=True,returnfig=True,figratio=(16,9),title='MXF 最近7天訊號（放大）')
out1=base/'MXF_v2_signals_7d.png'
fig.savefig(out1,dpi=170,bbox_inches='tight')
plt.close(fig)

# 2) last trade zoom
last=trades.iloc[-1]
start=last['entry_time']-pd.Timedelta(hours=24)
end=last['exit_time']+pd.Timedelta(hours=24)
z=h1[(h1.index>=start)&(h1.index<=end)].copy()
fig,ax=mpf.plot(z,type='candle',style='yahoo',volume=True,returnfig=True,figratio=(16,9),title='MXF 最近一筆交易（放大）')
price_ax=ax[0]
price_ax.axhline(last['entry'],color='lime',linewidth=1.2,label='entry')
price_ax.axhline(last['stop'],color='orange',linewidth=1.2,label='stop')
price_ax.axhline(last['t1'],color='gray',linewidth=1.0,label='t1')
price_ax.axhline(last['t2'],color='purple',linewidth=1.0,label='t2')
price_ax.legend(loc='upper left')
out2=base/'MXF_v2_last_trade_zoom.png'
fig.savefig(out2,dpi=170,bbox_inches='tight')
plt.close(fig)

print(out1)
print(out2)
