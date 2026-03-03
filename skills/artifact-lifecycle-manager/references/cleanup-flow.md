# Cleanup Flow (Safe)

## Phase 1: Audit (required)
輸出 keep/archive/delete 清單，並附原因標籤：
- duplicate
- superseded
- invalid-output
- stale-generated
- unused-script

## Phase 2: Confirm
取得使用者明確確認後才執行。

## Phase 3: Execute
- archive -> `archive/YYYYMMDD/`
- delete -> `archive/YYYYMMDD/trash/`（先可恢復）

## Phase 4: Record
更新：
- 清理審查報告（執行記錄）
- TODO 任務狀態
- 受影響路徑（若有）
