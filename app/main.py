from pathlib import Path
import hashlib, json, re, sqlite3, time
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
          request_url TEXT NOT NULL DEFAULT '', assertion_results TEXT NOT NULL DEFAULT '[]',
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
        "is_overseas": 0, "auto_device_config": 1, "auto_server_config": 1, "is_resolution": 0,
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
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO runs(case_id,status,duration_ms,response_status,response_body,error,request_url,assertion_results) VALUES(?,?,?,?,?,?,?,?)",
            (case_id, result_status, duration, status, safe_body, error, url, json.dumps(checks, ensure_ascii=False)),
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
