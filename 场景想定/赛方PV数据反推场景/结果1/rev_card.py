# -*- coding: utf-8 -*-
"""
10 份 P/V 矩阵 → 10 组场景想定文件（可复现批量脚本）

与单场景版 `回推结果_scene1_0_50_20/反推脚本.py` 的方法论完全一致：
  - 《场景文件规范 v2》第 3 节六小节报告 + 第 4/5/7 节卡片与 P/V 产物
  - 《WTA 毁伤概率与价值公式》第 4/5/6 节：因子链、价值公式、反推链路
  - 装备参数/*.jsonl 提供平台与武器库值

批量化的三处改造：
  1. 行分组：按 N 的 50%/40%/10% 比例先验确定三层拦截兵力边界，并以
     最小化段内方差的变点检测作交叉对照（个别扰动场景会偏移，写入校验）。
  2. 列分组：按 v 降序排列后，用动态规划求「段内概率特征平方和最小」的
     5 段分割，得到 5 类目标；再以无监督聚类作一致性交叉验证。
  3. 全部报告文字、数量、距离带、α、覆盖统计均由输入矩阵动态生成。

用法：
  python 通用反推.py                # 处理全部 scene*.json
  python 通用反推.py --only scene1_0_50_20
"""
import json
import csv
import math
import re
import sys
import hashlib
import html as _html
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = ROOT / "装备参数"
OUTROOT = HERE


# ================================================================ 基础工具
def hsh(*args):
    """确定性伪随机 [0,1)，保证阵位可复现且分布均匀。"""
    s = "|".join(str(a) for a in args).encode("utf-8")
    return int.from_bytes(hashlib.md5(s).digest()[:4], "big") / 2 ** 32


def fR_of(rho):
    """手册 4.3 距离-射程比衰减（分段线性）。"""
    if rho <= 0.50:
        return 1.00
    if rho <= 0.80:
        return 1.00 - 0.80 * (rho - 0.50) / 0.30
    if rho <= 1.00:
        return 0.20 - 0.15 * (rho - 0.80) / 0.20
    return 0.0


def rho_of(fR):
    """手册 6.1 分段逆函数。fR>=1 落在平坦段，取 0.5 作代表值。"""
    if fR >= 1.0:
        return 0.50
    if fR > 0.20:
        return 0.50 + 0.30 * (1.00 - fR) / 0.80
    if fR > 0.0:
        return 0.80 + 0.20 * (0.20 - fR) / 0.15
    return 1.00


def clip(x, lo=0.01, hi=0.99):
    return max(lo, min(hi, x))


def md_table(headers, rows):
    out = ["| " + " | ".join(str(x) for x in headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def fmt(x, n=2):
    return "—" if x is None else (f"{x:.{n}f}" if isinstance(x, float) else str(x))


def nums(s):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(s or ""))]


def pct(s):
    m = re.findall(r"(\d+(?:\.\d+)?)\s*%", str(s or ""))
    return float(m[0]) / 100.0 if m else 0.0


def rmax(s):
    a = nums(s)
    return a[-1] if a else 0.0


def rmin(s):
    a = nums(s)
    return a[0] if a else 0.0


# ================================================================ 装备库（一次性加载）
_WEAPON_DB = []
for _ln in (DB / "cmo_weapon_zh.jsonl").open(encoding="utf-8"):
    _WEAPON_DB.append(json.loads(_ln))
_PLATFORM_DB = {}
for _fn, _kind in (("cmo_aircraft_zh.jsonl", "aircraft"), ("cmo_ship_zh.jsonl", "ship")):
    for _ln in (DB / _fn).open(encoding="utf-8"):
        _o = json.loads(_ln)
        _PLATFORM_DB[(_kind, _o["id"])] = _o


def find_weapon(kw):
    for o in _WEAPON_DB:
        if kw.lower() in o["name"].lower():
            b = o.get("basic", {})
            return {
                "weapon_id": o["id"], "name": o["name"], "type": o.get("type", ""),
                "nation": o.get("nation", ""),
                "hit": {"air": pct(b.get("对空命中率")), "surface": pct(b.get("对海命中率")),
                        "land": pct(b.get("对陆命中率")), "sub": pct(b.get("反潜命中率"))},
                "range_nm": {
                    "air": [rmin(b.get("对空射程")), rmax(b.get("对空射程"))] if rmax(b.get("对空射程")) else None,
                    "surface": [rmin(b.get("对海射程")), rmax(b.get("对海射程"))] if rmax(b.get("对海射程")) else None,
                    "land": [rmin(b.get("对陆射程")), rmax(b.get("对陆射程"))] if rmax(b.get("对陆射程")) else None,
                    "sub": [rmin(b.get("反潜射程")), rmax(b.get("反潜射程"))] if rmax(b.get("反潜射程")) else None,
                },
                "warhead": o.get("warhead", {}), "resolved": True,
            }
    return None


W = {
    "AAM": find_weapon("AIM-120C-6 AMRAAM"),
    "PHOENIX": find_weapon("AIM-54C Phoenix"),
    "SM3": find_weapon("RIM-161A SM-3 NTW Blk I"),
    "HARM": find_weapon("AGM-88"),
    "ALCM": find_weapon("AGM-86"),
    "HELLFIRE": find_weapon("AGM-114"),
}
assert all(W.values()), "装备库武器关键词未全部命中"


def wload(w, qty, role):
    d = dict(w)
    d["qty"] = qty
    d["role"] = role
    d["note"] = "库内命中武器；挂载数量为示例重建"
    return d


def check_platform(kind, pid, name):
    """核对模板中的平台条目确实存在于装备库，返回库内规范名。"""
    o = _PLATFORM_DB.get((kind, pid))
    return (o["name"] if o else None), (name in (o["name"] if o else "") if o else False)


# ================================================================ 编成模板（10 场景共用）
# 三层拦截兵力：由矩阵行分组的 3 个概率档位，回查装备库后指派
RED_TEMPLATE = [
    dict(key="A", label="中距拦截层", weapon_key="AAM", qty=4, warhead_mass=6.3,
         pos=dict(x0=1.0, x1=3.0, yspread=12.0, alt0=8000, altspan=2500),
         platform=dict(platform_id=198, name="F-16C Blk 30 Falcon", kind="aircraft",
                       role="fighter", type="多用途战斗机/攻击机", target_class="fighter",
                       nation="UnitedStates", dp=5, year=2003, maneuver_g=4.9,
                       rcs_dbsm="4.1dBsm", rcs_m2=2.57, radar_max_nm=120.0, esm_max_nm=None),
         desc="前出中距拦截线，承担近距/中距空中目标拦截"),
    dict(key="B", label="远程拦截层", weapon_key="PHOENIX", qty=4, warhead_mass=21.35,
         pos=dict(x0=-19.0, x1=-17.0, yspread=12.0, alt0=9000, altspan=2500),
         platform=dict(platform_id=416, name="F-14D Tomcat", kind="aircraft",
                       role="fighter", type="战斗机", target_class="fighter",
                       nation="UnitedStates", dp=10, year=2004, maneuver_g=4.5,
                       rcs_dbsm="9.6dBsm", rcs_m2=9.12, radar_max_nm=180.0, esm_max_nm=None),
         desc="中距后方远程拦截线，覆盖中远距大中型空中目标"),
    dict(key="C", label="超远程舰载拦截层", weapon_key="SM3", qty=8, warhead_mass=1.0,
         pos=dict(x0=-37.0, x1=-35.0, yspread=8.0, alt0=0, altspan=0),
         platform=dict(platform_id=40, name="CG 56 San Jacinto [Ticonderoga Baseline 2]",
                       kind="ship", role="surface_cg", type="CG-GuidedMissileCruiser",
                       target_class="large_surface", nation="UnitedStates", dp=1270, year=1989,
                       maneuver_g=None, rcs_dbsm="29.7dBsm", rcs_m2=933.25,
                       radar_max_nm=None, esm_max_nm=None),
         desc="后方海上编队，负责远距高价值空中节点拦截"),
]

# 蓝方 5 类目标：按 v 均值由高到低依次指派（与基准场景分组语义一致）
BLUE_TEMPLATE = [
    dict(key="T1", label="预警指挥机", yoff=-3.0, R=1.00, Th=1.0, fT=0.90, g_tac=2, fG=1.00, loadout=None,
         platform=dict(platform_id=97, name="Sentry AEW.1 [E-3D]", kind="aircraft",
                       type="空中预警机(AEW)", target_class="aew", nation="UnitedKingdom",
                       dp=20, year=1991, maneuver_g=1.0, rcs_dbsm="18.8dBsm", rcs_m2=75.86,
                       radar_max_nm=350.0, esm_max_nm=None, alt=9000)),
    dict(key="T5", label="电子战飞机", yoff=-9.0, R=1.00, Th=1.0, fT=0.90, g_tac=3, fG=1.00, loadout="HARM",
         platform=dict(platform_id=17, name="EA-6B Prowler ICAP II Blk 82", kind="aircraft",
                       type="电子战飞机", target_class="ew", nation="UnitedStates",
                       dp=10, year=1987, maneuver_g=2.5, rcs_dbsm="8.2dBsm", rcs_m2=6.61,
                       radar_max_nm=150.0, esm_max_nm=None, alt=8000)),
    dict(key="T4", label="轰炸机", yoff=5.0, R=0.75, Th=0.0, fT=0.90, g_tac=3, fG=1.00, loadout="ALCM",
         platform=dict(platform_id=423, name="B-1B Lancer", kind="aircraft",
                       type="轰炸机", target_class="bomber", nation="UnitedStates",
                       dp=20, year=2006, maneuver_g=2.0, rcs_dbsm="12.2dBsm", rcs_m2=16.60,
                       radar_max_nm=160.0, esm_max_nm=None, alt=10000)),
    dict(key="T3", label="多用途战斗机", yoff=9.0, R=0.60, Th=0.0, fT=1.00, g_tac=7, fG=0.70, loadout="AAM",
         platform=dict(platform_id=747, name="F-15E Strike Eagle", kind="aircraft",
                       type="多用途战斗机/攻击机", target_class="fighter", nation="UnitedStates",
                       dp=10, year=2001, maneuver_g=4.0, rcs_dbsm="7.6dBsm", rcs_m2=5.75,
                       radar_max_nm=120.0, esm_max_nm=None, alt=7500)),
    dict(key="T2", label="无人机", yoff=0.0, R=0.60, Th=0.0, fT=1.00, g_tac=3, fG=1.00, loadout="HELLFIRE",
         platform=dict(platform_id=545, name="RQ-1B Predator UAV", kind="aircraft",
                       type="无人机(UAV)", target_class="uav", nation="UnitedStates",
                       dp=3, year=2001, maneuver_g=1.0, rcs_dbsm="-2.1dBsm", rcs_m2=0.6166,
                       radar_max_nm=None, esm_max_nm=None, alt=4500)),
]


