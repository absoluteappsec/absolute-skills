"""Server-rendered fixture app (Flask). Deterministic, in-memory state."""
import secrets
from html import escape

from flask import Flask, Response, abort, jsonify, redirect, request

USERS = {
    "admin": {"password": "admin-pw-Fx1", "role": "admin"},
    "alice": {"password": "alice-pw-Fx2", "role": "user"},
}
SESSION_LIMIT = 200
PAGE_SIZE = 10


def create_app() -> Flask:
    app = Flask(__name__)
    state: dict = {}

    def reset():
        state["projects"] = {
            i: {"id": i, "name": f"project-{i:02d}", "visibility": "public", "due": "2030-01-01"}
            for i in range(1, 26)
        }
        state["next_id"] = 26
        state["sessions"] = {}
        state["passwords"] = {u: v["password"] for u, v in USERS.items()}

    reset()

    def page(title: str, body: str, user: str | None = None, status: int = 200) -> Response:
        admin_link = '<a href="/admin">Admin</a> ' if user and USERS[user]["role"] == "admin" else ""
        nav = ('<a href="/">Home</a> <a href="/projects">Projects</a> <a href="/about">About</a> '
               + admin_link
               + ('<a href="/account/password">Account</a> <a href="/logout">Logout</a>'
                  if user else '<a href="/login">Login</a>'))
        html = (f"<!doctype html><html><head><title>{escape(title)}</title></head><body>"
                f"<nav>{nav}</nav><h1>{escape(title)}</h1>{body}</body></html>")
        return Response(html, status=status, mimetype="text/html")

    def current_user() -> str | None:
        """Valid-session user; counts the request and expires after SESSION_LIMIT."""
        sid = request.cookies.get("sid")
        sess = state["sessions"].get(sid)
        if not sess:
            return None
        sess["count"] += 1
        if sess["count"] > SESSION_LIMIT:
            del state["sessions"][sid]
            return None
        return sess["user"]

    @app.before_request
    def _auth():
        request.user = None if request.path.startswith("/__") else current_user()

    def need_login(api: bool = False):
        if request.user:
            return None
        return (jsonify(error="unauthenticated"), 401) if api else redirect("/login")

    @app.post("/__reset")
    def _reset():
        reset()
        return "ok"

    @app.post("/__expire")
    def _expire():
        state["sessions"].clear()
        return "ok"

    @app.get("/")
    def index():
        return page("Fixture SSR", '<p>Welcome. See <a href="/projects">projects</a>, '
                    '<a href="/login">login</a>, <a href="/logout">logout</a>.</p>', request.user)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = ""
        if request.method == "POST":
            u, p = request.form.get("username", ""), request.form.get("password", "")
            if u in USERS and state["passwords"][u] == p:
                sid = secrets.token_hex(16)
                state["sessions"][sid] = {"user": u, "count": 0}
                resp = redirect("/projects")
                resp.set_cookie("sid", sid, httponly=True)
                return resp
            err = "<p class='error'>Invalid credentials</p>"
        return page("Login", f"{err}<form method='post' action='/login'>"
                    "<label>Username <input name='username' type='text'></label>"
                    "<label>Password <input name='password' type='password'></label>"
                    "<button type='submit'>Sign in</button></form>",
                    status=401 if err else 200)

    @app.get("/logout")
    def logout():
        state["sessions"].pop(request.cookies.get("sid"), None)
        resp = redirect("/")
        resp.delete_cookie("sid")
        return resp

    @app.get("/projects")
    def projects():
        if (r := need_login()):
            return r
        n = max(1, request.args.get("page", 1, type=int))
        items = sorted(state["projects"].values(), key=lambda p: p["id"])
        chunk = items[(n - 1) * PAGE_SIZE: n * PAGE_SIZE]
        rows = "".join(f"<li><a href='/projects/{p['id']}'>{escape(p['name'])}</a></li>" for p in chunk)
        pages = max(1, -(-len(items) // PAGE_SIZE))
        links = " ".join(f"<a href='/projects?page={i}'>{i}</a>" for i in range(1, pages + 1))
        return page("Projects", f"<ul>{rows}</ul><p>Pages: {links}</p>"
                    "<a href='/projects/new'>New project</a>", request.user)

    def new_form(msg: str = "", values=None) -> str:
        v = values or {}
        return (f"{'<p class=error>' + escape(msg) + '</p>' if msg else ''}"
                "<form method='post' action='/projects/new'>"
                f"<label>Name <input name='name' required pattern=\"[a-z0-9-]{{3,40}}\" "
                f"value='{escape(v.get('name', ''))}'></label>"
                "<label>Visibility <select name='visibility'><option value='public'>public</option>"
                "<option value='private'>private</option></select></label>"
                "<label>Due <input type='date' name='due'></label>"
                "<button type='submit'>Create</button></form>")

    @app.route("/projects/new", methods=["GET", "POST"])
    def project_new():
        if (r := need_login()):
            return r
        if request.method == "GET":
            return page("New project", new_form(), request.user)
        name = request.form.get("name", "")
        if len(name) > 12:
            return page("New project", new_form("Name must be at most 12 characters", request.form),
                        request.user, status=422)
        if not (3 <= len(name) <= 40):
            return page("New project", new_form("Name must be 3-40 characters", request.form),
                        request.user, status=422)
        pid = state["next_id"]
        state["next_id"] += 1
        state["projects"][pid] = {"id": pid, "name": name, "due": request.form.get("due", ""),
                                  "visibility": request.form.get("visibility", "public")}
        return redirect(f"/projects/{pid}")

    @app.get("/projects/<int:pid>")
    def project_detail(pid):
        if (r := need_login()):
            return r
        p = state["projects"].get(pid) or abort(404)
        return page(f"Project {p['name']}",
                    f"<p>Visibility: {p['visibility']}</p><p>Due: {escape(p['due'])}</p>"
                    f"<form method='post' action='/projects/{pid}/delete' "
                    "onsubmit=\"return confirm('Are you sure?')\">"
                    "<button type='submit'>Delete</button></form>", request.user)

    @app.post("/projects/<int:pid>/delete")
    def project_delete(pid):
        if (r := need_login()):
            return r
        state["projects"].pop(pid, None)
        return redirect("/projects")

    @app.get("/admin")
    def admin():
        if (r := need_login()):
            return r
        if USERS[request.user]["role"] != "admin":
            return page("Forbidden", "<p>Admins only</p>", request.user, status=403)
        return page("Admin", "<p>Admin console</p>", request.user)

    @app.get("/admin/reports")
    def admin_reports():
        if (r := need_login()):
            return r
        if USERS[request.user]["role"] != "admin":
            return redirect("/")  # denial by redirecting non-admins home
        return page("Reports", "<p>Admin reports</p>", request.user)

    @app.route("/account/password", methods=["GET", "POST"])
    def account_password():
        if (r := need_login()):
            return r
        msg = ""
        if request.method == "POST":
            if state["passwords"][request.user] != request.form.get("current", ""):
                msg = "<p class=error>Current password incorrect</p>"
            elif request.form.get("new") != request.form.get("confirm") or not request.form.get("new"):
                msg = "<p class=error>Passwords do not match</p>"
            else:
                state["passwords"][request.user] = request.form["new"]
                msg = "<p>Password changed</p>"
        return page("Change password", msg + "<form method='post' action='/account/password'>"
                    "<label>Current <input type='password' name='current'></label>"
                    "<label>New <input type='password' name='new'></label>"
                    "<label>Confirm <input type='password' name='confirm'></label>"
                    "<button type='submit'>Change</button></form>", request.user)

    @app.get("/about")
    def about():
        return page("About", "<p>Fixture app.</p><a href='/go-external'>External site</a>", request.user)

    @app.get("/secret-report")
    def secret_report():
        return page("Secret report", "<p>Not for crawlers.</p>", request.user)

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nDisallow: /secret-report\n", mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        base = request.host_url.rstrip("/")
        urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in ("/", "/projects", "/about"))
        return Response('<?xml version="1.0" encoding="UTF-8"?>'
                        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                        mimetype="application/xml")

    @app.get("/go-external")
    def go_external():
        return redirect("https://external.invalid/", code=302)

    @app.get("/api/openapi.json")
    def openapi():
        ok = {"200": {"description": "OK"}}
        return jsonify({
            "openapi": "3.0.0", "info": {"title": "Fixture API", "version": "1"},
            "paths": {
                "/api/projects": {"get": {"responses": ok}},
                "/api/projects/{id}": {"get": {"parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": ok}},
                "/api/internal/stats": {"get": {"responses": ok}},
            }})

    @app.get("/api/projects")
    def api_projects():
        if (r := need_login(api=True)):
            return r
        return jsonify(sorted(state["projects"].values(), key=lambda p: p["id"]))

    @app.get("/api/projects/<int:pid>")
    def api_project(pid):
        if (r := need_login(api=True)):
            return r
        p = state["projects"].get(pid)
        return jsonify(p) if p else (jsonify(error="not found"), 404)

    @app.get("/api/internal/stats")
    def api_stats():
        return jsonify(projects=len(state["projects"]), sessions=len(state["sessions"]))

    return app
