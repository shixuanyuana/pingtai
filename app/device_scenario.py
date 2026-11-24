"""Create in the previously created court; delete by trusted creation record."""
import json
import uuid
from contextlib import contextmanager
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field


class CreateDevice(BaseModel):
    environment_id: int
    venue_version: str
    device_name: str = Field(min_length=1, max_length=255)
    device_sn: str = Field(min_length=1, max_length=255)
    is_aliyun: bool = False
    allow_write: bool = False


class DeleteDevice(BaseModel):
    environment_id: int
    run_id: str
    allow_write: bool = False


def register(app, connection_factory, decode, token_store, resolve_value, resolve_text, ensure_token=None, venue_target=None):
    @contextmanager
    def db():
        conn = connection_factory()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def table(conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS device_targets (
            run_id TEXT PRIMARY KEY, environment_id INTEGER NOT NULL,
            base_url TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")

    def environment(environment_id):
        with db() as conn:
            row = conn.execute("SELECT * FROM environments WHERE id=?", (environment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "环境不存在")
        env = decode(row)
        if not env["base_url"].startswith(("https://", "http://")):
            raise HTTPException(422, "请配置当前环境地址")
        return env

    async def headers_for(env):
        token = (await ensure_token(env["id"]))["token"] if ensure_token else token_store.get(env["id"], {}).get("token", "")
        if not token:
            raise HTTPException(401, "请先登录当前环境")
        try:
            headers = resolve_value(env["headers"], {**env.get("variables", {}), "token": token})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # Let httpx choose form or JSON content type from the actual body.
        headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "content-type", "content-length")}
        return {**headers, "Authorization": "Bearer " + token}

    def target_for(environment_id):
        if venue_target:
            return venue_target(environment_id)
        with db() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='venue_targets'").fetchone():
                return {"status": "empty"}
            row = conn.execute("SELECT * FROM venue_targets WHERE environment_id=?", (environment_id,)).fetchone()
        return dict(row) if row else {"status": "empty"}

    @app.get("/api/scenarios/create-device")
    def get_target(environment_id: int):
        env = environment(environment_id)
        target = target_for(environment_id)
        ready = target.get("status") == "ready" and target.get("base_url") == env["base_url"]
        return {"ready": ready, "environment_name": env["name"], "environment_id": environment_id,
                "base_url": env["base_url"], "target": target}

    @app.post("/api/scenarios/create-device/run")
    async def create_device(payload: CreateDevice):
        if not payload.allow_write:
            raise HTTPException(403, "请确认创建设备并关联场地")
        if not payload.device_name.strip() or not payload.device_sn.strip():
            raise HTTPException(422, "设备名称和编号不能为空")
        context = get_target(payload.environment_id)
        target = context["target"]
        if not context["ready"] or target.get("version") != payload.venue_version:
            raise HTTPException(409, "请先创建球馆和场地，或重新打开设备场景")
        if any(type(target.get(k)) is not int or target[k] <= 0 for k in ("shop_id", "store_id")):
            raise HTTPException(409, "球馆和场地 ID 无效")
        env = environment(payload.environment_id)
        headers = await headers_for(env)
        form = {"device_name": payload.device_name.strip(), "device_sn": payload.device_sn.strip(),
                "shop_id": str(target["shop_id"]), "store_id": str(target["store_id"]),
                "is_aliyun": "true" if payload.is_aliyun else "false"}
        run_id = uuid.uuid4().hex
        report = {"run_id": run_id, "environment_id": payload.environment_id, "base_url": env["base_url"],
                  "shop_id": target["shop_id"], "store_id": target["store_id"],
                  "shop_name": target.get("shop_name", ""), "store_name": target.get("store_name", ""),
                  "device_name": form["device_name"], "device_sn": form["device_sn"],
                  "device_id": None, "steps": []}
        # Persist intent before the network request so interruptions remain visible.
        with db() as conn:
            table(conn)
            conn.execute("INSERT INTO device_targets(run_id,environment_id,base_url,status,report) VALUES (?,?,?,?,?)",
                         (run_id, env["id"], env["base_url"], "creating", json.dumps(report, ensure_ascii=False)))
        step: dict[str, Any] = {"name": "创建设备并关联场地", "method": "POST", "path": "/develop/devices",
                                "form": form, "status": "failed"}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.post(env["base_url"].rstrip("/") + "/develop/devices", headers=headers, data=form)
            content = response.json()
            step["http_status"] = response.status_code
            step["business_code"] = content.get("code") if isinstance(content, dict) else None
            data = content.get("data") if isinstance(content, dict) else None
            device = data.get("device") if isinstance(data, dict) else None
            device_id = device.get("id") if isinstance(device, dict) else None
            if 200 <= response.status_code < 300 and step["business_code"] == 200 and type(device_id) is int and device_id > 0:
                report["device_id"] = device_id
                step["status"] = "passed"
            else:
                step["error"] = "HTTP、业务 code 或 data.device.id 未通过断言，请检查平台记录"
        except (httpx.HTTPError, ValueError):
            step["error"] = "创建响应未确认成功；请先核查平台记录，再决定是否重试"
        report["steps"].append(step)
        report["status"] = "ready" if report["device_id"] is not None else "check_required"
        with db() as conn:
            conn.execute("UPDATE device_targets SET status=?,report=? WHERE run_id=?",
                         (report["status"], json.dumps(report, ensure_ascii=False), run_id))
        return report

    @app.get("/api/scenarios/delete-device")
    def list_targets(environment_id: int):
        env = environment(environment_id)
        with db() as conn:
            table(conn)
            rows = conn.execute("SELECT * FROM device_targets WHERE environment_id=? ORDER BY created_at DESC, rowid DESC LIMIT 100", (environment_id,)).fetchall()
        return [{**json.loads(row["report"]), "status": row["status"], "created_at": row["created_at"],
                 "can_delete": row["status"] == "ready" and row["base_url"] == env["base_url"]} for row in rows]

    @app.post("/api/scenarios/delete-device/run")
    async def delete_device(payload: DeleteDevice):
        if not payload.allow_write:
            raise HTTPException(403, "请确认删除所选记录创建的设备")
        env = environment(payload.environment_id)
        with db() as conn:
            table(conn)
            row = conn.execute("SELECT * FROM device_targets WHERE run_id=?", (payload.run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "设备创建记录不存在")
        if row["environment_id"] != env["id"] or row["base_url"] != env["base_url"] or row["status"] != "ready":
            raise HTTPException(409, "环境已变化或设备不处于待删除状态")
        report = json.loads(row["report"])
        device_id = report.get("device_id")
        if type(device_id) is not int or device_id <= 0:
            raise HTTPException(409, "创建记录缺少有效设备 ID")
        headers = await headers_for(env)
        with db() as conn:
            changed = conn.execute("UPDATE device_targets SET status='deleting' WHERE run_id=? AND status='ready'", (payload.run_id,)).rowcount
        if not changed:
            raise HTTPException(409, "设备正在删除或已删除，请刷新场景")
        step = {"name": "删除本次创建的设备", "method": "DELETE", "path": "/develop/devices",
                "body": {"ids": [device_id]}, "status": "failed"}
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await client.request("DELETE", env["base_url"].rstrip("/") + "/develop/devices", headers=headers, json={"ids": [device_id]})
            content = response.json()
            step["http_status"] = response.status_code
            step["business_code"] = content.get("code") if isinstance(content, dict) else None
            if 200 <= response.status_code < 300 and step["business_code"] == 200:
                step["status"] = "passed"
            else:
                step["error"] = "删除成功断言未通过，请核查平台记录"
        except (httpx.HTTPError, ValueError):
            step["error"] = "删除响应未确认成功，请核查平台记录"
        report["steps"].append(step)
        report["status"] = "deleted" if step["status"] == "passed" else "check_required"
        with db() as conn:
            conn.execute("UPDATE device_targets SET status=?,report=? WHERE run_id=?", (report["status"], json.dumps(report, ensure_ascii=False), payload.run_id))
        return report
