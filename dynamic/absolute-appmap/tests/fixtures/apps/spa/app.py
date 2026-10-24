"""Vue SPA fixture backend (Flask): static bundle + JSON/GraphQL/SSE API."""
import base64
import json
import secrets
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

STATIC = Path(__file__).parent / "static"
USERS = {
    "admin": {"password": "admin-pw-Fx1", "role": "admin"},
    "alice": {"password": "alice-pw-Fx2", "role": "user"},
}
PAGE_SIZE = 10
ITEM_COUNT = 100


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    state: dict = {}

    def reset():
        state["items"] = {i: {"id": i, "title": f"Item {i}", "owner": "admin" if i % 2 else "alice"}
                          for i in range(1, ITEM_COUNT + 1)}
        state["next_id"] = ITEM_COUNT + 1
        state["tokens"] = {}

    reset()

    def user():
        h = request.headers.get("Authorization", "")
        return state["tokens"].get(h[7:]) if h.startswith("Bearer ") else None

    def unauth():
        return jsonify(error="unauthenticated"), 401

    @app.post("/__reset")
    def _reset():
        reset()
        return "ok"

    @app.post("/__expire")
    def _expire():
        state["tokens"].clear()
        return "ok"

    @app.post("/api/login")
    def login():
        d = request.get_json(silent=True) or {}
        u = USERS.get(d.get("username"))
        if not u or u["password"] != d.get("password"):
            return jsonify(error="invalid credentials"), 401
        token = ".".join([_b64({"alg": "HS256", "typ": "JWT"}),
                          _b64({"sub": d["username"], "role": u["role"], "jti": secrets.token_hex(8)}),
                          base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()])
        state["tokens"][token] = {"username": d["username"], "role": u["role"]}
        return jsonify(token=token, username=d["username"], role=u["role"])

    @app.get("/api/items")
    def items():
        if not user():
            return unauth()
        cursor = request.args.get("cursor") or "0"
        start = int(cursor) if cursor.isdigit() else 0
        ids = sorted(state["items"])
        chunk = ids[start:start + PAGE_SIZE]
        nxt = str(start + PAGE_SIZE) if start + PAGE_SIZE < len(ids) else ""
        return jsonify(items=[state["items"][i] for i in chunk], next_cursor=nxt)

    @app.post("/api/items")
    def item_create():
        u = user()
        if not u:
            return unauth()
        d = request.get_json(silent=True) or {}
        iid = state["next_id"]
        state["next_id"] += 1
        state["items"][iid] = {"id": iid, "title": d.get("title", ""), "owner": u["username"],
                               **{k: d[k] for k in ("category",) if k in d}}
        return jsonify(state["items"][iid]), 201

    @app.get("/api/items/<int:iid>")
    def item_get(iid):
        if not user():
            return unauth()
        it = state["items"].get(iid)
        return jsonify(it) if it else (jsonify(error="not found"), 404)

    @app.put("/api/items/<int:iid>")
    def item_put(iid):
        if not user():
            return unauth()
        it = state["items"].get(iid)
        if not it:
            return jsonify(error="not found"), 404
        d = request.get_json(silent=True) or {}
        it.update({k: v for k, v in d.items() if k != "id"})
        return jsonify(it)

    @app.get("/api/admin/stats")
    def admin_stats():
        u = user()
        if not u:
            return unauth()
        if u["role"] != "admin":
            return jsonify(error="forbidden"), 403
        return jsonify(items=len(state["items"]), sessions=len(state["tokens"]))

    @app.get("/api/internal/flags")
    def flags():
        return jsonify(beta=False, debug=True)

    @app.post("/graphql")
    def graphql():
        if not user():
            return unauth()
        d = request.get_json(silent=True) or {}
        q, v = d.get("query", ""), d.get("variables") or {}
        if "DeleteItem" in q:
            return jsonify(data={"deleteItem": state["items"].pop(v.get("id"), None) is not None})
        if "ListItems" in q:
            rows = [{"id": i["id"], "title": i["title"]} for i in sorted(
                state["items"].values(), key=lambda i: i["id"])[:PAGE_SIZE]]
            return jsonify(data={"items": rows})
        return jsonify(errors=[{"message": "unsupported operation"}]), 400

    @app.get("/api/events")
    def events():
        def gen():
            for n in range(3):
                yield f"data: {json.dumps({'tick': n})}\n\n"
        return Response(gen(), mimetype="text/event-stream")

    @app.get("/static/<path:p>")
    def static_files(p):
        return send_from_directory(STATIC, p)

    @app.get("/", defaults={"p": ""})
    @app.get("/<path:p>")
    def spa(p):
        if p.startswith("api/"):
            return jsonify(error="not found"), 404
        return send_from_directory(STATIC, "index.html")

    return app
