#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日更新：抓行情 → 算兩桶訊號 → 跑滾動 10 年回測 → 寫 data/data.json

只用 Python 標準函式庫。由 .github/workflows/update.yml 每個交易日自動執行，
也可以在本機手動跑：  python3 scripts/update.py

規則
  逆勢桶（資金一半）：平時 0050；加權指數自一年高點回落 10% / 20% / 30%
                    → 桶內 30% / 60% / 100% 換成 00631L；創一年新高 → 全部換回 0050
  順勢桶（資金一半）：加權指數 > 200 日線 ×1.03 → 全部 00631L；< 200 日線 ×0.97 → 全部現金
  訊號以收盤確認、隔一個交易日調整；訊號沒變時，偏離目標超過 10 個百分點才再平衡。
"""
import datetime as dt
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ───────── 規則參數 ─────────
LADDER = [(0.10, 0.30), (0.20, 0.60), (0.30, 1.00)]   # (回落門檻, 逆勢桶內 00631L 比例)
HIGH_WIN, MA_N, BUF, BUCKET, DRIFT = 252, 200, 0.03, 0.5, 0.10
# ───────── 回測假設 ─────────
BUY, SELL, RF = 0.000855, 0.000855 + 0.001, 0.01 / 252   # 手續費 6 折、賣出加證交稅 0.1%、現金年息 1%
BT_YEARS = 10
CLEAN_FROM = "2015-09-01"     # Yahoo 的 00631L 在這之前有錯價，回測不使用
DAILY_KEEP = 750

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
OUT = os.path.join(DATA_DIR, "data.json")
# Yahoo 會對某些 User-Agent 回 429，所以準備幾個輪流試
UAS = ["Mozilla/5.0", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "curl/8.4.0"]


# ───────────────────────────── 抓資料 ─────────────────────────────
def _open(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        return urllib.request.urlopen(req, timeout=30)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLError):
            import certifi  # 本機 Mac 的 python.org 版 Python 常缺憑證：pip install certifi
            return urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context(cafile=certifi.where()))
        raise


def fetch(sym, years):
    """回傳 {dates, close, adj, live}。失敗會重試；全部失敗就丟例外（舊的 data.json 會保留）。"""
    p2 = int(time.time()) + 86400
    p1 = p2 - int(years * 365.25 * 86400)
    last = None
    for attempt in range(6):
        host = ("query1", "query2")[attempt % 2]
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
               f"?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit")
        try:
            r = json.load(_open(url, UAS[(attempt // 2) % len(UAS)]))["chart"]["result"][0]
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"抓不到 {sym}：{last}")
    off = r["meta"].get("gmtoffset", 0)
    q = r["indicators"]["quote"][0]["close"]
    a = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q
    dates, close, adj, ts = [], [], [], []
    for t, c, ac in zip(r["timestamp"], q, a):
        if c is None:
            continue
        dates.append(dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y-%m-%d"))
        close.append(float(c))
        adj.append(float(ac if ac is not None else c))
        ts.append(t)
    per = r["meta"].get("currentTradingPeriod", {}).get("regular", {})
    live = bool(per) and bool(ts) and per.get("start", 0) <= ts[-1] and time.time() < per.get("end", 0)
    now = None
    if live:                                   # 盤中未收盤的那一根不用來算訊號，只留作盤中試算的預設值
        at = r["meta"].get("regularMarketTime") or ts[-1]
        now = {"date": dates[-1], "px": round(close[-1], 2), "at": dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        dates, close, adj = dates[:-1], close[:-1], adj[:-1]
    if len(dates) < 30:
        raise RuntimeError(f"{sym} 資料筆數異常：{len(dates)}")
    return {"dates": dates, "close": close, "adj": adj, "live": now}


def last_intraday(sym, date):
    """Yahoo 盤中常缺前一個交易日的日線。用小時線補該日最後一筆價格（和正式收盤價可能差一點）。"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=5d&interval=1h"
        r = json.load(_open(url, UAS[0]))["chart"]["result"][0]
        off, px = r["meta"].get("gmtoffset", 0), None
        for ts, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
            if c is not None and dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).strftime("%Y-%m-%d") == date:
                px = float(c)
        return px
    except Exception:  # noqa: BLE001
        return None


