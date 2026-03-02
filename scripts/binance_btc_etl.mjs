#!/usr/bin/env node
import fs from 'fs/promises';
import path from 'path';

const BASE = 'https://api.binance.com';
const SYMBOL = 'BTCUSDT';
const INTERVALS = ['1h', '4h', '1d'];
const LIMIT = 1000; // per request
const TOTAL_BARS = {
  '1h': 5000,
  '4h': 3000,
  '1d': 1500,
};

const outDir = path.resolve(process.cwd(), 'data', 'binance');

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function fetchKlines({ symbol, interval, endTime }) {
  const params = new URLSearchParams({
    symbol,
    interval,
    limit: String(LIMIT),
  });
  if (endTime) params.set('endTime', String(endTime));

  const url = `${BASE}/api/v3/klines?${params.toString()}`;
  const res = await fetch(url);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`Binance HTTP ${res.status}: ${txt.slice(0, 200)}`);
  }
  return await res.json();
}

function toCsv(rows) {
  const header = [
    'open_time',
    'open',
    'high',
    'low',
    'close',
    'volume',
    'close_time',
    'quote_asset_volume',
    'number_of_trades',
    'taker_buy_base_volume',
    'taker_buy_quote_volume',
    'ignore',
    'open_datetime_utc',
    'close_datetime_utc',
  ];

  const lines = [header.join(',')];
  for (const r of rows) {
    const openTs = Number(r[0]);
    const closeTs = Number(r[6]);
    lines.push([
      r[0], r[1], r[2], r[3], r[4], r[5],
      r[6], r[7], r[8], r[9], r[10], r[11],
      new Date(openTs).toISOString(),
      new Date(closeTs).toISOString(),
    ].join(','));
  }
  return lines.join('\n');
}

async function pullInterval(interval) {
  const need = TOTAL_BARS[interval] ?? 2000;
  let all = [];
  let endTime = undefined;

  while (all.length < need) {
    const batch = await fetchKlines({ symbol: SYMBOL, interval, endTime });
    if (!Array.isArray(batch) || batch.length === 0) break;

    // prepend older->newer handling by walking backwards with endTime
    all = batch.concat(all);

    const firstOpen = Number(batch[0][0]);
    endTime = firstOpen - 1;

    if (batch.length < LIMIT) break;
    await sleep(120);
  }

  // de-duplicate by open time
  const map = new Map();
  for (const r of all) map.set(String(r[0]), r);
  const dedup = [...map.values()].sort((a, b) => Number(a[0]) - Number(b[0]));

  // keep only requested bars from tail
  return dedup.slice(-need);
}

async function main() {
  await fs.mkdir(outDir, { recursive: true });

  const summary = [];
  for (const interval of INTERVALS) {
    const rows = await pullInterval(interval);
    const csv = toCsv(rows);
    const file = path.join(outDir, `BTCUSDT_${interval}.csv`);
    await fs.writeFile(file, csv, 'utf8');

    const first = rows[0]?.[0];
    const last = rows[rows.length - 1]?.[0];
    summary.push({
      interval,
      bars: rows.length,
      start: first ? new Date(Number(first)).toISOString() : null,
      end: last ? new Date(Number(last)).toISOString() : null,
      file,
    });
  }

  const meta = {
    source: 'Binance Spot REST /api/v3/klines',
    symbol: SYMBOL,
    generated_at_utc: new Date().toISOString(),
    intervals: summary,
  };

  await fs.writeFile(path.join(outDir, 'metadata.json'), JSON.stringify(meta, null, 2), 'utf8');
  console.log(JSON.stringify(meta, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
