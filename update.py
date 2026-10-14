# -*- coding: utf-8 -*-
"""同条件养护600℃·d每日累计脚本（多项目版）
GitHub Actions 每天北京时间07:00运行：
1. 读取 projects.json 注册表，逐项目抓取各自坐标的日均温（低于0按0计）
2. 每项目独立维护温度序列；部位浇筑日早于已收集数据时用 archive API 回补
3. 预计达标日期：按项目位置过去12个月分月气温基线外推整个冬天
4. 部位满600℃·d且未提醒过 → 用该项目绑定的 Server酱 SendKey（加密存储）推送
"""
import json, os, urllib.request, urllib.parse, base64, datetime

TARGET = float(os.environ.get("TARGET", "600"))
SALT = "JZ600-2026-QD-4B"   # 与前端一致的混淆盐

FORECAST = ("https://api.open-meteo.com/v1/forecast"
            "?latitude={lat}&longitude={lon}"
            "&daily=temperature_2m_mean&past_days=7&forecast_days=1"
            "&timezone=Asia%2FShanghai")
ARCHIVE = ("https://archive-api.open-meteo.com/v1/archive"
           "?latitude={lat}&longitude={lon}"
           "&daily=temperature_2m_mean&timezone=Asia%2FShanghai")

def http(url, data=None, form=False):
    if form:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                     headers={"User-Agent": "600cd"})
    elif data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={"User-Agent": "600cd"})
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "600cd"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def keystream(code, n):
    cs = sum(ord(c) for c in code)
    salt = [ord(c) for c in SALT]
    return bytes((salt[k % len(salt)] * 31 + (k // 32) * 97 + (k % 32) * 7 + cs) % 251 for k in range(n))

def decrypt(code, b64):
    raw = base64.b64decode(b64)
    return bytes(a ^ b for a, b in zip(raw, keystream(code, len(raw)))).decode()

def daterange(a, b):
    d = datetime.date.fromisoformat(a); e = datetime.date.fromisoformat(b)
    while d <= e:
        yield d.isoformat(); d += datetime.timedelta(days=1)

def climate_monthly(lat, lon, today):
    """过去12个月 archive 日均温 → {月份: 平均eff}（低于0按0），作为未来气候基线"""
    start = (today.replace(day=1) - datetime.timedelta(days=365)).isoformat()
    end = today.isoformat()
    try:
        ar = http(f"{ARCHIVE}&start_date={start}&end_date={end}")
        acc, cnt = {}, {}
        for d, v in zip(ar["daily"]["time"], ar["daily"]["temperature_2m_mean"]):
            if v is None:
                continue
            m = d[:7]  # YYYY-MM
            acc[m] = acc.get(m, 0.0) + max(0.0, v); cnt[m] = cnt.get(m, 0) + 1
        return {m: acc[m] / cnt[m] for m in acc}
    except Exception as e:
        print("气候基线获取失败(仅影响预测):", e)
        return {}

def month_eff(climate, y, m):
    """某月日均eff基线：优先取过去12个月同月实测均值；无数据则用全年均值"""
    key = f"{y:04d}-{m:02d}"
    if key in climate:
        return climate[key]
    # 找任意年份同月的均值
    same = [v for k, v in climate.items() if k[5:7] == f"{m:02d}"]
    if same:
        return sum(same) / len(same)
    allv = list(climate.values())
    return sum(allv) / len(allv) if allv else 0.0

def estimate_date(pour, cum, last_date, climate, today):
    """从last_date次日起按月基线外推至满600，返回预计达标日期"""
    if cum >= TARGET:
        return "已达标"
    if not climate or not last_date:
        return ""
    d = datetime.date.fromisoformat(last_date)
    c = cum
    for _ in range(800):
        d += datetime.timedelta(days=1)
        c += month_eff(climate, d.year, d.month)
        if c >= TARGET:
            return d.isoformat()
    return ""

with open("projects.json", encoding="utf-8") as f:
    registry = json.load(f)
with open("data.json", encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("projects", {})

for reg in registry:
    code, lat, lon, name = reg["code"], reg["lat"], reg["lon"], reg["name"]
    node = data["projects"].setdefault(code, {"days": [], "parts": []})
    days = node.setdefault("days", [])
    node["parts"] = [p for p in node.get("parts", []) if p.get("name")]
    have = {x["date"]: x for x in days}
    today = datetime.date.today()

    # 1) 追加最近实测（末尾一天是“今天”的预报，不计入累计）
    try:
        wx = http(FORECAST.format(lat=lat, lon=lon))
        dts, t = wx["daily"]["time"], wx["daily"]["temperature_2m_mean"]
        for d, v in zip(dts[:-1], t[:-1]):
            if d in have or v is None:
                continue
            rec = {"date": d, "temp": round(v, 1), "eff": max(0.0, round(v, 1))}
            days.append(rec); have[d] = rec
    except Exception as e:
        print(f"[{code}] 预报API失败(下次重试):", e)

    # 2) 回补历史
    pours = [p["pour"][:10] for p in node["parts"] if p.get("pour")]
    if pours and have:
        start, end = min(pours), max(have)
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
                        days.append(rec); have[d] = rec; n += 1
                    if n: print(f"[{code}] 回补历史 {n} 天")
                except Exception as e:
                    print(f"[{code}] 回补失败(下次重试):", e)

    days.sort(key=lambda x: x["date"])
    node["start_date"] = days[0]["date"] if days else None
    node["end_date"] = days[-1]["date"] if days else None

    # 3) 气候基线 + 部位累计/预测/分别提醒
    climate = climate_monthly(lat, lon, today)
    sk_enc = node.get("sendkey_enc")
    for p in node["parts"]:
        pour = p.get("pour", "")[:10]
        rows = [x for x in days if pour and x["date"] >= pour]
        p["cum"] = round(sum(x["eff"] for x in rows), 1)
        p["nday"] = len(rows)
        p["last"] = rows[-1]["date"] if rows else ""
        p["est"] = estimate_date(pour, p["cum"], p["last"], climate, today)
        if p["cum"] >= TARGET and not p.get("notified"):
            if not sk_enc:
                print(f"[{code}] 部位[{p['name']}]已满{p['cum']}℃·d，但该项目未绑定微信提醒")
                continue
            msg = (f"项目：{name}（{code}）\n工程部位：{p['name']}\n浇筑日期：{pour}\n"
                   f"最新日期：{p['last']}\n累计：{p['cum']}℃·d / {TARGET:.0f}℃·d（{p['nday']}天）\n"
                   f"该部位同条件养护600℃·d已达到，请安排强度评估。")
            try:
                key = decrypt(code, sk_enc)
                r = http(f"https://sctapi.ftqq.com/{key}.send",
                         {"title": f"【600℃·d】{name} {p['name']} 已达标", "desp": msg}, form=True)
                if r.get("code") == 0:
                    p["notified"] = True
                    print(f"[{code}] 已提醒部位: {p['name']}")
                else:
                    print(f"[{code}] 推送异常:", r)
            except Exception as e:
                print(f"[{code}] 推送失败(下次重试):", e)

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

print("== 完成 ==")
for reg in registry:
    node = data["projects"].get(reg["code"], {})
    days = node.get("days", [])
    print(f"[{reg['code']}] {reg['name']}: 温度{len(days)}天 {node.get('start_date')}~{node.get('end_date')}")
    for p in node.get("parts", []):
        print(f"    部位[{p['name']}] 浇筑{p.get('pour')} 累计{p.get('cum')}℃·d 预计达标{p.get('est') or '—'}")
