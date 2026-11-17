#!/usr/bin/env python3
"""인테리어 견적 작성기(아티팩트) '평면도' 탭의 3D 평면도 데이터 → 이 스킬의 작업 폴더.

견적 작성기는 평면도를 db 컬렉션 models/<현장 id> 에 미터 단위로 저장한다:
  rooms[{name, type, x, y, w, h}] · doors[{x, y, w, dir}] · windows[{x, y, w, dir}] · items[{type, x, y, w, d, rot}]
(x, y = 왼쪽 위 기준, y 아래로. 방은 축에 맞춘 직사각형, 문·창은 벽 위 가운데 점과 폭)

이미 치수가 정해진 평면이라 벽 찾기·평면도 읽기(1·2단계)가 필요 없다. 이 스크립트가
  source.png(깨끗한 평면도) · walls.json(벽 조각) · walls.png · plan.json(방·문·창·설비·축척)
을 만들고, 바로 `tour.py make <작업폴더>` 로 이어진다.

쓰기: python3 from_estimator.py <models/<id>.json> [--project <projects/<id>.json>] [--name 이름] [--out 저장위치] [--floor 층]
"""
import argparse, json, math, sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan import font, grid_overlay, raster, seg_rect, find_gaps  # noqa: E402

PPM = 50          # 그림 축척(px/m)
MARGIN = 1.5      # 건물 둘레 여백(m)
WALL_T = 0.14     # 벽 두께(m)
TYPE = {'bed': 'bedroom', 'living': 'living', 'kitchen': 'kitchen', 'bath': 'bath', 'entry': 'entry',
        'balcony': 'balcony', 'dress': 'dress', 'utility': 'utility', 'hall': 'hall'}
# 견적 작성기는 이웃한 방 사이에 모두 벽을 세운다. 실제 집에서 문 없이 트인 사이(거실·주방·복도·현관)는 벽을 빼야 걸어 다닐 수 있다
OPEN_PAIRS = {frozenset(p) for p in [('living', 'kitchen'), ('living', 'hall'), ('kitchen', 'hall'), ('hall', 'entry'),
                                     ('living', 'entry'), ('kitchen', 'entry'), ('hall', 'hall'), ('living', 'living')]}
# 견적 작성기 가구 → 스킬 가구(기본값 '구조만'에서는 counter·toilet·vanity·bathtub·shoe·shower 만 지어진다)
FURN = {'bedQ': ('bed', None), 'bedS': ('bed-single', None), 'wardrobe': ('wardrobe', None), 'desk': ('desk', None),
        'chair': ('chair', None), 'sofa3': ('sofa', None), 'sofa2': ('sofa', None), 'tvstand': ('tv', None), 'rug': ('rug', None),
        'table4': ('dining', 4), 'table6': ('dining', 6), 'counter': ('counter', None), 'island': ('island', None),
        'fridge': ('fridge', None), 'washer': ('washer', None), 'shelf': ('shelf', None), 'plant': ('plant', None),
        'toilet': ('toilet', None), 'basin': ('vanity', None), 'bathtub': ('bathtub', None)}


