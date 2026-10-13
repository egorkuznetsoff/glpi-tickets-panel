#!/usr/bin/env python3
import json, secrets, time, threading, os, zipfile, io, urllib.request, urllib.parse
from pathlib import Path
from flask import Flask, request, jsonify, send_file, session, Response
import psycopg2, psycopg2.extras

app = Flask(__name__)

PANEL_DIR   = Path("/home/egor/glpi-panel")
UPLOAD_DIR  = PANEL_DIR / "uploads"
AGENT_DIR   = PANEL_DIR / "agent"
AGENT_SRC   = PANEL_DIR / "agent_src"
CONFIG_FILE = PANEL_DIR / "config.json"

for d in [UPLOAD_DIR, AGENT_DIR, AGENT_SRC]:
    d.mkdir(exist_ok=True)

DB_DSN = "host=localhost dbname=foxdb user=fox password=CHANGE_ME_DB_PASSWORD"

def db_connect():
    return psycopg2.connect(DB_DSN)

# ── Config ────────────────────────────────────────────────────────────────────

def _init_config():
    cfg = {}
    if CONFIG_FILE.exists():
        try: cfg = json.loads(CONFIG_FILE.read_text())
        except: pass
    changed = False
    for k, v in [("secret_key",          secrets.token_hex(32)),
                 ("api_key",             secrets.token_hex(24)),
                 ("username",            "admin"),
                 ("password",            "CHANGE_ME_ADMIN_PASSWORD"),
                 ("telegram_bot_token",  "CHANGE_ME_TELEGRAM_BOT_TOKEN"),
                 ("telegram_chat_id",    "CHANGE_ME_TELEGRAM_CHAT_ID"),
                 ("glpi_url",            "https://glpi.example.com/apirest.php"),
                 ("glpi_app_token",      "CHANGE_ME_GLPI_APP_TOKEN"),
                 ("glpi_user_token",     "CHANGE_ME_GLPI_USER_TOKEN")]:
        if k not in cfg:
            cfg[k] = v; changed = True
    if changed:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    return cfg

_cfg = _init_config()
app.secret_key = _cfg["secret_key"]
PANEL_USERNAME = _cfg["username"]
PANEL_PASSWORD = _cfg["password"]
PANEL_API_KEY  = _cfg["api_key"]

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(msg):
    try:
        token   = _cfg.get("telegram_bot_token", "")
        chat_id = _cfg.get("telegram_chat_id", "")
        if not token or not chat_id:
            return
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat_id, "text": msg}).encode()
        req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

# ── GLPI proxy ────────────────────────────────────────────────────────────────

_glpi_lock = threading.Lock()
_glpi_tok  = {"val": None, "ts": 0}

def _glpi_new_token():
    req = urllib.request.Request(
        _cfg["glpi_url"] + "/initSession",
        headers={"Authorization": f"user_token {_cfg['glpi_user_token']}",
                 "App-Token": _cfg["glpi_app_token"]})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["session_token"]

def glpi_token():
    with _glpi_lock:
        if not _glpi_tok["val"] or time.time() - _glpi_tok["ts"] > 3500:
            _glpi_tok["val"] = _glpi_new_token()
            _glpi_tok["ts"]  = time.time()
        return _glpi_tok["val"]

def glpi_call(method, path, body=None, retry=True):
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        _cfg["glpi_url"] + path, data=data, method=method,
        headers={"Session-Token": glpi_token(),
                 "App-Token": _cfg["glpi_app_token"],
                 "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            with _glpi_lock: _glpi_tok["val"] = None
            return glpi_call(method, path, body, retry=False)
        raise

def glpi_resolve_users(uid_set):
    out = {}
    for uid in uid_set:
        if not uid or uid == 0: continue
        try:
            r = glpi_call("GET", f"/User/{uid}")
            out[uid] = f"{r.get('firstname','')} {r.get('realname','')}".strip() or r.get("name", f"#{uid}")
        except:
            out[uid] = f"#{uid}"
    return out

# ── Rate limiter ───────────────────────────────────────────────────────────────

_rl_lock = threading.Lock()
_rl_data = {}

def rl_check(ip):
    now = time.time()
    with _rl_lock:
        d = _rl_data.get(ip, {})
        bu = d.get("blocked_until", 0)
        if bu > now: return False, int(bu - now)
    return True, 0

def rl_fail(ip):
    now = time.time()
    with _rl_lock:
        d = _rl_data.setdefault(ip, {"fails": 0, "blocked_until": 0})
        d["fails"] += 1
        if d["fails"] >= 5:
            d["blocked_until"] = now + 900; d["fails"] = 0

def rl_reset(ip):
    with _rl_lock: _rl_data.pop(ip, None)

# ── Tickets DB ────────────────────────────────────────────────────────────────

def tickets_upsert(tickets):
    count = 0
    with db_connect() as conn:
        with conn.cursor() as cur:
            # Find which IDs are new
            ids = [int(t.get('id', 0)) for t in tickets if int(t.get('id', 0)) > 0]
            if ids:
                cur.execute("SELECT id FROM glpi_tickets WHERE id = ANY(%s)", (ids,))
                existing = {r[0] for r in cur.fetchall()}
            else:
                existing = set()
            new_tickets = [t for t in tickets if int(t.get('id', 0)) not in existing]

            for t in tickets:
                try:
                    cur.execute("""
                        INSERT INTO glpi_tickets
                            (id,name,status,priority,date_created,date_mod,requester,location,content,source,updated_at)
                        VALUES (%(id)s,%(name)s,%(status)s,%(priority)s,%(date)s,%(date_mod)s,
                                %(requester)s,%(location)s,%(content)s,%(source)s,NOW())
                        ON CONFLICT (id) DO UPDATE SET
                            name=EXCLUDED.name, status=EXCLUDED.status,
                            priority=EXCLUDED.priority, date_mod=EXCLUDED.date_mod,
                            requester=EXCLUDED.requester, location=EXCLUDED.location,
                            content=EXCLUDED.content, source=EXCLUDED.source,
                            updated_at=NOW()
                    """, {
                        'id':        int(t.get('id', 0)),
                        'name':      str(t.get('name', '')),
                        'status':    int(t.get('status', 0)),
                        'priority':  int(t.get('priority', 0)),
                        'date':      str(t.get('date', '')),
                        'date_mod':  str(t.get('date_mod', '')),
                        'requester': str(t.get('requester', '')),
                        'location':  str(t.get('location', '')),
                        'content':   str(t.get('content', '')),
                        'source':    str(t.get('source', '')),
                    })
                    count += 1
                except Exception:
                    pass
            cur.execute("""
                INSERT INTO glpi_meta (key,value) VALUES ('last_sync',%s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            """, (str(int(time.time())),))
            cur.execute("DELETE FROM glpi_meta WHERE key='resync_requested'")

    # Telegram notification for new tickets
    if new_tickets:
        lines = [f"• #{t.get('id')} {str(t.get('name',''))[:80]}" for t in new_tickets[:5]]
        tail  = f"\n...и ещё {len(new_tickets)-5}" if len(new_tickets) > 5 else ""
        send_telegram(f"Новых заявок GLPI: {len(new_tickets)}\n" + "\n".join(lines) + tail)

    return count

def tickets_query():
    with db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id,name,status,priority,
                       date_created AS date,date_mod,requester,location,content,source
                FROM glpi_tickets
                WHERE status NOT IN (5,6)
                ORDER BY id DESC
            """)
            tickets = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT value FROM glpi_meta WHERE key='last_sync'")
            row = cur.fetchone()
            synced_at = int(row['value']) if row else 0
    return {"tickets": tickets, "synced_at": synced_at}

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.before_request
def check_auth():
    if request.path in ("/", "/login"):
        return
    if request.headers.get("X-Api-Key") == PANEL_API_KEY:
        return
    if not session.get("ok"):
        return jsonify({"error": "unauthorized"}), 401

@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr
    allowed, retry = rl_check(ip)
    if not allowed:
        return jsonify({"error": f"Слишком много попыток. Повторите через {retry} с."}), 429
    d = request.json or {}
    if d.get("username") == PANEL_USERNAME and d.get("password") == PANEL_PASSWORD:
        rl_reset(ip); session["ok"] = True
        return jsonify({"ok": True})
    rl_fail(ip)
    return jsonify({"error": "Неверный логин или пароль"}), 403

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

# ── Agent sync (принимает данные от агента) ───────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def api_sync():
    if request.headers.get("X-Api-Key") != PANEL_API_KEY:
        return jsonify({"error": "forbidden"}), 403
    d = request.json or {}
    tickets = d.get("tickets", [])
    if not isinstance(tickets, list):
        return jsonify({"error": "invalid"}), 400
    count = tickets_upsert(tickets)
    return jsonify({"ok": True, "count": count})

# ── Tickets API ───────────────────────────────────────────────────────────────

@app.route("/api/tickets")
def api_tickets():
    return jsonify(tickets_query())

# ── Resync flag ───────────────────────────────────────────────────────────────

@app.route("/api/resync-flag")
def api_resync_flag():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM glpi_meta WHERE key='resync_requested'")
            row = cur.fetchone()
    return jsonify({"requested": bool(row and row[0] == '1')})

@app.route("/api/request-sync", methods=["POST"])
def api_request_sync():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO glpi_meta (key,value) VALUES ('resync_requested','1')
                ON CONFLICT (key) DO UPDATE SET value='1'
            """)
    return jsonify({"ok": True})