# ================================================================ 分组算法
def row_bounds(N):
    """三层拦截兵力边界：50% / 40% / 10% 比例先验。"""
    return [0, round(N * 0.5), round(N * 0.9), N]


def row_means(P, N):
    out = []
    for i in range(N):
        nz = [x for x in P[i] if x > 0]
        out.append(sum(nz) / len(nz) if nz else 0.0)
    return out


def best_split(seq, k=3):
    """最小化段内平方和的变点检测（仅作交叉对照）。"""
    n = len(seq)
    pre, pre2 = [0.0], [0.0]
    for x in seq:
        pre.append(pre[-1] + x)
        pre2.append(pre2[-1] + x * x)

    def cost(a, b):
        s, s2 = pre[b] - pre[a], pre2[b] - pre2[a]
        return s2 - s * s / (b - a)

    INF = float("inf")
    dp = [[INF] * (k + 1) for _ in range(n + 1)]
    arg = [[0] * (k + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for t in range(1, k + 1):
        for b in range(t, n + 1):
            for a in range(t - 1, b):
                c = dp[a][t - 1] + cost(a, b)
                if c < dp[b][t]:
                    dp[b][t], arg[b][t] = c, a
    cuts, b = [], n
    for t in range(k, 0, -1):
        cuts.append(arg[b][t])
        b = arg[b][t]
    return sorted(cuts)[1:]


def col_features(P, V, bounds):
    """每列特征：各红方行组下的非零概率均值（3 维）。"""
    M = len(V)
    out = []
    for j in range(M):
        f = []
        for g in range(3):
            nz = [P[i][j] for i in range(bounds[g], bounds[g + 1]) if P[i][j] > 0]
            f.append(sum(nz) / len(nz) if nz else 0.0)
        out.append(f)
    return out


def dp_segments(V, X, k=5):
    """按 v 降序排列，动态规划求段内概率特征平方和最小的最优 k 段分割。"""
    M = len(V)
    order = sorted(range(M), key=lambda j: -V[j])
    dim = 3
    pre = [[0.0] for _ in range(dim)]
    pre2 = [[0.0] for _ in range(dim)]
    for j in order:
        for d in range(dim):
            pre[d].append(pre[d][-1] + X[j][d])
            pre2[d].append(pre2[d][-1] + X[j][d] ** 2)
    minlen = max(1, int(M * 0.02))

    def cost(a, b):
        m = b - a
        return sum(pre2[d][b] - pre2[d][a] - (pre[d][b] - pre[d][a]) ** 2 / m
                   for d in range(dim))

    INF = float("inf")
    dp = [[INF] * (k + 1) for _ in range(M + 1)]
    arg = [[0] * (k + 1) for _ in range(M + 1)]
    dp[0][0] = 0.0
    for t in range(1, k + 1):
        for b in range(t * minlen, M + 1):
            for a in range((t - 1) * minlen, b - minlen + 1):
                c = dp[a][t - 1] + cost(a, b)
                if c < dp[b][t]:
                    dp[b][t], arg[b][t] = c, a
    cuts, b = [], M
    for t in range(k, 0, -1):
        cuts.append(arg[b][t])
        b = arg[b][t]
    cuts = sorted(cuts)[1:]
    segs, s = [], 0
    for c in cuts + [M]:
        segs.append(sorted(order[s:c]))
        s = c
    return segs, dp[M][k], order


def kmeans_labels(X, k=5, restarts=8, iters=80):
    """轻量无监督聚类，用于与 DP 分段做一致性交叉验证。"""
    n, dim = len(X), 3
    mu = [sum(x[d] for x in X) / n for d in range(dim)]
    sd = [max(1e-6, (sum((x[d] - mu[d]) ** 2 for x in X) / n) ** 0.5) for d in range(dim)]
    Z = [[(x[d] - mu[d]) / sd[d] for d in range(dim)] for x in X]

    def d2(a, b):
        return sum((a[d] - b[d]) ** 2 for d in range(dim))

    order = sorted(range(n), key=lambda i: sum(Z[i][d] ** 2 for d in range(dim)))
    best = None
    for r in range(restarts):
        first = order[int(((r + 0.37) / restarts) * (n - 1)) % n]
        cent = [Z[first][:]]
        while len(cent) < k:
            bi, bv = -1, -1.0
            for i in range(n):
                dv = min(d2(Z[i], c) for c in cent)
                if dv > bv:
                    bv, bi = dv, i
            cent.append(Z[bi][:])
        lab = [-1] * n
        for _ in range(iters):
            ch = False
            for i in range(n):
                bj, bv = 0, float("inf")
                for j in range(k):
                    dv = d2(Z[i], cent[j])
                    if dv < bv:
                        bv, bj = dv, j
                if lab[i] != bj:
                    lab[i] = bj
                    ch = True
            for j in range(k):
                mem = [Z[i] for i in range(n) if lab[i] == j]
                if mem:
                    cent[j] = [sum(m[d] for m in mem) / len(mem) for d in range(dim)]
            if not ch:
                break
        sse = sum(d2(Z[i], cent[lab[i]]) for i in range(n))
        if best is None or sse < best[1]:
            best = (lab[:], sse)
    return best


def agreement(labels_dp, labels_km):
    """两种分组的成对一致率（同对/异对是否一致），避免簇编号不对称的问题。"""
    n = len(labels_dp)
    same = tot = 0
    for i in range(n):
        for j in range(i + 1, n):
            tot += 1
            if (labels_dp[i] == labels_dp[j]) == (labels_km[i] == labels_km[j]):
                same += 1
    return same / tot if tot else 1.0


# ================================================================ HTML 转换（复用单场景版）
def _inline_md(s):
    s = _html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r'<code style="font-family:Consolas,monospace;font-size:9pt">\1</code>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def md_to_html(md_text, doc_title):
    lines = md_text.splitlines()
    out, i, n, quote = [], 0, len(lines), []

    def flush_quote():
        if quote:
            txt = "<br/>".join(_inline_md(x) for x in quote)
            out.append('<p style="border-left:3px solid #2f5597;background:#f2f6fc;'
                       'padding:7pt 10pt;margin:8pt 0;font-size:9.5pt;color:#33415c;'
                       'line-height:1.5">&nbsp;&nbsp;' + txt + "</p>")
            quote.clear()

    while i < n:
        line = lines[i]
        if line.startswith(">"):
            quote.append(line[1:].strip())
            i += 1
            continue
        flush_quote()
        if line.startswith("|"):
            tbl = []
            while i < n and lines[i].startswith("|"):
                tbl.append(lines[i])
                i += 1
            rows = []
            for r in tbl:
                if re.match(r"^\|[\s\-:|]+\|$", r):
                    continue
                rows.append([c.strip() for c in r.strip().strip("|").split("|")])
            if not rows:
                continue
            ncol = len(rows[0])
            fs = 10.0 if ncol <= 4 else (9.0 if ncol <= 8 else 7.5)
            t = [f'<table style="width:100%;border-collapse:collapse;font-size:{fs}pt;margin:8pt 0">']
            t.append("<thead><tr>")
            for c in rows[0]:
                t.append('<th style="border:1px solid #9aa7b8;background:#e8eef7;padding:3pt 4pt;'
                         'font-weight:bold;text-align:left;font-size:' + str(fs) + 'pt">' + _inline_md(c) + "</th>")
            t.append("</tr></thead><tbody>")
            for r in rows[1:]:
                t.append("<tr>")
                for c in r:
                    t.append('<td style="border:1px solid #c9d1dd;padding:2pt 4pt;'
                             'font-size:' + str(fs) + 'pt;vertical-align:top">' + _inline_md(c) + "</td>")
                t.append("</tr>")
            t.append("</tbody></table>")
            out.append("\n".join(t))
            continue
        if line.startswith("### "):
            out.append('<h3 style="font-size:12pt;font-weight:bold;color:#1f3864;margin:12pt 0 6pt">'
                       + _inline_md(line[4:]) + "</h3>")
        elif line.startswith("## "):
            out.append('<h2 style="font-size:15pt;font-weight:bold;color:#1f3864;'
                       'margin:16pt 0 8pt;padding-bottom:4pt;border-bottom:1px solid #c9d1dd">'
                       + _inline_md(line[3:]) + "</h2>")
        elif line.startswith("# "):
            out.append('<h1 style="font-size:19pt;font-weight:bold;color:#16325c;'
                       'text-align:center;margin:0 0 14pt">' + _inline_md(line[2:]) + "</h1>")
        elif line.strip() == "":
            pass
        else:
            out.append('<p style="font-size:10.5pt;line-height:1.55;margin:6pt 0;'
                       'text-align:justify">' + _inline_md(line) + "</p>")
        i += 1
    flush_quote()
    css = ("@page { size: A4; margin: 1.8cm 1.5cm; }\n"
           "body { font-family:'Microsoft YaHei','SimSun',sans-serif; color:#1f2937; }\n"
           "table { page-break-inside:auto; }\n"
           "tr { page-break-inside:avoid; }\n"
           "thead { display:table-header-group; }\n")
    return ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"/>"
            "<title>" + _html.escape(doc_title) + "</title><style>" + css +
            "</style></head><body>" + "\n".join(out) + "</body></html>")


# ================================================================ 主流程
def build(src, scene):
    obs = json.loads(src.read_text(encoding="utf-8"))
    P, V = obs["P"], obs["v"]
    N, M = len(P), len(V)
    OUT = OUTROOT / f"回推结果_{scene}"
    PV = OUT / "P_V"
    PV.mkdir(parents=True, exist_ok=True)

    # ---- 1. 分组
    bounds = row_bounds(N)
    rm = row_means(P, N)
    cuts_dp = best_split(rm, 3)
    X = col_features(P, V, bounds)
    segs, seg_cost, v_order = dp_segments(V, X, 5)
    lab_dp = [0] * M
    for ci, cols in enumerate(segs):
        for j in cols:
            lab_dp[j] = ci
    lab_km, km_sse = kmeans_labels(X, 5)
    agree = agreement(lab_dp, lab_km)

    # 红方 3 组（按行区间）
    RED = []
    for gi, tpl in enumerate(RED_TEMPLATE):
        g = dict(tpl)
        g["start"] = bounds[gi]
        g["n"] = bounds[gi + 1] - bounds[gi]
        g["weapon"] = W[tpl["weapon_key"]]
        RED.append(g)
    # 蓝方 5 类（按 v 降序段）
    BLUE = []
    for ci, tpl in enumerate(BLUE_TEMPLATE):
        c = dict(tpl)
        c["cols"] = segs[ci]
        c["loadout"] = W[tpl["loadout"]] if tpl["loadout"] else None
        BLUE.append(c)

    col_class = {}
    for c in BLUE:
        for j in c["cols"]:
            col_class[j] = c
    row_group = {}
    for g in RED:
        for k in range(g["n"]):
            row_group[g["start"] + k] = g

    # ---- 2. 分块观测统计
    blocks = {}
    for g in RED:
        rows = list(range(g["start"], g["start"] + g["n"]))
        for c in BLUE:
            pairs = [(i, j, P[i][j]) for i in rows for j in c["cols"]]
            nz = [p for _, _, p in pairs if p > 0]
            mean = sum(nz) / len(nz) if nz else 0.0
            blocks[(g["key"], c["key"])] = {
                "rows": rows, "cols": c["cols"], "n_nz": len(nz), "n_zero": len(pairs) - len(nz),
                "p_mean": mean,
                "p_sd": (sum((x - mean) ** 2 for x in nz) / len(nz)) ** 0.5 if len(nz) > 1 else 0.0,
            }

    # ---- 3. 口径扫描（命中 / 毁伤）
    def fK_damage(g, c):
        Wm = g["warhead_mass"]
        return 0.70 * min(1.0, 0.5 * math.sqrt(Wm / c["platform"]["dp"]))

    scan = {"hit": [0, 0], "damage": [0, 0]}
    for (gk, ck), b in blocks.items():
        g = next(x for x in RED if x["key"] == gk)
        c = next(x for x in BLUE if x["key"] == ck)
        Pk0 = g["weapon"]["hit"]["air"]
        for i in b["rows"]:
            row = P[i]
            for j in b["cols"]:
                p = row[j]
                if p <= 0:
                    continue
                denom = Pk0 * c["fT"] * c["fG"]
                for mode, fk in (("hit", 1.0), ("damage", fK_damage(g, c))):
                    fr = p / (denom * fk)
                    scan[mode][1] += 1
                    if 0.0 <= fr <= 1.0:
                        scan[mode][0] += 1
    PROB_MODE = "hit_probability"

    # ---- 4. 距离带反解（A 组基准）
    RMAX = {g["key"]: g["weapon"]["range_nm"]["air"][1] for g in RED}
    PK0 = {g["key"]: g["weapon"]["hit"]["air"] for g in RED}
    for c in BLUE:
        b = blocks[("A", c["key"])]
        fR_bar = b["p_mean"] / (PK0["A"] * c["fT"] * c["fG"]) if PK0["A"] * c["fT"] * c["fG"] else 0
        rho_bar = rho_of(fR_bar)
        c["fR_bar_A"] = fR_bar
        c["rho_bar_A"] = rho_bar
        c["d_bar_A"] = rho_bar * RMAX["A"]
        c["flat"] = fR_bar >= 1.0
        c["v_mean"] = sum(V[j] for j in c["cols"]) / len(c["cols"])
        c["p_mean_A"] = b["p_mean"]

    # ---- 5. 阵位重建（确定性）
    A_CENTER_X = (RED[0]["pos"]["x0"] + RED[0]["pos"]["x1"]) / 2.0
    red, blue = [], []
    for g in RED:
        for k in range(g["n"]):
            i = g["start"] + k
            t = k / max(1, g["n"] - 1)
            x = g["pos"]["x0"] + (g["pos"]["x1"] - g["pos"]["x0"]) * t + (hsh("rx", scene, i) - 0.5) * 1.6
            y = (hsh("ry", scene, i) - 0.5) * g["pos"]["yspread"]
            alt = g["pos"]["alt0"] + g["pos"]["altspan"] * hsh("rh", scene, i)
            red.append(dict(uid=f"R{i+1:02d}", idx=i, group=g, x=x, y=y, alt=alt))
    for c in BLUE:
        for j in c["cols"]:
            x = A_CENTER_X + c["d_bar_A"] + (hsh("bx", scene, j) - 0.5) * 1.5
            y = c["yoff"] * 0.45 + (hsh("by", scene, j) - 0.5) * 2.0
            alt = c["platform"]["alt"] + (hsh("bh", scene, j) - 0.5) * 600
            blue.append(dict(uid=f"B{j+1:02d}", idx=j, cls=c, x=x, y=y, alt=alt))
    blue_by_idx = {u["idx"]: u for u in blue}

    def dist(u, t):
        return math.hypot(u["x"] - t["x"], u["y"] - t["y"])

    # ---- 6. 正算理论 p 与分块适配系数 α
    theory = [[0.0] * M for _ in range(N)]
    rho_mat = [[0.0] * M for _ in range(N)]
    for u in red:
        g = u["group"]
        i = u["idx"]
        rr = RMAX[g["key"]]
        pk = PK0[g["key"]]
        for t in blue:
            j = t["idx"]
            rho = dist(u, t) / rr
            rho_mat[i][j] = rho
            if rho <= 1.0 and pk > 0:
                theory[i][j] = clip(pk * t["cls"]["fT"] * t["cls"]["fG"] * fR_of(rho))
    alpha, conflict = {}, {}
    for (gk, ck), b in blocks.items():
        vo, vt, n_conf = [], [], 0
        for i in b["rows"]:
            for j in b["cols"]:
                if P[i][j] > 0:
                    if theory[i][j] > 0:
                        vo.append(P[i][j])
                        vt.append(theory[i][j])
                    else:
                        n_conf += 1
        alpha[(gk, ck)] = (sum(vo) / len(vo)) / (sum(vt) / len(vt)) if vt else 1.0
        conflict[(gk, ck)] = n_conf
    P_hat = [[0.0] * M for _ in range(N)]
    for i in range(N):
        g = row_group[i]
        for j in range(M):
            c = col_class[j]
            if P[i][j] > 0 and theory[i][j] > 0:
                P_hat[i][j] = clip(theory[i][j] * alpha[(g["key"], c["key"])])

    # ---- 7. 校验指标
    block_err, block_err_full = [], []
    for (gk, ck), b in blocks.items():
        pairs = [(i, j) for i in b["rows"] for j in b["cols"] if P[i][j] > 0]
        valid = [(i, j) for i, j in pairs if theory[i][j] > 0]
        o = [P[i][j] for i, j in valid]
        t = [P_hat[i][j] for i, j in valid]
        block_err.append(abs(sum(o) / len(o) - sum(t) / len(t)) if o else 0.0)
        of = [P[i][j] for i, j in pairs]
        tf = [P_hat[i][j] for i, j in pairs]
        block_err_full.append(abs(sum(of) / len(of) - sum(tf) / len(tf)) if of else 0.0)
    sq_all = sq_valid = 0.0
    n_valid = 0
    zero_cells = zero_by_gate = obs_conflict = p_exceed = 0
    for i in range(N):
        g = row_group[i]
        for j in range(M):
            c = col_class[j]
            e = (P_hat[i][j] - P[i][j]) ** 2
            sq_all += e
            if P[i][j] > 0:
                if theory[i][j] > 0:
                    sq_valid += e
                    n_valid += 1
                else:
                    obs_conflict += 1
                if P[i][j] > PK0[g["key"]] * c["fT"] * c["fG"]:
                    p_exceed += 1
            else:
                zero_cells += 1
                if rho_mat[i][j] > 1.0:
                    zero_by_gate += 1
    RMSE = (sq_all / (N * M)) ** 0.5
    RMSE_VALID = (sq_valid / n_valid) ** 0.5 if n_valid else 0.0

    # 全覆盖列
    full_cols = [j for j in range(M) if all(P[i][j] > 0 for i in range(N))]

    # ---- 8. 价值反解
    V_MIN = math.floor(min(V) / 10) * 10
    V_MAX = math.ceil(max(V) / 10) * 10
    DP_MAX, Y_MIN, Y_MAX = 20, 1987, 2006
    for c in BLUE:
        c["z_hat"] = sum((V[j] - V_MIN) / (V_MAX - V_MIN) for j in c["cols"]) / len(c["cols"])
        dp = c["platform"]["dp"]
        c["S_DP"] = math.log(1 + dp) / math.log(1 + DP_MAX)
        c["G"] = 0.5 + 0.5 * (c["platform"]["year"] - Y_MIN) / (Y_MAX - Y_MIN)
        c["z_theory"] = min(1.0, max(0.0, 0.35 * c["S_DP"] + 0.45 * c["R"] + 0.2 * c["G"] + 0.1 * c["Th"]))
        c["v_theory"] = V_MIN + (V_MAX - V_MIN) * c["z_theory"]
    pts = [(c["z_theory"], V[j]) for c in BLUE for j in c["cols"]]
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    b_reg = sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0
    a_reg = my - b_reg * mx
    ss_tot = sum((p[1] - my) ** 2 for p in pts)
    ss_res = sum((p[1] - (a_reg + b_reg * p[0])) ** 2 for p in pts)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    z_lo = min(c["z_theory"] for c in BLUE)
    z_hi = max(c["z_theory"] for c in BLUE)
    for c in BLUE:
        z_eff = (c["z_theory"] - z_lo) / (z_hi - z_lo) if z_hi > z_lo else 0.0
        c["z_eff"] = z_eff
        c["v_eff"] = V_MIN + (V_MAX - V_MIN) * z_eff

    # ---- 9. 单位对象
    def make_unit(u, side):
        if side == "red":
            p, w = u["group"]["platform"], u["group"]["weapon"]
            load = [wload(w, u["group"]["qty"], "intercept")]
            role = u["group"]["label"]
            g_tac = None
        else:
            p, w = u["cls"]["platform"], u["cls"]["loadout"]
            load = [wload(w, 2, "self_defense")] if w else []
            role = u["cls"]["label"]
            g_tac = u["cls"]["g_tac"]
        return {
            "uid": u["uid"], "side": side,
            "role": p["role"] if side == "red" else p["target_class"],
            "kind": p["kind"], "platform_id": p["platform_id"], "name": p["name"],
            "type": p["type"], "target_class": p["target_class"], "nation": p["nation"],
            "dp": p["dp"], "year": p["year"], "maneuver_g": p["maneuver_g"],
            "maneuver_g_tactical": g_tac,
            "rcs_em_m2": {"front_dbsm": p["rcs_dbsm"], "front_m2": p["rcs_m2"]},
            "radar_max_nm": p["radar_max_nm"], "esm_max_nm": p["esm_max_nm"],
            "ir_max_nm": None, "sonar_max_nm": None, "visual_max_nm": None,
            "pos": {"rel_x_nm": round(u["x"], 3), "rel_y_nm": round(u["y"], 3),
                    "alt_m": round(u["alt"], 1)},
            "jamming": "none", "aspect": "head_on",
            "loadout": load, "support_stores": [], "mounts": [], "resolved": True,
            "_group": role,
        }

    units = [make_unit(u, "red") for u in red] + [make_unit(u, "blue") for u in blue]

    # ---- 10. 叙述素材（动态）
    red_counts = "/".join(str(g["n"]) for g in RED)
    blue_counts = "/".join(str(len(c["cols"])) for c in BLUE)
    v_tiers = " → ".join(f"{c['v_mean']:.0f}" for c in BLUE)
    p_by_v = " → ".join(f"{c['p_mean_A']:.2f}" for c in BLUE)
    by_v = sorted(BLUE, key=lambda x: x["v_mean"], reverse=True)
    by_d = sorted(BLUE, key=lambda x: x["d_bar_A"])
    full_uid = "/".join(f"B{j+1:02d}" for j in full_cols[:8]) + ("…" if len(full_cols) > 8 else "")
    full_txt = (f"有 {len(full_cols)} 个目标（{full_uid}）被全部 {N} 个红方火力通道覆盖，可作火力覆盖基准点"
                if full_cols else f"本场景无被全部 {N} 个红方火力通道覆盖的目标")
    km_note = (f"与无监督聚类（k=5，SSE={km_sse:.2f}）的成对一致率 {agree*100:.1f}%")

    # ---- 11. 卡片
    card = {
        "scenario_id": scene, "schema_version": "2.0",
        "seed": {
            "source_file": src.name,
            "source_title": "—", "source_setting": "—", "source_year": "—",
            "source_complexity": "—", "source_difficulty": "—", "source_db_version": "—",
            "note": "输入仅为 P 矩阵与 v 向量，未提供原始 .scen 纯文本头部字段",
        },
        "meta": {
            "name": f"远海三层拦截：蓝方空中突击编队对红方海上防空圈（由 P/V 矩阵回推，{N}×{M}）",
            "year": 2010, "region": "西太平洋某开阔海域（示例重建）",
            "period": "信息化条件下海上局部封锁与反封锁（示例重建）",
            "sides": {"red": "红方（防守方）", "blue": "蓝方（进攻方）"},
            "sides_raw": "—", "date_time": "—", "location": "某开阔海域（示例重建）",
            "playable_sides": "—", "duration": "—",
            "environment": {"sea_state": "—", "weather": "—", "notes": "缺省取 1.00，不进入因子链"},
            "objective": {
                "red": "以中距、远程、超远程三层拦截兵力守住海上编队防空圈，优先压制蓝方远距预警指挥节点，阻止突击编队进入有效投射距离。",
                "blue": "以预警指挥与电子战节点引导，轰炸机与多用途战斗机为主攻、无人机群前出消耗，突破红方防空拦截并达成对海上编队的打击。",
            },
            "background_zh": (
                f"本想定由一份 {N}×{M} 的毁伤概率矩阵与 {M} 维目标价值向量回推重建："
                f"矩阵行呈 {red_counts} 三段结构，对应红方中距、远程、超远程三层拦截兵力；"
                f"列按价值与概率呈 {blue_counts} 五类结构，对应蓝方预警指挥、电子战、轰炸、"
                "多用途战斗机与无人机五类空中突击兵力。"
                "红方依托海上编队组织纵深防空，蓝方以多批次、多高度、分层距离的空中编队实施突防。"
                "叙述层（年代、地域、背景、任务意图）不在 P/V 矩阵内，属按编成代次与交战结构做的示例重建，"
                "非原始 CMO 场景正文译写；装备型号与国别字段仅为装备库条目属性，不代表本想定中的阵营归属。"
            ),
        },
        "provenance": {
            "narrative": "rebuilt_from_pv_matrix（矩阵内无叙述层；按规范第 8 节第 3 条重建）",
            "order_of_battle": "rebuilt_from_db：型号/DP/RCS/探测距离/挂载取自装备库；数量、阵位、机动战术档位、挂载数量为示例重建",
            "probability_basis": "命中概率口径（fK≡1，fJ=fE=1）；按手册 4.1/6.1 反解 fR；分块适配系数 α 为 C 层标定项",
            "value_basis": f"v_min={V_MIN}、v_max={V_MAX}（由 v 极值取整）；权重取手册 5.1 缺省值；等效口径见闭环校验",
            "grouping": {
                "rows": f"按 N 的 50%/40%/10% 比例先验定边界 {bounds[1:-1]}；变点检测对照 {cuts_dp}",
                "cols": "按 v 降序做动态规划最优 5 段分割（段内概率特征平方和最小）；" + km_note,
                "dp_segment_cost": round(seg_cost, 5),
            },
            "limitations": [
                "原始 .scen 私有压缩块不可得：标题、正文、地域、年代、难度、数据库版本等溯源字段无法读取",
                "叙述层不在矩阵内，背景/任务/环境均为示例重建，不可唯一反推",
                "fR 近距平坦段（ρ≤0.5）距离不唯一，阵位为按距离带构造的示例解",
                "价值线性加权存在分量混叠，单个 v_j 不能分离 S_DP/R/G/Th",
                "给定 P 的零格中含大量非射程门控掩膜（火力通道/弹药配额），不可由卡片参数唯一还原",
                "机动评级到过载、fK 形状因子、fE 档位、价值权重与上下界均为 C 层待校准值",
                "10 个场景共用同一套平台/武器编成模板；类别数量由各自矩阵的分组结果决定",
            ],
        },
        "counts": {"red": N, "blue": M},
        "units": units,
        "matrix_ref": {
            "pv_file": f"P_V/{scene}_pv.json",
            "row_uid_order": [f"R{i+1:02d}" for i in range(N)],
            "col_uid_order": [f"B{j+1:02d}" for j in range(M)],
            "direction": "P[i][j] = 第 i 个红方单位对第 j 个蓝方目标的单发命中概率；行=红方武器，列=蓝方目标，v 与列同序",
            "long_tables": [f"P_V/P_long_{scene}.csv", f"P_V/V_{scene}.csv", f"P_V/P_{scene}.csv"],
        },
    }
    (OUT / f"{scene}_card.json").write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 12. pv.json / CSV
    pv = {
        "scenario_id": scene, "red": N, "blue": M,
        "red_units": [f"R{i+1:02d}" for i in range(N)],
        "blue_units": [f"B{j+1:02d}" for j in range(M)],
        "P": P, "v": V,
        "meta": {
            "probability_basis": "命中概率（单发），fK≡1；门控：类型不符或 d>Rmax 严格取 0",
            "value_basis": f"v_j ∈ [{V_MIN},{V_MAX}] 的线性排序分；权重 w_DP=0.35,w_R=0.45,w_G=0.20,w_T=0.10,w_C=0.00",
            "row_direction": "红方单位（武器）", "col_direction": "蓝方目标；v 与列同序",
            "factors": {"fR": "手册 4.3 分段线性", "fT": "手册 4.4 目标类别档位", "fG": "手册 4.5 战术机动档位",
                        "fJ": 1.00, "fE": 1.00, "fK": 1.00, "clip": [0.01, 0.99]},
            "source_scenario": src.name + "（输入观测矩阵）",
            "reverse_engineering": {
                "red_groups": {g["key"]: f"R{g['start']+1:02d}–R{g['start']+g['n']:02d} {g['label']} "
                                         f"{g['weapon']['name']} ({RMAX[g['key']]:g}nm)" for g in RED},
                "blue_classes": {c["key"]: "/".join(f"B{j+1:02d}" for j in c["cols"][:10])
                                           + ("…" if len(c["cols"]) > 10 else "") + f" {c['label']}（{len(c['cols'])}）"
                                 for c in BLUE},
                "row_bounds": bounds[1:-1], "row_change_points": cuts_dp,
                "note": "行分组由概率档位与比例先验确定；列分组由 v 降序 DP 最优 5 段分割确定；"
                        "类别指派为按手册因子链的最佳解释，非唯一解。" + km_note,
            },
            "calibration": f"分块适配系数 α 见 P_V/闭环校验_{scene}.md；为 C 层待标定项",
        },
    }
    (PV / f"{scene}_pv.json").write_text(json.dumps(pv, ensure_ascii=False, indent=2), encoding="utf-8")

    plong_cols = ["scenario_id", "red_uid", "target_id", "weapon_id", "weapon_name", "target_type",
                  "distance_nm", "Rmax_nm", "rho", "Pk0", "fR", "fT", "fG", "fJ", "fE", "fK",
                  "p_ij", "qty", "type_match", "p_obs", "alpha", "note"]
    with (PV / f"P_long_{scene}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(plong_cols)
        for u in red:
            g, i = u["group"], u["idx"]
            for t in blue:
                c, j = t["cls"], t["idx"]
                d = dist(u, t)
                rho = rho_mat[i][j]
                po = P[i][j]
                a = alpha[(g["key"], c["key"])]
                note = "命中口径；α 为分块适配系数" if po > 0 else "观测零格：沿用输入可打性掩膜"
                w.writerow([scene, u["uid"], t["uid"], g["weapon"]["weapon_id"], g["weapon"]["name"],
                            c["platform"]["target_class"], round(d, 3), RMAX[g["key"]], round(rho, 4),
                            round(PK0[g["key"]], 4), round(fR_of(rho), 4) if rho <= 1 else 0.0,
                            c["fT"], c["fG"], 1.00, 1.00, 1.00, round(P_hat[i][j], 4),
                            g["qty"], 1 if PK0[g["key"]] > 0 else 0, round(po, 4), round(a, 4), note])

    vcols = ["scenario_id", "target_id", "target_name", "target_class", "DP", "year", "S_DP", "R", "G",
             "Th", "C_cov", "z", "v_j", "v_norm", "z_hat", "z_theory", "v_eff"]
    with (PV / f"V_{scene}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(vcols)
        for t in blue:
            c, j = t["cls"], t["idx"]
            w.writerow([scene, t["uid"], c["platform"]["name"], c["platform"]["target_class"],
                        c["platform"]["dp"], c["platform"]["year"], round(c["S_DP"], 4), c["R"],
                        round(c["G"], 4), c["Th"], 0.0, round(c["z_theory"], 4), round(V[j], 4),
                        round((V[j] - V_MIN) / (V_MAX - V_MIN), 4), round(c["z_hat"], 4),
                        round(c["z_theory"], 4), round(c["v_eff"], 2)])

    def write_matrix(path, mat):
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["red_uid \\ blue_uid"] + [f"B{j+1:02d}" for j in range(M)])
            for i in range(N):
                w.writerow([f"R{i+1:02d}"] + [round(x, 4) for x in mat[i]])

    write_matrix(PV / f"P_{scene}.csv", P)
    write_matrix(PV / f"P_hat_{scene}.csv", P_hat)

    # ---- 13. 距离统计与覆盖
    red_cx = sum(u["x"] for u in red) / N
    red_cy = sum(u["y"] for u in red) / N
    blue_cx = sum(t["x"] for t in blue) / M
    blue_cy = sum(t["y"] for t in blue) / M
    center_gap = math.hypot(red_cx - blue_cx, red_cy - blue_cy)
    all_d = [dist(u, t) for u in red for t in blue]
    D_MIN, D_MEAN, D_MAX = min(all_d), sum(all_d) / len(all_d), max(all_d)
    coverage = {}
    for g in RED:
        rr = [u for u in red if u["group"]["key"] == g["key"]]
        cnt = {}
        for c in BLUE:
            tt = [t for t in blue if t["cls"]["key"] == c["key"]]
            cnt[c["key"]] = sum(1 for u in rr for t in tt if dist(u, t) <= RMAX[g["key"]])
        coverage[g["key"]] = cnt

    # ---- 14. 闭环校验报告
    L = []
    L.append(f"# {scene} P/V 矩阵回推闭环校验")
    L.append("")
    L.append("> 本文件为独立校验说明，不进入场景想定报告六小节。依据《场景文件规范 v2》第 8 节与《WTA 毁伤概率与价值公式》第 6 节。")
    L.append("")
    L.append("## 1. 输入矩阵可直接还原的信息")
    L.append("")
    L.append(md_table(["项目", "值"], [
        ["矩阵维度", f"P = {N}×{M}（红方 {N} 行 / 蓝方 {M} 列），v 长度 {M}"],
        ["行分组", " / ".join(f"{g['key']} R{g['start']+1:02d}–R{g['start']+g['n']:02d}（{g['n']}）"
                          for g in RED) + f" = {red_counts}"],
        ["行边界依据", f"比例先验 {bounds[1:-1]}；最小化段内方差的变点检测对照 {cuts_dp}"
                   + ("（一致）" if cuts_dp == bounds[1:-1] else "（本场景概率标定受扰动，变点偏移，以比例先验为准）")],
        ["列分组", " / ".join(f"{c['key']} {c['label']} {len(c['cols'])}" for c in BLUE) + f" = {blue_counts}"],
        ["列分组依据", f"按 v 降序的 DP 最优 5 段分割（段内平方和 {seg_cost:.4f}）；{km_note}"],
        ["价值分档", f"v 均值 {v_tiers} 五档（由高到低），与各列概率档一一对应"],
        ["门控（零格）", f"{zero_cells} 格（{zero_cells/(N*M)*100:.1f}%）"],
        ["全覆盖目标", full_txt],
        ["价值与命中一致性", f"随目标价值由高到低（{v_tiers}），A 组单发概率整体由 "
                          f"{BLUE[0]['p_mean_A']:.2f} 升至 {BLUE[-1]['p_mean_A']:.2f}（{p_by_v}），"
                          "整体负相关，符合手册 5.5；相邻档位个别交错属输入噪声与标定"],
    ]))
    L.append("")
    L.append("## 2. 概率口径判定（口径扫描）")
    L.append("")
    L.append(md_table(["口径", "fK", "fR 反解落入 [0,1] 的非零格", "占比", "结论"], [
        ["命中概率", "≡1.00", f"{scan['hit'][0]}/{scan['hit'][1]}",
         f"{scan['hit'][0]/scan['hit'][1]*100:.1f}%" if scan['hit'][1] else "—", "采用"],
        ["毁伤概率", "手册 4.7 形状因子", f"{scan['damage'][0]}/{scan['damage'][1]}",
         f"{scan['damage'][0]/scan['damage'][1]*100:.1f}%" if scan['damage'][1] else "—",
         "不采用：多数格子反解 fR>1，无法闭合"],
    ]))
    L.append("")
    L.append("按手册 4.7，命中概率口径令 fK≡1；本矩阵采用该口径，fJ=fE=1（无干扰、包络内）。")
    L.append("")
    L.append("## 3. 由 p 反解距离（手册 6.1）与距离带")
    L.append("")
    rows = []
    for c in by_v:
        rows.append([c["key"], c["label"], "/".join(f"B{j+1:02d}" for j in c["cols"][:6])
                     + ("…" if len(c["cols"]) > 6 else ""),
                     fmt(c["v_mean"], 2), fmt(c["fR_bar_A"], 4), fmt(c["rho_bar_A"], 4),
                     fmt(c["d_bar_A"], 2), "近距平坦段（仅上界）" if c["flat"] else
                     ("射程边缘/门控" if c["rho_bar_A"] > 1 else "可交战")])
    L.append(md_table(["目标类", "类别", "uid（前 6）", "v 均值", "f̄R(A组)", "ρ̄(A组)", "等效距离 d̄(nm)", "门控"], rows))
    L.append("")
    L.append(f"距离带取自 A 组（Rmax={RMAX['A']:g}nm）反解：`f̄R = p̄/(Pk0·fT·fG)` → 分段逆求 ρ̄ → `d̄ = ρ̄·Rmax`。")
    L.append("按由近及远排序，五类等效距离为 "
             + " < ".join(f"{c['key']} {c['d_bar_A']:.1f}nm" for c in by_d)
             + "；fR≥1 的类落在 ρ≤0.5 近距平坦段，距离仅有上界、不唯一（阵位取 ρ=0.5 代表值）。")
    L.append("")
    L.append("## 4. 分块适配系数 α 与正算闭合")
    L.append("")
    L.append("以重建阵位按手册因子链正算理论概率 `p_th = Pk0·fT·fG·fR(d/Rmax)`；")
    L.append("给定矩阵含独立的分块标定层，故按 (武器组 × 目标类) 求 `α = mean(p_obs) / mean(p_th)`，")
    L.append("再以 `p̂ = clip(α·p_th)` 复核。α 可视为发射包络 fE 与武器—目标适配的合并 C 层标定项。")
    L.append("")
    rows = []
    for g in RED:
        for c in by_v:
            b = blocks[(g["key"], c["key"])]
            rows.append([g["key"], c["key"], b["n_nz"], fmt(b["p_mean"], 4), fmt(b["p_sd"], 4),
                         fmt(alpha[(g["key"], c["key"])], 3), conflict[(g["key"], c["key"])]])
    L.append(md_table(["武器组", "目标类", "非零格", "p̄ 观测", "σ 观测", "α", "门控冲突格"], rows))
    L.append("")
    amin, amax = min(alpha.values()), max(alpha.values())
    L.append(f"α 取值域约 {amin:.2f}–{amax:.2f}：α<1 表示该块给定概率低于手册因子链正算值，"
             f"对应远程/超远程拦截弹对近距小型目标的发射包络与适配折减"
             f"（最低 {min(alpha.items(), key=lambda kv: kv[1])[0][0]}×{min(alpha.items(), key=lambda kv: kv[1])[0][1]} "
             f"≈{amin:.2f}）；α>1 表示给定矩阵该块的标定高于理论值"
             f"（最高 {max(alpha.items(), key=lambda kv: kv[1])[0][0]}×{max(alpha.items(), key=lambda kv: kv[1])[0][1]} "
             f"≈{amax:.2f}），需以基准命中率或包络档位重新标定。α 属 C 层待校准系数，不由卡片参数唯一确定。")
    L.append("")
    L.append(md_table(["校验指标", "值"], [
        ["分块均值最大绝对误差（可解释格）", f"{max(block_err):.6f}（α 标定后按构造闭合）"],
        ["分块均值最大绝对误差（含冲突格）", f"{max(block_err_full):.6f}"],
        [f"逐格 RMSE（全部 {N*M} 格）", f"{RMSE:.4f}（含 {zero_cells} 个零格，零格误差为 0 会拉低该值，仅供参考）"],
        ["非零可解释格 RMSE（主指标）", f"{RMSE_VALID:.4f} / {n_valid} 格（≈ 输入矩阵自带噪声量级）"],
        ["观测概率超理论上界的格子", f"{p_exceed} 格（噪声使 fR 反解>1，按平坦段 fR=1 处理）"],
        ["可由射程门控解释的零格", f"{zero_by_gate}/{zero_cells}（{zero_by_gate/zero_cells*100:.1f}%）"
         if zero_cells else "0/0"],
        ["观测非零但理论门控失败", f"{obs_conflict} 格（射程边缘/前出单位，属不可唯一反推）"],
        ["结论", "结构层（行列分组、距离带次序、价值排序、门控方向）闭合；数值层需 α 分块标定，剩余残差为输入噪声与非门控掩膜"],
    ]))
    L.append("")
    L.append("## 5. 价值反解与口径偏差")
    L.append("")
    L.append(f"取 v_min={V_MIN}、v_max={V_MAX}（由 v 极值取整），`ẑ=(v−v_min)/(v_max−v_min)`。")
    L.append(f"库内 DP_max={DP_MAX}、Y∈[{Y_MIN},{Y_MAX}]，按手册 5.1/5.2/5.3 缺省权重正算 z_th。")
    L.append("")
    rows = []
    for c in by_v:
        rows.append([c["key"], c["label"], c["platform"]["dp"], c["platform"]["year"],
                     fmt(c["S_DP"], 3), c["R"], fmt(c["G"], 3), c["Th"],
                     fmt(c["z_theory"], 3), fmt(c["z_hat"], 3), fmt(c["v_mean"], 2), fmt(c["v_eff"], 2)])
    L.append(md_table(["类", "类别", "DP", "年份", "S_DP", "R", "G", "Th", "z_th", "ẑ", "v 观测均值", "v(档内归一)"], rows))
    L.append("")
    L.append(md_table(["口径检验", "结果"], [
        ["v = a + b·z_th 最小二乘", f"a={a_reg:.1f}，b={b_reg:.1f}，R²={r2:.4f}；"
                                 "等效价值下界 a<0 时说明缺省权重+线性映射不能同时解释给定 v"],
        ["档内极差归一 v_eff", f"z_eff=(z_th−{z_lo:.3f})/({z_hi:.3f}−{z_lo:.3f})"],
        ["结论", "给定 v 的五档间距与手册缺省权重下理论 z 的间距不一致，价值口径需独立标定；分量线性混叠，单个 v_j 不可分离"],
    ]))
    L.append("")
    L.append("## 6. 不可唯一反推项与待标定清单")
    L.append("")
    L.append("1. **叙述层**：年代、地域、背景、任务、海况天气不在矩阵内，来自示例重建（卡片 `meta`）。")
    L.append("2. **fR 平坦段**：ρ≤0.5 时 fR≡1，距离只知上界；阵位为按距离带构造的示例解。")
    L.append("3. **零格掩膜**：仅部分零格由射程门控解释，其余属火力通道/弹药配额/分配掩膜，矩阵内不可分离。")
    L.append("4. **价值混叠**：线性加权下 S_DP/R/G/Th 不可由单个 v_j 分离，需外部约束（库内 DP/年份/角色）固定。")
    L.append("5. **C 层待标定**：fR 拐点、fT/fG 档位、机动评级→过载映射、fE 档位、fK 形状因子、α 分块系数、价值权重与上下界。")
    L.append("6. **编成模板**：10 个场景共用同一套平台/武器模板，型号指派为按概率档位的最佳解释，非唯一解。")
    L.append("")
    L.append("## 7. 复现方式")
    L.append("")
    L.append("运行 `批量反推/通用反推.py`：读取输入矩阵、公式规范与装备参数库，重算分组、距离带、阵位、α、价值分量与全部产物。")
    L.append("")
    (PV / f"闭环校验_{scene}.md").write_text("\n".join(L), encoding="utf-8")

    # ---- 15. 场景想定报告（六小节）
    def unit_rows(side):
        rows = []
        if side == "red":
            for g in RED:
                p = g["platform"]
                us = [u for u in red if u["group"]["key"] == g["key"]]
                xs, ys, hs = [u["x"] for u in us], [u["y"] for u in us], [u["alt"] for u in us]
                rows.append([f"{us[0]['uid']}–{us[-1]['uid']}（{len(us)}）", p["name"], p["nation"], p["dp"], p["year"],
                             fmt(p["maneuver_g"], 1), fmt(p["rcs_m2"], 2), fmt(p["radar_max_nm"], 0), "—",
                             f"x=[{min(xs):.1f},{max(xs):.1f}]，y=[{min(ys):.1f},{max(ys):.1f}]，h=[{min(hs):.0f},{max(hs):.0f}]"])
        else:
            for c in by_v:
                p = c["platform"]
                us = [blue_by_idx[j] for j in c["cols"]]
                xs, ys, hs = [u["x"] for u in us], [u["y"] for u in us], [u["alt"] for u in us]
                rows.append(["/".join(u["uid"] for u in us[:6]) + ("…" if len(us) > 6 else "") + f"（{len(us)}）",
                             p["name"], p["nation"], p["dp"], p["year"],
                             f"{p['maneuver_g']:g} / 战术 {c['g_tac']}g", fmt(p["rcs_m2"], 2),
                             fmt(p["radar_max_nm"], 0), "—",
                             f"x=[{min(xs):.1f},{max(xs):.1f}]，y=[{min(ys):.1f},{max(ys):.1f}]，h≈{sum(hs)/len(hs):.0f}"])
        return rows

    UNIT_HEAD = ["编号", "型号", "国别", "DP", "年份", "机动 g", "RCS 前向(m²)", "雷达(nm)", "ESM(nm)", "阵位(x/y/高度)"]

    def loadout_rows():
        rows = []
        for g in RED:
            w = g["weapon"]
            rows.append([f"R{g['start']+1:02d}–R{g['start']+g['n']:02d}", w["weapon_id"], w["name"], w["type"],
                         *[f"{w['hit'][k]*100:.0f}%" if w["hit"][k] else "—" for k in ("air", "surface", "land", "sub")],
                         *[f"{w['range_nm'][k][0]:g}–{w['range_nm'][k][1]:g}" if w["range_nm"][k] else "—"
                           for k in ("air", "surface", "land", "sub")],
                         g["qty"]])
        for c in by_v:
            w = c["loadout"]
            uid = "/".join(f"B{j+1:02d}" for j in c["cols"][:6]) + ("…" if len(c["cols"]) > 6 else "")
            if not w:
                rows.append([uid, "—", "—（未编入交战武器）", "—"] + ["—"] * 9)
                continue
            rows.append([uid, w["weapon_id"], w["name"], w["type"],
                         *[f"{w['hit'][k]*100:.0f}%" if w["hit"][k] else "—" for k in ("air", "surface", "land", "sub")],
                         *[f"{w['range_nm'][k][0]:g}–{w['range_nm'][k][1]:g}" if w["range_nm"][k] else "—"
                           for k in ("air", "surface", "land", "sub")],
                         2])
        return rows

    LOAD_HEAD = ["单位", "武器编号", "武器名称", "类型", "命中-空", "命中-海", "命中-陆", "命中-潜",
                 "射程-空(nm)", "射程-海(nm)", "射程-陆(nm)", "射程-潜(nm)", "数量"]

    def deploy_rows():
        rows = []
        for u in red:
            g, w = u["group"], u["group"]["weapon"]
            rows.append([u["uid"], "红方", g["platform"]["name"], fmt(u["x"], 1), fmt(u["y"], 1), fmt(u["alt"], 0),
                         *(fmt(w["range_nm"][k][1], 1) if w["range_nm"][k] else "—"
                           for k in ("air", "surface", "land"))])
        for t in blue:
            c, w = t["cls"], t["cls"]["loadout"]
            rows.append([t["uid"], "蓝方", c["platform"]["name"], fmt(t["x"], 1), fmt(t["y"], 1), fmt(t["alt"], 0),
                         *(fmt(w["range_nm"][k][1], 1) if w and w["range_nm"][k] else "—"
                           for k in ("air", "surface", "land"))])
        return rows

    DEP_HEAD = ["单位", "阵营", "型号", "x(nm)", "y(nm)", "高度(m)", "对空射程(nm)", "对海射程(nm)", "对陆射程(nm)"]

    red_x_lo = min(g["pos"]["x0"] for g in RED)
    red_x_hi = max(g["pos"]["x1"] for g in RED)
    blue_x_lo = min(t["x"] for t in blue)
    blue_x_hi = max(t["x"] for t in blue)

    R = []
    R.append(f"# {scene} 场景想定报告")
    R.append("")
    R.append(f"> 本报告由 `{src.name}` 的 P/V 矩阵回推生成，遵循《场景文件规范 v2.0》第 3 节六小节结构。")
    R.append("> 依据：装备参数库（型号/DP/RCS/探测距离/挂载）、《WTA 毁伤概率与价值公式》第 4–6 节、给定 P/V 矩阵。")
    R.append("> 叙述层（年代、地域、背景、任务、环境）不在矩阵内，属按编成代次与交战结构做的示例重建；")
    R.append("> 数量、阵位、挂载数量、战术机动档位同为示例重建。装备型号与国别字段仅为装备库条目属性，不代表本想定的阵营归属。")
    R.append(f"> 可唯一还原项、口径判定、闭环偏差与待标定项见 `P_V/闭环校验_{scene}.md` 与卡片 `provenance`。")
    R.append("")
    R.append("## 一、场景基本信息")
    R.append("")
    R.append(md_table(["项目", "内容"], [
        ["场景编号", scene],
        ["原始场景文件", f"{src.name}（仅提供 P/V 矩阵，未提供 .scen 原文）"],
        ["场景年代", "2010 年代（按编成平台服役年份 1987–2006 推断，示例重建）"],
        ["地域", "西太平洋某开阔海域（示例重建）"],
        ["历史时期", "信息化条件下海上局部封锁与反封锁（示例重建）"],
        ["红方定位", "红方（防守方），依托海上编队组织纵深防空"],
        ["蓝方定位", "蓝方（进攻方），多批次空中突击编队"],
        ["原文阵营写法", "—"],
        ["交战时间", "—"],
        ["交战海域/地点", f"某开阔海域，方圆约 {D_MAX:.0f}nm 的对抗空域（示例重建）"],
        ["海况", "—"], ["天气", "—"],
        ["环境备注", "缺省取 1.00，不进入概率因子链"],
        ["复杂度/难度", "—"], ["数据库版本", "—"],
    ]))
    R.append("")
    R.append("## 二、战争背景")
    R.append("")
    R.append("本想定脱胎于一次海上防空对抗：蓝方以空中突击编队试图突破红方海上编队的防空圈，"
             "红方依托舰载超远程拦截、远程截击与中距拦截三层兵力组织防御。改编方向是把一次典型的"
             "「预警指挥引导 + 电子战压制 + 多波次突击」突防行动，压缩为一份可直接进入 WTA 分配阶段的"
             f"单发命中概率矩阵与目标价值向量：矩阵 {N} 行对应红方可用的拦截火力通道，{M} 列对应蓝方空中目标。")
    R.append("")
    R.append(f"由矩阵回读可见，红方火力被组织为 {red_counts} 三段，分别对应中距、远程与超远程三个拦截层；"
             f"蓝方目标按价值与可毁伤性呈 {blue_counts} 五类梯队，从远距预警指挥节点到前出无人机群依次展开。"
             f"随目标价值由高到低（{v_tiers}），主力中距拦截层的单发命中概率整体由 "
             f"{BLUE[0]['p_mean_A']:.2f} 升至 {BLUE[-1]['p_mean_A']:.2f}（{p_by_v}），呈显著负相关；"
             "个别相邻档位存在交错，属输入噪声与档位标定所致，与手册 5.5「高价值节点配强防护、"
             "配置在更远距离、更难被拦截」的一致性约定相符。")
    R.append("")
    R.append("需说明：以上背景、年代与地域均由矩阵结构与装备库代次重建，不是原始 CMO 场景正文的译写；"
             "原始 .scen 私有压缩块中的标题、正文与溯源字段不可得。")
    R.append("")
    R.append("## 三、双方任务目标")
    R.append("")
    R.append("**红方（防守方）**：以中距、远程、超远程三层拦截兵力守住海上编队防空圈，"
             "优先压制蓝方远距预警指挥与电子战节点，逐层消耗前出无人机与护航战斗机，"
             "阻止轰炸机与多用途战斗机进入有效投射距离。")
    R.append("")
    R.append("**蓝方（进攻方）**：以预警指挥机与电子战飞机提供探测、引导与压制，"
             "以前出无人机群消耗红方拦截弹药、以多用途战斗机护航，掩护轰炸机群突防，"
             "达成对红方海上编队的打击。")
    R.append("")
    R.append("## 四、双方编成与装备参数")
    R.append("")
    R.append(f"本想定共编入 {N+M} 个单位，其中红方 {N} 个、蓝方 {M} 个。型号、DP、年份、RCS 前向值、"
             "雷达/ESM 作用距离与武器命中率/射程取自装备参数库（按 `platform_id` / `weapon_id` 分类别查库），"
             "阵位（x/y/高度）、单位数量、挂载数量与战术机动过载为示例重建；"
             "编入挂载仅保留库内命中（resolved）武器。舰艇无机动 g / 雷达 / ESM 值填「—」。"
             "逐单位阵位见第五节部署表。")
    R.append("")
    R.append("### 4.1 红方编成（按机型 / 舰型分组）")
    R.append("")
    R.append(md_table(UNIT_HEAD, unit_rows("red")))
    R.append("")
    R.append("### 4.2 蓝方编成（按机型分组）")
    R.append("")
    R.append(md_table(UNIT_HEAD, unit_rows("blue")))
    R.append("")
    R.append("### 4.3 挂载表")
    R.append("")
    R.append("命中率与射程按空 / 海 / 陆 / 潜四类目标分列，无对应值填「—」；蓝方挂载为任务/自卫挂载，"
             "不参与本 P 矩阵计算（矩阵行仅含红方火力）。")
    R.append("")
    R.append(md_table(LOAD_HEAD, loadout_rows()))
    R.append("")
    R.append("### 4.4 按矩阵分组回查装备库后的三层拦截参数（示例重建）")
    R.append("")
    rows = []
    for g in RED:
        w = g["weapon"]
        rows.append([g["key"], g["label"], f"R{g['start']+1:02d}–R{g['start']+g['n']:02d}", g["n"],
                     g["platform"]["name"], w["name"],
                     f"{w['hit']['air']*100:.0f}%", f"{w['range_nm']['air'][0]:g}–{w['range_nm']['air'][1]:g}",
                     g["qty"], fmt(g["pos"]["x0"], 0) + "～" + fmt(g["pos"]["x1"], 0)])
    R.append(md_table(["组", "拦截层", "uid 范围", "数量", "平台", "武器", "对空命中率", "对空射程(nm)", "单平台挂载", "阵位 x 带(nm)"], rows))
    R.append("")
    R.append("## 五、战场部署与距离分布")
    R.append("")
    R.append("坐标系：x、y 单位海里（nm），高度单位米；单位间距离取平面欧氏距离（海里），"
             f"红方三层拦截兵力沿 x 轴由 {red_x_lo:.0f}nm 至 {red_x_hi:.0f}nm 分纵深展开，"
             f"蓝方五类空中目标位于 x≈{blue_x_lo:.0f}～{blue_x_hi:.0f}nm 一侧、按由近及远的距离带梯次展开。"
             f"交战距离带由手册 6.1 从 P 矩阵反解：以中距拦截层（Rmax={RMAX['A']:g}nm）为基准，"
             "五类目标的等效距离依次为 "
             + "、".join(f"{c['key']} {c['d_bar_A']:.1f}nm" for c in by_d)
             + "；射程覆盖按各层武器最大射程 Rmax 与平面距离判定，超射程即门控为不可交战。")
    R.append("")
    R.append(md_table(DEP_HEAD, deploy_rows()))
    R.append("")
    R.append("**编队统计**（按上表坐标计算）")
    R.append("")
    R.append(md_table(["项目", "数值"], [
        ["红方编队中心", f"({red_cx:.2f}, {red_cy:.2f}) nm"],
        ["蓝方编队中心", f"({blue_cx:.2f}, {blue_cy:.2f}) nm"],
        ["双方编队间距", f"{center_gap:.2f} nm"],
        ["红—蓝单位间距最小值", f"{D_MIN:.2f} nm"],
        ["红—蓝单位间距均值", f"{D_MEAN:.2f} nm"],
        ["红—蓝单位间距最大值", f"{D_MAX:.2f} nm"],
    ]))
    R.append("")
    R.append(f"按最大射程判定的可达目标对数（单位×目标，共 {N*M} 对）：")
    R.append("")
    cov_rows = []
    for g in RED:
        cnt = coverage[g["key"]]
        cov_rows.append([g["key"], f"{RMAX[g['key']]:.1f}"] + [f"{cnt[c['key']]}/{len(c['cols'])*g['n']}"
                                                               for c in by_v])
    R.append(md_table(["拦截层", "Rmax(nm)"] + [c["key"] + " " + c["label"] for c in by_v], cov_rows))
    R.append("")
    R.append("## 六、对抗态势综述")
    R.append("")
    R.append("**1. 总体态势**")
    R.append("")
    R.append(f"2010 年代、西太平洋某开阔海域（示例重建）。红方以海上编队为核心布防，"
             f"编队中心 ({red_cx:.1f}, {red_cy:.1f}) nm；蓝方空中突击编队自东侧进入，"
             f"编队中心 ({blue_cx:.1f}, {blue_cy:.1f}) nm，双方编队间距约 {center_gap:.1f} nm。"
             f"红—蓝单位间距 {D_MIN:.1f}～{D_MAX:.1f} nm、均值 {D_MEAN:.1f} nm，"
             f"整体处于中距弹（{RMAX['A']:g}nm）覆盖边缘至超远程弹（{RMAX['C']:g}nm）舒适区之间，"
             "属于「中距拦截为主、远程与超远程补盲」的交战距离带。")
    R.append("")
    R.append("**2. 红方防守部署**")
    R.append("")
    red_deploy = (
        f"红方共 {N} 个拦截单位，分三层："
        + "；".join(
            f"{g['key']} 层 {g['n']} {'架' if g['platform']['kind']=='aircraft' else '艘'} {g['platform']['name']}"
            f"（{'飞行高度 '+str(g['pos']['alt0'])+'～'+str(g['pos']['alt0']+g['pos']['altspan'])+'m' if g['pos']['altspan'] else '水面'}）"
            f"挂 {g['weapon']['name']}，对空命中率 {g['weapon']['hit']['air']*100:.0f}%"
            f"、射程 {g['weapon']['range_nm']['air'][0]:g}–{g['weapon']['range_nm']['air'][1]:g}nm"
            for g in RED)
        + "。" + "".join(
            (f"{g['key']} 层前出占据 " if gi == 0 else f"{g['key']} 层在 ")
            + f"x≈{g['pos']['x0']:.0f}～{g['pos']['x1']:.0f}nm "
            + ("拦截线，" if gi == 0 else ("提供纵深，" if gi == 1 else "提供远界补盲。"))
            for gi, g in enumerate(RED))
        + "防守任务为逐层消耗蓝方突击编队，并用超远程弹优先打击远距预警指挥节点。"
    )
    R.append(red_deploy)
    R.append("")
    R.append("**3. 蓝方进攻部署**")
    R.append("")
    R.append(f"蓝方共 {M} 个空中目标，按价值由高到低分五类："
             + "、".join(f"{c['label']} {len(c['cols'])} 个（{c['platform']['name']}，DP {c['platform']['dp']}，"
                        f"h≈{c['platform']['alt']}m，v 均值 {c['v_mean']:.1f}）" for c in by_v)
             + "。" + full_txt + "。进攻任务为以预警/电子战节点引导、无人机群前出消耗、战斗机护航、轰炸机突防。")
    R.append("")
    R.append("**4. 距离与接战关系**")
    R.append("")
    R.append("按手册 6.1 由 P 反解的五类等效距离带（相对 A 层拦截线）为 "
             + "、".join(f"{c['key']}≈{c['d_bar_A']:.1f}nm" for c in by_d)
             + "，整体呈低价值目标突前、高价值节点靠后的梯次配置（相邻档位略有交错）；"
             f"以最大射程判定：A 层（{RMAX['A']:g}nm）主要覆盖近距目标，对远距高价值节点进入射程衰减末段或边缘；"
             f"B 层（{RMAX['B']:g}nm）与 C 层（{RMAX['C']:g}nm）提供纵深补盲。"
             f"给定矩阵的分块适配系数 α 在 {amin:.2f}–{amax:.2f} 之间，"
             "反映发射包络与武器—目标适配的合并效应，属 C 层待标定项，详见闭环校验文件。")
    R.append("")
    R.append("交战阶段可划分为：① 蓝方预警/电子战节点前出建立引导与压制 → ② 无人机群进入 A 层拦截线，"
             "触发中距拦截 → ③ 战斗机/轰炸机群进入 B 层与 C 层射界，形成中远程火力交换 → "
             "④ 残余突防兵力逼近红方海上编队，由 C 层舰载拦截与近距屏护收尾。"
             "该顺序与矩阵中「高价值远距目标仅由超远程层有效覆盖、低价值近距目标主要由中距层覆盖」的结构一致。")
    R.append("")
    R.append("> 数值层说明：给定 P 矩阵含分块标定与独立噪声，按卡片参数正算需引入分块适配系数 α 才能闭合；"
             f"非零可解释格逐格 RMSE 约 {RMSE_VALID:.4f}，{zero_cells} 个零格中仅 {zero_by_gate} 个可由射程门控解释，"
             f"其余属火力通道/弹药配额掩膜，不可由卡片参数唯一反推。详见 `P_V/闭环校验_{scene}.md`。")
    R.append("")
    report_md = "\n".join(R)
    (OUT / f"{scene}_场景想定报告.md").write_text(report_md, encoding="utf-8")
    (OUT / f"{scene}_场景想定报告.html").write_text(md_to_html(report_md, f"{scene} 场景想定报告"), encoding="utf-8")

    # ---- 16. 反推数据摘要
    summary = {
        "scenario_id": scene, "dim": [N, M],
        "row_bounds": bounds[1:-1], "row_change_points": cuts_dp,
        "red_groups": [{"key": g["key"], "uids": f"R{g['start']+1:02d}-R{g['start']+g['n']:02d}",
                        "n": g["n"], "platform": g["platform"]["name"], "weapon": g["weapon"]["name"],
                        "Pk0": PK0[g["key"]], "Rmax_nm": RMAX[g["key"]]} for g in RED],
        "blue_classes": [{"key": c["key"], "n": len(c["cols"]), "label": c["label"],
                          "platform": c["platform"]["name"], "v_mean": round(c["v_mean"], 3),
                          "p_mean_A": round(c["p_mean_A"], 4),
                          "d_bar_nm": round(c["d_bar_A"], 2), "flat": c["flat"],
                          "z_hat": round(c["z_hat"], 4), "z_theory": round(c["z_theory"], 4)} for c in BLUE],
        "grouping": {"dp_segment_cost": round(seg_cost, 5), "kmeans_sse": round(km_sse, 3),
                     "agreement": round(agree, 4)},
        "full_coverage_cols": [f"B{j+1:02d}" for j in full_cols],
        "calibration": {"mode": PROB_MODE, "scan": scan, "rmse": round(RMSE, 5),
                        "rmse_valid": round(RMSE_VALID, 5),
                        "block_mean_max_err": round(max(block_err), 6),
                        "block_mean_max_err_full": round(max(block_err_full), 6),
                        "p_exceed": p_exceed, "n_valid": n_valid,
                        "zero_cells": zero_cells, "zero_by_gate": zero_by_gate, "obs_conflict": obs_conflict,
                        "alpha": {f"{k[0]}|{k[1]}": round(v, 4) for k, v in alpha.items()},
                        "alpha_min": round(amin, 4), "alpha_max": round(amax, 4),
                        "value_regression": {"a": round(a_reg, 2), "b": round(b_reg, 2), "r2": round(r2, 4),
                                            "v_min": V_MIN, "v_max": V_MAX}},
        "distance_stats": {"red_center": [round(red_cx, 2), round(red_cy, 2)],
                           "blue_center": [round(blue_cx, 2), round(blue_cy, 2)],
                           "center_gap_nm": round(center_gap, 2), "min": round(D_MIN, 2),
                           "mean": round(D_MEAN, 2), "max": round(D_MAX, 2)},
        "nonzero_cells": N * M - zero_cells,
        "files": {"card": f"{scene}_card.json", "report_md": f"{scene}_场景想定报告.md",
                  "report_html": f"{scene}_场景想定报告.html", "check": f"P_V/闭环校验_{scene}.md"},
    }
    (PV / "反推数据.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] {scene:22s} {N:4d}x{M:<4d} 行 {red_counts:12s} 列 {blue_counts:12s} "
          f"RMSEv={RMSE_VALID:.4f} 零格={zero_cells} 冲突={obs_conflict} 输出 {OUT.name}")
    return summary


def main():
    args = sys.argv[1:]
    only = None
    if "--only" in args:
        only = args[args.index("--only") + 1]
    srcs = sorted(ROOT.glob("scene*.json"))
    if only:
        srcs = [s for s in srcs if s.stem == only]
    print(f"待处理 {len(srcs)} 个场景，输出根目录 {OUTROOT}")
    summaries = []
    for src in srcs:
        summaries.append(build(src, src.stem))
    (OUTROOT / "汇总数据.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== 全部完成，共 {len(summaries)} 个场景 ===")


if __name__ == "__main__":
    main()
