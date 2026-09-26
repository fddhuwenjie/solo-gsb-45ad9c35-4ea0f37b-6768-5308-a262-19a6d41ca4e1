"""可复算复盘结论包：自包含导出 + 只读核验。

导出（build_pack）：
- 收集指定事件、全部 causes 祖先，以及这些事件关联的 supports/refutes 证据；
- 连同版本元数据、在包内子图上重算的有效时间窗与证据审查结果组成 payload；
- 节点与连线按 id 排序，对象键按字典序，对 payload 的紧凑 UTF-8 JSON 计算 SHA-256。

核验（verify_pack）：
- 先校验 digest，再仅凭包内原始节点与连线重建 DAG，
  重算时间窗与证据审查，与随包结果逐字段比对（差异以 JSON Pointer 列出）。

两个入口均为纯函数 / 只读：不写库、不生成版本、不改变工作图。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app import graph

FORMAT = "incident-conclusion-pack"
FORMAT_VERSION = 1

_EDGE_KINDS = ("causes", "supports", "refutes")
_NODE_TYPES = ("event", "evidence")


def canonical_json(obj: Any) -> bytes:
    """紧凑、对象键按字典序的 UTF-8 JSON —— 导出与核验共用的规范化形式。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_of(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


# ---------- 序列化（与 main.node_dict / edge_dict 同构，但不含计算结果） ----------

def _node_payload(n: graph.Node) -> dict[str, Any]:
    return {
        "id": n.id, "type": n.type, "title": n.title, "description": n.description,
        "pos_x": n.pos_x, "pos_y": n.pos_y, "created_at": n.created_at,
        "earliest_time": n.earliest_time, "latest_time": n.latest_time,
        "reliability": n.reliability, "source_group": n.source_group,
        "trigger_mode": n.trigger_mode if n.type == "event" else None,
    }


def _edge_payload(e: graph.Edge) -> dict[str, Any]:
    return {"id": e.id, "source_id": e.source_id, "target_id": e.target_id,
            "kind": e.kind, "label": e.label, "created_at": e.created_at}


# ---------- 导出 ----------

def build_pack(g: graph.Graph, version_meta: dict[str, Any], event_id: str) -> dict[str, Any]:
    """以 event_id 为结论事件构建自包含结论包（含 digest）。

    时间窗与证据审查在「包内子图」上计算：下界仍等于全图传播结果（祖先已全量收集），
    上界只由包内事件收紧 —— 这样核验方仅凭包内数据即可复算出完全相同的结果。
    """
    node = g.nodes.get(event_id)
    if node is None:
        raise graph.GraphError(f"事件不存在：{event_id}", "node_not_found")
    if node.type != "event":
        raise graph.GraphError(f"结论包的目标必须是事件节点：{event_id}", "target_not_event")

    # 1) 沿 causes 边向上收集全部祖先（含结论事件自身）
    causes_by_target: dict[str, list[graph.Edge]] = {}
    for e in g.edges.values():
        if e.kind == "causes":
            causes_by_target.setdefault(e.target_id, []).append(e)
    event_ids = {event_id}
    stack = [event_id]
    while stack:
        u = stack.pop()
        for e in causes_by_target.get(u, []):
            if e.source_id not in event_ids:
                event_ids.add(e.source_id)
                stack.append(e.source_id)

    # 2) 这些事件关联的 supports/refutes 证据，以及全部相关连线
    evidence_ids: set[str] = set()
    pack_edges: dict[str, graph.Edge] = {}
    for e in g.edges.values():
        if e.kind == "causes" and e.source_id in event_ids and e.target_id in event_ids:
            pack_edges[e.id] = e
        elif e.kind in ("supports", "refutes") and e.target_id in event_ids and e.source_id in g.nodes:
            pack_edges[e.id] = e
            evidence_ids.add(e.source_id)
    node_ids = event_ids | evidence_ids

    # 3) 包内子图（按 id 排序建 dict，保证与核验侧迭代顺序一致、结果可复现）
    sub_nodes = {nid: g.nodes[nid] for nid in sorted(node_ids)}
    sub_edges = {eid: pack_edges[eid] for eid in sorted(pack_edges)}

    # 4) 在包内子图上重算有效时间窗与证据审查
    windows, _conflict = graph.compute_time_windows(sub_nodes, sub_edges)
    reviews = graph.compute_evidence_review(sub_nodes, sub_edges)
    sorted_events = sorted(event_ids)

    payload = {
        "event_id": event_id,
        "version": version_meta,
        "nodes": [_node_payload(sub_nodes[nid]) for nid in sorted(node_ids)],
        "edges": [_edge_payload(sub_edges[eid]) for eid in sorted(sub_edges)],
        "results": {
            "time_windows": {
                nid: graph.time_window_dict(sub_nodes, windows, nid) for nid in sorted_events
            },
            "evidence_reviews": {
                nid: graph.evidence_review_dict(sub_nodes, reviews, nid) for nid in sorted_events
            },
        },
    }
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "digest": digest_of(payload),
        "payload": payload,
    }


