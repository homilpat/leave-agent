"""Local browser UI for the leave/travel demo. Run `python web.py`."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import agent


PAGE = (Path(__file__).resolve().parent / "index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    def send(self, code, value, mime="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/", "/apply", "/manager"}:
            self.send(200, PAGE, "text/html; charset=utf-8")
        elif path.startswith("/api/proof/"):
            try:
                request_id = int(path.rsplit("/", 1)[1])
                db = agent.connect()
                try:
                    row = db.execute("SELECT proof_file,proof_status FROM requests WHERE id=?", (request_id,)).fetchone()
                    if not row or row["proof_status"] not in {"submitted", "verified"}:
                        raise ValueError("제출된 증빙서가 없습니다")
                    file = agent.ROOT / "proofs" / row["proof_file"]
                    mime = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg"}[file.suffix[1:]]
                    self.send(200, file.read_bytes(), mime)
                finally:
                    db.close()
            except (ValueError, KeyError, OSError):
                self.send(404, {"error": "증빙서를 찾을 수 없습니다"})
        elif path == "/api/state":
            db = agent.connect()
            self.send(200, {
                "departments": [dict(x) for x in db.execute("SELECT * FROM departments ORDER BY name")],
                "employees": [dict(x) for x in db.execute("SELECT * FROM employees ORDER BY name")],
                "requests": [dict(x) for x in db.execute("SELECT * FROM requests ORDER BY id DESC")],
                "notices": [dict(x) for x in db.execute("SELECT * FROM notices ORDER BY id DESC LIMIT 30")],
            })
            db.close()
        elif path == "/api/models":
            try:
                import urllib.request
                with urllib.request.urlopen(agent.LM_URL + "/models", timeout=3) as response:
                    models = json.load(response).get("data", [])
                self.send(200, {"models": [x["id"] for x in models],
                                "selected": agent.model_id(models) if models else None})
            except (OSError, ValueError, KeyError) as exc:
                self.send(503, {"error": str(exc)})
        else:
            self.send(404, {"error": "페이지가 없습니다"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/proof/"):
            try:
                request_id = int(path.rsplit("/", 1)[1])
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 5_000_000:
                    raise ValueError("증빙서는 5MB 이하여야 합니다")
                db = agent.connect()
                try:
                    agent.submit_proof(db, request_id, self.rfile.read(length))
                finally:
                    db.close()
                self.send(200, {"ok": True})
            except (ValueError, OSError, agent.sqlite3.Error) as exc:
                self.send(400, {"error": str(exc)})
            return
        if path not in {"/api/department", "/api/employee", "/api/submit", "/api/change", "/api/verify"}:
            self.send(404, {"error": "요청 경로가 없습니다"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100_000:
                raise ValueError("요청 크기가 올바르지 않습니다")
            data = json.loads(self.rfile.read(length))
            db = agent.connect()
            try:
                if path == "/api/department":
                    name, count = data["name"].strip(), data["headcount"]
                    if not name or type(count) is not int or count < 1:
                        raise ValueError("부서명과 1명 이상의 인원이 필요합니다")
                    with db:
                        db.execute("INSERT INTO departments VALUES(?,?) ON CONFLICT(name) DO UPDATE SET headcount=excluded.headcount", (name, count))
                    result = {"ok": True}
                elif path == "/api/employee":
                    name, department, days = data["name"].strip(), data["department"], data["leave_days"]
                    if not name or type(days) not in (int, float) or days < 0:
                        raise ValueError("이름과 0일 이상의 휴가일이 필요합니다")
                    with db:
                        db.execute("INSERT INTO employees VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                                   "department=excluded.department,leave_days=excluded.leave_days", (name, department, days))
                    result = {"ok": True}
                elif path == "/api/submit":
                    result = {"results": agent.submit(db, data)}
                elif path == "/api/change":
                    action = data["action"]
                    if action not in {"approve", "cancel", "yes", "no"} or type(data["id"]) is not int:
                        raise ValueError("신청번호 또는 동작이 올바르지 않습니다")
                    result = {"status": agent.change(db, data["id"], action)}
                else:
                    if type(data["id"]) is not int:
                        raise ValueError("신청번호가 올바르지 않습니다")
                    agent.verify_proof(db, data["id"])
                    result = {"ok": True}
                self.send(200, result)
            finally:
                db.close()
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, agent.sqlite3.Error) as exc:
            self.send(400, {"error": str(exc)})


def settle_forever():
    while True:
        try:
            db = agent.connect()
            try:
                agent.settle(db)
            finally:
                db.close()
        except (agent.sqlite3.Error, OSError) as exc:
            print(f"자동 승인 처리 오류: {exc}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=settle_forever, daemon=True).start()
    print("휴가 처리 시연: http://127.0.0.1:8765", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
