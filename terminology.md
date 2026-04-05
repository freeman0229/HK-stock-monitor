# 港股看板 — 數據術語對照表

## 1. 成交股數 & 成交金額
**Source:** HKEX Daily Quotation
`https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm`

| 中文 | 英文欄位 | 代碼 | 說明 |
|------|----------|------|------|
| 成交股數 | SHARES TRADED | `vol` | 當日換手的股份數量 |
| 成交金額 | TURNOVER ($) | `tv` | 當日成交的港元總值 |
| 收市價 | CLOSING RATE | `close` | 當日收市價格 |

---

## 2. 沽空股數 & 沽空金額
**Source:** HKEX Daily Quotation — 上日經調整賣空成交 section
`https://www.hkex.com.hk/chi/stat/smstat/dayquot/d{YYMMDD}c.htm#adj_short`

> ⚠️ 注意：文件所載為**前一個交易日**的沽空數據

| 中文 | 英文欄位 | 代碼 | 說明 |
|------|----------|------|------|
| 沽空股數 | SH | `sv` / `short_volume` | 當日總賣空成交量（股數） |
| 沽空金額 | $ | `st` / `short_turnover` | 當日總賣空成交值（港元） |

**衍生欄位：**
- `short_ratio` = 沽空股數 / 成交股數 × 100（%）

---

## 3. 累積沽空股數 & 累積沽空金額
**Source:** SFC Aggregated Reportable Short Positions CSV（每週五發布）
`https://www.sfc.hk/-/media/EN/pdf/spr/{YYYY}/{MM}/{DD}/Short_Position_Reporting_Aggregated_Data_{YYYYMMDD}.csv`

| 中文 | CSV 欄位 | 代碼 | 說明 |
|------|----------|------|------|
| 累積沽空股數 | Aggregated Reportable Short Positions (Shares) | `sfc_sh` | 申報的累積淨空倉股數 |
| 累積沽空金額 | Aggregated Reportable Short Positions (HK$) | `sfc_hkd` | 申報的累積淨空倉港元值 |

**衍生欄位：**
- `sfc_pct` = 累積沽空股數 / 總數 × 100（%）
- `sfc_week_delta` = 本週 sfc_pct − 上週 sfc_pct（pp）
- `sfc_hkd_delta` = 本週 sfc_hkd − 上週 sfc_hkd（港元）
- `sfc_level_dev` = sfc_pct − 4週滾動平均（pp）

---

## 4. 總數 & 佔已發行股份百分比
**Source:** HKEX 中央結算系統持股紀錄查詢服務
`https://www3.hkexnews.hk/sdw/search/searchsdw_c.aspx`

| 中文 | 代碼 | 說明 |
|------|------|------|
| 總數 | `total_sh` / `_sdw_total_sh_map` | 於中央結算系統的持股量（總數）|
| 佔已發行股份/權證/單位百分比 | `h.pct` / `pct_listed` | 各持倉人/總數 佔已發行股份的百分比 |
| 已發行股份/權證/單位（最近更新數目） | — | 全部已發行股份數量，**不直接存儲** |

> ⚠️ **重要：總數 ≠ 已發行股份**
> - **總數**（如：2,554,802,988）= 存放於 CCASS 的股份，即代碼中的 `total_sh`
> - **已發行股份**（如：3,830,044,500）= 全部已發行股份，數值更大
> - 總數 ≤ 已發行股份（並非所有已發行股份均存放於 CCASS）

**用途：**
- 換手率：成交股數之和 / 總數 × 100
- 持倉集中度：前5大持股量之和 / 總數 × 100
- sfc_pct：累積沽空股數 / 總數 × 100