# ── GLPI proxy routes ─────────────────────────────────────────────────────────

@app.route("/api/glpi/ticket/<int:tid>")
def api_glpi_ticket(tid):
    try:
        ticket = glpi_call("GET", f"/Ticket/{tid}?with_actors=1")
        try: followups = glpi_call("GET", f"/Ticket/{tid}/ITILFollowup?range=0-100&sort=2&order=ASC") or []
        except: followups = []
        try: solutions = glpi_call("GET", f"/Ticket/{tid}/ITILSolution?range=0-20") or []
        except: solutions = []

        # Resolve user names
        uid_set = set()
        for grp in (ticket.get("_actors") or {}).values():
            for a in grp: uid_set.add(a.get("id"))
        for f in followups: uid_set.add(f.get("users_id"))
        for s in solutions: uid_set.add(s.get("users_id"))
        users = glpi_resolve_users(uid_set - {None, 0})

        # Resolve location
        location = None
        loc_id = ticket.get("locations_id", 0)
        if loc_id and loc_id > 0:
            try:
                loc = glpi_call("GET", f"/Location/{loc_id}")
                location = {"id": loc_id, "name": loc.get("completename", loc.get("name", str(loc_id)))}
            except: location = {"id": loc_id, "name": str(loc_id)}

        return jsonify({"ticket": ticket, "followups": followups,
                        "solutions": solutions, "users": users, "location": location})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/glpi/ticket/<int:tid>/followup", methods=["POST"])
def api_glpi_followup(tid):
    content = (request.json or {}).get("content", "").strip()
    if not content: return jsonify({"error": "empty"}), 400
    try:
        r = glpi_call("POST", "/ITILFollowup",
                      {"input": {"items_id": tid, "itemtype": "Ticket", "content": content}})
        return jsonify({"ok": True, "id": r.get("id") if isinstance(r, dict) else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/glpi/ticket/<int:tid>/solution", methods=["POST"])
def api_glpi_solution(tid):
    content = (request.json or {}).get("content", "Решено").strip() or "Решено"
    try:
        glpi_call("POST", "/ITILSolution",
                  {"input": {"items_id": tid, "itemtype": "Ticket", "content": content}})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/glpi/ticket/<int:tid>/update", methods=["PUT"])
def api_glpi_ticket_update(tid):
    d = request.json or {}
    fields = {}
    if "priority"        in d: fields["priority"]          = int(d["priority"])
    if "status"          in d: fields["status"]            = int(d["status"])
    if "locations_id"    in d: fields["locations_id"]      = int(d["locations_id"])
    if "assign_user_id"  in d: fields["_users_id_assign"]  = int(d["assign_user_id"])
    if not fields: return jsonify({"error": "nothing"}), 400
    try:
        fields["id"] = tid
        glpi_call("PUT", f"/Ticket/{tid}", {"input": fields})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/glpi/users")
def api_glpi_users():
    q = request.args.get("q", "").strip()
    try:
        r = glpi_call("GET", f"/User?searchText[name]={urllib.parse.quote(q)}&range=0-20&sort=1&order=ASC") or []
        return jsonify([{"id": u["id"],
                         "name": f"{u.get('firstname','')} {u.get('realname','')}".strip() or u.get("name","")}
                        for u in r if isinstance(u, dict)])
    except: return jsonify([])

@app.route("/api/glpi/locations")
def api_glpi_locations():
    q = request.args.get("q", "").strip()
    try:
        r = glpi_call("GET", f"/Location?searchText[completename]={urllib.parse.quote(q)}&range=0-20&sort=1") or []
        return jsonify([{"id": l["id"],
                         "name": l.get("completename", l.get("name", ""))}
                        for l in r if isinstance(l, dict)])
    except: return jsonify([])

@app.route("/ticket/<int:tid>")
def ticket_page(tid):
    if not session.get("ok"):
        return Response('<script>location.href=location.href.replace(/\\/ticket\\/\\d+\\/?$/,"");</script>',
                        mimetype="text/html")
    creds = json.dumps({
        "url":        _cfg.get("glpi_url", ""),
        "app_token":  _cfg.get("glpi_app_token", ""),
        "user_token": _cfg.get("glpi_user_token", ""),
    })
    return Response(TICKET_HTML.replace("__GLPI_CREDS__", creds), mimetype="text/html")

# ── Agent download ────────────────────────────────────────────────────────────

@app.route("/api/agent/info")
def api_agent_info():
    files = list(AGENT_DIR.glob("*.zip"))
    if not files:
        return jsonify({"exists": False})
    f = sorted(files)[-1]
    return jsonify({"exists": True, "name": f.name, "size": f.stat().st_size})

@app.route("/api/agent")
def api_agent():
    files = list(AGENT_DIR.glob("*.zip"))
    if not files:
        return jsonify({"error": "Агент не найден"}), 404
    f = sorted(files)[-1]
    return send_file(f, as_attachment=True, download_name=f.name)

# ── File upload ───────────────────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("files")
    if not files: return jsonify({"error": "Нет файлов"}), 400
    saved = []
    for f in files:
        if not f.filename: continue
        safe = "".join(c for c in f.filename if c.isalnum() or c in "._- ()").strip()
        if not safe: continue
        data = f.read(50 * 1024 * 1024)
        (UPLOAD_DIR / safe).write_bytes(data)
        saved.append({"name": safe, "size": len(data)})
    return jsonify({"saved": saved})

@app.route("/api/files")
def api_files():
    return jsonify([{"name": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)}
                    for f in sorted(UPLOAD_DIR.iterdir()) if f.is_file()])

@app.route("/api/files/<name>")
def api_file_download(name):
    safe = "".join(c for c in name if c.isalnum() or c in "._- ()").strip()
    p = UPLOAD_DIR / safe
    if not p.exists(): return "not found", 404
    return send_file(p, as_attachment=True, download_name=safe)

@app.route("/api/files/<name>", methods=["DELETE"])
def api_file_delete(name):
    safe = "".join(c for c in name if c.isalnum() or c in "._- ()").strip()
    p = UPLOAD_DIR / safe
    if p.exists(): p.unlink()
    return jsonify({"ok": True})

# ── Форма создания тикетов (встроенная) ──────────────────────────────────────

@app.route("/form")
def form_page():
    if not session.get("ok"):
        return Response('<script>location.href=location.origin+location.pathname.replace(/form$/,"");</script>',
                        mimetype="text/html")
    form_file = UPLOAD_DIR / "index.html"
    if not form_file.exists():
        return "Форма не найдена. Загрузи index.html через панель.", 404
    html = form_file.read_text(encoding="utf-8")
    # Добавляем кнопку "Назад" перед заголовком
    back_btn = """<div style="background:#0033A0;padding:8px 40px">
  <a href="../" style="color:#fff;text-decoration:none;font-family:sans-serif;font-size:13px;opacity:.8">
    &#8592; Вернуться в панель
  </a>
</div>"""
    html = html.replace('<div class="slb-header">', back_btn + '\n<div class="slb-header">', 1)
    # Инжектируем скрипт автоназначения локации RU0974 и группы Global Onsite Support
    glpi_url  = _cfg.get("glpi_url", "")
    app_token = _cfg.get("glpi_app_token", "")
    usr_token = _cfg.get("glpi_user_token", "")
    inject = f"""<script>
(function(){{
  const GURL='{glpi_url}',AT='{app_token}',UT='{usr_token}';
  let locId=null, grpId=null;
  fetch(GURL+'/initSession',{{headers:{{'Authorization':'user_token '+UT,'App-Token':AT}}}})
    .then(r=>r.json()).then(s=>{{
      const tok=s.session_token;
      return Promise.all([
        fetch(GURL+'/Location?searchText[completename]=RU0974&range=0-5',{{headers:{{'Session-Token':tok,'App-Token':AT}}}}).then(r=>r.json()),
        fetch(GURL+'/Group?searchText[completename]=Global+Onsite&range=0-20',{{headers:{{'Session-Token':tok,'App-Token':AT}}}}).then(r=>r.json())
      ]);
    }}).then(([locs,groups])=>{{
      if(Array.isArray(locs)&&locs.length) locId=locs[0].id;
      if(Array.isArray(groups)&&groups.length) grpId=groups[0].id;
    }}).catch(()=>{{}});

  function patchBody(body){{
    try{{
      const b=JSON.parse(body||'{{}}');
      if(b.input){{
        if(locId&&!b.input.locations_id) b.input.locations_id=locId;
        if(grpId&&!b.input._groups_id_assign) b.input._groups_id_assign=grpId;
        return JSON.stringify(b);
      }}
    }}catch{{}}
    return body;
  }}
  function isTicketPost(method,url){{
    return method==='POST'&&typeof url==='string'&&/\\/Ticket(\\?|$)/.test(url);
  }}
  const origFetch=window.fetch;
  window.fetch=function(url,opts={{}}){{
    if(isTicketPost(opts.method,url)) opts={{...opts,body:patchBody(opts.body)}};
    return origFetch.call(this,url,opts);
  }};
  const origOpen=XMLHttpRequest.prototype.open,origSend=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(m,u,...a){{this._m=m;this._u=u;return origOpen.call(this,m,u,...a);}};
  XMLHttpRequest.prototype.send=function(body){{
    if(isTicketPost(this._m,this._u)) body=patchBody(body);
    return origSend.call(this,body);
  }};
}})();
</script>"""
    html = html.replace('</body>', inject + '\n</body>', 1)
    return Response(html, mimetype="text/html")

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GLPI — Панель</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#09090f;--card:#101018;--card2:#181826;--border:#21213a;--border2:#2c2c48;
  --text:#e0e0f2;--muted:#62628a;--accent:#7b68f0;--glow:rgba(123,104,240,.2);
  --green:#28c97a;--red:#e85555;--yellow:#e8a82a;--blue:#4a9ef0;--r:10px;--rs:6px;
  --row-hover:rgba(255,255,255,.015)
}
[data-theme="light"]{
  --bg:#f0f0f8;--card:#ffffff;--card2:#e8e8f4;--border:#d0d0e5;--border2:#b8b8d8;
  --text:#1a1a30;--muted:#7070a0;--row-hover:rgba(0,0,0,.04)
}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5;min-height:100vh}
#loginPage{min-height:100vh;display:none;align-items:center;justify-content:center;padding:20px;
  background:radial-gradient(ellipse 80% 40% at 50% 0%,rgba(123,104,240,.12) 0%,transparent 70%)}
