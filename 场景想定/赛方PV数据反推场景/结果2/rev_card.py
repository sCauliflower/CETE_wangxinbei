# -*- coding: utf-8 -*-
"""
从 P/V 矩阵反推场景卡片（*_card.json）——独立可运行版，仅依赖标准库。

用法：
    python rev_card.py                          # 默认反推 REV-01（scene1_0_50_20）
    python rev_card.py scene2_0_500_200.json    # 指定矩阵文件
    python rev_card.py scene1_1_40_30.json --cid REV-02 --out D:\\out

算法链路（6 步）：
    1. 读矩阵：P 为 [红方行 × 蓝方列] 概率，v 为每列目标价值
    2. 分行：50/40 → 25/20/5（40 行 → 20/16/4；500 → 250/200/50；400 → 200/160/40）
    3. 分列：按 v 的 5 档阈值 (30/58/77/95) 把每列映射到 drone/harpoon/brahmos/f15e/harm
    4. 阵位：A 层多簇屏护（簇数 K=round(nA/8)，簇间距 6.5 nm），B/C 层按跨度铺开，蓝方按列归类贴到各簇前方
    5. 概率核：p = Pk0·fR(ρ)·fT·fG·fJ·fE·fK，clip 0.01–0.99，ρ>1 取 0；同时算 RMSE / 门控一致率 / 分块对比
    6. 组装卡片：meta / units / deployment / matrix / provenance / matrix_ref / open_items
"""
import argparse, csv, json, math, random, re, statistics
from pathlib import Path

ROOT = Path(r'D:\项目文件\中国电科“网信杯”人工智能创新大赛\CommandModernOperations数据\场景反推')
LIB = ROOT / '装备参数'
VMAX, SEED = 150.0, 11
YB, YC = -24.0, -100.0          # B / C 层纵坐标（nm）

# ---- 蓝方五类目标（v 落在哪一档即判为该类） --------------------------------
CLASS = {
 'drone':   dict(name='ADM-160C MALD-J [Stand-In OECM]', wid=2442, dp=1, ct=1.00, ft=1.00, alt=300,
                 role='诱饵/随队干扰无人机', nation='美国', y=None, R=0.60, Th=0.20, kind='missile',
                 vlo=-1e9, vhi=30, short='诱饵/随队干扰无人机'),
 'harpoon': dict(name='AGM-84L Harpoon II', wid=816, dp=3, ct=1.00, ft=0.90, alt=0,
                 role='亚音速反舰导弹', nation='美国', y=1988, R=0.60, Th=0.80, kind='missile',
                 vlo=30, vhi=58, short='亚音速反舰导弹'),
 'brahmos': dict(name='PJ-10 BrahMos', wid=305, dp=4, ct=1.00, ft=0.90, alt=0,
                 role='超音速反舰导弹', nation='印度', y=2004, R=0.75, Th=0.90, kind='missile',
                 vlo=58, vhi=77, short='超音速反舰导弹'),
 'f15e':    dict(name='F-15E Strike Eagle', aid=747, dp=10, ct=1.00, ft=1.00, alt=8000,
                 role='多用途战斗轰炸机', nation='美国', y=2001, R=0.75, Th=0.85, kind='aircraft',
                 vlo=77, vhi=95, short='多用途战斗轰炸机'),
 'harm':    dict(name='AGM-88B HARM', wid=650, dp=2, ct=1.00, ft=0.90, alt=4000,
                 role='反辐射导弹', nation='美国', y=1991, R=1.00, Th=0.70, kind='missile',
                 vlo=95, vhi=1e9, short='反辐射导弹'),
}
ORDER = ['drone', 'harpoon', 'brahmos', 'f15e', 'harm']

