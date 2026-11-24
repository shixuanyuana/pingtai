from pathlib import Path
import hashlib, json, re, sqlite3, time, uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "platform.db"
app = FastAPI(title="接口自动化平台", version="0.2.0")
TOKEN_STORE: dict[int, dict[str, str]] = {}
TOKEN_TTL_SECONDS = 12 * 60 * 60
SCENARIO_TTL_SECONDS = 30 * 60
SCENARIO_SESSIONS: dict[str, dict[str, Any]] = {}

MODULE = "直播管理"
CATEGORIES = {"reservation": "直播预约", "match": "直播列表", "order": "直播订单"}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS environments (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, base_url TEXT NOT NULL,
          headers TEXT NOT NULL DEFAULT '{}', variables TEXT NOT NULL DEFAULT '{}', kind TEXT NOT NULL DEFAULT 'test');
        CREATE TABLE IF NOT EXISTS cases (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          module TEXT NOT NULL DEFAULT '直播管理', category TEXT NOT NULL DEFAULT '直播预约',
          method TEXT NOT NULL, path TEXT NOT NULL, headers TEXT NOT NULL DEFAULT '{}',
          params TEXT NOT NULL DEFAULT '{}', body TEXT NOT NULL DEFAULT '', assertions TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, status TEXT NOT NULL,
          duration_ms INTEGER NOT NULL, response_status INTEGER, response_body TEXT, error TEXT,
          request_url TEXT NOT NULL DEFAULT '', request_headers TEXT NOT NULL DEFAULT '{}',
          request_params TEXT NOT NULL DEFAULT '{}', request_body TEXT NOT NULL DEFAULT '',
          assertion_results TEXT NOT NULL DEFAULT '[]',
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS auth_profiles (
          environment_id INTEGER PRIMARY KEY, username TEXT NOT NULL, password_hash TEXT NOT NULL,
          token TEXT NOT NULL DEFAULT '', token_expires_at INTEGER NOT NULL DEFAULT 0,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        """)
        env_cols = {row[1] for row in conn.execute("PRAGMA table_info(environments)")}
        if "variables" not in env_cols:
            conn.execute("ALTER TABLE environments ADD COLUMN variables TEXT NOT NULL DEFAULT '{}'")
        if "kind" not in env_cols:
            conn.execute("ALTER TABLE environments ADD COLUMN kind TEXT NOT NULL DEFAULT 'test'")
            conn.execute("UPDATE environments SET kind='prod' WHERE base_url='https://z.vdirector.cn'")
        case_cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
        if "module" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN module TEXT NOT NULL DEFAULT '直播管理'")
        if "category" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN category TEXT NOT NULL DEFAULT '直播预约'")
        run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        if "request_url" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN request_url TEXT NOT NULL DEFAULT ''")
        if "request_headers" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN request_headers TEXT NOT NULL DEFAULT '{}'")
        if "request_params" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN request_params TEXT NOT NULL DEFAULT '{}'")
        if "request_body" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN request_body TEXT NOT NULL DEFAULT ''")
        if "assertion_results" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN assertion_results TEXT NOT NULL DEFAULT '[]'")
        conn.execute("UPDATE cases SET category='直播列表' WHERE name LIKE '直播列表-%'")
        conn.execute("UPDATE cases SET category='直播订单' WHERE name LIKE '直播订单-%'")
        if conn.execute("SELECT COUNT(*) FROM environments").fetchone()[0] == 0:
            conn.execute("INSERT INTO environments(name,base_url,headers,variables,kind) VALUES(?,?,?,?,?)",
                         ("真实环境", "https://z.vdirector.cn", "{}", "{}", "prod"))
        if conn.execute("SELECT COUNT(*) FROM environments WHERE kind='test'").fetchone()[0] == 0:
            conn.execute("INSERT INTO environments(name,base_url,headers,variables,kind) VALUES(?,?,?,?,?)",
                         ("测试环境（待配置）", "", "{}", "{}", "test"))
    # 首次启动即可在页面看到三个目录的全部内置接口；后续启动只会更新，不会重复插入。
    for group in ("reservation", "match", "order"):
        sync_templates(group)


class EnvironmentIn(BaseModel):
    name: str
    base_url: str
    kind: str = "test"
    headers: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)


class TokenLoginIn(BaseModel):
    environment_id: int
    username: str
    password: str


class CaseIn(BaseModel):
    name: str
    module: str = MODULE
    category: str = CATEGORIES["reservation"]
    method: str = "GET"
    path: str
    headers: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    body: str = ""
    assertions: list[dict[str, Any]] = Field(default_factory=list)


class VenueScenarioIn(BaseModel):
    environment_id: int
    allow_write: bool = False
    name: str = Field(min_length=1, max_length=255)
    stores_name: str = Field(min_length=1, max_length=255)
    status: int = 1
    stores_status: int = 1
    stores_type: str = Field(default="11", min_length=1)
    field_type: str = "1"
    city_id: list[int] = Field(default_factory=list)
    address: str = ""
    phone: str = ""


@app.post("/api/scenarios/create-venue/run")
async def create_venue_scenario(payload: VenueScenarioIn):
    if not payload.allow_write:
        raise HTTPException(403, "请确认在当前环境添加球馆和场地")
    if not payload.name.strip() or not payload.stores_name.strip() or not payload.stores_type.strip():
        raise HTTPException(422, "球馆名称、场地名称和场地类型不能为空")
    with db() as conn:
        row = conn.execute("SELECT * FROM environments WHERE id=?", (payload.environment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "环境不存在")
    env = decode(row)
    if not env["base_url"].startswith(("https://", "http://")):
        raise HTTPException(422, "请配置当前环境地址")
    token = (await ensure_token(payload.environment_id))["token"]
    headers = {**env.get("headers", {}), "Authorization": f"Bearer {token}"}
    base = env["base_url"].rstrip("/")
    steps = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        shop = await scenario_request(client, "POST", base + "/develop/shops", headers, body={
            "name": payload.name.strip(), "status": str(payload.status), "city_id": payload.city_id,
            "address": payload.address, "phone": payload.phone, "banner_url": "",
            "video_info": {"src": "", "poster": ""}, "groups_ids": [],
        })
        data = shop.pop("json", None)
        shop_id = positive_id(read_json_path(data, "data.id"))
        ok = shop["status"] == "passed" and isinstance(data, dict) and data.get("code") == 200 and shop_id is not None
        shop.update(name="添加球馆", status="passed" if ok else "failed")
        steps.append(shop)
        if not ok:
            return {"status": "failed", "steps": steps, "message": "球馆创建失败或未返回有效ID，场地步骤未执行"}
        target_version = uuid.uuid4().hex
        with db() as conn:
            venue_target_table(conn)
            conn.execute("INSERT OR REPLACE INTO venue_targets VALUES(?,?,?,?,?,?,?,?)", (
                payload.environment_id, target_version, env["base_url"], shop_id, None,
                payload.name, payload.stores_name, "incomplete"))
        store = await scenario_request(client, "POST", base + "/develop/stores", headers, body={
            "shop_id": shop_id, "stores_name": payload.stores_name.strip(), "stores_type": payload.stores_type,
            "field_type": payload.field_type, "stores_status": str(payload.stores_status),
            "mini_scan_visible": "no", "public_live": "no", "public_recording": "no", "time_slots": [],
            "scan_service_prices": {"recordings": {"uhd": 0, "data_analysis_enable": 0},
                "reservation_orders": {"auto_device_config": 0, "auto_server_config": 0,
                    "is_resolution": 0, "is_multiple": 0, "is_data_analysis": 0, "obs_record_enable": 0}},
            "live_config": {"stream_type": "h1_server", "streaming_protocol": "rtmp", "stream_tem": "aliyun", "al_delay_time": 0},
        })
        data = store.pop("json", None)
        store_id = positive_id(read_json_path(data, "data.id"))
        ok = (store["status"] == "passed" and isinstance(data, dict) and data.get("code") == 200
              and store_id is not None and positive_id(read_json_path(data, "data.shop_id")) == shop_id)
        store.update(name="添加子场地", status="passed" if ok else "failed", association=f"shop_id ← 球馆响应.data.id = {shop_id}")
        steps.append(store)
        with db() as conn:
            conn.execute("UPDATE venue_targets SET store_id=?,status=? WHERE environment_id=? AND version=?",
                         (store_id, "ready" if ok else "incomplete", payload.environment_id, target_version))
    return {"status": "passed" if ok else "failed", "shop_id": shop_id, "store_id": store_id, "steps": steps,
            "message": "球馆和子场地创建完成" if ok else f"球馆 {shop_id} 已创建，场地未确认成功；请核查响应，避免重复创建球馆"}


def venue_target_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS venue_targets (
        environment_id INTEGER PRIMARY KEY, version TEXT NOT NULL, base_url TEXT NOT NULL,
        shop_id INTEGER, store_id INTEGER, shop_name TEXT NOT NULL, store_name TEXT NOT NULL, status TEXT NOT NULL)""")


@app.get("/api/scenarios/delete-venue")
def get_venue_target(environment_id: int):
    with db() as conn:
        venue_target_table(conn)
        row = conn.execute("SELECT * FROM venue_targets WHERE environment_id=?", (environment_id,)).fetchone()
    return dict(row) if row else {"status": "empty"}


class DeleteVenueIn(BaseModel):
    environment_id: int
    version: str


@app.post("/api/scenarios/delete-venue/run")
async def delete_venue_scenario(payload: DeleteVenueIn):
    with db() as conn:
        venue_target_table(conn)
        row = conn.execute("SELECT * FROM venue_targets WHERE environment_id=?", (payload.environment_id,)).fetchone()
        env_row = conn.execute("SELECT * FROM environments WHERE id=?", (payload.environment_id,)).fetchone()
    if not row or not env_row:
        raise HTTPException(409, "请先运行添加球馆和场地场景")
    target, env = dict(row), decode(env_row)
    if (target["version"] != payload.version or target["status"] != "ready"
            or not target["store_id"] or not target["shop_id"] or target["base_url"] != env["base_url"]):
        raise HTTPException(409, "关联数据不完整、已执行或环境发生变化，请重新查看场景")
    token = (await ensure_token(payload.environment_id))["token"]
    with db() as conn:
        changed = conn.execute("UPDATE venue_targets SET status='running' WHERE environment_id=? AND version=? AND status='ready'",
                               (payload.environment_id, payload.version)).rowcount
    if not changed:
        raise HTTPException(409, "该场景正在执行或关联数据已变化")
    steps = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for name, path, target_id in [("删除场地", "/develop/stores", target["store_id"]),
                                      ("删除球馆", "/develop/shops", target["shop_id"])]:
            result = await scenario_request(client, "DELETE", env["base_url"].rstrip("/") + path,
                {**env.get("headers", {}), "Authorization": f"Bearer {token}"}, body={"ids": [target_id]})
            content = result.pop("json", None)
            ok = result["status"] == "passed" and isinstance(content, dict) and content.get("code") == 200
            result.update(name=name, path=path, ids=[target_id], status="passed" if ok else "failed")
            steps.append(result)
            if not ok:
                break
    passed = len(steps) == 2 and all(s["status"] == "passed" for s in steps)
    with db() as conn:
        conn.execute("UPDATE venue_targets SET status=? WHERE environment_id=? AND version=?",
                     ("deleted" if passed else "check_required", payload.environment_id, payload.version))
    return {"status": "passed" if passed else "failed", "steps": steps}


class ScenarioRunIn(BaseModel):
    environment_id: int
    variables: dict[str, Any] = Field(default_factory=dict)
    allow_write: bool = False


class ScenarioContinueIn(BaseModel):
    session_id: str
    variables: dict[str, Any] = Field(default_factory=dict)
    allow_write: bool = False


def decode(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("headers", "params", "assertions", "variables"):
        if key in item:
            item[key] = json.loads(item[key])
    return item


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/environments")
def list_environments() -> list[dict[str, Any]]:
    with db() as conn:
        return [decode(r) for r in conn.execute("SELECT * FROM environments ORDER BY id DESC")]


@app.post("/api/environments")
def create_environment(payload: EnvironmentIn) -> dict[str, Any]:
    if payload.kind not in ("test", "prod"):
        raise HTTPException(422, "环境类型只能是 test 或 prod")
    with db() as conn:
        cur = conn.execute("INSERT INTO environments(name,base_url,headers,variables,kind) VALUES(?,?,?,?,?)", (
            payload.name, payload.base_url.rstrip("/"), json.dumps(payload.headers, ensure_ascii=False),
            json.dumps(payload.variables, ensure_ascii=False), payload.kind))
        return decode(conn.execute("SELECT * FROM environments WHERE id=?", (cur.lastrowid,)).fetchone())


@app.put("/api/environments/{environment_id}")
def update_environment(environment_id: int, payload: EnvironmentIn) -> dict[str, Any]:
    if payload.kind not in ("test", "prod"):
        raise HTTPException(422, "环境类型只能是 test 或 prod")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM environments WHERE id=?", (environment_id,)).fetchone():
            raise HTTPException(404, "环境不存在")
        conn.execute("UPDATE environments SET name=?,base_url=?,headers=?,variables=?,kind=? WHERE id=?", (
            payload.name, payload.base_url.rstrip("/"), json.dumps(payload.headers, ensure_ascii=False),
            json.dumps(payload.variables, ensure_ascii=False), payload.kind, environment_id))
        return decode(conn.execute("SELECT * FROM environments WHERE id=?", (environment_id,)).fetchone())


def mask_token(token: str) -> str:
    return "******" if len(token) <= 12 else f"{token[:6]}…{token[-6:]}"


async def login_with_hash(environment_id: int, username: str, password_hash: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM environments WHERE id=?", (environment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "环境不存在")
    env = decode(row)
    if not env["base_url"].startswith(("http://", "https://")):
        raise HTTPException(422, "请先配置当前环境的 Base URL")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.post(
                env["base_url"].rstrip("/") + "/develop/auth/login",
                json={"username": username, "password": password_hash},
            )
        response.raise_for_status()
        content = response.json()
        data = content.get("data", content) if isinstance(content, dict) else {}
        token = data.get("token")
        if not token:
            raise HTTPException(401, content.get("message") or content.get("msg") or "登录响应中没有 Token")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"获取 Token 失败：{exc}") from exc
    expires_at = int(time.time()) + TOKEN_TTL_SECONDS
    with db() as conn:
        conn.execute(
            """INSERT INTO auth_profiles(environment_id,username,password_hash,token,token_expires_at,updated_at)
               VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(environment_id) DO UPDATE SET username=excluded.username,
               password_hash=excluded.password_hash,token=excluded.token,
               token_expires_at=excluded.token_expires_at,updated_at=CURRENT_TIMESTAMP""",
            (environment_id, username, password_hash, token, expires_at),
        )
    return {"ok": True, "token": token, "masked_token": mask_token(token),
            "username": username, "expires_at": expires_at, "auto_refresh": True}


@app.post("/api/auth/token")
async def acquire_token(payload: TokenLoginIn) -> dict[str, Any]:
    password_hash = hashlib.sha256(payload.password.encode()).hexdigest()
    return await login_with_hash(payload.environment_id, payload.username.strip(), password_hash)


async def ensure_token(environment_id: int, force: bool = False) -> dict[str, Any]:
    with db() as conn:
        profile = conn.execute("SELECT * FROM auth_profiles WHERE environment_id=?", (environment_id,)).fetchone()
    if not profile:
        raise HTTPException(401, "当前环境尚未保存登录账号，请先在账号与登录页面登录")
    item = dict(profile)
    if not force and item["token"] and item["token_expires_at"] > int(time.time()) + 60:
        return {"ok": True, "token": item["token"], "masked_token": mask_token(item["token"]),
                "username": item["username"], "expires_at": item["token_expires_at"], "auto_refresh": True}
    return await login_with_hash(environment_id, item["username"], item["password_hash"])


@app.post("/api/auth/token/ensure")
async def ensure_saved_token(environment_id: int, force: bool = False) -> dict[str, Any]:
    return await ensure_token(environment_id, force)


@app.get("/api/auth/token/status")
def token_status(environment_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM auth_profiles WHERE environment_id=?", (environment_id,)).fetchone()
    if not row:
        return {"available": False, "masked_token": "", "username": "", "expires_at": 0, "auto_refresh": False}
    item = dict(row)
    available = bool(item["token"] and item["token_expires_at"] > int(time.time()))
    return {"available": available, "masked_token": mask_token(item["token"]) if available else "",
            "username": item["username"], "expires_at": item["token_expires_at"], "auto_refresh": True}


@app.delete("/api/auth/profile")
def delete_auth_profile(environment_id: int) -> dict[str, bool]:
    with db() as conn:
        conn.execute("DELETE FROM auth_profiles WHERE environment_id=?", (environment_id,))
    return {"ok": True}


@app.get("/api/cases")
def list_cases(module: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
    sql, values = "SELECT * FROM cases WHERE 1=1", []
    if module:
        sql += " AND module=?"; values.append(module)
    if category:
        sql += " AND category=?"; values.append(category)
    with db() as conn:
        return [decode(r) for r in conn.execute(sql + " ORDER BY id DESC", values)]


def t(name: str, method: str, path: str, params=None, body=None, contains=None) -> dict[str, Any]:
    assertions = [{"type": "status", "expected": 200}]
    if contains:
        assertions.append({"type": "contains", "expected": contains})
    return {"name": name, "method": method, "path": path, "params": params or {},
            "body": json.dumps(body, ensure_ascii=False) if body else "", "assertions": assertions}


RESERVATION_TEMPLATES = [
    t("获取场地选项", "GET", "/develop/stores-options"),
    t("预约价格预览", "GET", "/develop/reservation-orders/price-preview", {
        "start_time": "{{start_time}}", "end_time": "{{end_time}}", "auto_device_config": 1,
        "auto_server_config": 1, "is_resolution": 0, "is_multiple": 0, "is_data_analysis": 0,
        "is_director": 1, "payment_type": "voucher", "voucher_code": "{{voucher_code}}", "obs_record_enable": 0}),
    t("直接创建预约（无需支付）", "POST", "/develop/reservations", body={
        "store_id": "{{store_id}}", "start_time": "{{start_time}}", "end_time": "{{end_time}}",
        "is_clound": 1, "is_overseas": 0, "auto_device_config": 1, "auto_server_config": 1,
        "is_resolution": 0, "is_multiple": 0, "is_data_analysis": 0, "auto_clip_enable": 0,
        "is_director": 1, "obs_record_enable": 0}),
    t("创建预约订单", "POST", "/develop/reservation-orders", body={
        "store_id": "{{store_id}}", "start_time": "{{start_time}}", "end_time": "{{end_time}}",
        "payment_type": "voucher", "voucher_code": "{{voucher_code}}", "is_clound": 1,
        "is_overseas": False, "auto_device_config": 1, "auto_server_config": 1, "is_resolution": 0,
        "is_multiple": 0, "is_data_analysis": 0, "auto_clip_enable": 0, "is_director": 1, "obs_record_enable": 0}),
    t("查询预约列表", "GET", "/develop/reservations", {"page_num": 1, "page_size": 20}),
    t("查询预约详情", "GET", "/develop/reservations/{{reservation_id}}"),
    t("按授权码查询预约", "GET", "/develop/reservations/info", {"auth_code": "{{auth_code}}"}),
]

MATCH_TEMPLATES = [
    t("查询直播列表", "GET", "/develop/matches", {"current": 1, "pageSize": 20}, contains="data"),
    t("按场地查询直播", "GET", "/develop/matches", {"current": 1, "pageSize": 20, "store_id": "{{store_id}}"}),
    t("查询直播详情", "GET", "/develop/matches/{{match_id}}", contains="matches_name"),
    t("获取场地直播选项", "GET", "/develop/matches-options", {"stores_id": "{{store_id}}"}),
    t("校验服务器资源", "POST", "/develop/matches/check-server", body={
        "device_id": "{{device_id}}", "server_sn": "{{server_sn}}", "start_time": "{{start_time}}", "end_time": "{{end_time}}"}),
    t("创建直播", "POST", "/develop/matches", body={
        "matches_name": "{{matches_name}}", "matches_type": "1", "server_sn": "{{server_sn}}",
        "device_id": "{{device_id}}", "stream_type": "h1_server", "stream_tem": "aliyun",
        "start_time": "{{start_time}}", "end_time": "{{end_time}}", "streaming_protocol": "rtmp",
        "is_overseas": False, "is_resolution": 0, "is_data_analysis": 0, "auto_clip_enable": 0,
        "is_director": 1, "obs_record_enable": 0, "team_a_name": "{{team_a_name}}",
        "team_b_name": "{{team_b_name}}", "osd_template_type": "template4", "osd_enable": False,
        "osd_timer_enable": False, "osd_timer_reverse": 0, "osd_time": "00:00", "alpha": .85,
        "jitter_buffer_sec": 0, "stream_config": None}),
]

ORDER_TEMPLATES = [
    t("查询订单列表", "GET", "/develop/recording-orders", {"page_num": 1, "page_size": 20}, contains="data"),
    t("按订单号筛选", "GET", "/develop/recording-orders", {"page_num": 1, "page_size": 20, "order_number": "{{order_number}}"}),
    t("按直播名称筛选", "GET", "/develop/recording-orders", {"page_num": 1, "page_size": 20, "matches_name": "{{matches_name}}"}),
    t("按状态筛选", "GET", "/develop/recording-orders", {"page_num": 1, "page_size": 20, "status": "{{status}}"}),
    t("查询订单详情", "GET", "/develop/recording-orders/{{order_no}}"),
    t("查询待支付订单", "GET", "/develop/recording-orders/unfinished", {"user_id": "{{user_id}}"}),
]

GROUPS = {"reservation": (CATEGORIES["reservation"], RESERVATION_TEMPLATES),
          "match": (CATEGORIES["match"], MATCH_TEMPLATES), "order": (CATEGORIES["order"], ORDER_TEMPLATES)}


def sync_templates(group: str) -> list[dict[str, Any]]:
    category, templates = GROUPS[group]
    result = []
    with db() as conn:
        for item in templates:
            old = conn.execute("SELECT id FROM cases WHERE module=? AND category=? AND name=?",
                               (MODULE, category, item["name"])).fetchone()
            values = (item["method"], item["path"], "{}", json.dumps(item["params"], ensure_ascii=False),
                      item["body"], json.dumps(item["assertions"], ensure_ascii=False))
            if old:
                conn.execute("UPDATE cases SET method=?,path=?,headers=?,params=?,body=?,assertions=? WHERE id=?",
                             values + (old["id"],)); case_id = old["id"]
            else:
                cur = conn.execute("INSERT INTO cases(name,module,category,method,path,headers,params,body,assertions) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (item["name"], MODULE, category) + values); case_id = cur.lastrowid
            result.append(decode(conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()))
    return result


@app.post("/api/templates/{group}")
def import_templates(group: str) -> list[dict[str, Any]]:
    if group not in GROUPS:
        raise HTTPException(404, "模板分类不存在")
    return sync_templates(group)


def import_reservation_templates(): return sync_templates("reservation")
def import_match_templates(): return sync_templates("match")
def import_order_templates(): return sync_templates("order")


@app.post("/api/cases")
def create_case(payload: CaseIn) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("INSERT INTO cases(name,module,category,method,path,headers,params,body,assertions) VALUES(?,?,?,?,?,?,?,?,?)", (
            payload.name, payload.module, payload.category, payload.method.upper(), payload.path,
            json.dumps(payload.headers, ensure_ascii=False), json.dumps(payload.params, ensure_ascii=False),
            payload.body, json.dumps(payload.assertions, ensure_ascii=False)))
        return decode(conn.execute("SELECT * FROM cases WHERE id=?", (cur.lastrowid,)).fetchone())


def assertion_passed(item: dict[str, Any], status: int, body: str) -> bool:
    kind, expected = item.get("type", "status"), item.get("expected")
    if kind == "status": return status == int(expected)
    if kind == "contains": return str(expected) in body
    if kind == "json_code":
        try: return json.loads(body).get("code") == expected
        except (ValueError, AttributeError): return False
    return False


PLACEHOLDER = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


def resolve_text(value: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables or variables[key] in (None, ""):
            raise ValueError(f"缺少环境变量：{key}")
        return str(variables[key])
    return PLACEHOLDER.sub(replace, value)


def resolve_value(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        exact = PLACEHOLDER.fullmatch(value)
        if exact:
            key = exact.group(1)
            if key not in variables or variables[key] in (None, ""): raise ValueError(f"缺少环境变量：{key}")
            return variables[key]
        return resolve_text(value, variables)
    if isinstance(value, list): return [resolve_value(v, variables) for v in value]
    if isinstance(value, dict): return {k: resolve_value(v, variables) for k, v in value.items()}
    return value


def redact(value: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret: value = value.replace(secret, "[REDACTED]")
    return value


def request_log_value(value: Any, secrets: list[str]) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return redact(str(value or ""), secrets)


def request_log_headers(headers: dict[str, Any]) -> str:
    safe = dict(headers)
    for key in list(safe):
        if key.lower() in ("authorization", "cookie", "x-api-key"):
            safe[key] = "[REDACTED]"
    return json.dumps(safe, ensure_ascii=False, indent=2)


@app.post("/api/cases/{case_id}/run")
async def run_case(case_id: int, environment_id: int = 1, allow_write: bool = False,
                   authorization: str = Header(default="")) -> dict[str, Any]:
    with db() as conn:
        case_row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        env_row = conn.execute("SELECT * FROM environments WHERE id=?", (environment_id,)).fetchone()
    if not case_row or not env_row: raise HTTPException(404, "用例或环境不存在")
    case, env = decode(case_row), decode(env_row)
    if case["method"] not in ("GET", "HEAD", "OPTIONS") and not allow_write:
        raise HTTPException(403, "写入类接口必须显式启用 allow_write")
    token = authorization.removeprefix("Bearer ").strip()
    managed_token = not bool(token)
    if managed_token:
        token = (await ensure_token(environment_id))["token"]
    variables = {**env.get("variables", {}), **({"token": token} if token else {})}
    try:
        if not env["base_url"].startswith(("http://", "https://")): raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        url = env["base_url"].rstrip("/") + "/" + resolve_text(case["path"].lstrip("/"), variables)
        headers = resolve_value({**env["headers"], **case["headers"]}, variables)
        if token: headers["Authorization"] = f"Bearer {token}"
        params = resolve_value(case["params"], variables)
        body: Any = case["body"]
        if body:
            try: body = resolve_value(json.loads(body), variables)
            except json.JSONDecodeError: body = resolve_text(body, variables)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    started = time.perf_counter(); status, response_body, error = 0, "", None
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.request(case["method"], url, headers=headers, params=params,
                json=body if isinstance(body, (dict, list)) else None,
                content=body if isinstance(body, str) and body else None)
            if response.status_code == 401 and managed_token:
                token = (await ensure_token(environment_id, force=True))["token"]
                headers["Authorization"] = f"Bearer {token}"
                response = await client.request(case["method"], url, headers=headers, params=params,
                    json=body if isinstance(body, (dict, list)) else None,
                    content=body if isinstance(body, str) and body else None)
            status, response_body = response.status_code, response.text[:100000]
    except Exception as exc: error = str(exc)
    duration = round((time.perf_counter() - started) * 1000)
    checks = [{**item, "passed": assertion_passed(item, status, response_body)} for item in case["assertions"]]
    passed = not error and 200 <= status < 300 and all(item["passed"] for item in checks)
    result_status = "passed" if passed else "failed"
    safe_body = redact(response_body, [token, str(variables.get("voucher_code", ""))])
    secrets = [token, str(variables.get("voucher_code", ""))]
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO runs(case_id,status,duration_ms,response_status,response_body,error,request_url,
               request_headers,request_params,request_body,assertion_results) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (case_id, result_status, duration, status, safe_body, error, url,
             request_log_headers(headers), request_log_value(params, secrets),
             request_log_value(body, secrets), json.dumps(checks, ensure_ascii=False)),
        )
    return {"id": cur.lastrowid, "status": result_status, "duration_ms": duration,
            "response_status": status, "response_body": safe_body, "error": error,
            "assertion_results": checks, "url": url.split("?", 1)[0]}


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT runs.*,cases.name AS case_name,cases.category,cases.method AS case_method,"
            "cases.path AS case_path,cases.assertions FROM runs "
            "JOIN cases ON cases.id=runs.case_id ORDER BY runs.id DESC LIMIT 100"
        )]


@app.post("/api/suites/{group}/run")
async def run_suite(group: str, environment_id: int = 1, authorization: str = Header(default="")) -> dict[str, Any]:
    if group not in GROUPS: raise HTTPException(404, "套件分类不存在")
    category, templates = GROUPS[group]
    names = [x["name"] for x in templates if x["method"] == "GET"]
    with db() as conn:
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM cases WHERE module=? AND category=?", (MODULE, category))}
    results = []
    for name in names:
        if name not in ids: continue
        try: results.append({"name": name, **await run_case(ids[name], environment_id, authorization=authorization)})
        except HTTPException as exc: results.append({"name": name, "status": "skipped", "error": exc.detail})
    return {"total": len(results), "passed": sum(x["status"] == "passed" for x in results), "results": results}


async def run_reservation_suite(environment_id: int = 1, authorization: str = ""):
    return await run_suite("reservation", environment_id, authorization)


CREATE_LIVE_FIELDS = [
    {"name": "store_id", "label": "场地 ID", "required": False, "default": 300, "readonly": True, "hint": "此场景固定使用 300"},
    {"name": "start_time", "label": "预约开始时间", "required": True, "hint": "格式：YYYY-MM-DD HH:mm:ss"},
    {"name": "end_time", "label": "预约结束时间", "required": True, "hint": "必须晚于开始时间"},
    {"name": "voucher_code", "label": "凭证码", "required": False, "default": "VP2026110985535A6DG5ZCFF7FX", "hint": "当前场景按 voucher 凭证支付创建订单"},
    {"name": "matches_name", "label": "直播名称", "required": False, "default": "", "hint": "可留空，系统会自动生成"},
    {"name": "device_id", "label": "主相机 ID", "required": False, "hint": "可选；已配置时会随创建直播请求发送"},
]

CREATE_RECORDING_FIELDS = [
    {"name": "recording_name", "label": "录像名称", "required": True, "hint": "创建录像所需名称"},
    {"name": "store_id", "label": "场地 ID", "required": True, "default": 30, "hint": "创建录像所需场地 ID"},
    {"name": "start_time", "label": "录像开始时间", "required": True, "hint": "格式：YYYY-MM-DD HH:mm:ss"},
    {"name": "end_time", "label": "录像结束时间", "required": True, "hint": "必须晚于开始时间"},
    {"name": "voucher_code", "label": "凭证码", "required": True, "default": "VP2026110985535A6DG5ZCFF7FX", "hint": "payment_type=voucher 时必填"},
]


def read_json_path(value: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value: return None
        value = value[key]
    return value


def scenario_missing(variables: dict[str, Any], fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [field for field in fields if field["required"] and variables.get(field["name"]) in (None, "")]


def scenario_result(session: dict[str, Any], state: str, *, missing: list[dict[str, Any]] | None = None,
                    message: str = "") -> dict[str, Any]:
    public_steps = [{key: value for key, value in step.items() if key != "json"} for step in session["steps"]]
    return {"session_id": session["id"], "state": state, "message": message,
            "missing": missing or [], "steps": public_steps,
            "variables": {key: value for key, value in session["variables"].items() if key not in {"token", "voucher_code"}}}


async def scenario_request(client: httpx.AsyncClient, method: str, url: str, headers: dict[str, str],
                           *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, headers=headers, params=params, json=body)
        raw = response.text[:100000]
        try: parsed = response.json()
        except ValueError: parsed = None
        code = parsed.get("code") if isinstance(parsed, dict) else None
        passed = 200 <= response.status_code < 300 and code in (None, 200)
        return {"status": "passed" if passed else "failed", "http_status": response.status_code,
                "response": redact(raw, [headers.get("Authorization", "")]), "json": parsed,
                "duration_ms": round((time.perf_counter() - started) * 1000)}
    except httpx.HTTPError as exc:
        return {"status": "failed", "http_status": 0, "response": "", "json": None,
                "error": str(exc), "duration_ms": round((time.perf_counter() - started) * 1000)}


async def execute_create_live_scenario(session: dict[str, Any]) -> dict[str, Any]:
    variables, env = session["variables"], session["environment"]
    missing = scenario_missing(variables, CREATE_LIVE_FIELDS)
    if missing:
        return scenario_result(session, "needs_input", missing=missing, message="请先填写场景必填参数")
    token = (await ensure_token(session["environment_id"]))["token"]
    headers = {**env.get("headers", {}), "Authorization": f"Bearer {token}"}
    base = env["base_url"].rstrip("/")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        if session["next_step"] == 0:
            result = await scenario_request(client, "POST", base + "/develop/reservation-orders", headers, body={
                "store_id": variables["store_id"], "start_time": variables["start_time"], "end_time": variables["end_time"],
                "payment_type": "voucher", "voucher_code": variables["voucher_code"], "is_clound": 1,
                "auto_device_config": 1, "auto_server_config": 1, "is_resolution": 0, "is_multiple": 0,
                "is_data_analysis": 0, "is_director": 1, "obs_record_enable": 0,
            })
            result.update(name="创建预约订单", method="POST", path="/develop/reservation-orders")
            session["steps"].append(result)
            if result["status"] != "passed": return scenario_result(session, "failed", message="创建预约订单失败")
            variables["auth_code"] = read_json_path(result["json"], "data.auth_code")
            variables["created_reservation_id"] = read_json_path(result["json"], "data.reservation_id")
            session["next_step"] = 1
            if not variables["auth_code"]:
                return scenario_result(session, "needs_input", missing=[{"name": "auth_code", "label": "预约授权码", "required": True,
                    "hint": "订单响应未返回授权码。完成支付后，从预约详情或授权码查询接口获取后填入。"}], message="预约订单已创建，等待授权码")
        if session["next_step"] == 1:
            if not variables.get("matches_name"):
                variables["matches_name"] = "自动创建直播 " + str(variables["start_time"])
            body = {"matches_name": variables["matches_name"], "matches_type": "1", "server_sn": variables["auth_code"],
                    "stream_type": "h1_server", "stream_tem": "aliyun", "start_time": variables["start_time"],
                    "end_time": variables["end_time"], "streaming_protocol": "rtmp", "is_resolution": 0,
                    "is_data_analysis": 0, "is_director": 1, "obs_record_enable": 0, "osd_enable": False,
                    "osd_timer_enable": False, "osd_time": "00:00", "alpha": .85, "jitter_buffer_sec": 0}
            if variables.get("device_id") not in (None, ""): body["device_id"] = variables["device_id"]
            result = await scenario_request(client, "POST", base + "/develop/matches", headers, body=body)
            result.update(name="创建直播", method="POST", path="/develop/matches", association="server_sn ← 创建预约订单.data.auth_code")
            session["steps"].append(result)
            if result["status"] != "passed": return scenario_result(session, "failed", message="创建直播失败")
            save_live_target(session, read_json_path(result["json"], "data.id"))
            session["next_step"] = 2
        if session["next_step"] == 2:
            result = await scenario_request(client, "GET", base + "/develop/matches", headers,
                params={"current": 1, "pageSize": 20, "matches_name": variables["matches_name"]})
            result.update(name="查询直播列表", method="GET", path="/develop/matches")
            session["steps"].append(result)
            session["next_step"] = 3
            if result["status"] != "passed": return scenario_result(session, "failed", message="查询直播列表失败")
    SCENARIO_SESSIONS.pop(session["id"], None)
    return scenario_result(session, "passed", message="创建直播场景执行完成")


async def execute_create_recording_scenario(session: dict[str, Any]) -> dict[str, Any]:
    variables, env = session["variables"], session["environment"]
    missing = scenario_missing(variables, CREATE_RECORDING_FIELDS)
    if missing:
        return scenario_result(session, "needs_input", missing=missing, message="请先填写创建录像必填参数")
    token = (await ensure_token(session["environment_id"]))["token"]
    headers = {**env.get("headers", {}), "Authorization": f"Bearer {token}"}
    base = env["base_url"].rstrip("/")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        result = await scenario_request(client, "POST", base + "/develop/recordings", headers, body={
            "recording_name": variables["recording_name"], "viewable_groups": [], "sport_type": "1",
            "store_id": variables["store_id"], "bitrate": "hd", "data_analysis_enable": 0,
            "end_time": variables["end_time"], "is_test": 0, "payment_type": "voucher",
            "start_time": variables["start_time"], "voucher_code": variables["voucher_code"],
        })
        result.update(name="创建录像", method="POST", path="/develop/recordings")
        session["steps"].append(result)
        if result["status"] != "passed": return scenario_result(session, "failed", message="创建录像失败")
        variables["recording_id"] = read_json_path(result["json"], "data.id")
        save_recording_target(session, result["json"])
        result = await scenario_request(client, "GET", base + "/develop/recordings", headers,
                                        params={"page_num": 1, "page_size": 20, "type": "video"})
        result.update(name="查询录像列表", method="GET", path="/develop/recordings?page_num=1&page_size=20&type=video",
                      association="查询参数固定为 page_num=1、page_size=20、type=video")
        session["steps"].append(result)
        if result["status"] != "passed": return scenario_result(session, "failed", message="查询录像列表失败")
    SCENARIO_SESSIONS.pop(session["id"], None)
    return scenario_result(session, "passed", message="创建录像场景执行完成")


@app.get("/api/scenarios/create-live")
def create_live_scenario_definition() -> dict[str, Any]:
    return {"name": "创建直播", "fields": CREATE_LIVE_FIELDS, "steps": [
        {"name": "创建预约订单", "method": "POST", "path": "/develop/reservation-orders"},
        {"name": "创建直播", "method": "POST", "path": "/develop/matches", "association": "server_sn ← 创建预约订单.data.auth_code"},
        {"name": "查询直播列表", "method": "GET", "path": "/develop/matches"},
    ]}


@app.get("/api/scenarios/create-recording")
def create_recording_scenario_definition() -> dict[str, Any]:
    return {"name": "创建录像", "fields": CREATE_RECORDING_FIELDS, "steps": [
        {"name": "创建录像", "method": "POST", "path": "/develop/recordings"},
        {"name": "查询录像列表", "method": "GET", "path": "/develop/recordings?page_num=1&page_size=20&type=video"},
    ]}


async def scenario_session(environment_id: int, variables: dict[str, Any], allow_write: bool,
                           fields: list[dict[str, Any]], fixed: dict[str, Any] | None = None) -> dict[str, Any]:
    if not allow_write: raise HTTPException(403, "请明确确认写入测试环境后再运行场景")
    with db() as conn:
        row = conn.execute("SELECT * FROM environments WHERE id=?", (environment_id,)).fetchone()
    if not row: raise HTTPException(404, "环境不存在")
    env = decode(row)
    if not env["base_url"].startswith(("http://", "https://")):
        raise HTTPException(422, "请先配置当前环境的 Base URL")
    defaults = {field["name"]: field["default"] for field in fields if "default" in field}
    session = {"id": uuid.uuid4().hex, "environment_id": environment_id, "environment": env,
               "variables": {**env.get("variables", {}), **defaults, **variables, **(fixed or {})}, "steps": [], "next_step": 0,
               "expires_at": int(time.time()) + SCENARIO_TTL_SECONDS}
    SCENARIO_SESSIONS[session["id"]] = session
    return session


@app.post("/api/scenarios/create-live/run")
async def run_create_live_scenario(payload: ScenarioRunIn) -> dict[str, Any]:
    session = await scenario_session(payload.environment_id, payload.variables, payload.allow_write, CREATE_LIVE_FIELDS, {"store_id": 300})
    return await execute_create_live_scenario(session)


from app.device_scenario import register as register_device_scenario
register_device_scenario(app, db, decode, TOKEN_STORE, resolve_value, resolve_text, ensure_token, get_venue_target)


@app.post("/api/scenarios/create-recording/run")
async def run_create_recording_scenario(payload: ScenarioRunIn) -> dict[str, Any]:
    session = await scenario_session(payload.environment_id, payload.variables, payload.allow_write, CREATE_RECORDING_FIELDS)
    return await execute_create_recording_scenario(session)


def recording_target_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS recording_targets (
        environment_id INTEGER PRIMARY KEY, version TEXT NOT NULL, base_url TEXT NOT NULL,
        recording_id INTEGER, name TEXT NOT NULL, status TEXT NOT NULL)""")


def save_recording_target(session, response):
    # Only IDs explicitly returned by this creation are eligible; never use an order ID or list position.
    recording_id = read_json_path(response, "data.recording_id")
    if recording_id is None:
        recording_id = read_json_path(response, "data.id")
    if isinstance(recording_id, bool) or not str(recording_id).isdigit() or int(recording_id) <= 0:
        recording_id = None
    with db() as conn:
        recording_target_table(conn)
        conn.execute("INSERT OR REPLACE INTO recording_targets VALUES(?,?,?,?,?,?)", (
            session["environment_id"], uuid.uuid4().hex, session["environment"]["base_url"],
            recording_id, str(session["variables"].get("recording_name", "")),
            "ready" if recording_id else "missing_id"))


@app.get("/api/scenarios/cancel-recording")
def get_recording_target(environment_id: int):
    with db() as conn:
        recording_target_table(conn)
        row = conn.execute("SELECT * FROM recording_targets WHERE environment_id=?", (environment_id,)).fetchone()
    return dict(row) if row else {"status": "empty"}


def live_target_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS live_targets (
        environment_id INTEGER PRIMARY KEY, version TEXT NOT NULL, base_url TEXT NOT NULL,
        reservation_id INTEGER, match_id INTEGER, name TEXT NOT NULL, status TEXT NOT NULL)""")


def positive_id(value):
    return int(value) if not isinstance(value, bool) and str(value).isdigit() and int(value) > 0 else None


def save_live_target(session, match_id):
    reservation_id = positive_id(session["variables"].get("created_reservation_id"))
    match_id = positive_id(match_id)
    with db() as conn:
        live_target_table(conn)
        conn.execute("INSERT OR REPLACE INTO live_targets VALUES(?,?,?,?,?,?,?)", (
            session["environment_id"], uuid.uuid4().hex, session["environment"]["base_url"],
            reservation_id, match_id, session["variables"]["matches_name"],
            "ready" if reservation_id and match_id else "missing_id"))


@app.get("/api/scenarios/delete-live")
def get_live_target(environment_id: int):
    with db() as conn:
        live_target_table(conn)
        row = conn.execute("SELECT * FROM live_targets WHERE environment_id=?", (environment_id,)).fetchone()
    return dict(row) if row else {"status": "empty"}


class CancelRecordingIn(BaseModel):
    environment_id: int
    version: str
    allow_write: bool = False


@app.post("/api/scenarios/delete-live/run")
async def delete_live_scenario(payload: CancelRecordingIn):
    if not payload.allow_write:
        raise HTTPException(403, "请确认删除所显示的直播")
    with db() as conn:
        live_target_table(conn)
        row = conn.execute("SELECT * FROM live_targets WHERE environment_id=?", (payload.environment_id,)).fetchone()
        env_row = conn.execute("SELECT * FROM environments WHERE id=?", (payload.environment_id,)).fetchone()
    if not row or not env_row:
        raise HTTPException(409, "请先在当前环境运行创建直播场景")
    target, env = dict(row), decode(env_row)
    if target["version"] != payload.version or target["status"] != "ready":
        raise HTTPException(409, "关联直播不可执行或已变化，请重新打开场景查看")
    if target["base_url"] != env["base_url"] or not target["reservation_id"] or not target["match_id"]:
        raise HTTPException(409, "环境地址变化或缺少关联ID，请重新创建直播")
    token = (await ensure_token(payload.environment_id))["token"]
    with db() as conn:
        changed = conn.execute("UPDATE live_targets SET status='running' WHERE environment_id=? AND version=? AND status='ready'",
                               (payload.environment_id, payload.version)).rowcount
    if not changed:
        raise HTTPException(409, "该场景已执行或目标发生变化")
    headers = {**env.get("headers", {}), "Authorization": f"Bearer {token}"}
    base = env["base_url"].rstrip("/")
    steps = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        deletion = await scenario_request(client, "DELETE", base + "/develop/reservations", headers,
                                          body={"ids": [target["reservation_id"]]})
        parsed = deletion.pop("json", None)
        deletion["status"] = "passed" if deletion["status"] == "passed" and isinstance(parsed, dict) and parsed.get("code") == 200 else "failed"
        steps.append({**deletion, "name": "删除预约", "reservation_id": target["reservation_id"]})
        if deletion["status"] == "passed":
            ended = await scenario_request(client, "POST", base + f'/develop/matches-end/{target["match_id"]}', headers)
            parsed = ended.pop("json", None)
            valid = (isinstance(parsed, dict) and str(read_json_path(parsed, "data.match_id")) == str(target["match_id"])
                     and read_json_path(parsed, "data.status") == "已结束")
            ended["status"] = "passed" if ended["status"] == "passed" and valid else "failed"
            steps.append({**ended, "name": "结束球局", "match_id": target["match_id"]})
    passed = len(steps) == 2 and all(s["status"] == "passed" for s in steps)
    with db() as conn:
        conn.execute("UPDATE live_targets SET status=? WHERE environment_id=? AND version=?",
                     ("deleted" if passed else "check_required", payload.environment_id, payload.version))
    return {"status": "passed" if passed else "failed", "steps": steps}


@app.post("/api/scenarios/cancel-recording/run")
async def cancel_recording_scenario(payload: CancelRecordingIn):
    if not payload.allow_write:
        raise HTTPException(403, "请确认取消所显示的录像")
    with db() as conn:
        recording_target_table(conn)
        row = conn.execute("SELECT * FROM recording_targets WHERE environment_id=?", (payload.environment_id,)).fetchone()
        env_row = conn.execute("SELECT * FROM environments WHERE id=?", (payload.environment_id,)).fetchone()
    if not row or not env_row:
        raise HTTPException(409, "请先在当前环境运行创建录像场景")
    target, env = dict(row), decode(env_row)
    if target["version"] != payload.version or target["status"] != "ready" or not target["recording_id"]:
        raise HTTPException(409, "关联录像已变化或不可取消，请重新打开取消录像场景")
    if target["base_url"] != env["base_url"]:
        raise HTTPException(409, "环境地址已变化，请在当前地址重新创建录像")
    token = (await ensure_token(payload.environment_id))["token"]
    with db() as conn:
        changed = conn.execute("UPDATE recording_targets SET status='running' WHERE environment_id=? AND version=? AND status='ready'",
                               (payload.environment_id, payload.version)).rowcount
    if not changed:
        raise HTTPException(409, "该录像正在取消或目标已变化")
    path = f'/develop/recordings/cancel/{target["recording_id"]}'
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        result = await scenario_request(client, "POST", env["base_url"].rstrip("/") + path,
            {**env.get("headers", {}), "Authorization": f"Bearer {token}"})
    parsed = result.pop("json", None)
    if not isinstance(parsed, dict) or parsed.get("code") != 200:
        result["status"] = "failed"
    with db() as conn:
        conn.execute("UPDATE recording_targets SET status=? WHERE environment_id=? AND version=?",
                     ("cancelled" if result["status"] == "passed" else "check_required", payload.environment_id, payload.version))
    return {**result, "path": path, "recording_id": target["recording_id"]}


@app.post("/api/scenarios/create-live/continue")
async def continue_create_live_scenario(payload: ScenarioContinueIn) -> dict[str, Any]:
    if not payload.allow_write: raise HTTPException(403, "请明确确认写入测试环境后再继续场景")
    session = SCENARIO_SESSIONS.get(payload.session_id)
    if not session or session["expires_at"] < int(time.time()):
        SCENARIO_SESSIONS.pop(payload.session_id, None)
        raise HTTPException(410, "场景已过期，请重新开始")
    session["variables"].update(payload.variables)
    session["expires_at"] = int(time.time()) + SCENARIO_TTL_SECONDS
    return await execute_create_live_scenario(session)