.login-card{background:var(--card);border:1px solid var(--border2);border-radius:18px;
  padding:40px;width:100%;max-width:360px;box-shadow:0 24px 80px rgba(0,0,0,.6)}
.login-icon{width:56px;height:56px;background:linear-gradient(135deg,#7b68f0,#5048c8);
  border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:26px;
  margin:0 auto 14px;box-shadow:0 8px 28px var(--glow)}
.login-card h1{font-size:22px;font-weight:700;text-align:center;margin-bottom:6px}
.login-card p{color:var(--muted);font-size:13px;text-align:center;margin-bottom:28px}
label{display:block;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:6px}
input[type=text],input[type=password]{width:100%;background:var(--bg);border:1px solid var(--border2);
  border-radius:var(--rs);padding:10px 14px;color:var(--text);font-size:14px;outline:none;transition:border-color .15s}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
.fg{margin-bottom:16px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 18px;border:none;
  border-radius:var(--rs);font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;
  white-space:nowrap;background:transparent;color:var(--text)}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}
.btn-p{background:var(--accent);color:#fff;box-shadow:0 4px 14px var(--glow)}
.btn-p:hover:not(:disabled){background:#6a5ae0}
.btn-full{width:100%;justify-content:center;padding:11px;margin-top:4px}
.btn-g{background:transparent;color:var(--muted);border:1px solid var(--border)}
.btn-g:hover:not(:disabled){color:var(--text);border-color:var(--border2);background:var(--card2)}
.btn-green{background:#1a4a2e;color:var(--green);border:1px solid rgba(40,201,122,.4)}
.btn-green:hover:not(:disabled){background:#1f5534}
.btn-del{background:transparent;border:1px solid var(--border);color:var(--muted);
  padding:4px 10px;border-radius:var(--rs);cursor:pointer;font-size:12px;transition:all .15s}
.btn-del:hover{color:var(--red);border-color:rgba(232,85,85,.4);background:rgba(232,85,85,.07)}
.err{background:rgba(232,85,85,.1);border:1px solid rgba(232,85,85,.3);color:var(--red);
  padding:10px 14px;border-radius:var(--rs);font-size:13px;margin-top:12px;display:none}
.err.on{display:block}
#app{display:none}
.topbar{background:var(--card);border-bottom:1px solid var(--border);padding:0 24px;height:54px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px}
.content{max-width:95%;margin:0 auto;padding:28px 20px;display:flex;flex-direction:column;gap:24px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:24px}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:16px}
.card-desc{color:var(--muted);font-size:13px;margin-bottom:18px;line-height:1.6}
.agent-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.agent-info{font-size:12px;color:var(--muted)}

/* Stats row */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:0;border-bottom:1px solid var(--border);padding-bottom:18px;margin-bottom:18px}
.stat-item{border-left:1px solid var(--border);padding:0 18px}
.stat-item:first-child{border-left:none;padding-left:0}
.stat-l{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:5px}
.stat-v{font-size:24px;font-weight:700;line-height:1}

/* Tickets table */
.tbl-w{background:var(--card2);border:1px solid var(--border);border-radius:var(--rs);overflow:hidden;overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:1252px}
thead th{text-align:left;padding:10px 14px;font-size:10px;font-weight:600;text-transform:uppercase;
  letter-spacing:.6px;color:var(--muted);background:var(--card);border-bottom:1px solid var(--border)}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
tbody tr:last-child{border:none}
tbody tr:hover{background:var(--row-hover)}
td{padding:11px 14px;vertical-align:middle;font-size:13px}
.t-id{font-family:monospace;font-size:12px}
.t-id a{color:var(--accent);text-decoration:none;transition:color .15s}
.t-id a:hover{color:#9b84f5;text-decoration:underline}
.t-name{font-weight:500;max-width:240px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;white-space:normal}
.t-desc{max-width:320px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;color:var(--muted);font-size:12px;line-height:1.5;white-space:normal}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;white-space:nowrap}
.badge-new{background:rgba(123,104,240,.15);color:#9b84f5}
.badge-proc{background:rgba(74,158,240,.15);color:var(--blue)}
.badge-pend{background:rgba(232,168,42,.12);color:var(--yellow)}
.badge-done{background:rgba(40,201,122,.1);color:var(--green)}
.badge-closed{background:rgba(98,98,138,.12);color:var(--muted)}
.badge-src-mine{background:rgba(40,201,122,.1);color:var(--green)}
.badge-src-loc{background:rgba(74,158,240,.1);color:var(--blue)}
.sync-info{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px}
.sync-ts{font-size:12px;color:var(--muted)}
.empty-tbl{text-align:center;padding:48px 20px;color:var(--muted);font-size:13px}

/* Filters */
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.filter-sel{background:var(--card2);border:1px solid var(--border2);color:var(--text);
  padding:6px 10px;border-radius:var(--rs);font-size:12px;outline:none;cursor:pointer;transition:border-color .15s}
.filter-sel:focus{border-color:var(--accent)}
.filter-search{background:var(--card2);border:1px solid var(--border2);color:var(--text);
  padding:6px 12px;border-radius:var(--rs);font-size:12px;outline:none;width:240px;transition:border-color .15s}
.filter-search:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
.filter-count{font-size:12px;color:var(--muted);margin-left:auto}
/* Sort */
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--text)}
.sort-arr{opacity:.3;margin-left:3px;font-size:9px}
th.sort-asc .sort-arr::after{content:'▲';opacity:1}
th.sort-desc .sort-arr::after{content:'▼';opacity:1}
/* Pagination */
.pager{display:flex;align-items:center;gap:6px;padding:10px 14px;border-top:1px solid var(--border);flex-wrap:wrap}
.pg-btn{background:var(--card2);border:1px solid var(--border);color:var(--muted);
  padding:4px 12px;border-radius:var(--rs);cursor:pointer;font-size:12px;transition:all .15s}
.pg-btn:hover:not(:disabled){color:var(--text);border-color:var(--border2)}
.pg-btn:disabled{opacity:.3;cursor:not-allowed}
.pg-info{font-size:12px;color:var(--muted);margin-left:auto}
/* New badge */
.new-badge{background:var(--red);color:#fff;border-radius:10px;
  font-size:10px;font-weight:700;padding:1px 7px;margin-left:6px;vertical-align:middle}
/* Upload */
.drop-zone{border:2px dashed var(--border2);border-radius:var(--r);padding:28px 20px;
  text-align:center;cursor:pointer;transition:border-color .2s,background .2s}
.drop-zone:hover,.drop-zone.drag{border-color:var(--accent);background:rgba(123,104,240,.05)}
.drop-zone p{color:var(--muted);font-size:13px;margin-top:6px}
.drop-icon{font-size:28px;opacity:.5}
.file-list-wrap{display:flex;flex-direction:column;gap:7px;margin-top:14px}
.file-item{display:flex;align-items:center;justify-content:space-between;
  background:var(--card2);border:1px solid var(--border);border-radius:var(--rs);padding:9px 13px}
.file-name{font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:500px}
.file-meta{font-size:11px;color:var(--muted);margin-top:2px}
.file-actions{display:flex;gap:6px;flex-shrink:0}
.empty-files{color:var(--muted);font-size:13px;text-align:center;padding:16px}
.progress-bar{height:3px;background:var(--border2);border-radius:2px;margin-top:10px;overflow:hidden;display:none}
.progress-fill{height:100%;background:var(--accent);transition:width .3s;width:0}
#toasts{position:fixed;bottom:22px;right:22px;display:flex;flex-direction:column;gap:8px;z-index:9999}
.toast{background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);
  padding:11px 16px;font-size:13px;font-weight:500;box-shadow:0 8px 28px rgba(0,0,0,.4);
  display:flex;align-items:center;gap:8px;transform:translateX(130%);
  transition:transform .3s cubic-bezier(.34,1.56,.64,1)}
