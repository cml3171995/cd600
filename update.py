# -*- coding: utf-8 -*-
"""同条件养护600℃·d每日累计脚本
由 GitHub Actions 每天定时运行：
1. 从 Open-Meteo 读取昨天（实测日均温），低于0按0计
2. 累加进 data.json
3. 累计满600℃·d 且尚未提醒时，通过 PushPlus 推送微信提醒
"""
import json, os, urllib.request

LAT = os.environ.get("LAT", "36.07")
LON = os.environ.get("LON", "120.38")
CITY = os.environ.get("CITY", "青岛")
TARGET = float(os.environ.get("TARGET", "600"))
SC_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()  # Server酱 SendKey，免费额度足够（只在满600时推一次）
START_DATE = os.environ.get("START_DATE", "").strip()  # 例如 2026-10-15，早于该日的数据不计入

API = ("https://api.open-meteo.com/v1/forecast"
       f"?latitude={LAT}&longitude={LON}"
       "&daily=temperature_2m_mean&past_days=7&forecast_days=1"
       "&timezone=Asia%2FShanghai")

def http(url, data=None, headers={}):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers=headers or {"User-Agent": "600cd"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

with open("data.json", encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("days", [])
data.setdefault("total", 0)
data.setdefault("notified", False)

wx = http(API)
t = wx["daily"]["temperature_2m_mean"]
dts = wx["daily"]["time"]
# 注意：数组末尾为未来预报日（无实测值），v 为 None 会被自然跳过；
# 当天均值一般已出，也计入
have = {x["date"]: x for x in data["days"]}
added = []
# 末尾一天是“今天”的预报值，不计入；只累计到昨天为止的实测
for d, v in zip(dts[:-1], t[:-1]):
    if d in have or v is None or (START_DATE and d < START_DATE):
        continue
    # 仅接收过去的实测日期：open-meteo 数组为 [过去7天..., 今天, 未来...]，
    # 今天若有均值也按实测计入（部分数据源当天即出实测）
    eff = max(0.0, round(v, 1))
    rec = {"date": d, "temp": round(v, 1), "eff": eff}
    data["days"].append(rec)
    have[d] = rec
    added.append(rec)

data["days"].sort(key=lambda x: x["date"])
if data["days"]:
    data["start_date"] = data["days"][0]["date"]
    data["end_date"] = data["days"][-1]["date"]
data["total"] = round(sum(x["eff"] for x in data["days"]), 1)
data["city"] = CITY
data["target"] = TARGET
data["updated"] = data.get("end_date")

push_msg = None
if data["total"] >= TARGET and not data["notified"]:
    if SC_KEY:
        days_n = len(data["days"])
        push_msg = (f"位置：{CITY}\n起算日期：{data['start_date']}\n"
                    f"最新日期：{data['end_date']}\n累计：{data['total']}℃·d / 600℃·d\n"
                    f"历时 {days_n} 天。同条件养护600℃·d已达到，请安排强度评估。")
        try:
            body = urllib.parse.urlencode(
                {"title": "【600℃·d】同条件养护累计已达标", "desp": push_msg}).encode()
            r2 = urllib.request.Request(f"https://sctapi.ftqq.com/{SC_KEY}.send",
                                        data=body, headers={"User-Agent": "600cd"})
            with urllib.request.urlopen(r2, timeout=30) as rr:
                print("Server酱返回:", rr.read().decode()[:120])
            data["notified"] = True
            print("已推送微信提醒")
        except Exception as e:
            print("推送失败，下次重试:", e)
    else:
        print("PUSHPLUS_TOKEN 未配置，暂不推送")

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

print(f"新增{len(added)}天:", [(a['date'], a['temp']) for a in added])
print(f"累计 {data['total']}℃·d / {TARGET}，共{len(data['days'])}天，区间 {data.get('start_date')} ~ {data.get('end_date')}")
