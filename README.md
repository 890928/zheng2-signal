# 正二兩桶訊號

00631L / 0050 兩桶規則的訊號網站。純靜態網頁，放在 GitHub Pages（免費），
由 GitHub Actions 每個交易日收盤後自動抓行情、重算訊號與近 10 年回測。

## 上線步驟（約 5 分鐘，全部免費）

1. 在 GitHub 建一個新的 **Public** repository，例如 `zheng2-signal`。不要勾選建立 README。
2. 在這個資料夾裡執行（把 `<帳號>` 換成你的 GitHub 帳號）：

   ```bash
   git init -b main
   git add .
   git commit -m "init"
   git remote add origin https://github.com/<帳號>/zheng2-signal.git
   git push -u origin main
   ```

3. 到 repo 的 **Settings → Pages**，把 **Source** 選成 **GitHub Actions**。
4. 到 **Actions** 分頁，點左邊的「更新資料並部署」→ **Run workflow**。
   （第一次 push 時自動跑的那一次，如果在步驟 3 之前執行會部署失敗，這是正常的，重跑一次即可。）
5. 跑完後網址是 `https://<帳號>.github.io/zheng2-signal/`。
   手機用 Safari 或 Chrome 開啟，選「加入主畫面」，之後就像 App 一樣點開。

## 自動更新

`.github/workflows/update.yml` 的排程（台北時間）：

| 時間 | 用途 |
|---|---|
| 週一到週五 14:10 | 收盤後的主要更新 |
| 週一到週五 18:40 | 補跑 |
| 週二到週六 07:40 | 補跑 |

- GitHub 的排程常會延遲幾分鐘到半小時，屬正常。
- 抓不到行情時不會弄壞網站：會沿用上一次的資料，頁面上方一直顯示「資料日」；超過 5 天沒更新會出現提醒。
- 想立刻更新：Actions →「更新資料並部署」→ Run workflow。
- 如果 repo 連續 60 天沒有任何活動，GitHub 會停用排程；到 Actions 頁面按 Enable 即可恢復。

## 檔案

| 路徑 | 說明 |
|---|---|
| `index.html` | 整個網站（今日、試算、走勢、回測、規則五個分頁） |
| `scripts/update.py` | 抓行情、算訊號、跑滾動 10 年回測，寫出 `data/data.json`。只用 Python 標準函式庫 |
| `data/data.json` | 每天自動產生的資料 |
| `data/stress.json` | 2000 年至今的壓力測試（固定結果，不會自動更新） |
| `assets/` | 圖示與 d3 圖表函式庫（已內含，不依賴外部 CDN） |

## 本機預覽

```bash
python3 scripts/update.py      # 更新資料（Mac 若出現憑證錯誤：pip3 install certifi）
python3 -m http.server 8000    # 開 http://localhost:8000
```

## 改規則

規則參數在 `scripts/update.py` 最上面（`LADDER`、`MA_N`、`BUF` 等）。改了之後訊號和近 10 年回測會一起重算；
壓力測試（`data/stress.json`）和頁面上的規則說明文字不會跟著變，需要另外更新。

## 注意

- 網站是公開的，知道網址的人都能看（已設定不讓搜尋引擎收錄）。免費方案的 GitHub Pages 需要 Public repo。
- 你在「試算」「回測」填的持股與本金只存在自己裝置的瀏覽器，不會上傳，別人看不到。
- 價格資料來自 Yahoo Finance。這個網站只是把固定規則算出來，不是投資建議；回測不代表未來。