.toast.on{transform:none}
.spinner{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,.2);
  border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:600px){
  .content{padding:14px 10px}
  .card{padding:16px}
  .stats-row{grid-template-columns:repeat(2,1fr);gap:12px;border:none;padding:0;margin:0}
  .stat-item{border:none;padding:0}
  .file-name{max-width:160px}
}
.theme-toggle{display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none;padding:0 2px}
.theme-toggle input{position:absolute;opacity:0;width:0;height:0}
.toggle-track{width:38px;height:21px;background:var(--border2);border-radius:12px;position:relative;
  transition:background .2s;flex-shrink:0;border:1px solid var(--border)}
.toggle-thumb{position:absolute;top:2px;left:2px;width:15px;height:15px;background:var(--accent);
  border-radius:50%;transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.theme-toggle input:checked+.toggle-track{background:rgba(123,104,240,.25)}
.theme-toggle input:checked+.toggle-track .toggle-thumb{transform:translateX(17px)}
.theme-icon{font-size:14px;line-height:1}
/* 2x font scale */
body,button,input,select,textarea{font-size:28px!important}
.login-card h1{font-size:44px!important}
.login-card p,.card-desc,.err,.toast,.file-name,.drop-zone p{font-size:26px!important}
label,.card-title,.badge,.file-meta,.theme-icon{font-size:22px!important}
.stat-l,.new-badge{font-size:20px!important}
.stat-v{font-size:48px!important}
.sort-arr{font-size:18px!important}
.sync-ts,.filter-sel,.filter-search,.filter-count,.pg-btn,.pg-info{font-size:24px!important}
.btn,.btn-del,.btn-g,.btn-p,.btn-green{font-size:26px!important}
.login-icon{font-size:52px!important}
thead th,td,.t-id,.t-name,.t-desc,.badge{font-size:20px!important;font-weight:400!important}
thead th{font-weight:600!important}
</style>
</head>
<body>

<div id="loginPage">
  <div class="login-card">
    <div class="login-icon">🖥</div>
    <h1>GLPI — Панель</h1>
    <p>Корпоративный агент</p>
    <form id="loginForm">
      <div class="fg"><label>Логин</label>
        <input type="text" id="lu" autocomplete="username"></div>
      <div class="fg"><label>Пароль</label>
        <input type="password" id="lp" autocomplete="current-password"></div>
      <button type="submit" class="btn btn-p btn-full" id="loginBtn">Войти</button>
      <div class="err" id="loginErr"></div>
    </form>
  </div>
</div>

<div id="app">
  <div class="topbar">
    <div class="brand">🖥 GLPI — Панель<span class="new-badge" id="newBadge" style="display:none"></span></div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn btn-g" id="syncNowBtn" onclick="requestSync()" style="padding:5px 12px;font-size:12px">↺ Синхронизировать</button>
      <a href="form" class="btn btn-p" style="padding:6px 14px;font-size:12px;text-decoration:none">+ Создать заявку</a>
      <label class="theme-toggle" title="Светлая / тёмная тема">
        <input type="checkbox" id="themeToggle" onchange="applyTheme(this.checked)">
        <span class="toggle-track"><span class="toggle-thumb"></span></span>
        <span class="theme-icon" id="themeIcon">🌙</span>
      </label>
      <button class="btn btn-g" onclick="doLogout()" style="padding:5px 12px;font-size:12px">Выйти</button>
    </div>
  </div>
  <div class="content">

    <!-- Заявки -->
    <div class="card">
      <div class="card-title">Заявки</div>
      <div class="stats-row" id="ticketStats" style="display:none">
        <div class="stat-item"><div class="stat-l">Всего</div><div class="stat-v" id="stTotal">—</div></div>
        <div class="stat-item"><div class="stat-l">Новые</div><div class="stat-v" style="color:var(--accent)" id="stNew">—</div></div>
        <div class="stat-item"><div class="stat-l">В работе</div><div class="stat-v" style="color:var(--blue)" id="stProc">—</div></div>
        <div class="stat-item"><div class="stat-l">Ожидание</div><div class="stat-v" style="color:var(--yellow)" id="stPend">—</div></div>
      </div>
      <div class="sync-info">
        <span class="sync-ts" id="syncTs">Нет данных от агента</span>
        <span style="font-size:11px;color:var(--muted)">Синхронизация каждые 30 мин</span>
      </div>
      <div class="filters">
        <input class="filter-search" id="fSearch" placeholder="Поиск по теме / описанию..." oninput="onFilter()">
        <select class="filter-sel" id="fStatus" onchange="onFilter()">
          <option value="">Все статусы</option>
          <option value="1">Новая</option>
          <option value="2">В обработке</option>
          <option value="3">Запланирована</option>
          <option value="4">Ожидание</option>
        </select>
        <span class="filter-count" id="filterCount"></span>
      </div>
      <div class="tbl-w">
        <table>
          <thead><tr>
            <th class="sortable" data-col="id" style="width:60px"># <span class="sort-arr"></span></th>
            <th class="sortable" data-col="name">Тема <span class="sort-arr"></span></th>
            <th class="sortable" data-col="content">Описание <span class="sort-arr"></span></th>
            <th class="sortable" data-col="status">Статус <span class="sort-arr"></span></th>
            <th class="sortable" data-col="priority">Приоритет <span class="sort-arr"></span></th>
            <th class="sortable" data-col="location">Локация <span class="sort-arr"></span></th>
            <th class="sortable" data-col="requester">Заявитель <span class="sort-arr"></span></th>
            <th class="sortable" data-col="date">Дата <span class="sort-arr"></span></th>
          </tr></thead>
          <tbody id="ticketTbody">
            <tr><td colspan="8"><div class="empty-tbl">Агент ещё не отправил данные</div></td></tr>
          </tbody>
        </table>
        <div class="pager" id="pager" style="display:none">
          <button class="pg-btn" id="pgPrev" onclick="goPage(-1)">← Пред.</button>
          <button class="pg-btn" id="pgNext" onclick="goPage(1)">След. →</button>
          <span class="pg-info" id="pgInfo"></span>
        </div>
      </div>
    </div>

    <!-- Агент -->
    <div class="card">
      <div class="card-title">Агент мониторинга</div>
      <div class="card-desc">
        Скачай агент, установи Python на корпоративный ПК и запусти <code>start.bat</code>.<br>
        Агент будет каждые 30 минут передавать заявки GLPI (твои + локации RU0974, RU0943, RU0975) на эту панель.
      </div>
      <div class="agent-row">
        <button class="btn btn-green" id="dlAgentBtn" onclick="window.location.href=BASE+'api/agent'" disabled>
          Скачать агент (ZIP)
        </button>
        <span class="agent-info" id="agentInfo">Загрузка...</span>
      </div>
    </div>

    <!-- Загрузка файлов -->
    <div class="card">
      <div class="card-title">Файлы</div>
      <div class="drop-zone" id="dropZone" onclick="document.getElementById('fileIn').click()">
        <div class="drop-icon">📂</div>
        <p>Нажми или перетащи файлы сюда</p>
        <p style="font-size:11px;margin-top:2px;color:var(--muted)">До 50 МБ, любые форматы</p>
      </div>
      <input type="file" id="fileIn" multiple style="display:none" onchange="uploadFiles(this.files)">
      <div class="progress-bar" id="progressBar"><div class="progress-fill" id="progressFill"></div></div>
      <div class="file-list-wrap" id="fileList"></div>
    </div>

  </div>
</div>

<div id="toasts"></div>

<script>
function applyTheme(light){
  document.documentElement.setAttribute('data-theme',light?'light':'dark');
  localStorage.setItem('glpi-theme',light?'light':'dark');
  const ic=document.getElementById('themeIcon');
  if(ic)ic.textContent=light?'☀️':'🌙';
}
(function(){
  const l=localStorage.getItem('glpi-theme')==='light';
  document.documentElement.setAttribute('data-theme',l?'light':'dark');
  const cb=document.getElementById('themeToggle');
  if(cb){cb.checked=l;}
  const ic=document.getElementById('themeIcon');
  if(ic)ic.textContent=l?'☀️':'🌙';
})();
const BASE = location.pathname.replace(/\/?$/, '/');
const $ = id => document.getElementById(id);

function toast(msg, err=false){
  const el=document.createElement('div'); el.className='toast';
  el.textContent=(err?'❌ ':'✅ ')+msg; $('toasts').appendChild(el);
  requestAnimationFrame(()=>el.classList.add('on'));
  setTimeout(()=>{el.classList.remove('on');setTimeout(()=>el.remove(),350);},3500);
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmtSize(b){if(b<1024)return b+' Б';if(b<1048576)return(b/1024).toFixed(1)+' КБ';return(b/1048576).toFixed(1)+' МБ';}
function fmtDate(ts){
  if(!ts)return'—';
  const d=new Date(typeof ts==='string'?ts:ts*1000);
  return d.toLocaleDateString('ru')+' '+d.toTimeString().slice(0,5);
}

// ── Auth ──
$('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const btn=$('loginBtn'), err=$('loginErr');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span>'; err.classList.remove('on');
  try{
    const r=await fetch(BASE+'login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:$('lu').value,password:$('lp').value})});
    if(r.ok){showApp();}
    else{const d=await r.json().catch(()=>({}));err.textContent=d.error||'Неверный логин или пароль';err.classList.add('on');}
  }catch{err.textContent='Ошибка соединения';err.classList.add('on');}
  finally{btn.disabled=false;btn.textContent='Войти';}
});
async function doLogout(){
  await fetch(BASE+'api/logout',{method:'POST'});
  $('app').style.display='none'; $('loginPage').style.display='flex'; $('lp').value='';
}
function showApp(){
  $('loginPage').style.display='none'; $('app').style.display='block';
  loadAgentInfo(); loadTickets(); loadFiles();
}

// ── Tickets ──
const STATUS_MAP = {
  1:{label:'Новая',col:'var(--accent)'},
  2:{label:'В обработке',col:'var(--blue)'},
  3:{label:'Запланирована',col:'var(--blue)'},
  4:{label:'Ожидание',col:'var(--yellow)'},
  5:{label:'Решена',col:'var(--green)'},
  6:{label:'Закрыта',col:'var(--muted)'}
};
const PRIO_MAP = {
  1:{label:'Очень низкий'},2:{label:'Низкий'},3:{label:'Средний'},
  4:{label:'Высокий'},5:{label:'Очень высокий'},6:{label:'Критический'}
};

let allTickets=[], knownIds=null, sortKey='id', sortAsc=false, curPage=0;
let fStatus='', fSearch='';
const PAGE_SIZE=50;

function onFilter(){
  fStatus=$('fStatus').value;
  fSearch=$('fSearch').value.trim(); curPage=0; renderTickets();
}
function goPage(d){curPage+=d; renderTickets();}

document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('th.sortable').forEach(th=>{
    th.addEventListener('click',()=>{
      const col=th.dataset.col;
      if(sortKey===col){sortAsc=!sortAsc;}else{sortKey=col;sortAsc=(col!=='id');}
      renderTickets();
    });
  });
});

