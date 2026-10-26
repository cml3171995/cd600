# -*- coding: utf-8 -*-
"""同条件养护600℃·d每日累计脚本（多项目版 + 7天/28天试块提醒）
GitHub Actions 每天北京时间08:00运行：
1. 逐项目抓取各自坐标日均温（低于0按0计），维护温度序列并回补历史
2. 预计达标日期：按过去12个月分月气温基线外推整个冬天
3. 微信提醒（每项目每天各一条，有内容才发）：
   a.【600℃·d】所有已满600且未送检的部位一起播报，直到页面点「已送检」
   b.【7天/28天】浇筑满7天未送7天检、满28天未送28天检的部位，直到页面点对应确认
"""
import json, os, urllib.request, urllib.parse, base64, datetime

TZ = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TZ).date()
TARGET = float(os.environ.get("TARGET", "600"))
MODE = os.environ.get("RUN_MODE", "full")   # full=08:00数据+600提醒；milestone=16:00试压到期提醒
SALT = "JZ600-2026-QD-4B"

FORECAST = ("https://api.open-meteo.com/v1/forecast"
            "?latitude={lat}&longitude={lon}"
            "&daily=temperature_2m_mean&past_days=92&forecast_days=1"
            "&timezone=Asia%2FShanghai")
ARCHIVE = ("https://archive-api.open-meteo.com/v1/archive"
           "?latitude={lat}&longitude={lon}"
           "&daily=temperature_2m_mean&timezone=Asia%2FShanghai")
NORMAL_QD = {1:-0.5,2:1.2,3:5.2,4:10.6,5:16.1,6:20.5,7:24.6,8:25.8,9:22.2,10:16.2,11:8.6,12:2.2}
NORMAL_WF = {1:-3.0,2:0.0,3:6.5,4:14.0,5:19.8,6:24.2,7:26.5,8:26.0,9:22.0,10:15.5,11:7.0,12:0.0}
def normals_for(code):
    return NORMAL_WF if code == "10188043" else NORMAL_QD

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

def climate_monthly(lat, lon, code):
    start = (TODAY.replace(day=1) - datetime.timedelta(days=365)).isoformat()
    try:
        ar = http(f"{ARCHIVE}&start_date={start}&end_date={TODAY.isoformat()}")
        acc, cnt = {}, {}
        for d, v in zip(ar["daily"]["time"], ar["daily"]["temperature_2m_mean"]):
            if v is None: continue
            m = d[:7]
            acc[m] = acc.get(m, 0.0) + max(0.0, v); cnt[m] = cnt.get(m, 0) + 1
        return {m: acc[m] / cnt[m] for m in acc}
    except Exception as e:
        print("气候基线接口限流，改用气候常值表:", e)
        return {"__fb__": normals_for(code)}

def month_eff(climate, y, m):
    key = f"{y:04d}-{m:02d}"
    if key in climate: return climate[key]
    same = [v for k, v in climate.items() if k != "__fb__" and k[5:7] == f"{m:02d}"]
    if same: return sum(same) / len(same)
    if "__fb__" in climate: return climate["__fb__"][m]
    allv = [v for k, v in climate.items() if k != "__fb__"]
    return sum(allv) / len(allv) if allv else 0.0

def estimate_date(cum, last_date, climate):
    if cum >= TARGET: return "已达标"
    if not climate or not last_date: return ""
    d = datetime.date.fromisoformat(last_date)
    c = cum
    for _ in range(800):
        d += datetime.timedelta(days=1)
        c += month_eff(climate, d.year, d.month)
        if c >= TARGET: return d.isoformat()
    return ""

MASTER_ENC = None  # 运行时由主流程注入

def _send(key, title, desp):
    r = http(f"https://sctapi.ftqq.com/{key}.send", {"title": title, "desp": desp}, form=True)
    if r.get("code") != 0:
        print("推送异常:", r)
        return False
    return True

def push(code, node, title, desp, tag="", to_master=True):
    """双通道推送：通道1试验员 / 通道2质量总监；to_master=False时不发监管端"""
    ch1_enc = node.get("ch1_enc")
    legacy = node.get("sendkey_enc")
    use1, use1_code = (ch1_enc, code+"_1") if ch1_enc else (legacy, code) if legacy else (None, code)
    channels = [("通道1试验员", use1, use1_code),
                ("通道2质量总监", node.get("ch2_enc"), code+"_2")]
    ok = False
    for ch_name, enc, ch_code in channels:
        if not enc:
            print(f"[{code}] {ch_name}未绑定")
            continue
        try:
            if _send(decrypt(ch_code, enc), title, desp):
                ok = True
                print(f"[{code}] {ch_name}已推送{tag}：{title}")
        except Exception as e:
            print(f"[{code}] {ch_name}推送失败:", e)
    if to_master and MASTER_ENC:
        try:
            if _send(decrypt("MASTER", MASTER_ENC), title, desp):
                print(f"[{code}] 监管端已推送{tag}")
        except Exception as e:
            print(f"[{code}] 监管端推送失败:", e)
    return ok

with open("projects.json", encoding="utf-8") as f:
    registry = json.load(f)