# ───────────────────────────── 融資資料（只供參考，不影響訊號） ─────────────────────────────
TWSE = "https://www.twse.com.tw/rwd/zh/marginTrading/"
TWSE_REF = "https://www.twse.com.tw/dashboard/zh/credit/margin.html"


def fetch_json(url, referer=None, tries=2):
    last = None
    for attempt in range(tries):
        try:
            h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept": "application/json"}
            if referer:
                h["Referer"] = referer
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001  證交所會擋太頻繁的請求，回來的可能不是 JSON
            last = e
            time.sleep(5)
    raise RuntimeError(f"{url} 讀取失敗：{last}")


def _iso(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _load(name, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _save(name, obj):
    with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _merge(hist, key, rows):
    """把 rows（dict，含 date）併進以欄位陣列存的歷史，依日期去重排序。"""
    cols = [c for c in hist if c != key] or [c for c in (rows[0] if rows else {}) if c != key]
    by = {d: {c: hist[c][i] for c in cols} for i, d in enumerate(hist.get(key, []))}
    for r in rows:
        by[r[key]] = {c: r[c] for c in cols}
    dates = sorted(by)[-2500:]
    return {key: dates, **{c: [by[d][c] for d in dates] for c in cols}}


def margin_block(fetch_new=True):
    """官方擔保維持率與融資餘額（只供參考）。歷史累積在 data/*_hist.json；抓不到就用已累積的資料。"""
    keep = _load("keep_hist.json", {"date": [], "rate": [], "call": [], "exe": []})
    try:   # 證交所臺股儀表板：全市場擔保維持率、低於 130% 戶數、應處分戶數
        if not fetch_new:
            raise LookupError
        days = 60 if len(keep["date"]) < 30 else 10
        r = fetch_json(TWSE + f"BFIJ3U_TREND?response=json&days={days}", TWSE_REF)
        if r.get("stat") == "OK" and r.get("data"):
            keep = _merge(keep, "date", [{"date": _iso(x["date"]), "rate": x["keepRate"], "call": x["callAccNum"], "exe": x["exeAccNum"]} for x in r["data"]])
            _save("keep_hist.json", keep)
    except LookupError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 擔保維持率沒有更新：{e}", file=sys.stderr)

    bal = _load("margin_hist.json", {"date": [], "amt": []})
    mv = None
    try:   # 證交所：上市融資餘額（仟元）與上市總市值（億元），近 30 個交易日
        if not fetch_new:
            raise LookupError
        time.sleep(3)
        r = fetch_json(TWSE + "MI_MARGN_TREND?response=json", TWSE_REF)
        if r.get("stat") == "OK" and r.get("data"):
            if bal["date"] and _iso(r["data"][0]["date"]) > bal["date"][-1]:
                raise RuntimeError("融資餘額歷史中間有缺口")
            bal = _merge(bal, "date", [{"date": _iso(x["date"]), "amt": round(x["marginAmt"] / 1e5)} for x in r["data"]])
            mv = r["data"][-1]["marketValue"]
            _save("margin_hist.json", bal)
    except LookupError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 融資餘額沒有更新：{e}", file=sys.stderr)

    out = {"keep": {k: v[-750:] for k, v in keep.items()}}
    amt, dates = bal["amt"], bal["date"]
    if len(amt) > 21:
        chg = lambda n: round((amt[-1] / amt[-1 - n] - 1) * 100, 1)  # noqa: E731
        out["bal"] = {"date": dates[-1], "amt": amt[-1], "chg1": chg(1), "chg5": chg(5), "chg20": chg(20)}
        i0 = max(20, len(amt) - 750)
        out["m20"] = {"date": dates[i0:], "v": [round((amt[i] / amt[i - 20] - 1) * 100, 1) for i in range(i0, len(amt))]}
    return out


# ───────────────────────────── 規則重播 ─────────────────────────────
def replay(dates, closes):
    rows, lvl, on, prev = [], 0, True, None
    for i, c in enumerate(closes):
        hi = max(closes[max(0, i - HIGH_WIN + 1): i + 1])
        dd = c / hi - 1
        ma = sum(closes[i - MA_N + 1: i + 1]) / MA_N if i >= MA_N - 1 else None
        if dd >= 0:
            lvl = 0
        for j, (th, _) in enumerate(LADDER):
            if dd <= -th and j + 1 > lvl:
                lvl = j + 1
        if ma is not None:
            if c > ma * (1 + BUF):
                on = True
            elif c < ma * (1 - BUF):
                on = False
        a_l = LADDER[lvl - 1][1] if lvl else 0.0
        w = (round(BUCKET * a_l + (1 - BUCKET) * (1.0 if on else 0.0), 4), round(BUCKET * (1 - a_l), 4))
        rows.append({"date": dates[i], "close": c, "hi": hi, "dd": dd, "ma": ma, "dist": (c / ma - 1) if ma else None,
                     "lvl": lvl, "on": on, "aL": a_l, "wL": w[0], "w50": w[1], "wC": round(1 - w[0] - w[1], 4),
                     "exp": round(2 * w[0] + w[1], 3), "changed": prev is not None and w != prev})
        prev = w
    return rows


# ───────────────────────────── 回測 ─────────────────────────────
def simulate(r_l, r_1, targets, band=DRIFT):
    """targets[i] = 第 i 天收盤後的目標 (00631L 比例, 0050 比例)；隔一個交易日收盤成交。回傳每日資產。"""
    v_l = v_1 = 0.0
    cash, prev, pend, eq = 1.0, None, None, []
    for i in range(len(r_l)):
        v_l *= 1 + r_l[i]
        v_1 *= 1 + r_1[i]
        cash *= 1 + RF
        tot = v_l + v_1 + cash
        if pend is not None:
            w_l, w_1 = pend
            if prev != pend or abs(v_l / tot - w_l) > band or abs(v_1 / tot - w_1) > band:
                d_l, d_1 = w_l * tot - v_l, w_1 * tot - v_1
                cost = sum((BUY if x > 0 else SELL) * abs(x) for x in (d_l, d_1))
                v_l += d_l
                v_1 += d_1
                cash -= d_l + d_1 + cost
                prev = pend
        eq.append(v_l + v_1 + cash)
        pend = targets[i]
    return eq


def backtest(rows, p50, pl):
    by = {r["date"]: r for r in rows}
    m50, ml = dict(zip(p50["dates"], p50["adj"])), dict(zip(pl["dates"], pl["close"]))
    dates = [d for d in by if d in m50 and d in ml and d >= CLEAN_FROM]
    end = dates[-1]
    if end != rows[-1]["date"]:
        raise RuntimeError(f"ETF 日線缺 {rows[-1]['date']}")
    y, m, d = end.split("-")
    start_target = f"{int(y) - BT_YEARS}-{m}-{d}"
    if dates[0] > start_target:
        raise RuntimeError("ETF 歷史不足 10 年")
    r_l = [0.0] + [ml[b] / ml[a] - 1 for a, b in zip(dates, dates[1:])]
    r_1 = [0.0] + [m50[b] / m50[a] - 1 for a, b in zip(dates, dates[1:])]
    if max(abs(x) for x in r_1) > 0.25 or max(abs(x) for x in r_l) > 0.45:
        raise RuntimeError("ETF 價格出現異常跳動（可能是分割未還原），回測不更新")
    strategies = {
        "s0050": [(0.0, 1.0)] * len(dates),
        "sL": [(1.0, 0.0)] * len(dates),
        "ladder": [(by[x]["aL"], 1 - by[x]["aL"]) for x in dates],
        "blend": [(by[x]["wL"], by[x]["w50"]) for x in dates],
    }
    names = {"s0050": "0050 持有", "sL": "00631L 持有", "ladder": "純逆勢階梯", "blend": "混合（兩桶）"}
    i0 = next(i for i, x in enumerate(dates) if x >= start_target)
    win = dates[i0:]
    yrs = (dt.date.fromisoformat(win[-1]) - dt.date.fromisoformat(win[0])).days / 365.25
    month_idx = [i for i in range(len(win)) if i == len(win) - 1 or win[i][:7] != win[i + 1][:7]]
    year_idx = [i for i in range(len(win)) if i == len(win) - 1 or win[i][:4] != win[i + 1][:4]]
    out = {"start": win[0], "end": win[-1], "months": [win[i][:7] for i in month_idx], "eq": {}, "stats": [],
           "yearly": [{"year": win[i][:4]} for i in year_idx],
           "changes": sum(1 for x in win[1:] if by[x]["changed"])}
    for k, tg in strategies.items():
        eq = simulate(r_l, r_1, tg)[i0:]
        eq = [v / eq[0] for v in eq]
        peak, mdd = 0.0, 0.0
        for v in eq:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        prev, yr = 1.0, []
        for j, i in enumerate(year_idx):
            yr.append(eq[i] / prev - 1)
            out["yearly"][j][k] = round(yr[-1] * 100, 1)
            prev = eq[i]
        out["eq"][k] = [round(eq[i], 4) for i in month_idx]
        out["stats"].append({"key": k, "name": names[k], "cagr": round((eq[-1] ** (1 / yrs) - 1) * 100, 1),
                             "mdd": round(mdd * 100, 1), "worst": round(min(yr[1:]) * 100, 0), "final": round(eq[-1], 2)})
    return out


# ───────────────────────────── 通知 ─────────────────────────────
def zone_of(ext, key, v):
    """v 落在哪個極端區（回傳該區設定，沒有則 None）。"""
    hit = None
    for z in (ext.get("ind", {}).get(key, {}).get("zones") or []):
        if v is not None and (v <= z["v"] if z["op"] == "<=" else v >= z["v"]):
            hit = z
    return hit


def notify(data, old, ext):
    """有新事件時把通知的標題與內文寫到 NOTIFY_DIR，交給 workflow 開 Issue。同一件事只通知一次。"""
    out_dir = os.environ.get("NOTIFY_DIR")
    if not out_dir:
        return
    t, ot, md = data["today"], old.get("today", {}), data["asof"][5:].replace("-", "/")
    tgt = f"00631L {t['wL']}% / 0050 {t['w50']}% / 現金 {t['wC']}%"
    same_day = old.get("asof") == data["asof"]
    same_tgt = (ot.get("wL"), ot.get("w50")) == (t["wL"], t["w50"])
    events = []
    if t["changed"] and not (same_day and same_tgt and ot.get("changed")):
        events.append(f"訊號變更 {md}：{tgt}")
    elif same_day and ot.get("changed") and not t["changed"]:
        events.append(f"更正 {md}：收盤價修正後訊號沒有變更，目標維持 {tgt}")
    names = {k: v["name"] for k, v in ext.get("ind", {}).items()}
    for k, z in data["zones"].items():
        if z and z != (old.get("zones") or {}).get(k) and "zones" in old:
            events.append(f"{names.get(k, k)} 進入「{z}」：{data['zvals'][k]}")
    flag = os.path.join(DATA_DIR, "notify_enabled.txt")
    if not os.path.exists(flag):
        with open(flag, "w", encoding="utf-8") as f:
            f.write(data["generated"] + "\n")
        events.append("通知已啟用（測試）")
    if not events:
        return
    repo, owner = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    site = f"https://{owner}.github.io/{repo.split('/')[-1]}/" if repo and owner else ""
    m = data["margin"]
    body = [f"@{owner}" if owner else "", "", *[f"- {e}" for e in events], "",
            "| 項目 | 數值 |", "|---|---|",
            f"| 資料日 | {data['asof']} |", f"| 目標配置 | {tgt}（曝險 {t['exp']:.2f}x） |",
            f"| 加權指數 | {t['close']:,}（{t['chg']:+.2f}%） |", f"| 距一年高點 | {t['dd']}% |", f"| 距 200 日線 | {t['dist']:+}% |",
            f"| VIX | {t['vix']} |"]
    if m.get("bal"):
        body.append(f"| 融資餘額 20 日變化 | {m['bal']['chg20']:+}%（{m['bal']['date']}） |")
    if m.get("keep", {}).get("rate"):
        body.append(f"| 全市場擔保維持率 | {m['keep']['rate'][-1]}%（{m['keep']['date'][-1]}） |")
    body += ["", site, "", "規則計算結果，不是投資建議。"]
    with open(os.path.join(out_dir, "z2_title.txt"), "w", encoding="utf-8") as f:
        f.write(events[0] + (f" 等 {len(events)} 項" if len(events) > 1 else ""))
    with open(os.path.join(out_dir, "z2_body.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    print("通知：" + "；".join(events))


# ───────────────────────────── 組裝輸出 ─────────────────────────────
def asof(series, key, d, strict=False):
    """series 在日期 d（strict 時為 d 之前）最後一筆的 (日期, 值)。"""
    best = None
    for x, v in zip(series["dates"], series[key]):
        if x < d or (x == d and not strict):
            best = (x, v)
        else:
            break
    return best


def main():
    tw = fetch("^TWII", BT_YEARS + 2.5)
    p50 = fetch("0050.TW", BT_YEARS + 1.6)
    pl = fetch("00631L.TW", BT_YEARS + 1.6)
    vix = fetch("^VIX", 4.5)
    rows = replay(tw["dates"], tw["close"])
    t = rows[-1]
    old = _load("data.json", {})

    levels = []
    for j, (th, a) in enumerate(LADDER):
        if j + 1 > t["lvl"]:
            levels.append({"key": f"drop{int(th * 100)}", "kind": "加碼", "px": round(t["hi"] * (1 - th)),
                           "label": f"回落 {int(th * 100)}%：逆勢桶的 00631L 提高到 {int(a * 100)}%"})
    if t["lvl"] > 0:
        levels.append({"key": "reset", "kind": "換回 0050", "px": round(t["hi"]), "label": "創一年新高：逆勢桶全部換回 0050"})
    if t["on"]:
        levels.append({"key": "exit", "kind": "順勢出場", "px": round(t["ma"] * (1 - BUF)), "label": "跌破 200 日線 3%：順勢桶賣出 00631L 轉現金"})
    else:
        levels.append({"key": "enter", "kind": "順勢進場", "px": round(t["ma"] * (1 + BUF)), "label": "站上 200 日線 3%：順勢桶買進 00631L"})
    for lv in levels:
        lv["gap"] = round((lv["px"] / t["close"] - 1) * 100, 1)
    levels.sort(key=lambda x: -x["px"])

    keep = rows[-DAILY_KEEP:]
    daily = {
        "date": [r["date"] for r in keep], "close": [round(r["close"]) for r in keep], "hi": [round(r["hi"]) for r in keep],
        "dd": [round(r["dd"] * 100, 1) for r in keep], "ma": [round(r["ma"]) for r in keep],
        "dist": [round(r["dist"] * 100, 1) for r in keep], "lvl": [r["lvl"] for r in keep], "on": [int(r["on"]) for r in keep],
        "wL": [round(r["wL"] * 100) for r in keep], "w50": [round(r["w50"] * 100) for r in keep], "wC": [round(r["wC"] * 100) for r in keep],
        "exp": [r["exp"] for r in keep], "changed": [int(r["changed"]) for r in keep],
    }
    vi, vseries = 0, []
    for r in keep:                                   # 每個台股交易日收盤時已知的 VIX（美股前一晚收盤）
        while vi + 1 < len(vix["dates"]) and vix["dates"][vi + 1] < r["date"]:
            vi += 1
        vseries.append(round(vix["close"][vi], 1))
    daily["vix"] = vseries
    v = asof(vix, "close", t["date"], strict=True)      # 台股收盤時已知的美股前一晚收盤
    a50, al = asof(p50, "close", t["date"]), asof(pl, "close", t["date"])
    a_l = round(t["aL"] * 100)
    today = {k: daily[k][-1] for k in daily}
    today.update({"vix": round(v[1], 1), "vix_date": v[0], "px50": round(a50[1], 2), "pxL": round(al[1], 2), "px_date": a50[0],
                  "aL": a_l, "a50": 100 - a_l, "bL": 100 if t["on"] else 0, "bC": 0 if t["on"] else 100})

    def day_chg(series, key, d, strict=False):
        """series 在日期 d（strict 時為 d 之前）最後一筆相對前一筆的漲跌 %。"""
        xs = [v for x, v in zip(series["dates"], series[key]) if x < d or (x == d and not strict)]
        return round((xs[-1] / xs[-2] - 1) * 100, 2) if len(xs) >= 2 else None

    today.update({"chg": round((rows[-1]["close"] / rows[-2]["close"] - 1) * 100, 2), "vix_chg": day_chg(vix, "close", t["date"], True),
                  "px50_chg": day_chg(p50, "adj", t["date"]), "pxL_chg": day_chg(pl, "close", t["date"])})
    ot = old.get("today", {})
    if a50[0] != t["date"] or al[0] != t["date"]:          # Yahoo 盤中常缺前一個交易日的 ETF 日線
        if old.get("asof") == t["date"] and ot.get("px_date") == t["date"] and not ot.get("px_est"):
            today.update({k: ot[k] for k in ("px50", "pxL", "px_date", "px50_chg", "pxL_chg") if k in ot})   # 沿用先前抓到的正式收盤價
        else:
            e50, el = last_intraday("0050.TW", t["date"]), last_intraday("00631L.TW", t["date"])
            if e50 and el:                                  # 用小時線補，標記為估計值
                today.update({"px50_chg": round((e50 / a50[1] - 1) * 100, 2), "pxL_chg": round((el / al[1] - 1) * 100, 2),
                              "px50": round(e50, 2), "pxL": round(el, 2), "px_date": t["date"], "px_est": 1})

    closes = tw["close"]
    data = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "asof": t["date"],
            "today": today, "levels": levels, "daily": daily, "drift": int(DRIFT * 100),
            "margin": margin_block(fetch_new=tw["live"] is None),
            "live": dict(tw["live"], px50=(p50["live"] or {}).get("px"), pxL=(pl["live"] or {}).get("px")) if tw["live"] else None,
            # 下一個交易日收在某個價位時要怎麼算（給網頁的盤中試算用）
            "next": {"hi251": round(max(closes[-(HIGH_WIN - 1):]), 2), "sum199": round(sum(closes[-(MA_N - 1):]), 2),
                     "lvl": t["lvl"], "on": int(t["on"])},
            "rule": {"ladder": LADDER, "ma": MA_N, "buf": BUF, "bucket": BUCKET}}
    ext = _load("extremes.json", {})
    bal = data["margin"].get("bal") or {}
    data["zvals"] = {"dd": f"{today['dd']}%", "dist": f"{today['dist']:+}%", "vix": f"{today['vix']}", "m20": f"{bal.get('chg20', 0):+}%"}
    zv = {"dd": today["dd"], "dist": today["dist"], "vix": today["vix"], "m20": bal.get("chg20")}
    data["zones"] = {k: (zone_of(ext, k, v) or {}).get("label") for k, v in zv.items()}
    try:
        data["bt"] = backtest(rows, p50, pl)
    except Exception as e:  # noqa: BLE001  回測失敗不影響每日訊號：沿用上一次的回測
        print(f"[警告] 回測沒有更新：{e}", file=sys.stderr)
        if "bt" not in old:
            raise
        data["bt"], data["bt_stale"] = old["bt"], True
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    notify(data, old, ext)
    print(f"資料日 {t['date']}：00631L {today['wL']}% / 0050 {today['w50']}% / 現金 {today['wC']}%"
          f"｜回測 {data['bt']['start']} ~ {data['bt']['end']}")


if __name__ == "__main__":
    main()