function getFiltered(){
  return allTickets.filter(t=>{
    if(fStatus && String(t.status)!==fStatus) return false;

    if(fSearch){
      const q=fSearch.toLowerCase();
      const nm=(t.name||'').toLowerCase();
      const dc=(t.content||'').replace(/<[^>]*>/g,'').toLowerCase();
      if(!nm.includes(q)&&!dc.includes(q)) return false;
    }
    return true;
  });
}
function getSorted(arr){
  return [...arr].sort((a,b)=>{
    let av=a[sortKey]??'', bv=b[sortKey]??'';
    if(sortKey==='id'||sortKey==='status'||sortKey==='priority')
      return sortAsc?Number(av)-Number(bv):Number(bv)-Number(av);
    const r=String(av).localeCompare(String(bv),'ru');
    return sortAsc?r:-r;
  });
}

function renderTickets(){
  const filtered=getFiltered(), sorted=getSorted(filtered);
  const total=sorted.length, pageCount=Math.ceil(total/PAGE_SIZE)||1;
  curPage=Math.max(0,Math.min(curPage,pageCount-1));
  const start=curPage*PAGE_SIZE, slice=sorted.slice(start,start+PAGE_SIZE);

  $('filterCount').textContent=filtered.length!==allTickets.length
    ?`Найдено: ${filtered.length} из ${allTickets.length}`
    :`Всего: ${allTickets.length}`;

  const tb=$('ticketTbody');
  if(!slice.length){
    tb.innerHTML='<tr><td colspan="8"><div class="empty-tbl">Ничего не найдено</div></td></tr>';
  } else {
    tb.innerHTML=slice.map(t=>{
      const st=STATUS_MAP[t.status]||{label:'?',col:'var(--muted)'};
      const pr=PRIO_MAP[t.priority]||{label:'—'};
      const ticketUrl=`${BASE}ticket/${t.id}`;
      const rawDesc=(t.content||'').replace(/<[^>]*>/g,'').replace(/&[a-z#0-9]+;/g,' ').trim();
      return `<tr>
        <td><span class="t-id"><a href="${ticketUrl}">${t.id}</a></span></td>
        <td><span class="t-name" title="${esc(t.name)}">${esc(t.name)}</span></td>
        <td><span class="t-desc" title="${esc(rawDesc)}">${esc(rawDesc)||'—'}</span></td>
        <td><span style="color:${st.col}">${st.label}</span></td>
        <td>${pr.label}</td>
        <td>${esc(t.location||'—')}</td>
        <td>${esc(t.requester||'—')}</td>
        <td>${fmtDate(t.date)}</td>
      </tr>`;
    }).join('');
  }

  // Pagination
  const pager=$('pager');
  if(pageCount>1){
    pager.style.display='';
    $('pgPrev').disabled=curPage===0;
    $('pgNext').disabled=curPage>=pageCount-1;
    $('pgInfo').textContent=`Стр. ${curPage+1} / ${pageCount} · ${start+1}–${Math.min(start+PAGE_SIZE,total)} из ${total}`;
  } else { pager.style.display='none'; }

  // Sort arrows
  document.querySelectorAll('th.sortable').forEach(th=>{
    th.classList.remove('sort-asc','sort-desc');
    if(th.dataset.col===sortKey) th.classList.add(sortAsc?'sort-asc':'sort-desc');
  });
}

async function loadTickets(){
  try{
    const d=await fetch(BASE+'api/tickets').then(r=>r.json());
    const tickets=d.tickets||[];
    const syncedAt=d.synced_at||0;

    // New ticket detection
    const newIds=new Set(tickets.map(t=>t.id));
    if(knownIds!==null){
      const cnt=[...newIds].filter(id=>!knownIds.has(id)).length;
      if(cnt>0){
        const b=$('newBadge'); b.textContent='+'+cnt; b.style.display='';
        toast(`Новых заявок: ${cnt}`);
      }
    }
    knownIds=newIds;
    allTickets=tickets;

    // Stats
    if(tickets.length){
      $('ticketStats').style.display='';
      $('stTotal').textContent=tickets.length;
      $('stNew').textContent=tickets.filter(t=>t.status==1).length;
      $('stProc').textContent=tickets.filter(t=>t.status==2||t.status==3).length;
      $('stPend').textContent=tickets.filter(t=>t.status==4).length;
    }
    $('syncTs').textContent=syncedAt
      ?'Последняя синхронизация: '+fmtDate(syncedAt)
      :'Нет данных от агента';

    renderTickets();
  }catch{}
}

async function requestSync(){
  const btn=$('syncNowBtn');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
  try{
    await fetch(BASE+'api/request-sync',{method:'POST'});
    toast('Запрос отправлен — агент синхронизирует в течение ~1 мин');
    setTimeout(loadTickets, 65000);
  }catch{toast('Ошибка',true);}
  setTimeout(()=>{btn.disabled=false;btn.innerHTML='↺ Синхронизировать';},5000);
}

// ── Agent ──
async function loadAgentInfo(){
  try{
    const d=await fetch(BASE+'api/agent/info').then(r=>r.json());
    const btn=$('dlAgentBtn'), info=$('agentInfo');
    if(d.exists){btn.disabled=false;info.textContent=d.name+' · '+fmtSize(d.size);}
    else{btn.disabled=true;info.textContent='Агент ещё собирается...';}
  }catch{}
}

// ── Upload ──
const dz=$('dropZone');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag');});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');uploadFiles(e.dataTransfer.files);});

async function uploadFiles(files){
  if(!files||!files.length)return;
  const bar=$('progressBar'),fill=$('progressFill');
  bar.style.display='block'; fill.style.width='10%';
  const fd=new FormData();
  for(const f of files) fd.append('files',f);
  try{
    fill.style.width='40%';
    const r=await fetch(BASE+'api/upload',{method:'POST',body:fd});
    fill.style.width='90%';
    const d=await r.json();
    if(d.saved&&d.saved.length){toast(`Загружено: ${d.saved.map(f=>f.name).join(', ')}`);loadFiles();}
    else toast('Ничего не загружено',true);
  }catch{toast('Ошибка загрузки',true);}
  finally{fill.style.width='100%';setTimeout(()=>{bar.style.display='none';fill.style.width='0';},600);}
  $('fileIn').value='';
}