with open("data.json", encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("projects", {})
MASTER_ENC = data.get("master_enc")

for reg in registry:
    code, lat, lon, name = reg["code"], reg["lat"], reg["lon"], reg["name"]
    node = data["projects"].setdefault(code, {"days": [], "parts": []})
    days = node.setdefault("days", [])
    if node.get("sendkey_enc") and not node.get("ch1_enc"):
        node["ch1_enc"] = node["sendkey_enc"]   # 旧绑定迁移为通道1（试验员）
    node["parts"] = [p for p in node.get("parts", []) if p.get("name")]
    # 兼容旧数据：已送检视为7天28天均已送检
    for p in node["parts"]:
        if p.get("sent"):
            p.setdefault("m7", 1); p.setdefault("m28", 1)
    have = {x["date"]: x for x in days}

    if MODE == "milestone":
        # 16:00 试压提醒专跑：只算龄期，不动温度数据
        for p in node["parts"]:
            pour = p.get("pour", "")[:10]
            try:
                p["age"] = (TODAY - datetime.date.fromisoformat(pour)).days if pour else 0
            except Exception:
                p["age"] = 0
        # 到期当天(age==7/28)必提醒；第8/29天起，点了"已试压"才不再提醒
        due7 = [p for p in node["parts"] if p.get("age", 0) >= 7 and (p.get("age", 0) == 7 or not p.get("m7"))]
        due28 = [p for p in node["parts"] if p.get("age", 0) >= 28 and (p.get("age", 0) == 28 or not p.get("m28"))]
        if due7 or due28:
            lines = []
            if due7:
                lines.append("【7天试压到期】")
                lines += [f"- {p['name']}（浇筑{p.get('pour')}，今天第{p['age']}天）" for p in due7]
            if due28:
                lines.append("")
                lines.append("【28天试压到期】")
                lines += [f"- {p['name']}（浇筑{p.get('pour')}，今天第{p['age']}天）" for p in due28]
            desp = f"项目：{name}（{code}）\n\n" + "\n".join(lines) +                    "\n\n试块试压完成后，请在页面该部位上点「7天✓」或「28天✓」，此后不再提醒。"
            push(code, node, f"【试压提醒】{name} 7天{len(due7)}个/28天{len(due28)}个到期", desp, tag="(试压)")
        continue

    # 1) 最近92天实测（末尾一天是今天的预报，不计入）
    try:
        wx = http(FORECAST.format(lat=lat, lon=lon))
        dts, t = wx["daily"]["time"], wx["daily"]["temperature_2m_mean"]
        for d, v in zip(dts[:-1], t[:-1]):
            if d in have or v is None: continue
            rec = {"date": d, "temp": round(v, 1), "eff": max(0.0, round(v, 1))}
            days.append(rec); have[d] = rec
    except Exception as e:
        print(f"[{code}] 预报API失败(下次重试):", e)

    # 2) 92天以外旧日期回补
    pours = [p["pour"][:10] for p in node["parts"] if p.get("pour")]
    if pours and have:
        start, end = min(pours), max(have)
        limit = (TODAY - datetime.timedelta(days=92)).isoformat()
        missing = [d for d in daterange(start, end) if d not in have and d < limit]
        if missing:
            try:
                ar = http(f"{ARCHIVE}&start_date={min(missing)}&end_date={max(missing)}")
                n = 0
                for d, v in zip(ar["daily"]["time"], ar["daily"]["temperature_2m_mean"]):
                    if d in have or v is None: continue
                    rec = {"date": d, "temp": round(v, 1), "eff": max(0.0, round(v, 1))}
                    days.append(rec); have[d] = rec; n += 1
                if n: print(f"[{code}] archive回补 {n} 天")
            except Exception as e:
                print(f"[{code}] 92天以外回补失败(不影响近期):", e)

    days.sort(key=lambda x: x["date"])
    node["start_date"] = days[0]["date"] if days else None
    node["end_date"] = days[-1]["date"] if days else None

    climate = climate_monthly(lat, lon, code)

    # 3) 部位累计/预测
    for p in node["parts"]:
        pour = p.get("pour", "")[:10]
        rows = [x for x in days if pour and x["date"] >= pour]
        p["cum"] = round(sum(x["eff"] for x in rows), 1)
        p["nday"] = len(rows)
        p["last"] = rows[-1]["date"] if rows else ""
        p["est"] = estimate_date(p["cum"], p["last"], climate) if p["cum"] < TARGET else "已达标"
        try:
            p["age"] = (TODAY - datetime.date.fromisoformat(pour)).days if pour else 0
        except Exception:
            p["age"] = 0

    # 4a) 600℃·d 每日汇总提醒（满600且未送检）
    need600 = [p for p in node["parts"] if p["cum"] >= TARGET and not p.get("sent")]
    if need600:
        lines = [f"- {p['name']}（浇筑{p.get('pour')}，累计{p['cum']}℃·d）" for p in need600]
        desp = f"项目：{name}（{code}）\n\n以下部位同条件养护已满600℃·d，请安排600℃·d同条件试块评估，完成后在页面点「600✓」：\n\n" + "\n".join(lines)
        push(code, node, f"【600℃·d】{name} {len(need600)}个部位待评估", desp, tag="(600)", to_master=False)

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

print("== 完成 ==")
for reg in registry:
    node = data["projects"].get(reg["code"], {})
    print(f"[{reg['code']}] {reg['name']}: 温度{len(node.get('days',[]))}天")
    for p in node.get("parts", []):
        flag = ("600✓" if p.get("sent") else "") + (" 7✓" if p.get("m7") else "") + (" 28✓" if p.get("m28") else "")
        print(f"    {p['name'][:16]} 浇筑{p.get('pour')} 第{p.get('age',0)}天 累计{p.get('cum')} 预计{p.get('est') or '—'} {flag}")