def r2(v): return round(v * 100) / 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('model'); ap.add_argument('--project'); ap.add_argument('--name'); ap.add_argument('--out')
    ap.add_argument('--floor', type=int, default=None, help='몇 층(창밖 높이). 없으면 2')
    ap.add_argument('--rescale', default=None, help="평면 전체 크기 바꾸기: 'supply'(공급 평수로 어림한 전용면적에 맞춤) 또는 전용면적 ㎡ 숫자")
    a = ap.parse_args()
    M = json.loads(Path(a.model).read_text(encoding='utf-8'))
    PRJ = json.loads(Path(a.project).read_text(encoding='utf-8')) if a.project else {}
    # 크기 점검: 견적 작성기는 평면도 그림을 '현장 전용면적'에 맞춰 키운다. 전용면적이 잘못 적혀 있으면 집 전체가 작거나 크다
    inner = sum(r['w'] * r['h'] for r in M.get('rooms', []) if r.get('type') != 'balcony')
    supply_py = float(PRJ.get('supplyPy') or 0)
    guess = round(supply_py * 3.3058 * 0.76, 1) if supply_py else 0  # 견적 작성기와 같은 어림(공급 평수 × 3.3058 × 0.76)
    want = guess if a.rescale == 'supply' else float(a.rescale) if a.rescale else 0
    if want and inner > 0:
        k = math.sqrt(want / inner)
        for coll in ('rooms', 'doors', 'windows', 'items'):
            for o in M.get(coll, []):
                for f_ in ('x', 'y', 'w', 'h', 'd'):
                    if f_ in o and not (coll in ('doors',) and f_ == 'w'): o[f_] = o[f_] * k
        print(f'  크기 맞춤: 실내 {inner:.1f}㎡ → {want}㎡ (×{k:.2f}, 문 폭은 그대로)')
        inner = want
    elif guess and inner < guess * 0.7:
        print(f'  확인 필요: 평면 실내 면적 {inner:.1f}㎡ 가 공급 {supply_py:g}평의 어림 전용면적 {guess}㎡ 보다 많이 작습니다(현장 전용면적 {PRJ.get("exM2")}㎡ 로 맞춰진 평면).'
              f' 전용면적이 틀렸으면 --rescale supply 또는 --rescale <㎡> 로 다시 만드세요')
    rooms0 = [r for r in M.get('rooms', []) if r.get('w', 0) > 0.2 and r.get('h', 0) > 0.2]
    if not rooms0: sys.exit('평면도에 방이 없습니다(견적 작성기에서 3D 평면도를 먼저 만드세요)')
    # 벽선 맞추기: 10cm 안의 방 테두리는 한 선으로(1cm 어긋나도 다른 벽선이 되어 트인 사이·겹침을 놓쳤다)
    for ax, ln in (('x', 'w'), ('y', 'h')):
        vals = sorted(v for r in rooms0 for v in (r[ax], r[ax] + r[ln]))
        groups = []
        for v in vals:
            if groups and v - groups[-1][-1] < 0.1: groups[-1].append(v)
            else: groups.append([v])
        snap = lambda v: next(r2(sum(g_) / len(g_)) for g_ in groups if g_[0] - 1e-9 <= v <= g_[-1] + 1e-9)
        for r in rooms0:
            a0_, b0_ = snap(r[ax]), snap(r[ax] + r[ln]); r[ax] = a0_; r[ln] = r2(b0_ - a0_)
    # 겹친 방: 견적 작성기 평면은 손으로 고치다 방끼리 겹치기도 한다 → 겹친 부분은 더 큰 방에서 떼어 낸다(큰 방이 ㄱ자가 된다)
    rects = [[dict(r)] for r in rooms0]
    overlaps = []
    def cut(A, B):  # 직사각형 A에서 B를 뺀 조각들
        ax0, ay0, ax1, ay1 = A['x'], A['y'], A['x'] + A['w'], A['y'] + A['h']
        bx0, by0, bx1, by1 = max(ax0, B['x']), max(ay0, B['y']), min(ax1, B['x'] + B['w']), min(ay1, B['y'] + B['h'])
        if bx1 - bx0 < 0.05 or by1 - by0 < 0.05: return [A]
        out = []
        for (x_0, y_0, x_1, y_1) in [(ax0, ay0, ax1, by0), (ax0, by1, ax1, ay1), (ax0, by0, bx0, by1), (bx1, by0, ax1, by1)]:
            if x_1 - x_0 > 0.05 and y_1 - y_0 > 0.05: out.append(dict(A, x=r2(x_0), y=r2(y_0), w=r2(x_1 - x_0), h=r2(y_1 - y_0)))
        return out
    order = sorted(range(len(rooms0)), key=lambda i: rooms0[i]['w'] * rooms0[i]['h'])
    for k, i in enumerate(order):          # 작은 방부터: 자기보다 큰 방에서 겹친 곳을 뗀다
        for j in order[k + 1:]:
            before = sum(q['w'] * q['h'] for q in rects[j])
            rects[j] = [piece for q in rects[j] for piece in cut(q, rooms0[i])]
            if before - sum(q['w'] * q['h'] for q in rects[j]) > 0.05: overlaps.append(f"{rooms0[j]['name']}에서 {rooms0[i]['name']}와 겹친 {r2(before - sum(q['w'] * q['h'] for q in rects[j]))}㎡를 뗌")
    rooms = [dict(q, _ri=i) for i, rs_ in enumerate(rects) for q in rs_]
    name = a.name or PRJ.get('name') or M.get('projectId') or 'estimator'
    slug = ''.join(c if c.isalnum() or c in '-_' else '-' for c in str(M.get('projectId') or M.get('id') or name)).strip('-') or 'estimator'
    import os
    out = Path(a.out or os.environ.get('FLOORPLAN_TOUR_OUT') or Path.cwd() / 'floorplan-3d-tour').expanduser().resolve()
    work = out / f'{slug}.work'; work.mkdir(parents=True, exist_ok=True)

    x0 = min(r['x'] for r in rooms); y0 = min(r['y'] for r in rooms)
    x1 = max(r['x'] + r['w'] for r in rooms); y1 = max(r['y'] + r['h'] for r in rooms)
    P = lambda x, y: ((x - x0 + MARGIN) * PPM, (y - y0 + MARGIN) * PPM)
    Wpx, Hpx = int(math.ceil((x1 - x0 + 2 * MARGIN) * PPM)), int(math.ceil((y1 - y0 + 2 * MARGIN) * PPM))
    key = lambda v: round(v * 20) / 20  # 5cm 안의 선은 같은 선

    # ---- 1. 방 테두리 → 선마다 구간 합치기 ----
    lines = {'h': {}, 'v': {}}
    def add(o, at, a_, b_, ri): lines[o].setdefault(key(at), []).append([min(a_, b_), max(a_, b_), ri])
    for i, r in enumerate(rooms):
        add('h', r['y'], r['x'], r['x'] + r['w'], i); add('h', r['y'] + r['h'], r['x'], r['x'] + r['w'], i)
        add('v', r['x'], r['y'], r['y'] + r['h'], i); add('v', r['x'] + r['w'], r['y'], r['y'] + r['h'], i)

    # 문 없이 트인 사이: 두 방이 같은 선에서 맞닿은 구간
    opens = {'h': [], 'v': []}
    for o in ('h', 'v'):
        for at, L in lines[o].items():
            for i in range(len(L)):
                for j in range(i + 1, len(L)):
                    ri, rj = rooms[L[i][2]], rooms[L[j][2]]
                    if L[i][2] == L[j][2]: continue
                    same = ri['_ri'] == rj['_ri']  # 겹침을 떼어 낸 같은 방의 조각끼리: 벽 없음
                    if not same and frozenset((TYPE.get(ri['type'], 'bedroom'), TYPE.get(rj['type'], 'bedroom'))) not in OPEN_PAIRS: continue
                    a_, b_ = max(L[i][0], L[j][0]), min(L[i][1], L[j][1])
                    if b_ - a_ < 0.3: continue
                    # 맞닿음 = 한 방은 이 선의 위(왼)쪽, 다른 방은 아래(오른)쪽
                    side = lambda r: (r['y'] + r['h'] / 2 < at) if o == 'h' else (r['x'] + r['w'] / 2 < at)
                    if side(ri) == side(rj): continue
                    opens[o].append((at, a_, b_, ri['name'], rj['name'], same))

    ops = [dict(o, kind='door') for o in M.get('doors', [])] + [dict(o, kind='window') for o in M.get('windows', [])]
    segs, cut_list = [], []
    for o in ('h', 'v'):
        for at, L in lines[o].items():
            L = sorted([s[:2] for s in L])
            merged = []
            for s in L:
                if merged and s[0] <= merged[-1][1] + 0.01: merged[-1][1] = max(merged[-1][1], s[1])
                else: merged.append(list(s))
            # 뺄 구간: 문·창(그 선 ±0.25m) + 트인 사이
            holes = [(c - op['w'] / 2, c + op['w'] / 2, op) for op in ops if op.get('dir') == o
                     and abs((op['y'] if o == 'h' else op['x']) - at) < 0.25 for c in [op['x'] if o == 'h' else op['y']]]
            holes += [((a_ - 0.01, b_ + 0.01) if sm else (a_ + 0.02, b_ - 0.02)) + (None,) for (at2, a_, b_, _a, _b, sm) in opens[o] if abs(at2 - at) < 0.03]
            holes.sort(key=lambda h: h[0])
            for a_, b_ in merged:
                cur = a_
                for ha, hb, op in holes:
                    if hb <= a_ or ha >= b_: continue
                    ha, hb = max(a_, ha), min(b_, hb)
                    if ha - cur > 0.05: segs.append((o, at, cur, ha))
                    if op is not None: cut_list.append((o, at, ha, hb, op))
                    cur = max(cur, hb)
                if b_ - cur > 0.05: segs.append((o, at, cur, b_))

    # ---- 2. walls.json(벽 조각 px) ----
    t = WALL_T * PPM
    WS = []
    for i, (o, at, a_, b_) in enumerate(segs):
        # 벽 끝을 반 두께만큼 늘려 모서리가 비지 않게
        if o == 'h':
            (ax, ay), (bx, _) = P(a_, at), P(b_, at)
            WS.append({'o': 'h', 'a': [ax - t / 2, ay], 'b': [bx + t / 2, ay], 't': t, 'id': i})
        else:
            (ax, ay), (_, by) = P(at, a_), P(at, b_)
            WS.append({'o': 'v', 'a': [ax, ay - t / 2], 'b': [ax, by + t / 2], 't': t, 'id': i})
    rs = [seg_rect(s) for s in WS]
    bbox = [int(min(r[0] for r in rs)), int(min(r[1] for r in rs)), int(max(r[2] for r in rs)), int(max(r[3] for r in rs))]

    # ---- 3. 깨끗한 평면도 그림(source.png): 방 색 + 벽 + 이름 ----
    COL = {'living': (246, 239, 227), 'kitchen': (246, 239, 227), 'bed': (233, 239, 246), 'bath': (227, 235, 238), 'entry': (241, 236, 226),
           'balcony': (236, 235, 231), 'dress': (240, 234, 244), 'utility': (227, 235, 238), 'hall': (241, 236, 226)}
    im = Image.new('RGB', (Wpx, Hpx), 'white'); d = ImageDraw.Draw(im)
    for r in rooms:
        (ax, ay), (bx, by) = P(r['x'], r['y']), P(r['x'] + r['w'], r['y'] + r['h'])
        d.rectangle([ax, ay, bx, by], fill=COL.get(r['type'], (240, 240, 240)))
    for (o, at, ha, hb, op) in cut_list:
        if op['kind'] == 'window':
            (ax, ay) = P(ha, at) if o == 'h' else P(at, ha); (bx, by) = P(hb, at) if o == 'h' else P(at, hb)
            d.line([ax, ay, bx, by], fill=(110, 160, 210), width=2)
    for r_ in rs: d.rectangle(r_, fill=(30, 30, 30))
    f = font(max(12, int(0.3 * PPM)))
    for i, r in enumerate(rooms0):
        if not rects[i]: continue
        big = max(rects[i], key=lambda q: q['w'] * q['h']); cx, cy = P(big['x'] + big['w'] / 2, big['y'] + big['h'] / 2)
        d.text((cx, cy), r['name'], fill=(70, 70, 70), font=f, anchor='mm')
    im.save(work / 'source.png')
    (work / 'walls.json').write_text(json.dumps({'image': 'source.png', 'size': [Wpx, Hpx], 'dark': 100, 'thick': round(t), 'bbox': bbox,
                                                 'segments': WS, 'from': 'estimator'}, ensure_ascii=False, indent=1), encoding='utf-8')

    # ---- 4. 틈 번호 맞추기: build 와 같은 방법으로 틈을 찾고, 견적 작성기 문·창에 짝지어 종류를 적는다 ----
    shape = (Hpx, Wpx)
    Wm = raster(rs, shape)
    gaps = find_gaps(WS, Wm, PPM, minm=max(0.3, 2.2 * t / PPM))
    room_at = lambda x, y: next((r for r in rooms if r['x'] - 0.02 <= x <= r['x'] + r['w'] + 0.02 and r['y'] - 0.02 <= y <= r['y'] + r['h'] + 0.02), None)
    openings, widths, swings, notes = {}, {}, {}, []
    for i, g in enumerate(gaps):
        gid = f'g{i + 1}'
        mx = ((g['a'][0] + g['b'][0]) / 2) / PPM + x0 - MARGIN; my = ((g['a'][1] + g['b'][1]) / 2) / PPM + y0 - MARGIN
        best = min(cut_list, key=lambda c: abs(((c[2] + c[3]) / 2 if c[0] == 'h' else c[1]) - mx) + abs((c[1] if c[0] == 'h' else (c[2] + c[3]) / 2) - my)
                   + (0 if c[0] == g['o'] else 99), default=None)
        if best is not None:
            bx_ = (best[2] + best[3]) / 2 if best[0] == 'h' else best[1]; by_ = best[1] if best[0] == 'h' else (best[2] + best[3]) / 2
        if best is not None and best[0] == g['o'] and math.hypot(bx_ - mx, by_ - my) < 0.3:
            op = best[4]
            if op['kind'] == 'window': openings[gid] = 'window'; continue
            # 문: 양옆 방 종류를 본다(발코니 사이 = 미닫이)
            off = 0.3
            sides = [room_at(mx, my - off), room_at(mx, my + off)] if g['o'] == 'h' else [room_at(mx - off, my), room_at(mx + off, my)]
            kinds = {s['type'] for s in sides if s}
            openings[gid] = 'slide' if 'balcony' in kinds and len([s for s in sides if s]) == 2 else 'door'
            # 문짝은 사적인 방 쪽으로 연다(복도·거실 쪽으로 열면 좁은 복도를 막는다). 부호: 가로 틈 +1 = 위(-z), 세로 틈 +1 = 오른쪽(+x)
            RANK = {'living': 0, 'kitchen': 0, 'hall': 0, 'entry': 0, 'bed': 1, 'dress': 2, 'utility': 2, 'bath': 2, 'balcony': 2}
            if openings[gid] == 'door' and all(sides):
                ka, kb = [(RANK.get(sd['type'], 1), -sd['w'] * sd['h']) for sd in sides]
                into_first = ka > kb  # sides[0] = 위(가로) / 왼쪽(세로)
                swings[gid] = (1 if into_first else -1) if g['o'] == 'h' else (-1 if into_first else 1)
            if op['w'] >= 0.75: widths[gid] = 0  # 견적 작성기에서 넓게 그린 문은 그 폭 그대로
        else:
            # 짝 없는 틈 = 트인 사이(벽을 뺀 곳) 또는 방이 맞붙지 않아 생긴 빈틈
            hit = any(abs(at - (my if g['o'] == 'h' else mx)) < 0.1 and a_ - 0.2 <= (mx if g['o'] == 'h' else my) <= b_ + 0.2 for (at, a_, b_, _a, _b, sm) in opens[g['o']] if not sm)
            openings[gid] = 'open' if hit else 'none'
    for o in ('h', 'v'):
        for (at, a_, b_, na, nb, sm) in opens[o]:
            if not sm: notes.append(f'{na}–{nb} 사이 벽을 뺌(문 없이 트인 곳으로 봄)')

    # ---- 5. plan.json ----
    furn = []
    counters = 0
    for it in M.get('items', []):
        m_ = FURN.get(it.get('type'))
        if not m_: continue
        typ, chairs = m_
        cx, cy = P(it['x'], it['y'])
        rot = (360 - int(round(float(it.get('rot', 0)) / 90)) * 90) % 360  # 견적 작성기: 왼쪽 90 · 오른쪽 270 → 스킬: 오른쪽 90 · 왼쪽 270
        e = {'type': typ, 'at': [round(cx, 1), round(cy, 1)], 'rot': rot, 'size': [r2(it.get('w') or 1), r2(it.get('d') or 0.6)]}
        if typ == 'counter':
            e.update({'sink': True, 'upper': True} if counters == 0 else {'cooktop': True}); counters += 1
        if chairs: e['chairs'] = chairs
        furn.append(e)
    # ㄱ자 주방처럼 조리대끼리 모서리에서 겹치면 나중 것을 겹친 만큼 짧게(견적 작성기는 겹쳐 그려도 되지만 3D 검사는 겹침을 막는다)
    def foot(e):
        w, d_ = e['size'][0] * PPM, e['size'][1] * PPM
        if e['rot'] in (90, 270): w, d_ = d_, w
        return [e['at'][0] - w / 2, e['at'][1] - d_ / 2, e['at'][0] + w / 2, e['at'][1] + d_ / 2]
    for i, e in enumerate(furn):
        if e['type'] not in ('counter', 'island'): continue
        for e0 in furn[:i]:
            if e0['type'] not in ('counter', 'island'): continue
            A, B = foot(e0), foot(e)
            ox, oy = min(A[2], B[2]) - max(A[0], B[0]), min(A[3], B[3]) - max(A[1], B[1])
            if ox <= 0 or oy <= 0: continue
            along_x = e['rot'] in (0, 180)  # 조리대 길이 방향
            k = 0 if along_x else 1
            sgn = 1 if (B[k] + B[k + 2]) / 2 > (A[k] + A[k + 2]) / 2 else -1  # 겹친 쪽 반대로 민다
            ov = ((A[k + 2] - B[k]) if sgn > 0 else (B[k + 2] - A[k])) + 0.02 * PPM  # 가까운 끝을 상대 조리대 끝까지 물린다
            if ov <= 0 or ov >= e['size'][0] * PPM - 0.3 * PPM: continue
            e['size'][0] = r2(e['size'][0] - ov / PPM); e['at'][k] = round(e['at'][k] + sgn * ov / 2, 1)
    plan = {
        'name': name, 'image': 'source.png', 'source': 'estimator',
        'estimator': {'projectId': M.get('projectId') or M.get('id'), 'modelUpdatedAt': M.get('updatedAt') or M.get('createdAt')},
        'scale': {'a': [0, 0], 'b': [PPM * 10, 0], 'm': 10},
        'wallHeight': float(M.get('height') or 2.3), 'view': 'forest',
        'walls': {'remove': [], 'add': []},
        'openings': openings, 'widths': widths, 'swings': swings, 'openingsAdd': [],
        'rooms': [{'name': r['name'], 'type': TYPE.get(r['type'], 'bedroom'),
                   'at': [round(v, 1) for v in P(big['x'] + big['w'] / 2, big['y'] + big['h'] / 2)],
                   'area': r2(sum(q['w'] * q['h'] for q in rects[i]))}
                  for i, r in enumerate(rooms0) if rects[i] for big in [max(rects[i], key=lambda q: q['w'] * q['h'])]],
        'furniture': furn, 'start': None, 'tour': [],
    }
    # 견적 작성기 평면은 복도를 실제보다 좁게(0.8~1m) 그리는 일이 많다 → 걷는 길 여유를 줄여 지나갈 수 있게
    narrow = [r['name'] for r in rooms0 if r['type'] in ('hall', 'entry') and min(r['w'], r['h']) < 1.15]
    if narrow:
        plan['clearance'] = {'wall': 0.2}
        notes.append(f"좁은 통로({', '.join(narrow)}): 벽과 걷는 길 사이 여유를 0.2m로 줄임")
    if a.floor: plan['floorLevel'] = a.floor
    if PRJ.get('buildingType') and PRJ['buildingType'] not in ('아파트', '오피스텔', '빌라'): plan['corridor'] = False
    (work / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')

    def draw(dd, ff):
        for s in WS:
            dd.line([tuple(s['a']), tuple(s['b'])], fill=(230, 90, 60) if s['o'] == 'h' else (40, 150, 90), width=3)
    grid_overlay(im.copy(), draw).save(work / 'walls.png')
    n_open = sum(1 for v in openings.values() if v == 'open')
    print(f'[견적 작성기 평면도 → 작업 폴더]\n방 {len(rooms0)} · 벽 {len(WS)} · 문 {len(M.get("doors", []))} · 창 {len(M.get("windows", []))} · 트임 {n_open} · 가구 {len(furn)}')
    print(f'건물 {r2(x1 - x0)}×{r2(y1 - y0)}m (견적 작성기 치수 그대로, 축척 확인 불필요)')
    for nline in overlaps + notes: print('  ' + nline)
    print(f'작업폴더: {work}\n다음: python3 tour.py make "{work}"')


if __name__ == '__main__':
    main()