# ---------- 核验 ----------

_MISSING = object()  # 哨兵：区分「键缺失」与「值为 null」


def _json_pointer(path: list[str]) -> str:
    """RFC 6901：~ → ~0，/ → ~1；根为空的字符串。"""
    if not path:
        return ""
    return "/" + "/".join(str(seg).replace("~", "~0").replace("/", "~1") for seg in path)


def _diff(packaged: Any, recomputed: Any, path: list[str], out: list[dict[str, Any]]) -> None:
    """逐字段深比较，把每一处叶级差异记录为 JSON Pointer + 包内值 + 重算值。"""
    if isinstance(packaged, dict) and isinstance(recomputed, dict):
        for key in sorted(set(packaged) | set(recomputed)):
            _diff(packaged.get(key, _MISSING), recomputed.get(key, _MISSING), path + [key], out)
        return
    if isinstance(packaged, list) and isinstance(recomputed, list):
        if len(packaged) != len(recomputed):
            out.append(_mismatch(path, packaged, recomputed))
            return
        for i, (a, b) in enumerate(zip(packaged, recomputed)):
            _diff(a, b, path + [str(i)], out)
        return
    if packaged is _MISSING or recomputed is _MISSING or packaged != recomputed:
        out.append(_mismatch(path, packaged, recomputed))


def _mismatch(path: list[str], packaged: Any, recomputed: Any) -> dict[str, Any]:
    return {
        "pointer": _json_pointer(path),
        "packaged": None if packaged is _MISSING else packaged,
        "recomputed": None if recomputed is _MISSING else recomputed,
        "packaged_missing": packaged is _MISSING,
        "recomputed_missing": recomputed is _MISSING,
    }


def _rebuild(nodes_raw: Any, edges_raw: Any, errors: list[str]):
    """仅凭包内原始节点与连线重建内存 DAG；非法结构记入 errors 并返回 None。"""
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        errors.append("payload.nodes / payload.edges 必须是数组")
        return None

    nodes: dict[str, graph.Node] = {}
    for i, raw in enumerate(nodes_raw):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            errors.append(f"payload.nodes[{i}] 缺少字符串 id")
            return None
        nid = raw["id"]
        if nid in nodes:
            errors.append(f"payload.nodes 中节点 id 重复：{nid}")
            return None
        ntype = raw.get("type")
        if ntype not in _NODE_TYPES:
            errors.append(f"节点 {nid} 的 type 非法：{ntype!r}")
            return None
        reliability = raw.get("reliability")
        if reliability is not None and (not isinstance(reliability, (int, float)) or isinstance(reliability, bool)):
            errors.append(f"节点 {nid} 的 reliability 必须是数字或 null")
            return None
        source_group = raw.get("source_group")
        if source_group is not None and not isinstance(source_group, str):
            errors.append(f"节点 {nid} 的 source_group 必须是字符串或 null")
            return None
        for field in ("earliest_time", "latest_time"):
            if raw.get(field) is not None and not isinstance(raw.get(field), str):
                errors.append(f"节点 {nid} 的 {field} 必须是字符串或 null")
                return None
        title = raw.get("title")
        nodes[nid] = graph.Node(
            id=nid, type=ntype,
            title=title if isinstance(title, str) else "",
            description=raw.get("description") if isinstance(raw.get("description"), str) else "",
            pos_x=0.0, pos_y=0.0, created_at="",
            earliest_time=raw.get("earliest_time"), latest_time=raw.get("latest_time"),
            reliability=reliability, source_group=source_group,
            trigger_mode=raw.get("trigger_mode") if raw.get("trigger_mode") in ("ANY", "ALL") else "ANY",
        )

    edges: dict[str, graph.Edge] = {}
    for i, raw in enumerate(edges_raw):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            errors.append(f"payload.edges[{i}] 缺少字符串 id")
            return None
        eid = raw["id"]
        if eid in edges:
            errors.append(f"payload.edges 中连线 id 重复：{eid}")
            return None
        kind = raw.get("kind")
        if kind not in _EDGE_KINDS:
            errors.append(f"连线 {eid} 的 kind 非法：{kind!r}")
            return None
        src, dst = raw.get("source_id"), raw.get("target_id")
        if src not in nodes or dst not in nodes:
            errors.append(f"连线 {eid} 的端点不在包内节点中：{src!r} → {dst!r}")
            return None
        label = raw.get("label")
        edges[eid] = graph.Edge(eid, src, dst, kind,
                                label if isinstance(label, str) else "", created_at="")

    # 与导出侧一致：按 id 排序重建，保证拓扑序 / 平局取舍可复现
    nodes = {nid: nodes[nid] for nid in sorted(nodes)}
    edges = {eid: edges[eid] for eid in sorted(edges)}
    return nodes, edges


