"""Business rule checks at boundaries and through state transitions."""

import csv
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

import agent
import mail_agent
import web


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_paths = agent.ROOT, agent.DB, agent.CSV
        agent.ROOT = Path(self.temp.name)
        agent.DB = agent.ROOT / "agent.db"
        agent.CSV = agent.ROOT / "출타자 현황.csv"
        self.db = agent.connect()
        with self.db:
            self.db.execute("INSERT INTO departments VALUES('개발',10)")
            self.db.executemany("INSERT INTO employees VALUES(?,'개발',5)",
                                [(x,) for x in "가나다라마바사아자차"])

    def tearDown(self):
        self.db.close()
        agent.ROOT, agent.DB, agent.CSV = self.old_paths
        self.temp.cleanup()

    def item(self, employee, *, kind="leave", period="full", reason="개인 휴가", evidence=False):
        return {"employee": employee, "day": "2026-10-01", "kind": kind,
                "period": period, "reason": reason, "evidence": evidence,
                "evidence_note": "출장 승인 문서 123" if evidence else ""}

    def submit(self, items, categories=None):
        """Submits, then lets a day pass so the 30% decision is made."""
        categories = categories or ["other"] * len(items)
        results = agent.submit(self.db, items, classifier=lambda _: categories)
        agent.settle(self.db, datetime.now().astimezone() + timedelta(days=2))
        return [dict(x, status=self.db.execute("SELECT status FROM requests WHERE id=?", (x["id"],)).fetchone()[0])
                for x in results]

    def test_normal_requests_wait_a_day_then_are_decided_together(self):
        day = (date.today() + timedelta(days=10)).isoformat()
        for name in "가나다라":
            result = agent.submit(self.db, [dict(self.item(name), day=day)], classifier=lambda _: ["other"])[0]
            self.assertEqual(result["status"], "pending")
        exception = agent.submit(self.db, [dict(self.item("마", reason="조부모 장례"), day=day)],
                                 classifier=lambda _: ["family_event"])[0]
        self.assertEqual(exception["status"], "priority_review")
        self.assertEqual(agent.settle(self.db), 0)
        status = lambda: [x[0] for x in self.db.execute("SELECT status FROM requests WHERE status!='priority_review'")]
        self.assertEqual(status(), ["pending"] * 4)
        self.assertEqual(agent.settle(self.db, datetime.now().astimezone() + timedelta(days=1, minutes=1)), 4)
        self.assertEqual(status(), ["manager_review"] * 4)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        agent.submit(self.db, [dict(self.item("바"), day=tomorrow)], classifier=lambda _: ["other"])
        self.assertEqual(agent.settle(self.db), 1)
        self.assertEqual(self.db.execute("SELECT status FROM requests WHERE employee='바'").fetchone()[0], "approved")

    def test_30_percent_boundary_and_overflow(self):
        for name in "가나다":
            self.assertEqual(self.submit([self.item(name)])[0]["status"], "approved")
        self.assertEqual(self.submit([self.item("라")])[0]["status"], "manager_review")
        with agent.CSV.open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(len(list(csv.reader(f))), 4)

    def test_same_day_batch_is_decided_together(self):
        self.assertEqual([x["status"] for x in self.submit([self.item(x) for x in "가나다라"])],
                         ["manager_review"] * 4)
        self.assertEqual([x["status"] for x in self.submit([self.item(x) for x in "마바사"])],
                         ["approved"] * 3)

    def test_half_days_have_separate_am_pm_capacity(self):
        for name in "가나다":
            self.assertEqual(self.submit([self.item(name, period="am")])[0]["status"], "approved")
        self.assertEqual(self.submit([self.item("라", period="pm")])[0]["status"], "approved")
        self.assertEqual(self.submit([self.item("마", period="am")])[0]["status"], "manager_review")

    def test_leave_balance_and_trip_difference(self):
        with self.db:
            self.db.execute("UPDATE employees SET leave_days=0 WHERE name='가'")
        rejected = self.submit([self.item("가")])[0]
        self.assertEqual(rejected["status"], "rejected")
        self.assertTrue(any("잔여 휴가일 부족" in x[0] for x in
                            self.db.execute("SELECT message FROM notices WHERE employee='가'")))
        self.assertEqual(self.submit([self.item("가", kind="trip", reason="업무 출장")])[0]["status"], "approved")

    def test_exception_needs_evidence_for_trip(self):
        items = [self.item("가", reason="조부모 장례"),
                 self.item("나", kind="trip", reason="고객사 점검", evidence=True),
                 self.item("다", kind="trip", reason="고객사 점검")]
        self.assertEqual([x["status"] for x in self.submit(items,
            ["family_event", "important_trip", "important_trip"])],
            ["priority_review", "priority_review", "approved"])

    def test_trip_evidence_reference_is_required(self):
        item = dict(self.item("가", kind="trip", evidence=True), evidence_note="")
        with self.assertRaisesRegex(ValueError, "증빙 자료 식별 정보"):
            self.submit([item])

    def test_one_short_balance_does_not_block_other_batch_requests(self):
        with self.db:
            self.db.execute("UPDATE employees SET leave_days=0 WHERE name='가'")
        results = self.submit([self.item("가"), self.item("나")])
        self.assertEqual([x["status"] for x in results], ["rejected", "approved"])

    def test_cancel_offer_requires_acceptance(self):
        approved = [self.submit([self.item(x)])[0] for x in "가나다"]
        waiting = self.submit([self.item("라")])[0]
        agent.change(self.db, approved[0]["id"], "cancel")
        status = lambda: self.db.execute("SELECT status FROM requests WHERE id=?", (waiting["id"],)).fetchone()[0]
        self.assertEqual(status(), "offered")
        self.assertEqual(agent.change(self.db, waiting["id"], "no"), "manager_review")
        agent.change(self.db, approved[1]["id"], "cancel")
        self.assertEqual(status(), "offered")
        self.assertEqual(agent.change(self.db, waiting["id"], "yes"), "approved")
        self.assertTrue(any("출타 가능 자리" in x[0] for x in
                            self.db.execute("SELECT message FROM notices WHERE employee='라'")))

    def test_duplicate_and_bad_input_do_not_write(self):
        self.submit([self.item("가")])
        with self.assertRaisesRegex(ValueError, "중복 신청"):
            self.submit([self.item("가")])
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.submit([dict(self.item("나"), day="tomorrow")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)

    def test_model_failure_goes_to_review(self):
        def failure(_):
            raise ValueError("LM Studio 응답 오류")
        result = agent.submit(self.db, [self.item("가")], classifier=failure)[0]
        row = self.db.execute("SELECT category,status FROM requests WHERE id=?", (result["id"],)).fetchone()
        self.assertEqual(tuple(row), ("uncertain", "manager_review"))

    def test_ambiguous_reason_goes_to_review(self):
        result = self.submit([self.item("가", reason="경조사")], ["uncertain"])[0]
        self.assertEqual(result["status"], "manager_review")
        approved = [self.submit([self.item(x)])[0] for x in "나다"]
        agent.change(self.db, approved[0]["id"], "cancel")
        self.assertEqual(self.db.execute("SELECT status FROM requests WHERE id=?", (result["id"],)).fetchone()[0],
                         "manager_review")

    def test_family_proof_after_return_and_verification(self):
        item = dict(self.item("가", reason="누나 결혼식"), day=(date.today()-timedelta(days=1)).isoformat())
        result = self.submit([item], ["family_event"])[0]
        self.assertEqual(result["status"], "priority_review")
        self.assertEqual(self.db.execute("SELECT proof_status FROM requests WHERE id=?", (result["id"],)).fetchone()[0], "pending")
        agent.change(self.db, result["id"], "approve")
        with self.assertRaisesRegex(ValueError, "PDF, PNG, JPG"):
            agent.submit_proof(self.db, result["id"], b"not a certificate")
        with self.assertRaisesRegex(ValueError, "5MB"):
            agent.submit_proof(self.db, result["id"], b"%PDF-" + b"x" * 5_000_000)
        agent.submit_proof(self.db, result["id"], b"%PDF-1.4\nDemo certificate")
        self.assertTrue((agent.ROOT / "proofs" / f"{result['id']}.pdf").exists())
        agent.verify_proof(self.db, result["id"])
        self.assertEqual(self.db.execute("SELECT proof_status FROM requests WHERE id=?", (result["id"],)).fetchone()[0], "verified")

    def test_proof_cannot_be_submitted_before_return(self):
        item = dict(self.item("가", reason="누나 결혼식"), day=(date.today()+timedelta(days=1)).isoformat())
        result = self.submit([item], ["family_event"])[0]
        agent.change(self.db, result["id"], "approve")
        with self.assertRaisesRegex(ValueError, "복귀 후"):
            agent.submit_proof(self.db, result["id"], b"%PDF-1.4\nDemo certificate")

    def test_web_page_and_api_flow(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        old_classifier = agent.lm_categories
        agent.lm_categories = lambda items: ["family_event" if "결혼식" in x["reason"] else "other" for x in items]
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/") as response:
                page = response.read().decode()
                self.assertIn("신청 접수", page)
                self.assertIn('<input id="request-employee" type="text"', page)
            for route in ("/apply", "/manager"):
                with urlopen(base + route) as response:
                    self.assertEqual(response.status, 200)
            request = Request(base + "/api/submit", data=json.dumps([self.item("가")]).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)["results"][0]["status"], "pending")
            with urlopen(base + "/api/state") as response:
                self.assertEqual(len(json.load(response)["requests"]), 1)
            past = dict(self.item("나", reason="누나 결혼식"), day=(date.today()-timedelta(days=1)).isoformat())
            request = Request(base + "/api/submit", data=json.dumps([past]).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                proof_id = json.load(response)["results"][0]["id"]
            request = Request(base + "/api/change", data=json.dumps({"id": proof_id, "action": "approve"}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                self.assertEqual(json.load(response)["status"], "approved")
            request = Request(base + f"/api/proof/{proof_id}", data=b"%PDF-1.4\nDemo certificate",
                              headers={"Content-Type": "application/pdf"})
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["ok"])
            with urlopen(base + f"/api/proof/{proof_id}") as response:
                self.assertTrue(response.read().startswith(b"%PDF-"))
            request = Request(base + "/api/verify", data=json.dumps({"id": proof_id}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["ok"])
        finally:
            agent.lm_categories = old_classifier
            server.shutdown()
            server.server_close()
            thread.join()

    def test_mail_is_filed_only_after_the_employee_confirms(self):
        mail_agent.setup(self.db)
        self.enterContext(patch.object(mail_agent, "mail_related", return_value=True))
        with self.db:
            self.db.execute("INSERT INTO employee_emails VALUES('ga@example.com','가')")
        def call(name, **args):
            return {"content": "", "tool_calls": [{"id": name, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
        seen = []
        script = iter([
            call("leave_balance"),
            call("propose", kind="leave", reason="개인 휴가"),  # period missing
            call("ask", question="전일인가요, 반차인가요?"),
            call("propose", kind="leave", period="am", reason="개인 휴가"),
            call("propose", kind="leave", period="pm", reason="개인 휴가")])
        def model(messages):
            seen.append([dict(m) for m in messages])
            return next(script)
        old_classifier = agent.lm_categories
        agent.lm_categories = lambda items: ["other"] * len(items)
        self.addCleanup(setattr, agent, "lm_categories", old_classifier)
        today = date(2026, 9, 29)
        count = lambda: self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        self.assertIsNone(mail_agent.handle(self.db, "stranger@example.com", "휴가", model))
        first = mail_agent.handle(self.db, "GA@example.com", "10월 1일 휴가 쓸게요", model, today)
        self.assertEqual(first, "전일인가요, 반차인가요?")
        self.assertIn('"remaining_days": 5', seen[1][-1]["content"])
        self.assertIn("전일/오전 반차/오후 반차", seen[2][-1]["content"])
        # "확인" before any proposal is just a message for the model, never a filing.
        proposal = mail_agent.handle(self.db, "ga@example.com", "반차요 오전", model, today)
        self.assertIn("2026-10-01(목) · 휴가 · 오전 반차", proposal)
        self.assertEqual([m["content"] for m in seen[3][1:]],
                         ["10월 1일 휴가 쓸게요", "전일인가요, 반차인가요?", "반차요 오전"])
        self.assertEqual(count(), 0)
        corrected = mail_agent.handle(self.db, "ga@example.com", "네 근데 오후로 바꿔주세요", model, today)
        self.assertIn("오후 반차", corrected)
        self.assertEqual(count(), 0)
        calls = len(seen)
        done = mail_agent.handle(self.db, "ga@example.com", "확인\n\n김가 드림", model, today)
        self.assertEqual(len(seen), calls)  # confirmation is handled by code, not the model
        self.assertIn("접수 결과", done)
        row = self.db.execute("SELECT employee,day,period,status FROM requests").fetchone()
        self.assertEqual(tuple(row), ("가", "2026-10-01", "pm", "pending"))
        self.assertIsNone(self.db.execute("SELECT 1 FROM mail_drafts").fetchone())

    def test_mail_dates_are_resolved_by_code(self):
        today = date(2026, 9, 29)  # Tuesday
        cases = {"오늘": "2026-09-29", "낼": "2026-09-30", "tomorrow": "2026-09-30", "내일모레": "2026-10-01",
                 "모레": "2026-10-01", "글피": "2026-10-02", "이번 주 금요일": "2026-10-02",
                 "담주 화욜": "2026-10-06", "다음주 화": "2026-10-06", "다다음 주 월요일": "2026-10-12",
                 "금요일": "2026-10-02", "10/6": "2026-10-06", "10.8": "2026-10-08", "10월12일": "2026-10-12",
                 "1 0 / 1 3": "2026-10-13", "담달 2일": "2026-10-02", "Oct 12": "2026-10-12",
                 "10월 7일(수)": "2026-10-07", "10/7 수요일": "2026-10-07", "10월 7일에": "2026-10-07",
                 "9/1": "2027-09-01", "2026-10-07": "2026-10-07"}
        for phrase, expected in cases.items():
            self.assertEqual(str(mail_agent.resolve_date(phrase, today)), expected, phrase)
        # Anything not fully understood is unknown, never partly read.
        for unclear in ("다음 주에", "며칠", "화요일", "이번 주 월요일", "2/30", "반차", "내일 오후",
                        "지난주 금요일", "저번 주 월요일", "10월 7일(목)", "10월 말쯤", "11월 25일 또는 26일"):
            self.assertIsNone(mail_agent.resolve_date(unclear, today), unclear)
        # The date comes from the employee's own words, found by code in the whole mail.
        clear = {"낼 오후반차 쓸게요": "2026-09-30", "담주 금욜 연차 하루요": "2026-10-09",
                 "12.22 오후 외근입니다": "2026-12-22", "연차 낼게요, 내일 하루": "2026-09-30",
                 "10월 7일(수) 연차": "2026-10-07", "품의 EXP-7731, 공문 NRF-2026-0912, 12/23 출장": "2026-12-23"}
        for text, expected in clear.items():
            self.assertEqual(mail_agent.email_dates([text], today)[0], (date.fromisoformat(expected),) * 2, text)
        periods = {"10월 5일부터 7일까지 연차": ("2026-10-05", "2026-10-07"),
                   "10/5~10/7 휴가": ("2026-10-05", "2026-10-07"),
                   "내일부터 모레까지 쉽니다": ("2026-09-30", "2026-10-01"),
                   "담주 월요일부터 수요일까지": ("2026-10-05", "2026-10-07"),
                   "12월 30일부터 1월 2일까지": ("2026-12-30", "2027-01-02"),
                   "12월 30일부터 2일까지": ("2026-12-30", "2027-01-02"),
                   "10월 5일부터 7일까지 연차 3일": ("2026-10-05", "2026-10-07")}
        for text, (first, last) in periods.items():
            found = mail_agent.email_dates([text], today)[0]
            self.assertEqual((str(found[0]), str(found[1])), (first, last), text)
        for text in ("10월 7일 휴가요, 아 아니다 8일로", "지난주 금요일 반차 소급", "10월 7일(목) 연차",
                     "다음 주 중에 하루", "10월 7일 또는 8일", "10월 7일, 8일 연차", "5일~7일 휴가",
                     "10월 5일부터 7일까지 연차 5일", "휴가 쓸게요"):
            self.assertIsNone(mail_agent.email_dates([text], today)[0], text)
        # The latest message that mentions a date wins, so a correction replaces the first date.
        self.assertEqual(mail_agent.email_dates(["내일 휴가요", "죄송해요 10월 14일로 바꿔주세요", "종일이요"], today)[0],
                         (date(2026, 10, 14), date(2026, 10, 14)))
        person = self.db.execute("SELECT * FROM employees WHERE name='가'").fetchone()
        trip = {"kind": "trip", "period": "full", "reason": "고객사 점검", "evidence": True}
        result, final, _, proposal = mail_agent.run_tool(self.db, person, "propose", trip, ["10월 8일 출장"], today)
        self.assertIsNone(final)
        self.assertIn("증빙", result["error"])
        _, final, keep, proposal = mail_agent.run_tool(
            self.db, person, "propose", dict(trip, evidence_note="BT-0931"), ["10월 8일 출장 BT-0931"], today)
        self.assertIn("증빙: BT-0931", final)
        self.assertTrue(keep)
        self.assertEqual(proposal["day"], "2026-10-08")
        leave = {"kind": "leave", "period": "full", "reason": "가족 여행"}
        _, final, _, proposal = mail_agent.run_tool(
            self.db, person, "propose", leave, ["10월 9일부터 13일까지 휴가"], today)
        self.assertIn("기간 2026-10-09(금) ~ 2026-10-13(화) · 사용일 3일(주말 제외)", final)
        self.assertEqual((proposal["day"], proposal["end_day"]), ("2026-10-09", "2026-10-13"))
        result, final, _, _ = mail_agent.run_tool(
            self.db, person, "propose", dict(leave, period="am"), ["10월 9일부터 13일까지 휴가"], today)
        self.assertIn("반차는 하루짜리", result["error"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_balance_only_mail_never_enters_request_tools(self):
        mail_agent.setup(self.db)
        self.enterContext(patch.object(mail_agent, "mail_related", return_value=True))
        with self.db:
            self.db.execute("INSERT INTO employee_emails VALUES('ga@example.com','가')")
        def unexpected_model(_):
            self.fail("잔여일만 묻는 메일은 모델 도구 선택으로 보내면 안 됩니다")
        today = date(2026, 12, 10)
        for body in ("남은 연차 알려주실 수 있나요?", "제 남은 연차가 몇 개인지 알 수 있을까요?",
                     "남은 휴가 며칠이에요?", "남은 연차 5일?"):
            self.assertEqual(mail_agent.handle(self.db, "ga@example.com", body, unexpected_model, today),
                             "남은 휴가일은 5일입니다.")
        self.assertFalse(mail_agent.balance_only("내일 연차 쓰고 싶은데 남은 연차가 몇 일인가요?", today))
        self.assertFalse(mail_agent.balance_only("남은 연차 알려주시고 휴가 신청도 할게요", today))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_model_question_cannot_invent_a_calendar_date(self):
        person = self.db.execute("SELECT * FROM employees WHERE name='가'").fetchone()
        _, question, keep, _ = mail_agent.run_tool(
            self.db, person, "ask", {"question": "담주 금요일(12월 17일)에 쉬실 건가요?"},
            ["담주 금욜 연차 하루요"], date(2026, 12, 10))
        self.assertTrue(keep)
        self.assertIn("2026-12-18", question)
        self.assertNotIn("17일", question)
        _, question, _, _ = mail_agent.run_tool(
            self.db, person, "ask", {"question": "12월 17일에 쉬실 건가요?"},
            ["휴가 쓸게요"], date(2026, 12, 10))
        self.assertEqual(question, "신청할 날짜를 YYYY-MM-DD 형식으로 알려주세요.")

    def test_mail_poller_reads_only_leave_subjects(self):
        def message(subject, body):
            item = mail_agent.EmailMessage()
            item["Subject"] = subject
            item["From"] = "ga@example.com"
            item.set_content(body)
            return item.as_bytes()
        mails = {b"1": message("일반 문의", "첫 번째"), b"2": message("휴가 신청", "두 번째")}
        class Inbox:
            def __init__(self):
                self.fetched, self.seen = [], []
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def login(self, *_):
                pass
            def select(self, *_):
                pass
            def search(self, *_):
                return "OK", [b"1 2"]
            def fetch(self, number, section):
                self.fetched.append((number, section))
                raw = mails[number]
                if "HEADER.FIELDS" in section:
                    raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
                return "OK", [(b"", raw)]
            def store(self, number, *_):
                self.seen.append(number)
        inbox, bodies = Inbox(), []
        with (patch.object(agent, "connect", return_value=self.db),
              patch.object(mail_agent.imaplib, "IMAP4_SSL", return_value=inbox),
              patch.object(mail_agent, "handle", side_effect=lambda _, __, body: bodies.append(body)),
              patch.object(mail_agent.time, "sleep", side_effect=StopIteration),
              patch.dict(mail_agent.os.environ, {"MAIL_USER": "bot@example.com", "MAIL_PASSWORD": "test",
                                               "MAIL_TRUST_SENDER": "1"})):
            with self.assertRaises(StopIteration):
                mail_agent.run()
        self.assertEqual(bodies, ["두 번째"])
        self.assertEqual(inbox.seen, [b"2"])
        self.assertEqual([number for number, section in inbox.fetched if section == "(BODY.PEEK[])"], [b"2"])

    def test_unrelated_mail_stops_before_request_tools(self):
        mail_agent.setup(self.db)
        with self.db:
            self.db.execute("INSERT INTO employee_emails VALUES('ga@example.com','가')")
        with (patch.object(agent, "lm_json", side_effect=[{"related": False}, {"related": True}]),
              patch.object(mail_agent, "lm_step", side_effect=AssertionError("신청 도구를 호출하면 안 됩니다"))):
            self.assertIsNone(mail_agent.handle(self.db, "ga@example.com", "오늘 날씨 좋네요"))
            self.assertEqual(mail_agent.handle(self.db, "ga@example.com", "남은 연차 알려주세요"),
                             "남은 휴가일은 5일입니다.")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_periods_use_weekdays_and_are_decided_as_one_request(self):
        # 2026-10-09 is a Friday: 10-09..10-13 uses Fri, Mon, Tue = 3 leave days.
        period = lambda name, first, last, **kw: dict(self.item(name, **kw), day=first, end_day=last)
        self.assertEqual(agent.units({"day": "2026-10-09", "end_day": "2026-10-13", "period": "full"}), 3)
        self.assertEqual(agent.units({"day": "2026-10-10", "end_day": "2026-10-10", "period": "am"}), .5)
        for bad, message in ((period("가", "2026-10-13", "2026-10-09"), "종료일"),
                             (period("가", "2026-10-09", "2026-10-13", period="am"), "반차"),
                             (period("가", "2026-10-10", "2026-10-11"), "평일"),
                             (period("가", "2026-10-01", "2026-11-15"), "최대")):
            with self.assertRaisesRegex(ValueError, message):
                self.submit([bad])
        with self.db:
            self.db.execute("UPDATE employees SET leave_days=3 WHERE name='가'")
        self.assertEqual(self.submit([period("가", "2026-10-09", "2026-10-13")])[0]["status"], "approved")
        self.assertEqual(agent.used_leave(self.db, "가"), 3)
        with self.assertRaisesRegex(ValueError, "겹치는"):
            self.submit([period("가", "2026-10-13", "2026-10-14", kind="trip")])
        # 10-12 already has 가; 나, 다 fit, but a third over 10-12 makes 4/10 on that day only.
        self.assertEqual(self.submit([period("나", "2026-10-12", "2026-10-12")])[0]["status"], "approved")
        self.assertEqual(self.submit([period("다", "2026-10-12", "2026-10-12")])[0]["status"], "approved")
        whole = self.submit([period("라", "2026-10-08", "2026-10-14")])[0]
        self.assertEqual(whole["status"], "manager_review")  # one full day blocks the whole period
        self.assertEqual(self.submit([period("마", "2026-10-14", "2026-10-15")])[0]["status"], "approved")
        with agent.CSV.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["신청번호", "부서", "이름", "기간", "사용일", "구분", "시간"])
        self.assertIn(["1", "개발", "가", "2026-10-09 ~ 2026-10-13", "3", "leave", "full"], rows)
        # Cancelling 가 frees 10-09..10-13, so 라's whole period now fits and is offered.
        agent.change(self.db, 1, "cancel")
        self.assertEqual(self.db.execute("SELECT status FROM requests WHERE id=?", (whole["id"],)).fetchone()[0],
                         "offered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
