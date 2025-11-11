# T6 公開交友群 MVP 交付總結

## Plan
- 梳理公開交友群資料模型、風控與積分消耗規則。
- 實作 `models/public_group.py`、`services/public_group_service.py`、`routers/public_group.py` 及對應 Feature Flag。
- 擴充測試（單元 + 整合）涵蓋創建、加入、置頂、風控與獎勵流程。
- 更新配置、腳本與 README，確保開發者可快速啟用與驗證。

## Changes
- **資料模型**：新增 `PublicGroup`、`PublicGroupMember`、`PublicGroupRewardClaim`、`GroupCostLedger`，整合 Feature Flag 控制載入。
- **服務層**：`services/public_group_service.py` 提供 create/join/pin/unpin/list 功能，內建風控檢查、積分扣除與獎勵池管理。
- **路由層**：`routers/public_group.py` 提供 Aiogram 指令與回呼處理，串接 Bot 菜單（旗標開啟時注入）。
- **資料庫啟動**：`models/db.py` 根據 `FLAG_ENABLE_PUBLIC_GROUPS` 動態建立表結構，並補強 `gsheet_membership_logged` 幂等表。
- **測試**：新增 `tests/test_public_group_service.py`，擴充 `tests/test_regression_features.py` 及 SQLite Decimal 適配，確保關鍵流程與回歸案例。
- **工具與文檔**：更新 `.env.example`、`README.md`（新增公開群說明與目錄）、`scripts/check_env.py` 驗證環境變數。

## Self-check
- 自動化：`pytest`（全套 25 項）於 `sqlite:///./test_all.sqlite` 通過。
- 手動驗證：
  - 本地啟動 `app.py`，確認公開群路由在 `ENABLE_PUBLIC_GROUPS=1` 時載入成功。
  - Telegram Bot 實際互動檢查主菜單、今日戰績、公開群列表顯示正常（依倉庫已有旗標設定運行）。
- 環境檢視：`scripts/check_env.py` 在補齊 `.env.example` 後無缺漏警告。

## DoD
- ✅ 模型、服務、路由與配置均在 Feature Flag 受控下可啟用或關閉。
- ✅ 主要流程（創建 → 加入 → 置頂 → 列表）具備單元／整合測試覆蓋。
- ✅ README 與 `.env.example` 含啟用公開群所需資訊，`scripts/check_env.py` 可驗證環境變數。
- ✅ 本地運行 `app.py` + Telegram Bot 實測按鈕與核心功能可用，無阻塞性 bug。

