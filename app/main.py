"""FastAPI 入口：图编辑 API + 静态前端托管。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import database, graph
from app.schemas import CommitRequest, CounterfactualRequest, RollbackRequest
from app.graph import GraphError


def get_conn():
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = database.connect()
    try:
        database.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="事故复盘因果图", version="1.0.0", lifespan=lifespan)


@app.exception_handler(GraphError)
async def graph_error_handler(request: Request, exc: GraphError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": exc.message, "code": exc.code, "cycle": exc.cycle,
                 "conflict": exc.conflict},
    )


# ---------- 序列化 ----------

def node_dict(n: graph.Node, windows: dict | None = None, reviews: dict | None = None) -> dict[str, Any]:
    data = {"id": n.id, "type": n.type, "title": n.title, "description": n.description,
            "pos_x": n.pos_x, "pos_y": n.pos_y, "created_at": n.created_at,
            "earliest_time": n.earliest_time, "latest_time": n.latest_time,
            "reliability": n.reliability, "source_group": n.source_group,
            # 触发模式随每个快照原样返回；证据节点恒为 ANY（推演时忽略）
            "trigger_mode": n.trigger_mode if n.type == "event" else None}
    if windows is not None:
        data["time_window"] = graph.time_window_dict(windows["nodes"], windows["windows"], n.id)
    if reviews is not None:
        data["evidence_review"] = graph.evidence_review_dict(reviews["nodes"], reviews["reviews"], n.id)
    return data


def edge_dict(e: graph.Edge) -> dict[str, Any]:
    return {"id": e.id, "source_id": e.source_id, "target_id": e.target_id,
            "kind": e.kind, "label": e.label, "created_at": e.created_at}


def graph_dict(g: graph.Graph) -> dict[str, Any]:
    # 有效时间窗与证据强度均按当前所查看的版本重算（历史快照即按该版本拓扑/数据重算）
    windows, _conflict = graph.compute_time_windows(g.nodes, g.edges)
    reviews = graph.compute_evidence_review(g.nodes, g.edges)
    win_ctx = {"nodes": g.nodes, "windows": windows}
    rev_ctx = {"nodes": g.nodes, "reviews": reviews}
    return {
        "version_id": g.version_id,
        "nodes": [node_dict(n, win_ctx, rev_ctx) for n in g.nodes.values()],
        "edges": [edge_dict(e) for e in g.edges.values()],
    }


def version_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    node_count = conn.execute("SELECT COUNT(*) FROM nodes WHERE version_id = ?", (row["id"],)).fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM edges WHERE version_id = ?", (row["id"],)).fetchone()[0]
    return {
        "id": row["id"],
        "message": row["message"],
        "kind": row["kind"],
        "base_id": row["base_id"],
        "created_at": row["created_at"],
        "node_count": node_count,
        "edge_count": edge_count,
    }


# ---------- 查询 ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state")
def get_state(conn: sqlite3.Connection = Depends(get_conn)):
    vid = graph.current_version_id(conn)
    g = graph.load_graph(conn, vid)
    return graph_dict(g)


@app.get("/api/versions")
def list_versions(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM versions ORDER BY id DESC").fetchall()
    current = graph.current_version_id(conn)
    return {"current_version_id": current, "versions": [version_dict(conn, r) for r in rows]}


@app.get("/api/versions/{version_id}")
def get_version(version_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    g = graph.load_graph(conn, version_id)
    if g is None:
        raise HTTPException(status_code=404, detail=f"版本不存在：{version_id}")
    return graph_dict(g)


@app.get("/api/nodes/{node_id}/downstream")
def node_downstream(node_id: str, version_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    vid = version_id or graph.current_version_id(conn)
    g = graph.load_graph(conn, vid)
    if g is None:
        raise HTTPException(status_code=404, detail=f"版本不存在：{vid}")
    try:
        return graph.downstream_of(g, node_id)
    except GraphError as exc:
        raise HTTPException(status_code=404, detail=exc.message)


# ---------- 反事实干预沙盘（只读：不写库、不产生 changeset / 版本） ----------

@app.post("/api/counterfactual/simulate")
def counterfactual_simulate(body: CounterfactualRequest, conn: sqlite3.Connection = Depends(get_conn)):
    vid = body.version_id or graph.current_version_id(conn)
    g = graph.load_graph(conn, vid)
    if g is None:
        raise HTTPException(status_code=404, detail=f"版本不存在：{vid}")
    try:
        return graph.simulate_counterfactual(g, body.target_id, body.prevented_ids)
    except GraphError as exc:
        raise HTTPException(status_code=400, detail=exc.message)


# ---------- 提交一组修改（事务：校验整张图，任一失败全部拒绝） ----------

@app.post("/api/changes/commit", response_model_exclude_none=True)
def commit_changes(body: CommitRequest, conn: sqlite3.Connection = Depends(get_conn)):
    ch = body.changes
    if not any([ch.node_creates, ch.node_updates, ch.node_deletes,
                ch.edge_creates, ch.edge_updates, ch.edge_deletes]):
        raise GraphError("没有任何可提交的修改", "empty_changeset")

    try:
        conn.execute("BEGIN IMMEDIATE")

        current = graph.current_version_id(conn)
        if ch.base_version_id != current:
            raise GraphError(
                f"基准版本已过期（页面基于 v{ch.base_version_id}，当前为 v{current}），请刷新后重新提交",
                "stale_version",
            )

        old = graph.load_graph(conn, current)
        # 任何校验失败（含成环）都抛 GraphError → 回滚，不留半成品
        new_nodes, new_edges = graph.apply_changes(old, ch)

        cur = conn.execute(
            "INSERT INTO versions (message, kind, base_id) VALUES (?, 'commit', ?)",
            (body.message, current),
        )
        new_id = cur.lastrowid
        graph.persist_graph(conn, new_id, new_nodes, new_edges)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM versions WHERE id = ?", (new_id,)).fetchone()
    return {"version_id": new_id, "message": body.message, "created_at": row["created_at"]}


# ---------- 回滚：把旧版快照复制为一个新版本 ----------

@app.post("/api/versions/rollback")
def rollback(body: RollbackRequest, conn: sqlite3.Connection = Depends(get_conn)):
    target = graph.load_graph(conn, body.target_version_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"版本不存在：{body.target_version_id}")

    current = graph.current_version_id(conn)
    if body.target_version_id == current:
        raise GraphError("该版本就是当前版本，无需回滚", "already_current")

    message = body.message or f"回滚到 v{body.target_version_id}"
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO versions (message, kind, base_id) VALUES (?, 'rollback', ?)",
            (message, body.target_version_id),
        )
        new_id = cur.lastrowid
        graph.persist_graph(conn, new_id, target.nodes, target.edges)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM versions WHERE id = ?", (new_id,)).fetchone()
    return {"version_id": new_id, "message": message, "created_at": row["created_at"],
            "restored_from": body.target_version_id}


# 静态前端（API 路由先注册，未匹配的路径交给 StaticFiles）
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
