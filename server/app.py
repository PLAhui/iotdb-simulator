"""FastAPI 服务：配置 API、任务控制、SSE 实时日志、静态页托管。"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from typing import AsyncGenerator, Dict

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .config import AppConfig, config_from_dict, default_config, load_config, save_config
from .engine import scale
from .writer import Writer

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"

app = FastAPI(title="IoTDB 数据模拟器")

cfg: AppConfig = load_config(CONFIG_PATH)
# 每个 SSE 连接一条独立队列（发布/订阅），不再共用一个全局队列
_subscribers: "set[queue.Queue]" = set()
_sub_lock = threading.Lock()
stop_event = threading.Event()
run_state: Dict[str, object] = {"running": False, "phase": "idle", "state": "idle", "result": None}
worker: threading.Thread | None = None


def push(item: dict) -> None:
    """把一条事件广播给所有 SSE 订阅者。

    早期是「一个全局队列 + 每个连接各自 get_nowait()」：只要同时存在两个连接
    （多开标签页、EventSource 重连残留、右侧预览面板另开一个），日志就会被
    连接之间互相抢走，每个页面只能看到其中一部分，表现为轮次/进度跳号。
    """
    with _sub_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(item)
        except queue.Full:   # 消费太慢的连接丢掉旧事件，不阻塞写入线程
            pass


def emit(level: str, message: str) -> None:
    push({"type": "log", "level": level, "message": message, "ts": time.time()})


def emit_state(state: str) -> None:
    """推送运行状态变更，前端据此切换按钮互斥与动画。"""
    run_state["state"] = state
    # 终态同步落 running 标志：否则 status 快照可能出现 running=true + state=stopped
    # 的竞态组合，前端只看 running 会把按钮带回"写入中"。
    if state in ("done", "stopped", "error", "idle"):
        run_state["running"] = False
    push({"type": "state", "state": state, "ts": time.time()})


def do_run() -> None:
    global worker
    try:
        result = Writer(cfg, emit, stop_event, progress=on_progress).run()
        run_state["result"] = result
        # 终态补推一条进度，保证短任务（<1s，writer 未触发周期上报）进度条也能收敛
        done_dev = result.get("devices", 0)
        done_pts = result.get("points", 0)
        if result.get("streaming"):
            # 持续写入没有总量：进度条只作"进行中"指示，终态展示轮次与累计点数
            push({"type": "progress", "phase": "done", "streaming": True,
                           "pct": 0 if result.get("stopped") else 100,
                           "rounds": result.get("rounds", 0), "points": done_pts,
                           "devices": done_dev, "elapsed": result.get("elapsed", 0),
                           "rate": round(done_pts / max(result.get("elapsed", 1), 0.001)),
                           "stopped": result.get("stopped"), "ts": time.time()})
        else:
            # 终态补推一条进度，保证短任务（<1s，writer 未触发周期上报）进度条也能收敛
            final_pct = 0 if result.get("stopped") else 100
            total_dev = result.get("total_devices") or done_dev
            total_pts = result.get("total_points") or done_pts
            push({"type": "progress", "phase": "done", "pct": final_pct,
                           "devices": done_dev, "total_devices": total_dev,
                           "remaining_devices": 0 if not result.get("stopped") else max(total_dev - done_dev, 0),
                           "points": done_pts, "total_points": total_pts,
                           "remaining_points": 0 if not result.get("stopped") else max(total_pts - done_pts, 0),
                           "rate": round(done_pts / max(result.get("elapsed", 1), 0.001)),
                           "elapsed": result.get("elapsed", 0), "ts": time.time()})
        if result.get("stopped"):
            if result.get("streaming"):
                emit("WARN", f"持续写入已停止 · 共 {result.get('rounds', 0):,} 轮 / 累计 {done_pts:,} 点 "
                             f"· 耗时 {result['elapsed']}s")
            else:
                emit("WARN", f"写入已停止 · 已写入 {done_pts:,} 点")
            emit_state("stopped")
        else:
            if result.get("streaming"):
                emit("SUCCESS", f"持续写入结束 · 共 {result.get('rounds', 0):,} 轮 / 累计 {done_pts:,} 点 "
                                f"· 耗时 {result['elapsed']}s")
            else:
                emit("SUCCESS", f"写入完成 · {done_pts:,} 点 / {done_dev:,} 设备 "
                                f"· 耗时 {result['elapsed']}s" + ("（dry-run）" if result.get("dry_run") else ""))
            emit_state("done")
    except Exception as exc:  # 真实写入失败要可见
        emit("ERROR", f"写入失败：{type(exc).__name__}: {exc}")
        run_state["result"] = {"error": str(exc)}
        emit_state("error")
    finally:
        run_state["running"] = False
        worker = None


def on_progress(info: dict) -> None:
    phase = info.get("phase", "running")
    run_state["phase"] = phase
    if phase in ("building", "running", "streaming"):
        if phase in ("running", "streaming"):
            run_state["progress"] = info
        push({"type": "progress", **info, "ts": time.time()})


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/config")
def get_config():
    return cfg.to_dict()


@app.post("/api/config")
async def post_config(request: Request):
    global cfg
    data = await request.json()
    cfg = config_from_dict(data)
    save_config(CONFIG_PATH, cfg)
    return {"ok": True}


@app.post("/api/test")
async def test_connection(request: Request):
    """测试连接。

    - 不传 body：测当前选中配置（dry-run 下按模拟通过返回，保持旧行为）
    - 传 host/port/user/password：测指定配置（列表里逐条检测用）
    - quiet=true：不写日志，供前端指示灯心跳静默检测
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}

    db = cfg.active_db()
    quiet = bool(body.get("quiet"))
    use_body = bool(str(body.get("host") or "").strip())
    host = str(body.get("host")).strip() if use_body else db.host
    try:
        port = int(body.get("port") or db.port)
    except (TypeError, ValueError):
        port = db.port
    user = str(body.get("user") or db.user) if use_body else db.user
    password = (str(body.get("password") or "") if use_body else db.password)
    label = (str(body.get("name") or "") if use_body else db.name) or f"{host}:{port}"
    endpoint = f"{host}:{port}"
    # 单次探测的连接超时：默认 3s，避免不可达主机的默认超时（实测 75s）拖住指示灯/心跳
    try:
        timeout_ms = max(500, min(int(body.get("timeout_ms") or 3000), 30000))
    except (TypeError, ValueError):
        timeout_ms = 3000

    if not use_body and cfg.write.dry_run:
        if not quiet:
            emit("SUCCESS", f"连接测试通过（dry-run 模拟 · {endpoint}）")
        return {"ok": True, "mock": True, "latency_ms": 0, "endpoint": endpoint}

    # 客户端库导入放在计时之外，避免首次导入（约 1.5s）污染延迟读数
    try:
        from iotdb.Session import Session
    except Exception as exc:  # 依赖缺失时明确报错，而不是计时异常
        return JSONResponse({"ok": False, "error": f"客户端库不可用：{exc}",
                             "latency_ms": None, "endpoint": endpoint})

    started = time.perf_counter()

    def _ping() -> None:
        s = Session(host, port, user, password, connection_timeout_in_ms=timeout_ms)
        s.open(False)
        s.close()

    try:
        await asyncio.wait_for(asyncio.to_thread(_ping), timeout=timeout_ms / 1000 + 1.5)
        latency = round((time.perf_counter() - started) * 1000)
        if not quiet:
            emit("SUCCESS", f"连接测试通过 · {label} · {endpoint} · {latency}ms")
        return {"ok": True, "latency_ms": latency, "endpoint": endpoint}
    except asyncio.TimeoutError:
        latency = round((time.perf_counter() - started) * 1000)
        msg = f"连接超时（>{timeout_ms}ms）"
        if not quiet:
            emit("ERROR", f"连接失败 · {label} · {endpoint}：{msg}")
        return JSONResponse({"ok": False, "error": msg, "latency_ms": latency,
                             "endpoint": endpoint})
    except Exception as exc:
        latency = round((time.perf_counter() - started) * 1000)
        if not quiet:
            emit("ERROR", f"连接失败 · {label} · {endpoint}：{exc}")
        return JSONResponse({"ok": False, "error": str(exc), "latency_ms": latency,
                             "endpoint": endpoint})


