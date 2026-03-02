#!/usr/bin/env python3
"""
Shioaji Read-Only Guard

用途：
1) 登入 Shioaji（只做連線測試）
2) 嘗試呼叫查詢資料 API（可讀）
3) 明確禁止任何下單函式（程式層防呆）

使用方式：
  export SINO_API_KEY='...'
  export SINO_SECRET_KEY='...'
  python3 scripts/shioaji_readonly_guard.py

注意：
- 本腳本不會呼叫 place_order，也不會 activate_ca。
- 若你未安裝 shioaji：pip install shioaji
"""

import os
import sys


def fail(msg: str, code: int = 1):
    print(f"[FAIL] {msg}")
    sys.exit(code)


def ok(msg: str):
    print(f"[OK] {msg}")


def main():
    api_key = os.getenv("SINO_API_KEY")
    secret_key = os.getenv("SINO_SECRET_KEY")

    if not api_key or not secret_key:
        fail("請先設定環境變數 SINO_API_KEY / SINO_SECRET_KEY")

    try:
        import shioaji as sj
    except Exception as e:
        fail(f"無法匯入 shioaji：{e}. 請先 pip install shioaji")

    api = sj.Shioaji(simulation=False)

    # ---- 防呆：封鎖任何可能下單的入口 ----
    def _blocked(*args, **kwargs):
        raise RuntimeError("Read-only guard: place_order blocked")

    # 常見下單方法（若 SDK 命名異動，至少主要入口會被擋）
    if hasattr(api, "place_order"):
        setattr(api, "place_order", _blocked)

    if hasattr(api, "place_comboorder"):
        setattr(api, "place_comboorder", _blocked)

    # ---- 登入測試 ----
    try:
        accounts = api.login(api_key=api_key, secret_key=secret_key)
        ok(f"登入成功，accounts={len(accounts) if accounts else 0}")
    except Exception as e:
        fail(f"登入失敗：{e}")

    # ---- 只讀測試：抓合約與快照（不涉及下單） ----
    try:
        # 小台近月（若欄位變動，這段可能需依版本調整）
        mtx = api.Contracts.Futures.MXF.MXF202503 if hasattr(api.Contracts.Futures, 'MXF') else None
        if mtx is None:
            # 退而求其次抓任何可用 futures 合約
            # 只要能取到合約即表示行情查詢可用
            futures_root = api.Contracts.Futures
            names = [n for n in dir(futures_root) if not n.startswith('_')]
            if not names:
                fail("找不到任何期貨合約節點，請檢查權限")
            ok(f"可讀取期貨合約節點，nodes={names[:5]}")
        else:
            snaps = api.snapshots([mtx])
            ok(f"可讀取期貨快照，筆數={len(snaps)}")
    except Exception as e:
        print(f"[WARN] 只讀測試（合約/快照）發生例外：{e}")
        print("[WARN] 這不一定代表沒權限，可能是合約月份或 API 版本差異。")

    # ---- 額外安全提示 ----
    print("\n[SAFEGUARD] 本腳本未啟用 CA，且已封鎖 place_order。")
    print("[SAFEGUARD] 若要進一步驗證『不可交易』，建議在永豐後台確認交易權限關閉。")

    try:
        api.logout()
    except Exception:
        pass


if __name__ == "__main__":
    main()
