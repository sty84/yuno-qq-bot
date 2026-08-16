"""平面图几何层（P0 几何化）：房间多边形 → 面积 / 质心 / 门图 / 实际路程。

数据源：personas/<pack>/world.json 的 floorplan 段（手写 JSON 多边形，
未来可由 tools.py floorplan-import 从 SVG 自动解析生成）。
没有 floorplan 时全部回退，不影响现有拓扑逻辑。

推导（加载时按 mtime 缓存）：
- 面积（鞋带公式）× scale² → 回答"哪个房间大"；
- 质心 + 门中点 → 房间间实际路程（Dijkstra，质心↔门中点按欧氏距离）；
- 邻接边表从 doors 推导，不再手写 edges。
"""

import json
import math


_cache = {"key": None, "data": None}


def active_pack() -> str:
    try:
        from memory import pack
        return pack.active()
    except Exception:
        return "yuno"


def _raw(pack_name=None) -> dict:
    """读取指定（或当前激活）pack 的 floorplan 段，按 (pack, mtime) 缓存。"""
    try:
        from memory import pack
        name = pack_name or active_pack()
        p = pack.pack_dir(name) / "world.json"
        key = (name, p.stat().st_mtime_ns if p.exists() else 0)
        if _cache["key"] == key:
            return _cache["data"]
        if not p.exists():
            _cache.update({"key": key, "data": {}})  # type: ignore[dict-item]
            return {}
        fp = json.loads(p.read_text(encoding="utf-8")).get("floorplan") or {}
        _cache.update({"key": key, "data": fp})  # type: ignore[dict-item]
        return fp
    except Exception:
        return {}


def data(pack_name=None) -> dict:
    return _raw(pack_name)


def rooms(pack_name=None) -> dict:
    return _raw(pack_name).get("rooms") or {}


def doors(pack_name=None) -> list:
    return _raw(pack_name).get("doors") or []