async function loadFiles(){
  try{
    const files=await fetch(BASE+'api/files').then(r=>r.json());
    const list=$('fileList');
    if(!files.length){list.innerHTML='<div class="empty-files">Загруженных файлов нет</div>';return;}
    list.innerHTML=files.map(f=>`
      <div class="file-item">
        <div><div class="file-name">${esc(f.name)}</div>
        <div class="file-meta">${fmtSize(f.size)} · ${fmtDate(f.mtime)}</div></div>
        <div class="file-actions">
          <a class="btn-del" href="${BASE}api/files/${encodeURIComponent(f.name)}" download="${esc(f.name)}">Скачать</a>
          <button class="btn-del" onclick="deleteFile('${esc(f.name)}')">Удалить</button>
        </div>
      </div>`).join('');
  }catch{}
}
async function deleteFile(name){
  if(!confirm(`Удалить "${name}"?`))return;
  await fetch(BASE+`api/files/${encodeURIComponent(name)}`,{method:'DELETE'});
  toast(`Файл удалён`); loadFiles();
}

// ── Init ──
(async()=>{
  try{
    const r=await fetch(BASE+'api/tickets');
    if(r.ok){showApp();}
    else $('loginPage').style.display='flex';
  }catch{$('loginPage').style.display='flex';}
})();

