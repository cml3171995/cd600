# -*- coding: utf-8 -*-
"""同条件养护600℃·d每日累计脚本（多部位版）
由 GitHub Actions 每天北京时间07:00运行：
1. 从 Open-Meteo 读取昨日实测日均温（低于0按0计），追加到全局温度序列
2. 对每个工程部位：按其浇筑日期回填补齐历史温度（archive API），独立累计
3. 任一部位满600℃·d且未提醒过 → Server酱微信提醒（每部位一条，只发一次）
"""
import json, os, urllib.request, urllib.parse

LAT = os.environ.get("LAT", "36.07")
LON = os.environ.get("LON", "120.38")
CITY = os.environ.get("CITY", "青岛")
TARGET = float(os.environ.get("TARGET", "600"))
SC_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()

FORECAST = ("https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}"
            "&daily=temperature_2m_mean&past_days=7&forecast_days=1"
            "&timezone=Asia%2FShanghai")
ARCHIVE = ("https://archive-api.open-meteo.com/v1/archive"
           f"?latitude={LAT}&longitude={LON}"
           "&daily=temperature_2m_mean&timezone=Asia%2FShanghai")

def http(url, data=None, form=False):
    if form:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body, headers={"User-Agent": "600cd"})
    elif data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={"User-Agent": "600cd"})
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "600cd"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def daterange(a, b):
    import datetime
    d0 = datetime.date.fromisoformat(a); d1 = datetime.date.fromisoformat(b)
    d = d0
    while d <= d1:
        yield d.isoformat(); d += datetime.timedelta(days=1)

with open("data.json", encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("days", [])
data.setdefault("parts", [])
data.setdefault("notified", False)  # 旧版兼容

have = {x["date"]: x for x in data["days"]}

# 1) 追加最近实测（数组末尾一天是“今天”的预报值，不计入）
wx = http(FORECAST)
dts, t = wx["daily"]["time"], wx["daily"]["temperature_2m_mean"]
for d, v in zip(dts[:-1], t[:-1]):
    if d in have or v is None:
        continue
    rec = {"date": d, "temp": round(v, 1), "eff": max(0.0, round(v, 1))}
    data["days"].append(rec); have[d] = rec

# 2) 按部位浇筑日期回补历史温度（archive 有约5天延迟，缺的由每日运行逐渐补齐）
pours = [p.get("pour", "")[:10] for p in data["parts"] if p.get("pour")]
if pours:
    start = min(pours)
    if have:
        end = max(have)
        if start < end:
            missing = [d for d in daterange(start, end) if d not in have]
            if missing:
                try:
                    ar = http(f"{ARCHIVE}&start_date={start}&end_date={end}")
                    n = 0
                    for d, v in zip(ar["daily"]["time"], ar["daily"]["temperature_2m_mean"]):
                        if d in have or v is None:
                            continue
                        rec = {"date": d, "temp": round(v, 1), "eff": max(0.0, round(v, 1))}
                        data["days"].append(rec); have[d] = rec; n += 1
                    print(f"回补历史温度 {n} 天")
                except Exception as e:
                    print("archive回补失败(下次重试):", e)

data["days"].sort(key=lambda x: x["date"])
if data["days"]:
    data["start_date"] = data["days"][0]["date"]
    data["end_date"] = data["days"][-1]["date"]
data["city"] = CITY
data["target"] = TARGET

# 3) 各部位独立累计 + 分别提醒
for p in data["parts"]:
    pour = p.get("pour", "")[:10]
    rows = [x for x in data["days"] if pour and x["date"] >= pour]
    p["cum"] = round(sum(x["eff"] for x in rows), 1)
    p["nday"] = len(rows)
    p["last"] = rows[-1]["date"] if rows else ""
    if p["cum"] >= TARGET and not p.get("notified"):
        if SC_KEY:
            msg = (f"工程部位：{p['name']}\n浇筑日期：{pour}\n最新日期：{p['last']}\n"
                   f"累计：{p['cum']}℃·d / {TARGET:.0f}℃·d（{p['nday']}天）\n"
                   f"该部位同条件养护600℃·d已达到，请安排强度评估。")
            try:
                r = http("https://sctapi.ftqq.com/%s.send" % SC_KEY,
                         {"title": "【600℃·d】%s 已达标" % p["name"], "desp": msg}, form=True)
                if r.get("code") == 0:
                    p["notified"] = True
                    print("已提醒部位:", p["name"])
                else:
                    print("推送异常:", r)
            except Exception as e:
                print("推送失败(下次重试):", e)
        else:
            print("SERVERCHAN_KEY 未配置，未提醒:", p["name"])

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

print("全局累计 %s℃·d（%s~%s，%d天），部位数 %d"
      % (round(sum(x["eff"] for x in data["days"]), 1),
         data.get("start_date"), data.get("end_date"), len(data["days"]), len(data["parts"])))
for p in data["parts"]:
    print("  部位[%s] 浇筑%s 累计%s℃·d %s" % (p["name"], p.get("pour"), p.get("cum"), "✅已提醒" if p.get("notified") else ""))
