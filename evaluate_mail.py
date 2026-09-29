"""Live LM Studio evaluation of the email agent on a throwaway database.

python evaluate_mail.py eval_mail/dev.json            # 개발셋: 프롬프트 수정용
python evaluate_mail.py eval_mail/test.json           # 테스트셋: 프롬프트 확정 후 한 번만 실행
"""

import hashlib
import json
import math
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import agent
import mail_agent


def wilson(k, n, z=1.96):
    if not n:
        return 0.0, 0.0
    p = k / n
    centre, spread = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n), (centre + spread) / (1 + z * z / n)


def run_case(db, case, today):
    with db:
        for table in ("requests", "mail_drafts", "notices"):
            db.execute(f"DELETE FROM {table}")
    trace = []

    def chat(messages):
        message = mail_agent.lm_step(messages)
        trace.append([(c["function"]["name"], c["function"].get("arguments", "")) for c in message.get("tool_calls") or []]
                     or ["(no tool call)", (message.get("content") or "")[:200]])
        return message

    replies = [mail_agent.handle(db, "hana@example.com", body, chat, today) for body in case["mails"]]
    expect = case["expect"]
    keys = ("day", "kind", "period", "evidence")
    proposal = mail_agent.load(db, "김하나").get("proposal")
    proposed = {k: proposal[k] for k in keys} if proposal else None
    # Simulated employee: confirms only a proposal that matches what they asked for.
    right = bool(proposal) and expect["action"] == "submit" and all(proposed[k] == expect[k] for k in keys)
    if right:
        replies.append(mail_agent.handle(db, "hana@example.com", "확인", chat, today))
    row = db.execute("SELECT day,kind,period,evidence,status FROM requests").fetchone()
    if row:
        action = "submit"
    elif {mail_agent.UNAVAILABLE, mail_agent.FAILED} & set(replies):
        action = "error"
    elif proposal:
        action = "wrong_proposal"  # the employee would not confirm it: caught, nothing filed
    elif db.execute("SELECT 1 FROM mail_drafts").fetchone():
        action = "ask"
    else:
        action = "reply"
    filed = {"day": row[0], "kind": row[1], "period": row[2], "evidence": bool(row[3]), "status": row[4]} if row else {}
    got = {"action": action, **(filed or proposed or {})}
    fields = {f: got.get(f) == expect[f] for f in keys} if expect["action"] == "submit" else {}
    passed = {"submit": action == "submit" and all(fields.values()),
              "ask": action == "ask",
              "reply": action == "reply" and expect.get("contains", "") in replies[-1],
              "no_submit": action in {"ask", "reply"}}[expect["action"]]
    # Unsafe = a request was filed that the employee did not ask for, or with wrong values.
    unsafe = bool(row) and (expect["action"] != "submit" or not all(filed[k] == expect[k] for k in keys))
    return {"id": case["id"], "group": case["group"], "passed": passed, "unsafe": unsafe,
            "caught": action == "wrong_proposal", "mails": len(replies), "expect": expect,
            "got": got, "fields": fields, "replies": replies, "trace": trace}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "eval_mail/dev.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    today = date.fromisoformat(data["reference_date"])
    with tempfile.TemporaryDirectory() as folder:
        agent.ROOT = Path(folder)
        agent.DB, agent.CSV = agent.ROOT / "agent.db", agent.ROOT / "출타자 현황.csv"
        db = agent.connect()
        mail_agent.setup(db)
        with db:
            db.execute("INSERT INTO departments VALUES('개발',10)")
            db.execute("INSERT INTO employees VALUES('김하나','개발',15)")
            db.execute("INSERT INTO employees VALUES('김철수','개발',15)")
            db.execute("INSERT INTO employee_emails VALUES('hana@example.com','김하나')")
        started = time.time()
        results = []
        for case in data["cases"]:
            result = run_case(db, case, today)
            results.append(result)
            print(f"{'PASS' if result['passed'] else 'FAIL'}{' UNSAFE' if result['unsafe'] else ''} | {case['id']} "
                  f"| {' / '.join(case['mails'])}\n       예상 {result['expect']}\n       실제 {result['got']}"
                  f"\n       답장 {result['replies'][-1]!r}", flush=True)
        db.close()

    n = len(results)
    k = sum(r["passed"] for r in results)
    low, high = wilson(k, n)
    unsafe = sum(r["unsafe"] for r in results)
    u_low, u_high = wilson(unsafe, n)
    submits = [r for r in results if r["expect"]["action"] == "submit"]
    print(f"\n평가셋 {path} (sha256 {hashlib.sha256(path.read_bytes()).hexdigest()[:16]}…), 모델 기준일 {today}")
    print(f"사례 정확도: {k}/{n} = {k / n:.1%} (95% Wilson CI {low:.1%}–{high:.1%})")
    print(f"위험 오류(원치 않는/틀린 접수): {unsafe}/{n} = {unsafe / n:.1%} (95% CI {u_low:.1%}–{u_high:.1%})")
    caught = sum(r["caught"] for r in results)
    print(f"틀린 제안(확인 단계에서 걸러짐, 접수 안 됨): {caught}/{n}")
    print(f"사례당 평균 메일 수(확인 포함): {sum(r['mails'] for r in results) / n:.2f}")
    for field in ("day", "kind", "period", "evidence"):
        hit = sum(r["fields"][field] for r in submits)
        print(f"  접수 기대 사례의 {field} 일치: {hit}/{len(submits)}")
    groups = defaultdict(list)
    for r in results:
        groups[r["group"]].append(r["passed"])
    print("그룹별: " + ", ".join(f"{g} {sum(v)}/{len(v)}" for g, v in groups.items()))
    print(f"소요 {time.time() - started:.0f}초")
    log = path.with_name(path.stem + "-results.json")
    log.write_text(json.dumps({"set": str(path), "reference_date": str(today), "passed": k, "total": n,
                               "unsafe": unsafe, "caught": caught, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"상세 기록: {log}")


if __name__ == "__main__":
    main()