setInterval(()=>{if($('app').style.display!=='none')loadTickets();}, 60000);
</script>
</body>
</html>"""

TICKET_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Заявка — GLPI</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#09090f;--card:#101018;--card2:#181826;--border:#21213a;--border2:#2c2c48;
  --text:#e0e0f2;--muted:#62628a;--accent:#7b68f0;--glow:rgba(123,104,240,.2);
  --green:#28c97a;--red:#e85555;--yellow:#e8a82a;--blue:#4a9ef0;--r:10px;--rs:6px
}
[data-theme="light"]{
  --bg:#f0f0f8;--card:#ffffff;--card2:#e8e8f4;--border:#d0d0e5;--border2:#b8b8d8;
  --text:#1a1a30;--muted:#7070a0
}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5;min-height:100vh}
.topbar{background:var(--card);border-bottom:1px solid var(--border);padding:0 24px;height:54px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;gap:12px}
.brand{font-weight:700;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.content{max-width:70%;margin:0 auto;padding:24px 20px;display:flex;flex-direction:column;gap:18px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:20px}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:14px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border:none;border-radius:var(--rs);font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;white-space:nowrap;background:transparent;color:var(--text);text-decoration:none}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}
.btn-p{background:var(--accent);color:#fff;box-shadow:0 4px 14px var(--glow)}
.btn-p:hover:not(:disabled){background:#6a5ae0}
.btn-g{background:transparent;color:var(--muted);border:1px solid var(--border)}
.btn-g:hover:not(:disabled){color:var(--text);border-color:var(--border2);background:var(--card2)}
.btn-danger{background:#3a1010;color:var(--red);border:1px solid rgba(232,85,85,.35)}
.btn-danger:hover:not(:disabled){background:#4a1515}
.btn-ok{background:#0d2e1a;color:var(--green);border:1px solid rgba(40,201,122,.35)}
.btn-ok:hover:not(:disabled){background:#123520}
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:4px;font-size:12px;font-weight:600}
.badge-new{background:rgba(123,104,240,.15);color:#9b84f5}
.badge-proc{background:rgba(74,158,240,.15);color:var(--blue)}
.badge-pend{background:rgba(232,168,42,.12);color:var(--yellow)}
.badge-done{background:rgba(40,201,122,.1);color:var(--green)}
.badge-closed{background:rgba(98,98,138,.12);color:var(--muted)}
/* Meta */
.meta-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.ml{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:3px}
.mv{font-size:13px;color:var(--text)}
/* Edit row */
.edit-row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.fg{display:flex;flex-direction:column;gap:5px}
.fg label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.edit-sel,.edit-inp{background:var(--card2);border:1px solid var(--border2);color:var(--text);
  padding:7px 10px;border-radius:var(--rs);font-size:13px;outline:none;transition:border-color .15s;width:100%}
.edit-sel:focus,.edit-inp:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
/* Autocomplete */
.ac-wrap{position:relative}
.ac-drop{position:absolute;top:calc(100% + 4px);left:0;right:0;background:var(--card2);
  border:1px solid var(--border2);border-radius:var(--rs);z-index:200;max-height:200px;overflow-y:auto;display:none}
.ac-drop.open{display:block}
.ac-item{padding:8px 12px;cursor:pointer;font-size:13px;border-bottom:1px solid var(--border)}
.ac-item:last-child{border:none}
.ac-item:hover{background:rgba(123,104,240,.1);color:var(--accent)}
/* Description */
.desc-text{color:var(--text);font-size:13px;line-height:1.7;white-space:pre-wrap;word-break:break-word}
/* Timeline */
.followup{background:var(--card2);border:1px solid var(--border);border-radius:var(--rs);padding:14px;margin-bottom:10px}
.followup:last-child{margin:0}
.followup-mine{border-color:rgba(123,104,240,.3);background:rgba(123,104,240,.04)}
.followup-solution{border-color:rgba(40,201,122,.35);background:rgba(40,201,122,.04)}
.followup-meta{display:flex;justify-content:space-between;margin-bottom:8px;font-size:12px;flex-wrap:wrap;gap:4px}
.followup-author{font-weight:600}
.followup-date{color:var(--muted)}
.followup-type{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--green);margin-bottom:4px}
.followup-content{font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.empty-tl{text-align:center;padding:32px;color:var(--muted);font-size:13px}
/* Reply */
.reply-ta{width:100%;background:var(--card2);border:1px solid var(--border2);color:var(--text);
  border-radius:var(--rs);padding:12px;font-size:13px;font-family:inherit;
  resize:vertical;min-height:100px;outline:none;transition:border-color .15s}
.reply-ta:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--glow)}
.tpl-list{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.tpl-btn{background:var(--card2);border:1px solid var(--border2);color:var(--muted);
  padding:5px 12px;border-radius:var(--rs);cursor:pointer;font-size:13px;font-family:inherit;
  transition:all .15s;white-space:nowrap;line-height:1.4}
.tpl-btn:hover{border-color:var(--accent);color:var(--text)}
/* Toast */
#toasts{position:fixed;bottom:22px;right:22px;display:flex;flex-direction:column;gap:8px;z-index:9999}
.toast{background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);
  padding:11px 16px;font-size:13px;font-weight:500;box-shadow:0 8px 28px rgba(0,0,0,.4);
  display:flex;align-items:center;gap:8px;transform:translateX(130%);
  transition:transform .3s cubic-bezier(.34,1.56,.64,1)}
.toast.on{transform:none}
.spinner{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,.2);
  border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.theme-toggle{display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none;padding:0 2px}
.theme-toggle input{position:absolute;opacity:0;width:0;height:0}
.toggle-track{width:38px;height:21px;background:var(--border2);border-radius:12px;position:relative;
  transition:background .2s;flex-shrink:0;border:1px solid var(--border)}
.toggle-thumb{position:absolute;top:2px;left:2px;width:15px;height:15px;background:var(--accent);
  border-radius:50%;transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.theme-toggle input:checked+.toggle-track{background:rgba(123,104,240,.25)}
.theme-toggle input:checked+.toggle-track .toggle-thumb{transform:translateX(17px)}
.theme-icon{font-size:14px;line-height:1}
/* 2x font scale */
body,button,input,select,textarea{font-size:28px!important}
.brand,.btn,.btn-g,.btn-p,.btn-danger,.btn-ok,.btn-del{font-size:26px!important}
.card-title,.badge,.ml{font-size:22px!important}
.stat-l,thead th{font-size:20px!important}
.stat-v{font-size:48px!important}
.sort-arr{font-size:18px!important}
.mv,.desc-text,.followup-content,.reply-ta,.ac-item,.edit-sel,.edit-inp{font-size:26px!important}
.followup-meta,.followup-author,.followup-date,.followup-type{font-size:22px!important}
.toast{font-size:26px!important}
</style>
</head>
<body>

<div class="topbar">
  <div style="display:flex;align-items:center;gap:10px;min-width:0;flex:1">
    <a id="backBtn" href="#" class="btn btn-g" style="flex-shrink:0;font-size:12px;padding:5px 12px">← Назад</a>
    <div class="brand" id="ticketTitle">Загрузка...</div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;flex-shrink:0">
    <span id="statusBadge"></span>
    <label class="theme-toggle" title="Светлая / тёмная тема">
      <input type="checkbox" id="themeToggle" onchange="applyTheme(this.checked)">
      <span class="toggle-track"><span class="toggle-thumb"></span></span>
      <span class="theme-icon" id="themeIcon">🌙</span>
    </label>
    <a id="glpiLink" href="#" target="_blank" rel="noopener" class="btn btn-g" style="font-size:12px;padding:5px 12px">↗ GLPI</a>
  </div>
</div>

<div class="content">

  <div class="card">
    <div class="card-title">Детали</div>
    <div class="meta-grid" id="metaGrid"></div>
    <div class="edit-row">
      <div class="fg" style="min-width:160px">
        <label>Приоритет</label>
        <select class="edit-sel" id="ePriority">
          <option value="1">Очень низкий</option>
          <option value="2">Низкий</option>
          <option value="3">Средний</option>
          <option value="4">Высокий</option>
          <option value="5">Очень высокий</option>
          <option value="6">Критический</option>
        </select>
      </div>
      <div class="fg" style="flex:1;min-width:180px">
        <label>Локация</label>
        <div class="ac-wrap">
          <input class="edit-inp" id="eLocation" placeholder="Начни вводить..." autocomplete="off" oninput="acSearch('loc')">
          <div class="ac-drop" id="locDrop"></div>
        </div>
        <input type="hidden" id="eLocationId">
      </div>
      <div class="fg" style="flex:1;min-width:200px">
        <label>Исполнитель</label>
        <div class="ac-wrap">
          <input class="edit-inp" id="eAssignee" placeholder="Начни вводить..." autocomplete="off" oninput="acSearch('user')">
          <div class="ac-drop" id="userDrop"></div>
        </div>
        <input type="hidden" id="eAssigneeId">
      </div>
      <div class="fg" style="flex-direction:row;gap:8px;align-items:flex-end">
        <button class="btn btn-p" id="saveBtn" onclick="saveChanges()">Сохранить</button>
        <button class="btn btn-ok" id="assignMeBtn" onclick="assignToMe()" title="Назначить на ekuznecov + Global Onsite Support">Назначить мне</button>
        <button class="btn btn-danger" id="closeBtn" onclick="closeTicket()">Закрыть заявку</button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Описание</div>
    <div class="desc-text" id="descContent">—</div>
  </div>

  <div class="card">
    <div class="card-title">Переписка</div>
    <div id="timeline"><div class="empty-tl">Загрузка...</div></div>
  </div>

  <div class="card">
    <div class="card-title">Добавить ответ</div>
    <div class="tpl-list" id="tplList"></div>
    <textarea class="reply-ta" id="replyText" placeholder="Введите текст ответа..."></textarea>
    <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
      <button class="btn btn-p" id="replyBtn" onclick="sendReply(false)">Отправить</button>
      <button class="btn btn-ok" id="resolveBtn" onclick="sendReply(true)">Решить и закрыть</button>
    </div>
  </div>

</div>
<div id="toasts"></div>

<script>
function applyTheme(light){
  document.documentElement.setAttribute('data-theme',light?'light':'dark');
  localStorage.setItem('glpi-theme',light?'light':'dark');
  const ic=document.getElementById('themeIcon');
  if(ic)ic.textContent=light?'☀️':'🌙';
}
(function(){
  const l=localStorage.getItem('glpi-theme')==='light';
  document.documentElement.setAttribute('data-theme',l?'light':'dark');
  const cb=document.getElementById('themeToggle');
  if(cb){cb.checked=l;}
  const ic=document.getElementById('themeIcon');
  if(ic)ic.textContent=l?'☀️':'🌙';
})();
// ── Шаблоны ответов ──────────────────────────────────────────────────────────
const TEMPLATES = [
  {
    name: 'Провайдер восстановлен',
    text: 'The provider\\'s work has resumed. If you do not have access to network resources, you must restart your computer.'
  },
  {
    name: 'Принтер подключён',
    text: 'The printer has been successfully configured and connected.'
  },
];

function initTemplates(){
  const list=document.getElementById('tplList');
  if(!list||!TEMPLATES.length)return;
  list.innerHTML=TEMPLATES.map((t,i)=>
    `<button class="tpl-btn" onclick="useTemplate(${i})">${esc(t.name)}</button>`
  ).join('');
}
function useTemplate(i){
  const ta=document.getElementById('replyText');
  ta.value=TEMPLATES[i].text;
  ta.focus();
}

// GLPI credentials injected by server
const G = __GLPI_CREDS__;

const BASE = location.pathname.replace(/ticket\\/\\d+\\/?$/, '');
const TID  = parseInt(location.pathname.match(/\\/ticket\\/(\\d+)/)[1]);
document.getElementById('backBtn').href  = BASE;
document.getElementById('glpiLink').href = 'https://glpi.example.com/front/ticket.form.php?id=' + TID;

const STATUS_MAP = {
  1:{label:'Новая',cls:'badge-new'},2:{label:'В обработке',cls:'badge-proc'},
  3:{label:'Запланирована',cls:'badge-proc'},4:{label:'Ожидание',cls:'badge-pend'},
  5:{label:'Решена',cls:'badge-done'},6:{label:'Закрыта',cls:'badge-closed'}
};

function toast(msg, err=false){
  const el=document.createElement('div'); el.className='toast';
  el.textContent=(err?'❌ ':'✅ ')+msg; document.getElementById('toasts').appendChild(el);
  requestAnimationFrame(()=>el.classList.add('on'));
  setTimeout(()=>{el.classList.remove('on');setTimeout(()=>el.remove(),350);},3500);
}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmtDate(s){
  if(!s)return'—';
  const d=new Date(typeof s==='string'?s.replace(' ','T')+'Z':s);
  return d.toLocaleDateString('ru')+' '+d.toTimeString().slice(0,5);
}
function stripHtml(s){return(s||'').replace(/<[^>]*>/g,' ').replace(/&[a-z#0-9]+;/g,' ').replace(/\\s{2,}/g,' ').trim();}

// ── GLPI API (вызовы из браузера напрямую) ────────────────────────────────────
let SESSION = null;

async function glpiInit(){
  const r = await fetch(G.url+'/initSession', {headers:{
    'Authorization':'user_token '+G.user_token,
    'App-Token':G.app_token
  }});
  SESSION = (await r.json()).session_token;
}
function glpiH(extra={}){
  return {'Session-Token':SESSION,'App-Token':G.app_token,'Content-Type':'application/json',...extra};
}
async function glpiGet(path){
  const r=await fetch(G.url+path,{headers:glpiH()});
  if(!r.ok) throw new Error('GLPI '+r.status);
  const txt=await r.text(); return txt?JSON.parse(txt):null;
}
async function glpiPost(path,body){
  const r=await fetch(G.url+path,{method:'POST',headers:glpiH(),body:JSON.stringify(body)});
  if(!r.ok){const t=await r.text();throw new Error('GLPI '+r.status+': '+t.slice(0,150));}
  return await r.json();
}
async function glpiPut(path,body){
  const r=await fetch(G.url+path,{method:'PUT',headers:glpiH(),body:JSON.stringify(body)});
  if(!r.ok) throw new Error('GLPI '+r.status);
  return await r.json();
}
async function glpiDel(path){
  await fetch(G.url+path,{method:'DELETE',headers:glpiH()});
}
window.addEventListener('beforeunload',()=>{
  if(SESSION) navigator.sendBeacon(G.url+'/killSession',null);
});

// ── Данные ────────────────────────────────────────────────────────────────────
let usersMap={};

async function resolveUsers(ids){
  const out={};
  for(const uid of ids){
    if(!uid||uid===0)continue;
    try{
      const u=await glpiGet('/User/'+uid);
      out[uid]=(u.firstname+' '+u.realname).trim()||u.name||'#'+uid;
    }catch{out[uid]='#'+uid;}
  }
  return out;
}

async function load(){
  const tl=document.getElementById('timeline');
  try{
    await glpiInit();

    const [ticket, rawFollowups, rawSolutions, rawGroupTicket] = await Promise.all([
      glpiGet('/Ticket/'+TID+'?with_actors=1'),
      glpiGet('/Ticket/'+TID+'/ITILFollowup?range=0-100&sort=2&order=ASC').catch(()=>null),
      glpiGet('/Ticket/'+TID+'/ITILSolution?range=0-20').catch(()=>null),
      glpiGet('/Ticket/'+TID+'/Group_Ticket?range=0-10').catch(()=>null),
    ]);
    const followups    = Array.isArray(rawFollowups)?rawFollowups:[];
    const solutions    = Array.isArray(rawSolutions)?rawSolutions:[];
    const groupTickets = Array.isArray(rawGroupTicket)?rawGroupTicket:[];

    // Извлекаем пользователей из _actors (with_actors=1)
    const actors = ticket._actors || {};
    const actorFlat = Object.values(actors).flat();
    // Заявитель — тип 1, назначен — тип 3 (нестрогое сравнение)
    const reqActor  = actorFlat.find(a=>Number(a.type)==1);
    const asgnActor = actorFlat.find(a=>Number(a.type)==3);

    // Заявитель: из _actors или прямого поля тикета
    const reqUid  = (reqActor  && (reqActor.items_id  || reqActor.id))  || ticket.users_id_recipient || 0;
    const asgnUid = (asgnActor && (asgnActor.items_id || asgnActor.id)) || 0;

    const uids=new Set([reqUid, asgnUid, ...followups.map(f=>f.users_id), ...solutions.map(s=>s.users_id)].filter(Boolean));
    usersMap = await resolveUsers([...uids]);

    // Группа — берём первую запись из Group_Ticket (тип 2 или 3)
    const grpEntry = groupTickets.find(g=>Number(g.type)>=2) || groupTickets[0];
    let asgnGroupName='—';
    const grpId = grpEntry && grpEntry.groups_id;
    if(grpId){
      try{
        const g=await glpiGet('/Group/'+grpId);
        asgnGroupName=g.completename||g.name||'#'+grpId;
      }catch{asgnGroupName='#'+grpId;}
    }

    // Resolve location
    let locName='', locId=ticket.locations_id||0;
    if(locId>0){
      try{const l=await glpiGet('/Location/'+locId);locName=l.completename||l.name||'';}catch{}
    }

    // Render title & status
    const title='#'+TID+' — '+(ticket.name||'');
    document.title=title;
    document.getElementById('ticketTitle').textContent=title;
    const st=STATUS_MAP[ticket.status]||{label:'?',cls:'badge-closed'};
    document.getElementById('statusBadge').innerHTML='<span class="badge '+st.cls+'">'+st.label+'</span>';

    // Edit controls
    document.getElementById('ePriority').value=ticket.priority||3;
    if(locName){document.getElementById('eLocation').value=locName;document.getElementById('eLocationId').value=locId;}
    if(asgnUid){
      document.getElementById('eAssignee').value=usersMap[asgnUid]||'';
      document.getElementById('eAssigneeId').value=asgnUid;
    }

    const reqName  = reqUid  ? (usersMap[reqUid]  || '#'+reqUid)  : '—';
    const asgnName = asgnUid ? (usersMap[asgnUid] || '#'+asgnUid) : '—';
    document.getElementById('metaGrid').innerHTML=`
      <div><div class="ml">Заявитель</div><div class="mv">${esc(reqName)}</div></div>
      <div><div class="ml">Назначен</div><div class="mv">${esc(asgnName)}</div></div>
      <div><div class="ml">Группа</div><div class="mv">${esc(asgnGroupName)}</div></div>
      <div><div class="ml">Создана</div><div class="mv">${fmtDate(ticket.date_creation||ticket.date)}</div></div>
      <div><div class="ml">Изменена</div><div class="mv">${fmtDate(ticket.date_mod)}</div></div>
    `;

    // Description
    document.getElementById('descContent').textContent=stripHtml(ticket.content)||'—';

    // Timeline
    renderTimeline(followups,solutions);

  }catch(e){
    tl.innerHTML='<div class="empty-tl">Ошибка: '+esc(e.message)+'</div>';
    toast('Ошибка загрузки: '+e.message,true);
  }
}

function renderTimeline(followups,solutions){
  const el=document.getElementById('timeline');
  const all=[
    ...followups.map(f=>({...f,_t:'followup'})),
    ...solutions.map(s=>({...s,_t:'solution'}))
  ].sort((a,b)=>new Date((a.date_creation||a.date||'').replace(' ','T'))-new Date((b.date_creation||b.date||'').replace(' ','T')));
  if(!all.length){el.innerHTML='<div class="empty-tl">Переписки пока нет</div>';return;}
  el.innerHTML=all.map(item=>{
    const uid=item.users_id;
    const name=usersMap[uid]||(uid?'Пользователь #'+uid:'Система');
    const isSol=item._t==='solution', isMine=uid==16343;
    return `<div class="followup${isMine?' followup-mine':''}${isSol?' followup-solution':''}">
      ${isSol?'<div class="followup-type">Решение</div>':''}
      <div class="followup-meta"><span class="followup-author">${esc(name)}</span><span class="followup-date">${fmtDate(item.date_creation||item.date)}</span></div>
      <div class="followup-content">${esc(stripHtml(item.content))||'—'}</div>
    </div>`;
  }).join('');
}

// ── Автодополнение ────────────────────────────────────────────────────────────
const acT={};
async function acSearch(type){
  clearTimeout(acT[type]);
  const inp=document.getElementById(type==='user'?'eAssignee':'eLocation');
  const drop=document.getElementById(type==='user'?'userDrop':'locDrop');
  const q=inp.value.trim();
  if(q.length<2){drop.classList.remove('open');return;}
  acT[type]=setTimeout(async()=>{
    try{
      const path=type==='user'
        ?'/User?searchText[name]='+encodeURIComponent(q)+'&range=0-15&sort=1&order=ASC'
        :'/Location?searchText[completename]='+encodeURIComponent(q)+'&range=0-15';
      const items=await glpiGet(path)||[];
      if(!items.length){drop.innerHTML='<div class="ac-item" style="color:var(--muted)">Ничего</div>';drop.classList.add('open');return;}
      drop.innerHTML=items.map(i=>{
        const nm=type==='user'?((i.firstname+' '+i.realname).trim()||i.name||''):(i.completename||i.name||'');
        return `<div class="ac-item" data-id="${i.id}" data-name="${esc(nm)}">${esc(nm)}</div>`;
      }).join('');
      drop.classList.add('open');
      drop.querySelectorAll('.ac-item').forEach(el=>el.addEventListener('click',()=>{
        inp.value=el.dataset.name;
        document.getElementById(type==='user'?'eAssigneeId':'eLocationId').value=el.dataset.id;
        drop.classList.remove('open');
      }));
    }catch{}
  },300);
}
document.addEventListener('click',e=>{
  if(!e.target.closest('.ac-wrap')) document.querySelectorAll('.ac-drop').forEach(d=>d.classList.remove('open'));
});

// ── Действия ─────────────────────────────────────────────────────────────────
async function saveChanges(){
  const inp={id:TID,priority:parseInt(document.getElementById('ePriority').value)};
  const locId=document.getElementById('eLocationId').value;
  const uid  =document.getElementById('eAssigneeId').value;
  if(locId) inp.locations_id=parseInt(locId);
  if(uid)   inp._users_id_assign=parseInt(uid);
  const btn=document.getElementById('saveBtn');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
  try{
    await glpiPut('/Ticket/'+TID,{input:inp});
    toast('Изменения сохранены');
  }catch(e){toast(e.message,true);}
  btn.disabled=false; btn.textContent='Сохранить';
}

async function assignToMe(){
  const btn=document.getElementById('assignMeBtn');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
  try{
    const [users, groups, curGroups, curUsers] = await Promise.all([
      glpiGet('/User?searchText[name]=ekuznecov&range=0-5'),
      glpiGet('/Group?searchText[completename]=Global+Onsite&range=0-20'),
      glpiGet('/Ticket/'+TID+'/Group_Ticket?range=0-20').catch(()=>null),
      glpiGet('/Ticket/'+TID+'/User_Ticket?range=0-20').catch(()=>null),
    ]);
    const user=Array.isArray(users)&&users.find(u=>(u.name||'').toLowerCase().includes('ekuznecov'));
    if(!user) throw new Error('Пользователь ekuznecov не найден');
    const group=Array.isArray(groups)&&groups.find(g=>(g.completename||g.name||'').toLowerCase().includes('onsite'));
    if(!group) throw new Error('Группа Onsite не найдена');
    // Удаляем все существующие назначения
    const delGroup = Array.isArray(curGroups)?curGroups.map(g=>glpiDel('/Group_Ticket/'+g.id).catch(()=>{})):[];
    const delUser  = Array.isArray(curUsers) ?curUsers.map(u=>glpiDel('/User_Ticket/'+u.id).catch(()=>{})):[];
    await Promise.all([...delGroup,...delUser]);
    // Назначаем нового пользователя и группу
    await glpiPut('/Ticket/'+TID,{input:{id:TID,_users_id_assign:user.id,_groups_id_assign:group.id}});
    toast('Назначено: Kuznecov Egor / '+((group.completename||group.name||'').split(' - ').pop()));
    await load();
  }catch(e){toast(e.message,true);}
  btn.disabled=false; btn.textContent='Назначить мне';
}

async function closeTicket(){
  if(!confirm('Закрыть заявку #'+TID+'?'))return;
  const btn=document.getElementById('closeBtn');
  btn.disabled=true;
  try{
    await glpiPut('/Ticket/'+TID,{input:{id:TID,status:6}});
    toast('Заявка закрыта');
    document.getElementById('statusBadge').innerHTML='<span class="badge badge-closed">Закрыта</span>';
  }catch(e){toast(e.message,true);}
  btn.disabled=false;
}

async function translateToEn(text){
  if(!/[а-яёА-ЯЁ]/.test(text)) return text;
  try{
    const r=await fetch('https://api.mymemory.translated.net/get?q='+encodeURIComponent(text)+'&langpair=ru|en');
    const d=await r.json();
    if(d.responseStatus===200&&d.responseData&&d.responseData.translatedText)
      return d.responseData.translatedText;
  }catch{}
  return text;
}

async function sendReply(resolve){
  const raw=document.getElementById('replyText').value.trim();
  if(!raw){toast('Введите текст',true);return;}
  const btn=document.getElementById(resolve?'resolveBtn':'replyBtn');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
  try{
    const text=await translateToEn(raw);
    if(resolve){
      await glpiPost('/ITILSolution',{input:{items_id:TID,itemtype:'Ticket',content:text}});
      toast('Заявка решена и закрыта');
    }else{
      await glpiPost('/ITILFollowup',{input:{items_id:TID,itemtype:'Ticket',content:text}});
      toast('Ответ отправлен');
    }
    document.getElementById('replyText').value='';
    await load();
  }catch(e){toast(e.message,true);}
  btn.disabled=false;
  btn.textContent=resolve?'Решить и закрыть':'Отправить';
}

initTemplates();
load();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8087, debug=False)