@app.get("/api/scale")
def get_scale():
    return scale(cfg)


@app.post("/api/run")
def start_run():
    global worker
    if run_state["running"]:
        return JSONResponse({"ok": False, "error": "任务已在运行"}, status_code=409)
    stop_event.clear()
    run_state.update({"running": True, "phase": "starting", "state": "running", "result": None, "progress": None})
    worker = threading.Thread(target=do_run, daemon=True)
    worker.start()
    emit_state("running")
    emit("INFO", "任务已启动")
    return {"ok": True}


@app.post("/api/stop")
def stop_run():
    if not run_state["running"]:
        return {"ok": False, "error": "当前无运行任务"}
    stop_event.set()
    emit("WARN", "已请求停止，将在当前批次完成后中断")
    return {"ok": True}


@app.get("/api/status")
def get_status():
    return dict(run_state)


@app.get("/api/logs/stream")
async def log_stream(request: Request):
    """SSE 日志流：先推送当前状态，再把队列中的日志逐条推送。"""

    async def gen() -> AsyncGenerator[str, None]:
        q: "queue.Queue[dict]" = queue.Queue(maxsize=8000)
        with _sub_lock:
            _subscribers.add(q)
        try:
            yield f"data: {json.dumps({'type':'status','data':dict(run_state)})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                # 一次把队列里的都取出来，成批下发：突发多行日志也不会被拖慢
                items = []
                try:
                    while len(items) < 200:
                        items.append(q.get_nowait())
                except queue.Empty:
                    pass
                if items:
                    yield "".join(f"data: {json.dumps(i)}\n\n" for i in items)
                else:
                    await asyncio.sleep(0.1)
        finally:
            with _sub_lock:
                _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/health")
def health():
    return {"ok": True, "version": "1.0"}