# ---- 红方三线（中国舰） ------------------------------------------------------
GRP = {
 'A': dict(sid=2703, name='Type 056A Jiangdao II [505 Qinhuangdao]', typ='FFL-Corvette',
           wid=4695, wname='HQ-10A', pk0=0.85, rmax=10.0, W=3.15, role='内层点防御',
           loadout=[(4695, 8), (541, 2)]),
 'B': dict(sid=1965, name='Type 054A Jiangkai II [529 Zhoushan]', typ='FFG',
           wid=4362, wname='HQ-16C', pk0=0.90, rmax=40.0, W=24.5, role='中层区域防空',
           loadout=[(4362, 32), (535, 1), (541, 4)]),
 'C': dict(sid=2296, name='Type 052D Luyang III [172 Kunming]', typ='DDG',
           wid=4090, wname='HHQ-9B', pk0=0.90, rmax=140.0, W=63.0, role='远程区域防空',
           loadout=[(4090, 32), (4695, 24), (535, 1)]),
}
FJ = {('A', 'harm'): 0.10, ('B', 'harm'): 0.30, ('C', 'harm'): 1.00}   # 反辐射分级压制
FE = {('C', 'drone'): 0.60}                                            # 包络外降效
T_A = {'drone': 5.5, 'harpoon': 5.5, 'brahmos': 7.4, 'f15e': 3.5, 'harm': 6.2}  # 蓝方距屏护线基线

RECS = {}


# ============================================================ 1. 库里取值
def parse_m2(s):
    m = re.search(r'\(\s*([\d.]+)\s*m', s or '')
    return float(m.group(1)) if m else None


def parse_rng(s):
    if not s or not isinstance(s, str):
        return None
    nums = re.findall(r'[\d.]+', s)
    if not nums:
        return None
    return [float(nums[0]), float(nums[1])] if len(nums) >= 2 else [0.0, float(nums[0])]


def parse_pct(s):
    m = re.search(r'([\d.]+)\s*%', s or '')
    return round(float(m.group(1)) / 100.0, 4) if m else None


def load_records():
    need = (('cmo_ship_zh.jsonl', {2703, 1965, 2296}),
            ('cmo_aircraft_zh.jsonl', {747}),
            ('cmo_weapon_zh.jsonl', {4695, 4362, 4090, 535, 541, 816, 305, 650, 2442}))
    for fn, ids in need:
        with (LIB / fn).open('r', encoding='utf-8') as f:
            for line in f:
                r = json.loads(line)
                if r.get('id') in ids:
                    RECS[r['id']] = r


# ============================================================ 2. 概率核
def clip(x, a, b):
    return max(a, min(b, x))


def fR(rho):
    """手册 4.3 距离衰减：ρ≤0.5 平坦 1.0；0.5–0.8 线性到 0.2；0.8–1.0 到 0.05；>1 为 0。"""
    if rho <= 0.5:
        return 1.0
    if rho <= 0.8:
        return 1.0 - 0.8 * (rho - 0.5) / 0.3
    if rho <= 1.0:
        return 0.2 - 0.15 * (rho - 0.8) / 0.2
    return 0.0


def fK(g, c):
    """毁伤形状因子：min(1, 0.5·√(W/DP))·ct。"""
    return min(1.0, 0.5 * math.sqrt(GRP[g]['W'] / CLASS[c]['dp'])) * CLASS[c]['ct']


def base_p(g, c):
    """不含距离项的基线命中概率。"""
    return (GRP[g]['pk0'] * fK(g, c) * CLASS[c]['ft']
            * FJ.get((g, c), 1.0) * FE.get((g, c), 1.0))


# ============================================================ 3. 行列映射
def cls_of_v(v):
    """按价值档位把列映射到目标类别（阈值 30/58/77/95）。"""
    for k in ORDER:
        if CLASS[k]['vlo'] <= v < CLASS[k]['vhi']:
            return k
    return 'harm' if v >= 95 else 'drone'


def row_split(n):
    """红方行按 5:4:1 分三线，尾差全给 C 层。"""
    a = round(n * 0.5); b = round(n * 0.4)
    return a, b, n - a - b