def scale_m_per_px(pack_name=None) -> float:
    try:
        return float(_raw(pack_name).get("scale_m_per_px") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def walk_m_per_min(pack_name=None) -> float:
    try:
        return float(_raw(pack_name).get("walk_m_per_min") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def enabled(pack_name=None) -> bool:
    return bool(rooms(pack_name)) and scale_m_per_px(pack_name) > 0


# ===== 多边形几何 =====
def _points(poly) -> list:
    pts = [(float(x), float(y)) for x, y in poly]
    if len(pts) >= 3 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def polygon_area(poly) -> float:
    """鞋带公式（取绝对值，顺时针/逆时针都行）。"""
    pts = _points(poly)
    if len(pts) < 3:
        return 0.0
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_centroid(poly):
    pts = _points(poly)
    n = len(pts)
    if n < 3:
        return (0.0, 0.0)
    a = cx = cy = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    a *= 0.5
    if abs(a) < 1e-9:
        return (0.0, 0.0)
    return (round(cx / (6.0 * a), 2), round(cy / (6.0 * a), 2))


def _segments(poly) -> list:
    pts = _points(poly)
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _on_segment(p, a, b, tol=1e-6) -> bool:
    px, py = float(p[0]), float(p[1])
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > 1e-6:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -tol:
        return False
    l2 = (bx - ax) ** 2 + (by - ay) ** 2
    return l2 > 0 and dot <= l2 + tol


def _seg_proper_intersect(p1, p2, p3, p4) -> bool:
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = ccw(p3, p4, p1), ccw(p3, p4, p2)
    d3, d4 = ccw(p1, p2, p3), ccw(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def room_area_m2(room, pack_name=None) -> float:
    poly = (rooms(pack_name).get(room) or {}).get("polygon")
    return polygon_area(poly) * scale_m_per_px(pack_name) ** 2 if poly else 0.0


def room_centroid(room, pack_name=None):
    poly = (rooms(pack_name).get(room) or {}).get("polygon")
    return polygon_centroid(poly) if poly else (0.0, 0.0)


# ===== 门图 / 邻接 / 路程 =====
def adjacency_edges(pack_name=None) -> list:
    """从 doors 推导邻接边表（排除大门），返回去重后的 [(a, b)]。"""
    seen, out = set(), []
    for d in doors(pack_name):
        between = d.get("between") or []
        if len(between) < 2:
            continue
        a, b = str(between[0]), str(between[1])
        if not a or not b or a == b or "大门" in (a, b):
            continue
        k = tuple(sorted((a, b)))
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _graph(pack_name=None):
    """节点 = 房间质心 + 门中点；边 = 质心↔该房间的门中点（欧氏距离，px）。"""
    rs, ds = rooms(pack_name), doors(pack_name)
    nodes, door_mids = {}, {}
    for name in rs:
        nodes["room:" + name] = room_centroid(name, pack_name)
    for i, d in enumerate(ds):
        between = d.get("between") or ["", ""]
        pos = d.get("pos") or [[0, 0], [0, 0]]
        mid = ((float(pos[0][0]) + float(pos[1][0])) / 2.0,
               (float(pos[0][1]) + float(pos[1][1])) / 2.0)
        nid = "door:%d" % i
        door_mids[nid] = (str(between[0]), str(between[1]))
        nodes[nid] = mid
    adj = {k: [] for k in nodes}

    def add(u, v):
        dx = nodes[u][0] - nodes[v][0]
        dy = nodes[u][1] - nodes[v][1]
        w = math.sqrt(dx * dx + dy * dy)
        adj[u].append((v, w))
        adj[v].append((u, w))

    for nid, (a, b) in door_mids.items():
        if a in rs:
            add("room:" + a, nid)
        if b in rs:
            add("room:" + b, nid)
    return nodes, adj, door_mids


def _nodes_for(name, rs, door_mids) -> list:
    if name == "大门":
        return [nid for nid, (a, b) in door_mids.items() if "大门" in (a, b)]
    if name in rs:
        return ["room:" + name]
    return []


def route_distance(a, b, pack_name=None):
    """房间间实际路程（米）。a/b 可为房间名或"大门"；无几何/不可达返回 None。"""
    a, b = str(a or ""), str(b or "")
    if not a or not b or a == b:
        return 0.0
    rs = rooms(pack_name)
    if a not in rs and a != "大门":
        return None
    if b not in rs and b != "大门":
        return None
    nodes, adj, door_mids = _graph(pack_name)
    starts = _nodes_for(a, rs, door_mids)
    targets = _nodes_for(b, rs, door_mids)
    if not starts or not targets:
        return None
    dist = {s: 0.0 for s in starts}
    import heapq
    pq = [(0.0, s) for s in starts]
    heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        for v, w in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    best = min((dist.get(t) for t in targets if t in dist), default=None)
    if best is None:
        return None
    return best * scale_m_per_px(pack_name)


def route_minutes(a, b, pack_name=None):
    """实际路程分钟数；无几何/不可达返回 None。"""
    m = route_distance(a, b, pack_name)
    speed = walk_m_per_min(pack_name)
    if m is None or speed <= 0:
        return None
    return round(max(0.1, m / speed), 2)


def dist_to_entrance(room, pack_name=None):
    return route_distance(room, "大门", pack_name)


# ===== 事实注入 =====
def facts_text(room, pack_name=None) -> str:
    """平面图推导的房间事实文本（面积/排名/邻居/离大门距离）。"""
    rs = rooms(pack_name)
    if not enabled(pack_name) or room not in rs:
        return ""
    area = room_area_m2(room, pack_name)
    areas = sorted(((room_area_m2(r, pack_name), r) for r in rs), reverse=True)
    rank = [r for _a, r in areas].index(room) + 1
    n = len(areas)
    if rank == 1:
        size_word = "是家里最大的房间"
    elif rank == n:
        size_word = "是家里最小的房间"
    else:
        size_word = f"面积排第{rank}"
    parts = [f"{room}约{area:.1f}㎡，{size_word}"]
    nei = sorted(
        {x for a, b in adjacency_edges(pack_name) if a == room for x in (b,)}
        | {x for a, b in adjacency_edges(pack_name) if b == room for x in (a,)}
    )
    if nei:
        parts.append("与" + "、".join(nei) + "相邻")
    dm = dist_to_entrance(room, pack_name)
    if dm is not None:
        minutes = max(1, int(round(dm / max(0.1, walk_m_per_min(pack_name)))))
        parts.append(f"离大门约{dm:.1f}米，步行约{minutes}分钟")
    return "；".join(parts) + "。"


# ===== 校验 =====
def _self_intersects(poly) -> bool:
    segs = _segments(poly)
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if j == i or (j + 1) % len(segs) == i or (i + 1) % len(segs) == j:
                continue
            if _seg_proper_intersect(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                return True
    return False


def validate(pack_name=None) -> list:
    """返回问题列表；空列表 = 合法。"""
    fp = _raw(pack_name)
    if not fp:
        return []
    issues = []
    rs = fp.get("rooms") or {}
    ds = fp.get("doors") or []
    if not rs:
        issues.append("floorplan.rooms 为空")
    if scale_m_per_px(pack_name) <= 0:
        issues.append("scale_m_per_px 必须 > 0")
    if walk_m_per_min(pack_name) <= 0:
        issues.append("walk_m_per_min 必须 > 0")
    for name, r in rs.items():
        poly = r.get("polygon")
        if not poly or len(poly) < 3:
            issues.append(f"房间 {name} 多边形点数不足")
            continue
        if polygon_area(poly) <= 0:
            issues.append(f"房间 {name} 面积为 0")
        if _self_intersects(poly):
            issues.append(f"房间 {name} 多边形自交")
    deg = {name: 0 for name in rs}
    for i, d in enumerate(ds):
        between = d.get("between") or []
        pos = d.get("pos") or []
        if len(between) < 2 or len(pos) < 2:
            issues.append(f"门 #{i} between/pos 格式错误")
            continue
        a, b = str(between[0]), str(between[1])
        if a not in rs and a != "大门":
            issues.append(f"门 #{i} 房间不存在：{a}")
        if b not in rs and b != "大门":
            issues.append(f"门 #{i} 房间不存在：{b}")
        if a in rs:
            deg[a] += 1
            if not any(_on_segment(p, s[0], s[1]) for p in pos for s in _segments(rs[a]["polygon"])):
                issues.append(f"门 #{i} 端点不在 {a} 的墙上")
        if b in rs:
            deg[b] += 1
            if not any(_on_segment(p, s[0], s[1]) for p in pos for s in _segments(rs[b]["polygon"])):
                issues.append(f"门 #{i} 端点不在 {b} 的墙上")
    for name, d in deg.items():
        if d == 0:
            issues.append(f"房间 {name} 没有门（图不连通）")
    # 门图连通性
    adj = {}  # type: ignore[var-annotated]
    for a, b in adjacency_edges(pack_name):
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    if rs:
        start = next(iter(rs))
        seen, queue = {start}, [start]
        while queue:
            cur = queue.pop()
            for nxt in adj.get(cur, []):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if len(seen) != len(rs):
            issues.append(f"房间图不连通，孤立：{sorted(set(rs) - seen)}")
    # 大门可达
    has_entrance = any(
        "大门" in [str(x) for x in (d.get("between") or [])]
        for d in ds
    )
    if has_entrance:
        for name in rs:
            if dist_to_entrance(name, pack_name) is None:
                issues.append(f"{name} 到大门不可达")
    return issues


# ===== SVG 渲染（给人看的预览，也是将来 floorplan-import 的回环样本）=====
def render_svg(pack_name=None) -> str:
    fp = _raw(pack_name)
    rs = fp.get("rooms") or {}
    ds = fp.get("doors") or []
    if not rs:
        return ""
    xs = [p[0] for r in rs.values() for p in r.get("polygon", [])]
    ys = [p[1] for r in rs.values() for p in r.get("polygon", [])]
    if not xs:
        return ""
    m = 12
    min_x, max_x = min(xs) - m, max(xs) + m
    min_y, max_y = min(ys) - m, max(ys) + m
    w, h = max_x - min_x, max_y - min_y
    palette = ["#fde68a", "#a7f3d0", "#bfdbfe", "#fbcfe8", "#e9d5ff", "#fecaca", "#fef3c7"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x} {min_y} {w} {h}" '
        f'width="{w}" height="{h}" font-family="sans-serif">',
        "<title>floorplan %s</title>" % (pack_name or "active"),
    ]
    for i, (name, r) in enumerate(rs.items()):
        pts = " ".join(f"{x},{y}" for x, y in _points(r.get("polygon", [])))
        fill = palette[i % len(palette)]
        parts.append(f'<polygon points="{pts}" fill="{fill}" stroke="#334155" stroke-width="2"/>')
        cx, cy = polygon_centroid(r.get("polygon", []))
        area = room_area_m2(name, pack_name)
        parts.append(
            f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" font-size="13" fill="#0f172a" '
            f'font-weight="bold">{name}</text>'
        )
        parts.append(f'<text x="{cx}" y="{cy + 12}" text-anchor="middle" font-size="11" fill="#475569">{area:.1f}㎡</text>')
    for d in ds:
        between = d.get("between") or []
        pos = d.get("pos") or []
        if len(pos) >= 2:
            x1, y1 = pos[0]
            x2, y2 = pos[1]
            parts.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#dc2626" stroke-width="3" '
                f'stroke-dasharray="1 2"/>'
            )
            if "大门" in between:
                mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                parts.append(f'<text x="{mx}" y="{my - 6}" text-anchor="middle" font-size="11" fill="#dc2626">大门</text>')
    scale = scale_m_per_px(pack_name)
    parts.append(f'<text x="{min_x + 2}" y="{max_y - 3}" font-size="10" fill="#94a3b8">'
                 f'scale {scale} m/px · walk {walk_m_per_min(pack_name)} m/min · 门=红色短线</text>')
    parts.append("</svg>")
    return "\n".join(parts)