def verify_pack(pack: Any) -> dict[str, Any]:
    """只读核验：先校验 digest，再重建 DAG 重算并逐字段比对。

    返回 {ok, verdict, digest, errors, mismatches, checked}；
    ok 当且仅当摘要匹配、结构合法且重算结果与随包结果完全一致。
    """
    errors: list[str] = []
    mismatches: list[dict[str, Any]] = []
    digest_info: dict[str, Any] = {"packaged": None, "recomputed": None, "match": False}
    checked = {"events": 0, "nodes": 0, "edges": 0}

    def result(ok: bool) -> dict[str, Any]:
        return {
            "ok": ok,
            "verdict": "pass" if ok else "fail",
            "digest": digest_info,
            "errors": errors,
            "mismatches": mismatches,
            "checked": checked,
        }

    if not isinstance(pack, dict):
        errors.append("文件内容不是 JSON 对象")
        return result(False)
    if pack.get("format") != FORMAT:
        errors.append(f"format 缺失或不识别：{pack.get('format')!r}（期望 {FORMAT!r}）")

    payload = pack.get("payload")
    digest_info["packaged"] = pack.get("digest")
    if not isinstance(payload, dict):
        errors.append("缺少 payload 对象")
        return result(False)

    # 1) 摘要校验：对包内 payload 的规范化紧凑 UTF-8 JSON 重算 SHA-256
    digest_info["recomputed"] = digest_of(payload)
    digest_info["match"] = digest_info["packaged"] == digest_info["recomputed"]
    if not digest_info["match"]:
        errors.append("digest 与包内 payload 不匹配（内容可能被篡改或截断）")

    # 2) 仅凭包内原始节点与连线重建 DAG
    rebuilt = _rebuild(payload.get("nodes"), payload.get("edges"), errors)
    if rebuilt is None:
        return result(False)
    nodes, edges = rebuilt
    checked.update({"nodes": len(nodes), "edges": len(edges)})

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or event_id not in nodes:
        errors.append(f"payload.event_id 不在包内节点中：{event_id!r}")
        return result(False)
    if nodes[event_id].type != "event":
        errors.append(f"payload.event_id 不是事件节点：{event_id}")
        return result(False)

    cycle = graph.find_cycle(nodes, edges)
    if cycle:
        errors.append("包内节点与连线构成环：" + " → ".join(cycle + [cycle[0]]))
        return result(False)

    # 3) 重算时间窗与证据审查，与随包结果逐字段比对
    event_ids = sorted(nid for nid, n in nodes.items() if n.type == "event")
    checked["events"] = len(event_ids)
    windows, _conflict = graph.compute_time_windows(nodes, edges)
    reviews = graph.compute_evidence_review(nodes, edges)
    recomputed_results = {
        "time_windows": {nid: graph.time_window_dict(nodes, windows, nid) for nid in event_ids},
        "evidence_reviews": {nid: graph.evidence_review_dict(nodes, reviews, nid) for nid in event_ids},
    }
    packaged_results = payload.get("results")
    if not isinstance(packaged_results, dict):
        errors.append("payload.results 缺失或不是对象，无法逐字段比对")
    else:
        _diff(packaged_results, recomputed_results, ["results"], mismatches)

    ok = digest_info["match"] and not errors and not mismatches
    return result(ok)