# ============================================================ 4. 阵位反解
def make_positions(nA, nB, nC, cols, rnd):
    """A 层：K 个屏护簇沿 x 排布，簇内圆盘抖动；B/C 层按 span 铺开；蓝方按列序贴簇。"""
    k = max(3, min(12, max(1, round(nA / 8))))
    span = 6.5 * (k - 1)
    centers = [-span / 2 + 6.5 * i for i in range(k)]

    rpos, rgrp = [], []
    cnt = [nA // k] * k
    for i in range(nA - sum(cnt)):
        cnt[i % k] += 1
    for ki in range(k):
        for _ in range(cnt[ki]):
            a = rnd.random() * 2 * math.pi
            rr = 0.7 * math.sqrt(rnd.random())
            rpos.append((round(centers[ki] + rr * math.cos(a), 2), round(rr * math.sin(a), 2)))
            rgrp.append('A')
    for _ in range(nB):
        x = (-span / 2 + span * rnd.random()) if span > 0 else (-5 + 10 * rnd.random())
        rpos.append((round(x, 2), round(YB + 6 * (rnd.random() - 0.5), 2))); rgrp.append('B')
    for _ in range(nC):
        x = ((-span / 4) + (span / 2) * rnd.random()) if span > 0 else (-2 + 4 * rnd.random())
        rpos.append((round(x, 2), round(YC + 6 * (rnd.random() - 0.5), 2))); rgrp.append('C')

    tpos = []
    for j, c in enumerate(cols):
        ki = j % k
        tpos.append((round(centers[ki] + (rnd.random() - 0.5) * 2.4, 2),
                     round(T_A[c] + (rnd.random() - 0.5) * 1.0, 2)))
    # 1.5 nm 最小安全间隔修正（最多推 4 次）
    for j in range(len(tpos)):
        for _ in range(4):
            x, y = tpos[j]
            near = min(rpos, key=lambda p: math.hypot(p[0] - x, p[1] - y))
            d = math.hypot(near[0] - x, near[1] - y)
            if d >= 1.5 or d == 0:
                break
            ux, uy = (x - near[0]) / d, (y - near[1]) / d
            tpos[j] = (round(x + ux * (1.6 - d), 2), round(y + uy * (1.6 - d), 2))
    return rpos, rgrp, tpos, k, span


# ============================================================ 5. 建卡
def build_card(cid, src):
    data = json.loads((ROOT / f'{src}.json').read_text(encoding='utf-8'))
    P0, v = data['P'], data['v']
    nrow, ncol = len(P0), len(v)
    nA, nB, nC = row_split(nrow)
    cols = [cls_of_v(x) for x in v]
    rnd = random.Random(SEED)
    rpos, rgrp, tpos, kA, span = make_positions(nA, nB, nC, cols, rnd)

    # ---------- 红方单位（按行顺序取三线型号，库内解析 RCS / 挂载）
    units = []
    for i, g in enumerate(rgrp):
        G = GRP[g]; R = RECS[G['sid']]
        sig = R.get('signatures') or {}
        em = next((kk for kk in sig if 'E-M波段' in kk), None)
        rcs = ({a: {'m2': parse_m2((sig.get(em) or {}).get(a))} for a in ('front', 'side', 'rear')}
               if em else None)
        loadout = []
        for wid, qty in G['loadout']:
            W = RECS[wid]; b = W['basic']
            loadout.append({'weapon_id': wid, 'name': W['name'], 'type': b.get('类型'),
                            'nation': W.get('nation', 'China'), 'role': G['role'], 'qty': qty,
                            'hit': {'air': parse_pct(b.get('对空命中率')),
                                    'surface': parse_pct(b.get('对海命中率')),
                                    'land': parse_pct(b.get('对陆命中率')),
                                    'sub': parse_pct(b.get('对潜命中率'))},
                            'range_nm': {'air': parse_rng(b.get('对空射程')),
                                         'surface': parse_rng(b.get('对海射程')),
                                         'land': parse_rng(b.get('对陆射程')),
                                         'sub': parse_rng(b.get('反潜射程'))},
                            'warhead': list((W.get('warhead') or {}).values())[0] if W.get('warhead') else '—',
                            'note': '库内命中武器', 'resolved': True})
        units.append({'uid': f'R{i+1}', 'side': 'red', 'role': G['role'], 'kind': 'ship',
                      'platform_id': G['sid'], 'name': G['name'], 'type': G['typ'],
                      'target_class': '水面舰艇', 'nation': '中国', 'dp': R.get('dp'),
                      'year': R.get('year'), 'maneuver_g': None, 'rcs_em_m2': rcs,
                      'radar_max_nm': None, 'esm_max_nm': None, 'ir_max_nm': None,
                      'sonar_max_nm': None, 'visual_max_nm': None,
                      'pos': {'rel_x_nm': rpos[i][0], 'rel_y_nm': rpos[i][1], 'alt_m': 0},
                      'jamming': 'none', 'aspect': 'front', 'loadout': loadout,
                      'support_stores': [], 'mounts': [], 'resolved': True})

    # ---------- 蓝方单位（战机取库内传感器/机动性，导弹取库内战斗部）
    for j, c in enumerate(cols):
        C = CLASS[c]
        if C['kind'] == 'aircraft':
            R = RECS[C['aid']]
            sig = R.get('signatures') or {}
            em = next((kk for kk in sig if 'E-M波段' in kk), None)
            rcs = ({a: {'m2': parse_m2((sig.get(em) or {}).get(a))} for a in ('front', 'side', 'rear')}
                   if em else None)
            s_radar = max((parse_rng(s.get('作用距离'))[1] for s in (R.get('sensors') or [])
                           if 'Radar' in (s.get('用途') or '') and parse_rng(s.get('作用距离'))), default=None)
            s_esm = max((parse_rng(s.get('作用距离'))[1] for s in (R.get('sensors') or [])
                         if ('ESM' in (s.get('型号') or '') or 'RWR' in (s.get('用途') or ''))
                         and parse_rng(s.get('作用距离'))), default=None)
            sn = R['basic']
            units.append({'uid': f'B{j+1}', 'side': 'blue', 'role': C['role'], 'kind': 'aircraft',
                          'platform_id': R['id'], 'name': R['name'], 'type': R.get('type'),
                          'target_class': '战机', 'nation': C['nation'], 'dp': R.get('dp'),
                          'year': R.get('year'), 'maneuver_g': float(sn.get('机动性') or 0) or None,
                          'rcs_em_m2': rcs, 'radar_max_nm': s_radar, 'esm_max_nm': s_esm,
                          'ir_max_nm': parse_rng((sig.get('红外探测距离') or {}).get('front')),
                          'sonar_max_nm': None,
                          'visual_max_nm': parse_rng((sig.get('目视探测距离') or {}).get('front')),
                          'pos': {'rel_x_nm': tpos[j][0], 'rel_y_nm': tpos[j][1], 'alt_m': C['alt']},
                          'jamming': 'none', 'aspect': 'front',
                          'loadout': [{'weapon_id': 816, 'name': 'AGM-84L Harpoon II', 'type': '制导武器',
                                       'nation': '美国', 'role': '防区外反舰', 'qty': 2,
                                       'hit': {'air': None, 'surface': 0.95, 'land': 0.95, 'sub': None},
                                       'range_nm': {'air': None, 'surface': [2, 75], 'land': [2, 75], 'sub': None},
                                       'warhead': 'SAP 166.6', 'note': '想定备弹', 'resolved': True}],
                          'support_stores': [], 'mounts': [], 'resolved': True})
        else:
            W = RECS[C['wid']]; b = W['basic']
            sig = W.get('signatures') or {}
            em = next((kk for kk in sig if 'E-M波段' in kk), None)
            rcs = ({a: {'m2': parse_m2((sig.get(em) or {}).get(a))} for a in ('front', 'side', 'rear')}
                   if em else None)
            units.append({'uid': f'B{j+1}', 'side': 'blue', 'role': C['role'], 'kind': 'missile',
                          'platform_id': W['id'], 'name': W['name'], 'type': b.get('类型'),
                          'target_class': '制导武器', 'nation': C['nation'], 'dp': C['dp'],
                          'year': C['y'], 'maneuver_g': None, 'rcs_em_m2': rcs,
                          'radar_max_nm': None, 'esm_max_nm': None, 'ir_max_nm': None,
                          'sonar_max_nm': None, 'visual_max_nm': None,
                          'pos': {'rel_x_nm': tpos[j][0], 'rel_y_nm': tpos[j][1], 'alt_m': C['alt']},
                          'jamming': 'none', 'aspect': 'front', 'loadout': [], 'support_stores': [],
                          'mounts': [], 'resolved': True})
        units[-1]['cls'] = c

    # ---------- 概率矩阵 + 拟合指标
    Pc = [[0.0] * ncol for _ in range(nrow)]
    for i in range(nrow):
        g = rgrp[i]; G = GRP[g]; rx, ry = rpos[i]
        for j in range(ncol):
            c = cols[j]
            d = math.hypot(rx - tpos[j][0], ry - tpos[j][1])
            rho = d / G['rmax']
            Pc[i][j] = 0.0 if rho > 1.0 else clip(base_p(g, c) * fR(rho), 0.01, 0.99)

    rmse = math.sqrt(statistics.mean((P0[i][j] - Pc[i][j]) ** 2 for i in range(nrow) for j in range(ncol)))
    cov = sum(1 for i in range(nrow) for j in range(ncol) if (P0[i][j] > 0) == (Pc[i][j] > 0)) / (nrow * ncol)
    nz0 = sum(1 for i in range(nrow) for j in range(ncol) if P0[i][j] > 0)
    nz = sum(1 for i in range(nrow) for j in range(ncol) if Pc[i][j] > 0)
    block = {}
    for g in 'ABC':
        idx = [i for i in range(nrow) if rgrp[i] == g]
        for c in ORDER:
            js = [j for j in range(ncol) if cols[j] == c]
            n0 = [P0[i][j] for i in idx for j in js if P0[i][j] > 0]
            n1 = [Pc[i][j] for i in idx for j in js if Pc[i][j] > 0]
            block[f'{g}|{c}'] = [round(statistics.mean(n0), 4) if n0 else 0.0,
                                 round(statistics.mean(n1), 4) if n1 else 0.0, len(n0), len(n1)]

    # ---------- 部署统计
    rc = dict(x_nm=round(statistics.mean(p[0] for p in rpos), 2) + 0.0,
              y_nm=round(statistics.mean(p[1] for p in rpos), 2) + 0.0)
    bc = dict(x_nm=round(statistics.mean(p[0] for p in tpos), 2) + 0.0,
              y_nm=round(statistics.mean(p[1] for p in tpos), 2) + 0.0)
    sep = round(math.hypot(rc['x_nm'] - bc['x_nm'], rc['y_nm'] - bc['y_nm']), 2)
    dists = [math.hypot(p[0] - q[0], p[1] - q[1]) for p in rpos for q in tpos]
    groups = {}
    for g in 'ABC':
        idx = [i for i in range(nrow) if rgrp[i] == g]
        groups[g] = dict(n=len(idx),
                         x=[round(min(rpos[i][0] for i in idx), 1), round(max(rpos[i][0] for i in idx), 1)],
                         y=[round(min(rpos[i][1] for i in idx), 1), round(max(rpos[i][1] for i in idx), 1)],
                         cx=round(statistics.mean(rpos[i][0] for i in idx), 2),
                         cy=round(statistics.mean(rpos[i][1] for i in idx), 2))
    cnt = {c: sum(1 for x in cols if x == c) for c in ORDER}
    geo = dict(
        A=f'{min(T_A.values()):.1f}–{max(T_A.values()):.1f}',
        B=f'{min(abs(YB - T_A[c]) for c in ORDER):.0f}–{max(abs(YB - T_A[c]) for c in ORDER):.0f}',
        C=f'{min(abs(YC - T_A[c]) for c in ORDER):.0f}–{max(abs(YC - T_A[c]) for c in ORDER):.0f}',
        AY=f'{groups["A"]["y"][0]}～{groups["A"]["y"][1]}', BY=f'{groups["B"]["y"][0]}～{groups["B"]["y"][1]}',
        CY=f'{groups["C"]["y"][0]}～{groups["C"]["y"][1]}', depth=f'{abs(groups["C"]["cy"]):.0f}',
        bdepth=f'{abs(groups["B"]["y"][1] - groups["B"]["y"][0]):.0f}',
        AX=f'{groups["A"]["x"][0]}～{groups["A"]["x"][1]}', span=f'{span:.0f}', kA=kA)

    meta = {'name': f'三线纵深联合防空反导拦截（{cid}）', 'year': 2026,
            'region': '西太平洋·第一岛链以东某海域', 'period': '现代（2020 年代）',
            'sides': {'red': '红方（防守方）：海军联合防空编队群',
                      'blue': '蓝方（进攻方向）：外军联合海空突击群'},
            'sides_raw': '红方 / 蓝方', 'date_time': '2026-05-18 04:20（当地时间）',
            'location': '西太平洋某海域（示例坐标，原点取红方内层屏护线中心）',
            'playable_sides': ['red', 'blue'], 'geo': geo, 'duration': '约 2 小时',
            'environment': {'sea_state': '4 级（浪高 1.25–2.5 m）',
                            'weather': '晴，能见度 12 km，中低云，无降水',
                            'notes': '拂晓前后；伴随式干扰与诱饵同时存在，电磁环境复杂'},
            'objective': {
                'red': f'以三线纵深防空体系拦截蓝方突击群，掩护核心编队与后方指挥节点，保持对 '
                       f'{geo["A"]}／{geo["B"]}／{geo["C"]} nm 三线全纵深来袭目标的连续拦截，力争拦下全部反舰导弹与反辐射弹。',
                'blue': '以诱饵与随队干扰开辟走廊，反辐射弹压制红方舰载雷达，反舰导弹多轴向突防，'
                        '击破红方内层屏护并重创其区域防空节点。'}}
    meta['background_zh'] = (
        '第一岛链以东某海域，区域紧张态势持续升级。蓝方以一支联合海空突击群前出，企图在拂晓前后对红方海上编队群实施饱和式反舰突击，'
        '并以反辐射火力瘫痪红方舰载对空搜索雷达，为后续梯队打开突防窗口。\n'
        f'红方在该海域组织联合防空编队群，按内层点防御、中层区域防空、远程区域防空三线梯次配置：{nA} 艘轻型护卫舰前出组成屏护线，'
        f'{nB} 艘护卫舰构成中层拦截带，{nC} 艘驱逐舰位于后方担负远程拦截与区域指挥，横向展开约 {span:.0f} nm，'
        f'纵深约 {abs(groups["C"]["cy"]):.0f} nm。\n'
        f'本场景取矩阵对应的态势时点：蓝方突击群已突入红方内层屏护线，{cnt["drone"]} 架诱饵/随队干扰无人机在距屏护簇 {geo["A"]} nm '
        f'的距离上模拟假目标并施放压制，{cnt["harpoon"]} 枚亚音速反舰导弹与 {cnt["brahmos"]} 枚超音速反舰导弹沿多轴向扑向红方编队，'
        f'{cnt["harm"]} 枚反辐射导弹追瞄前出舰艇的对空雷达，{cnt["f15e"]} 架 F-15E 在编队上空以中距对海火力补射。'
        '红方三层舰空火力同时开火，形成远近衔接的拦截窗口。\n'
        '电磁态势是本次对抗的关键变量：反辐射突击伴随强压制，红方内层小舰的电子对抗能力有限，'
        '其中层与远程单元的抗击能力依次增强，这也是各层拦截概率出现量级差异的直接原因。')

    # ---------- 卡片
    seed = {'source_file': f'{src}.json', 'source_title': f'{src}（{nrow}×{ncol} P/V 矩阵，无 .scen 正文）',
            'source_setting': '矩阵反推（无原始文本）', 'source_year': '—', 'source_complexity': '—',
            'source_difficulty': '—', 'source_db_version': '—'}
    prov = {'narrative': f'rebuilt_from_pv_matrix（{src}.json，{nrow}×{ncol}，逐格反解后按类别基线重建）',
            'order_of_battle': 'rebuilt_from_db（cmo_ship/aircraft/weapon_zh.jsonl，按 DP/RCS/射程/命中率取库值）',
            'limitations': [
                '原矩阵不含 .scen 正文与单位元数据，单位型号、阵位、干扰状态均为按矩阵数值反解重建，非原始实装编成。',
                f'矩阵零元占 {nrow * ncol - nz0}/{nrow * ncol}，经检验其门控与几何射程不构成嵌套关系，判定为合成门控；'
                '因此反推阵位无法同时复现零元位置，本场景门控由射程判定给出。',
                f'反推非零 {nz} 格 vs 矩阵非零 {nz0} 格（{nz / nz0:.0%}），逐格 RMSE {rmse:.3f}；'
                '几何法只能复现覆盖带与量级，不能逐格闭合。',
                'A 组对有人战轰档超出该组武器可解释上限（Pk0×fK≈0.24）；A/C 组对反辐射弹档相差约 10 倍，'
                '超出 f_R 可解释范围，两处按“目标类别基线赋值”处理，不作逐格公式闭合声明。',
                '价值 v 为矩阵给定值；按 §5 权重正算与给定值存在偏差，说明原矩阵价值按目标类别基线赋值，'
                '价值分项仅用于结构解释。',
                '导弹类目标无库内 DP，按“命中即毁/弹体规模”赋 DP（诱饵 1、反舰弹 3、超音速弹 4、反辐射弹 2），属 C 层估算。',
                '舰艇 sensors 字段在库中为空数组，报告与卡片中的雷达/ESM 距离按规范填“—”。',
                'RCS 统一取库内“雷达，E-M 波段（2–100 GHz）”前向值。',
                '场景年代与交战时间（2026 年 5 月 18 日拂晓）为示例设定，矩阵未给出时间/地域等元数据。']}
    matrix = {'file': f'{src}.json', 'rows': nrow, 'cols': ncol, 'nonzero': nz0,
              'recomputed_nonzero': nz, 'rmse': round(rmse, 4), 'gate_agreement': round(cov, 4),
              'block_fit': block, 'layout': f'rows=R1..R{nrow}（红方），cols=B1..B{ncol}（蓝方）'}
    open_items = [
        'f_R 近距平坦段（d≤0.5Rmax）距离不唯一，前出屏护线 5–7 nm 的精确间距为示例重建。',
        '反辐射档的分级压制系数（0.10/0.30/1.00）需按各舰 ECCM 实装能力复核。',
        '价值权重（0.35/0.45/0.20/0.10）对给定 v 的拟合偏差需后续标定。',
        '蓝方为外军联合突击群（美制为主、含印方超音速反舰弹），原文阵营写法未在矩阵中给出。']

    dep_units = []
    for u in units:
        if u['side'] == 'red':
            G = GRP[rgrp[int(u['uid'][1:]) - 1]]
            dep_units.append(dict(uid=u['uid'], side='red', role=u['role'], platform=u['name'], pos=u['pos'],
                                  max_air_range_nm=G['rmax'], max_surface_range_nm=None, max_land_range_nm=None,
                                  radar_max_nm=None, esm_max_nm=None, ir_max_nm=None))
        else:
            C = CLASS[u['cls']]
            if u['kind'] == 'aircraft':
                sr = lr = 75.0
            else:
                b = RECS[C['wid']]['basic']
                sr = (parse_rng(b.get('对海射程')) or [None, None])[1]
                lr = (parse_rng(b.get('对陆射程')) or [None, None])[1]
            dep_units.append(dict(uid=u['uid'], side='blue', role=u['role'], platform=u['name'], pos=u['pos'],
                                  max_air_range_nm=None, max_surface_range_nm=sr, max_land_range_nm=lr,
                                  radar_max_nm=u['radar_max_nm'], esm_max_nm=u['esm_max_nm'], ir_max_nm=None))

    deployment = dict(units=dep_units, red_center=rc, blue_center=bc, side_separation_nm=sep)
    if nrow * ncol <= 5000:   # 大矩阵不逐对写入，避免卡片膨胀；逐对距离在 P_long 里
        deployment['red_blue_distances'] = [
            dict(red=f'R{i+1}', blue=f'B{j+1}',
                 distance_nm=round(math.hypot(rpos[i][0] - tpos[j][0], rpos[i][1] - tpos[j][1]), 2))
            for i in range(nrow) for j in range(ncol)]
    deployment['distance_stats'] = dict(min=round(min(dists), 2), mean=round(statistics.mean(dists), 2),
                                        max=round(max(dists), 2))

    return {'scenario_id': cid, 'schema_version': '2.0', 'seed': seed, 'meta': meta, 'provenance': prov,
            'counts': {'red': nrow, 'blue': ncol}, 'units': units, 'deployment': deployment,
            'matrix': matrix,
            'matrix_ref': {'pv': f'P_V/{cid}_pv.json', 'p_csv': f'P_V/P_{cid}.csv',
                           'p_long': f'P_V/P_long_{cid}.csv', 'v_csv': f'P_V/V_{cid}.csv',
                           'layout': f'rows=R1..R{nrow}（红方），cols=B1..B{ncol}（蓝方）'},
            'open_items': open_items}, Pc, v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src', nargs='?', default='scene1_0_50_20.json')
    ap.add_argument('--cid', default='REV-01')
    ap.add_argument('--out', default=str(ROOT / 'REV'))
    a = ap.parse_args()
    src = a.src.replace('.json', '')
    load_records()
    card, Pc, v = build_card(a.cid, src)
    out = Path(a.out) / a.cid
    out.mkdir(parents=True, exist_ok=True)
    (out / f'{a.cid}_card.json').write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'{a.cid} {src} 卡片已写出：{out / (a.cid + "_card.json")}')
    print(f'  单位 {card["counts"]} | 非零 {card["matrix"]["recomputed_nonzero"]}/{card["matrix"]["nonzero"]}'
          f' | 门控一致 {card["matrix"]["gate_agreement"]:.1%} | RMSE {card["matrix"]["rmse"]}')


if __name__ == '__main__':
    main()
