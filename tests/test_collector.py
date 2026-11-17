"""수집기 오프라인 테스트 — 네트워크 대신 tests/fixtures 의 응답을 끼워 전체 경로를 실행한다.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector import __main__ as cli  # noqa: E402
from collector import config, dedup, feeds, store, util  # noqa: E402
from collector.classify import Classifier, load_taxonomy  # noqa: E402
from collector.reports import ReportMatcher, build_queries, edition_year, load_reports  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
# 수집 로직 테스트는 고정된 보고서 목록으로 돌린다 — assets/reports.json 에 새 판을 추가해도 배포 전 테스트가 깨지지 않는다.
# (assets/*.json 내용 자체의 형식 점검은 ContentTest 가 실제 파일로 한다)
FIX_REPORTS = FIX / "reports.json"
NOW = datetime(2026, 11, 13, 6, 30, tzinfo=timezone.utc)  # 15:30 KST
KEY = "TESTKEY+/=abc"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeNet:
    """URL 에 따라 고정 응답을 돌려주고 호출 기록을 남긴다."""

    def __init__(self, fail_first_query: bool = False):
        self.calls: list[str] = []
        self.fail_first_query = fail_first_query

    def __call__(self, url: str, **_kw) -> bytes:
        self.calls.append(url)
        if "news.google.com" in url:
            if self.fail_first_query and self.count("news.google.com") == 1:
                raise RuntimeError("HTTP Error 503")
            return fixture("gnews_ko.xml")
        if "aitimes.com" in url:
            return fixture("aitimes.xml")
        raise RuntimeError(f"예상하지 못한 요청: {url}")

    def count(self, part: str) -> int:
        return sum(1 for u in self.calls if part in u)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.net = FakeNet()
        self.patches = [
            mock.patch.object(util, "fetch", self.net),
            mock.patch.object(util, "now_utc", lambda: NOW),
            mock.patch.object(config, "REPORTS_PATH", FIX_REPORTS),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args: str) -> tuple[list[dict], dict]:
        with mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["--data-dir", str(self.tmp), *args]), 0)
        return store.load(self.tmp)

    def by(self, items, pred):
        found = [i for i in items if pred(i)]
        self.assertTrue(found, "조건에 맞는 항목이 없다")
        return found[0]

    def test_full_run(self):
        items, meta = self.run_cli("--force")
        # 보고서와 연결되지 않는 기사(휴머노이드·반도체)와 오래된 기사는 빠진다
        titles = " ".join(i["title"] for i in items)
        for gone in ("휴머노이드", "반도체 수출", "아주 오래된"):
            self.assertNotIn(gone, titles)
        self.assertEqual(meta["counts"], {"news": 4})

        # 같은 발표: 언론사 직접 주소가 대표, 다른 매체는 '같은 소식', 가장 이른 보도 시각
        web = self.by(items, lambda i: "인터넷이용실태조사" in i["title"])
        self.assertTrue(web["url"].startswith("https://www.aitimes.com/news/articleView.html?idxno=900001"))
        self.assertEqual(sorted(a["source"] for a in web.get("also", [])), ["샘플경제", "테스트일보"])
        self.assertEqual(web["date"], "2026-11-13T03:00:00Z")
        self.assertEqual(web["reps"], ["kr-internet-2025"])
        self.assertEqual(web["newer"], ["kr-internet-2025"])          # 2026 > 최신판 2025
        self.assertEqual(web["cats"][0], "korea")                     # 국내 보고서 소식은 '한국 관련'

        bok = self.by(items, lambda i: "한국은행" in i["title"])
        self.assertEqual(bok["reps"], ["bok-ai-2026"])
        self.assertEqual(bok["date"], "2026-11-12T09:00:00Z")          # 시간대 없는 국내 RSS 는 KST

        idx = self.by(items, lambda i: "AI Index" in i["title"])
        self.assertEqual(idx["reps"], ["ai-index-2026"])
        self.assertEqual(idx["newer"], ["ai-index-2026"])
        self.assertEqual(idx["source"], "Example Times")
        jobs = self.by(items, lambda i: "Future of Jobs" in i["title"])
        self.assertEqual(jobs["newer"], [])                           # '2030년까지' 같은 전망 연도는 새 판이 아니다
        self.assertEqual(jobs["summary"], "")                         # 너무 짧은 요약은 버린다
        self.assertEqual(meta["newer"], 2)

        st = {s["name"]: s for s in meta["sources"]}
        self.assertTrue(st["AI타임스"]["ok"] and st["Google 뉴스"]["ok"])
        # 검색어는 reports.json 의 watch 낱말로 만든 만큼 호출된다
        self.assertEqual(self.net.count("news.google.com"), len(build_queries(load_reports(), config.GNEWS_TERMS_PER_QUERY)))
        recent = json.loads((self.tmp / "items-recent.json").read_text(encoding="utf-8"))["items"]
        archive = json.loads((self.tmp / "items-archive.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(recent) + len(archive), len(items))

    def test_rerun_is_idempotent(self):
        first, _ = self.run_cli("--force")
        again, _ = self.run_cli("--force")
        self.assertEqual(sorted(i["id"] for i in first), sorted(i["id"] for i in again))
        web = self.by(again, lambda i: "인터넷이용실태조사" in i["title"])
        self.assertEqual(len(web.get("also", [])), 2)

    def test_one_query_failing_keeps_the_rest(self):
        self.net.fail_first_query = True
        items, meta = self.run_cli("--force")
        st = {s["name"]: s for s in meta["sources"]}
        self.assertTrue(st["Google 뉴스"]["ok"])
        self.assertEqual(meta["counts"]["news"], 4)

    def test_interval_gating(self):
        with mock.patch.dict(config.INTERVAL_HOURS, {"news": 6}):
            self.run_cli()
            n = self.net.count("news.google.com")
            later = NOW + timedelta(hours=3)
            with mock.patch.object(util, "now_utc", lambda: later):
                _, meta = self.run_cli()
        self.assertEqual(self.net.count("news.google.com"), n)   # 6시간이 안 돼 건너뜀
        self.assertEqual(meta["last_run"]["news"], util.iso(NOW))
        self.assertIn("Google 뉴스", {s["name"] for s in meta["sources"]})  # 이전 상태 유지

    def test_retag_cleans_old_items(self):
        """보고서와 연결되지 않는 옛 기사(예: watch 낱말을 바꾼 뒤)는 다음 실행에서 정리된다."""
        old = {"key": "url:https://x.y/old", "id": "old1", "type": "news", "title": "반도체 수출 사상 최대",
               "url": "https://x.y/old", "source": "테스트", "date": util.iso(NOW - timedelta(days=1)), "summary": ""}
        keep = dict(old, key="url:https://x.y/keep", id="keep1", title="글로벌 혁신지수 한국 4위", url="https://x.y/keep")
        self.tmp.mkdir(exist_ok=True)
        (self.tmp / "items-recent.json").write_text(json.dumps({"items": [old, keep]}, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(config.INTERVAL_HOURS, {"news": 6}), \
                mock.patch.object(cli, "_due", lambda *a: []):
            items, _ = self.run_cli()
        ids = {i["id"]: i for i in items}
        self.assertNotIn("old1", ids)
        self.assertEqual(ids["keep1"]["reps"], ["gii-2026"])

    def test_demo_then_real_starts_clean(self):
        _, meta = self.run_cli("--demo")
        self.assertTrue(meta["demo"])
        items, meta = self.run_cli("--force")
        self.assertFalse(meta["demo"])
        self.assertFalse(any("example.com/report-news" in i["url"] for i in items))


class ReportMatchTest(unittest.TestCase):
    def setUp(self):
        self.reports = load_reports(FIX_REPORTS)
        self.rm = ReportMatcher(self.reports)

    def test_match(self):
        m = self.rm.match
        self.assertEqual(m("Stanford AI Index 2026 shows…"), ["ai-index-2026"])
        self.assertEqual(m("IMD's 2026 AI Index ranks Saudi Arabia"), [])            # 흔한 이름은 기관명이 있어야 연결
        self.assertEqual(m("Salesforce Global AI Readiness Index"), [])
        self.assertEqual(m("한국은행 이슈노트 \"가계부채\""), [])
        self.assertEqual(m("한국은행 이슈노트 \"생성형 AI 생산성\""), ["bok-ai-2026"])
        self.assertEqual(m("2026 인터넷 이용 실태조사"), ["kr-internet-2025"])   # 띄어쓰기 무시
        self.assertEqual(m("Global  Innovation Index ranks"), ["gii-2026"])      # 여러 칸 공백
        self.assertEqual(m("Gartner explains its hype cycles"), ["gartner-hc-ai-2026"])
        self.assertEqual(m("Hype cycle anxiety"), [])
        self.assertEqual(m("KAI index fund"), [])                               # 단어 안의 'ai index' 는 무시
        self.assertEqual(m("반도체 수출 사상 최대"), [])

    def test_newer(self):
        self.assertEqual(edition_year({"title": "2024 인공지능산업 실태조사", "date": "2025"}), 2024)
        self.assertEqual(edition_year({"title": "Hype Cycle for AI", "date": "2026-08-28"}), 2026)
        self.assertEqual(self.rm.newer("2025 인공지능산업 실태조사 발표", ["kr-ai-industry-2024"], 2027), ["kr-ai-industry-2024"])
        self.assertEqual(self.rm.newer("Future of Jobs: 170M roles by 2030", ["wef-jobs-2025"], 2027), [])
        self.assertEqual(self.rm.newer("AI Index 2026 review", ["ai-index-2026"], 2027), [])

    def test_queries(self):
        qs = build_queries(self.reports, 6)
        self.assertTrue(qs and all(q["lang"] in ("en", "ko") for q in qs))
        self.assertTrue(all(q["q"].count(" OR ") <= 5 for q in qs))
        self.assertIn('"인터넷이용실태조사"', " ".join(q["q"] for q in qs))


class ContentTest(unittest.TestCase):
    """직접 고치는 내용 파일(보고서·지표·쟁점)이 형식에 맞는지 — 실수를 배포 전에 잡는다."""

    def test_reports(self):
        rep = json.loads((ROOT / "assets" / "reports.json").read_text(encoding="utf-8"))
        topics = {t["key"] for t in rep["topics"]}
        cycles = {c["key"] for c in rep["cycles"]}
        tax = {c["key"] for c in load_taxonomy()["categories"]}
        self.assertEqual(topics, tax)                       # 소식 분류와 보고서 주제가 같은 체계
        ids = set()
        for r in rep["reports"]:
            self.assertNotIn(r["id"], ids)
            ids.add(r["id"])
            for k in ("title", "short", "org", "date", "url", "about", "when", "next", "method", "limits", "korea"):
                self.assertTrue(r.get(k), f"{r['id']}.{k}")
            self.assertIn(r["region"], ("intl", "kr"), r["id"])
            self.assertIn(r["cycle"], cycles, r["id"])
            self.assertTrue(r["months"] and all(1 <= m <= 12 for m in r["months"] + r.get("minor", [])), r["id"])
            if r.get("nextYM"):
                self.assertRegex(r["nextYM"], r"^\d{4}(-\d{2})?$", r["id"])
            self.assertTrue(r.get("watch") or r.get("watchIf"), f"{r['id']}.watch")
            for g in r.get("watchIf") or []:
                self.assertTrue(g.get("terms") and g.get("with"), f"{r['id']}.watchIf")
            self.assertTrue(r["findings"], r["id"])
            for f in r["findings"]:
                self.assertTrue(f["t"].strip(), r["id"])
                self.assertTrue(f["topics"] and set(f["topics"]) <= topics, f"{r['id']}: {f['t'][:20]}")

    def test_report_status(self):
        from collector.reports import STATUSES, active_reports, check_reports, load_reports
        reps = load_reports()
        chs = json.loads((ROOT / "assets" / "chapters.json").read_text(encoding="utf-8"))["reports"]
        self.assertEqual(check_reports(reps, chs), [])                    # 목록 점검 통과
        for r in reps:
            self.assertIn(r.get("status", "active"), STATUSES, r["id"])
        # 발간 중단·수집 중지 보고서는 검색어와 새 기사 연결에서 빠진다
        sample = [dict(reps[0], id="a"), dict(reps[0], id="b", status="paused"), dict(reps[0], id="c", status="ended", statusNote="2026년 발간 중단")]
        self.assertEqual([r["id"] for r in active_reports(sample)], ["a"])
        self.assertEqual(check_reports([dict(reps[0], status="ended")]), [f"{reps[0]['id']}: 발간 중단 사유·시점(statusNote)을 적어 주세요"])

    def test_chapters(self):
        rep = json.loads((ROOT / "assets" / "reports.json").read_text(encoding="utf-8"))
        rids = [r["id"] for r in rep["reports"]]
        chs = json.loads((ROOT / "assets" / "chapters.json").read_text(encoding="utf-8"))["reports"]
        self.assertEqual(set(chs), set(rids))                   # 모든 보고서에 챕터 정리가 있다
        for rid, doc in chs.items():
            self.assertTrue(doc.get("basis"), rid)
            self.assertGreaterEqual(len(doc["chapters"]), 2, rid)
            self.assertTrue(doc.get("lead", "").strip(), f"{rid}: 한 장 요약 첫 문단(lead)")
            self.assertEqual(len(doc.get("keys", [])), 4, f"{rid}: 핵심 수치(keys)는 4개")
            for k in doc["keys"]:
                self.assertTrue(k["v"].strip() and k["l"].strip(), rid)
                self.assertLessEqual(len(k["v"]), 10, f"{rid}: 핵심 수치는 짧게 — {k['v']}")
            for c in doc["chapters"]:
                self.assertTrue(c["t"].strip() and c["s"].strip(), rid)
                self.assertTrue(c["p"] and all(p.strip() for p in c["p"]), f"{rid}: {c['t']}")
                self.assertLessEqual(len(c["s"]), 90, f"{rid}: {c['t']} — 한 장 요약 문장은 짧게")
                # 한 장 요약 줄(b): 챕터마다 2~5줄, 한 줄 45자 이내
                self.assertTrue(2 <= len(c.get("b", [])) <= 5, f"{rid}: {c['t']} — b 줄 수")
                for line in c["b"]:
                    self.assertTrue(line.strip(), rid)
                    self.assertLessEqual(len(line), 45, f"{rid}: {c['t']} — {line}")
                # 더보기 상세(more): 있으면 빈 줄 없이, 상세 정리(p)와 같은 문장을 되풀이하지 않는다
                for m in c.get("more", []):
                    self.assertTrue(m.strip(), rid)
                    self.assertNotIn(m, c["p"], f"{rid}: {c['t']} — more와 p 중복")
                # 대표 그림(fig): 보고서 수치로 다시 그린 막대·추이 그림
                f = c.get("fig")
                if f:
                    where = f"{rid}: {c['t']} — 그림"
                    self.assertIn(f["type"], ("bar", "bar2", "line"), where)
                    self.assertTrue(f["title"].strip(), where)
                    if f["type"] == "line":
                        self.assertTrue(f["series"], where)
                        for ser in f["series"]:
                            self.assertEqual(len(ser["values"]), len(f["x"]), where)
                            self.assertTrue(all(isinstance(v, (int, float)) for v in ser["values"]), where)
                    else:
                        width = 3 if f["type"] == "bar2" else 2
                        if f["type"] == "bar2":
                            self.assertEqual(len(f["series"]), 2, where)
                        self.assertGreaterEqual(len(f["items"]), 2 if f["type"] == "bar" else 1, where)
                        for it in f["items"]:
                            self.assertEqual(len(it), width, where)
                            self.assertTrue(isinstance(it[0], str) and it[0].strip(), where)
                            self.assertTrue(all(isinstance(v, (int, float)) and v >= 0 for v in it[1:]), where)
                # 직접 넣은 그림(img): 경로와 설명(대체 텍스트)·출처가 있어야 한다
                m = c.get("img")
                if m:
                    self.assertTrue(m.get("src") and (m.get("alt") or m.get("cap")) and m.get("credit"), f"{rid}: {c['t']} — img")

    def test_metrics_and_issues(self):
        rep = json.loads((ROOT / "assets" / "reports.json").read_text(encoding="utf-8"))
        rids = {r["id"] for r in rep["reports"]}
        topics = {t["key"] for t in rep["topics"]}
        met = json.loads((ROOT / "assets" / "metrics.json").read_text(encoding="utf-8"))
        charts = {c["id"]: c for c in met["charts"]}
        for g in met["groups"]:
            for cid in g["charts"]:
                self.assertIn(cid, charts, g["key"])
        for c in charts.values():
            self.assertIn(c["type"], ("line", "time", "bar"), c["id"])
            self.assertTrue(set(c.get("topics", [])) <= topics, c["id"])
            if c.get("report"):
                self.assertIn(c["report"], rids, c["id"])
            for s in c["series"]:
                if c["type"] == "time":
                    self.assertTrue(all(c["xMin"] <= p["x"] <= c["xMax"] for p in s["points"]), c["id"])
                    self.assertIn(s["report"], rids, c["id"])
                else:
                    self.assertEqual(len(s["values"]), len(c["x"]), f"{c['id']}: {s['name']}")
            self.assertLessEqual(len(c["series"]), 5, c["id"])          # 계열 색은 5개까지만 검증했다
        iss = json.loads((ROOT / "assets" / "issues.json").read_text(encoding="utf-8"))
        for it in iss["issues"]:
            for k in ("q", "short", "verdict", "korea"):
                self.assertTrue(it.get(k), f"{it['id']}.{k}")
            self.assertTrue(it["why"] and it["rows"], it["id"])
            self.assertTrue(set(it["topics"]) <= topics, it["id"])
            for r in it["rows"]:
                self.assertIn(r["r"], rids, it["id"])
            if it.get("chart"):
                self.assertIn(it["chart"], charts, it["id"])


class UnitTest(unittest.TestCase):
    def setUp(self):
        self.clf = Classifier(load_taxonomy())

    def test_classify(self):
        c = self.clf.classify
        self.assertIn("work", c("AI가 청년 일자리를 줄이나"))
        self.assertIn("ip", c("WIPO patent landscape on generative AI"))
        self.assertIn("infra", c("데이터센터 전력 수요 두 배로"))
        self.assertEqual(c("오늘의 날씨"), ["etc"])

    def test_canon_url(self):
        a = dedup.canon_url("https://www.Example.com/a/b/?utm_source=x&id=3&fbclid=y#top")
        b = dedup.canon_url("http://example.com/a/b?id=3")
        self.assertEqual(a, b)

    def test_norm_title(self):
        self.assertEqual(dedup.norm_title("[단독] 엔비디아, 새 칩 공개 (종합) - 테스트일보"),
                         dedup.norm_title("엔비디아 새 칩 공개"))

    def test_similarity_threshold(self):
        idx = dedup.TitleIndex(0.55, 3)
        base = {"key": "a", "type": "news", "title": "삼성전자, 차세대 HBM4 양산 돌입…AI 반도체 공략"}
        idx.add(base, NOW)
        near = {"key": "b", "type": "news", "title": "삼성전자 차세대 HBM4 양산 돌입, AI반도체 시장 공략"}
        far = {"key": "c", "type": "news", "title": "SK하이닉스, HBM4 샘플 공급 시작"}
        self.assertEqual(idx.find(near, NOW), "a")
        self.assertIsNone(idx.find(far, NOW))
        self.assertIsNone(idx.find(near, NOW + timedelta(days=5)))  # 기간 밖은 비교하지 않는다

    def test_parse_dates(self):
        self.assertEqual(util.iso(util.parse_date("2026-11-13 07:00:00")), "2026-11-12T22:00:00Z")
        self.assertEqual(util.iso(util.parse_date("Thu, 12 Nov 2026 22:42:51 +0000")), "2026-11-12T22:42:51Z")
        self.assertEqual(util.iso(util.parse_date("2026-10-07T20:00:00.000Z")), "2026-10-07T20:00:00Z")
        self.assertEqual(util.iso(util.parse_date("20261110")), "2026-11-09T15:00:00Z")
        self.assertIsNone(util.parse_date("not a date"))

    def test_feed_with_html_entities(self):
        data = b'<rss><channel><item><title>A&nbsp;B &middot; C</title><link>https://x.y/1</link></item></channel></rss>'
        rows = feeds.parse_feed(data)
        self.assertEqual(util.clean_text(rows[0]["title"]), "A B · C")

    def test_redact(self):
        util.register_secret(KEY)
        msg = util.redact(f"http://x/y?accessKey={KEY} and ServiceKey=TESTKEY%2B%2F%3Dabc")
        self.assertNotIn(KEY, msg)
        self.assertNotIn("TESTKEY%2B%2F%3Dabc", msg)

    def test_trim(self):
        old = {"key": "o", "id": "o", "type": "news", "title": "t", "url": "u",
               "date": util.iso(NOW - timedelta(days=config.RETENTION_DAYS["news"] + 1))}
        new = dict(old, key="n", id="n", date=util.iso(NOW))
        self.assertEqual([i["key"] for i in store.trim([old, new], NOW)], ["n"])


# 지표·쟁점 참조 예시(실제 파일 대신) — 지난 판을 가리키는 곳 찾기에 쓴다
MET = {"charts": [{"id": "m1", "report": "ai-index-2026", "series": [{"report": "ai-index-2026"}]},
                  {"id": "m2", "report": "gii-2026", "series": []}]}
ISS = {"issues": [{"id": "q1", "rows": [{"r": "ai-index-2026"}, {"r": "kr-internet-2025"}]}]}


def _with_new_edition(reports: list[dict]) -> list[dict]:
    """AI Index 2027 판을 '추가'한 보고서 목록(지난 판 ai-index-2026 은 그대로)."""
    old = next(r for r in reports if r["id"] == "ai-index-2026")
    new = dict(old, id="ai-index-2027", title="AI Index Report 2027", date="2027-04-14", nextYM="2028-04")
    return [new] + reports


class EditionTest(unittest.TestCase):
    """판 쌓기 — 새 판은 추가하고 지난 판은 남긴다. 수집·알림은 최신판만 본다."""

    def setUp(self):
        self.reports = load_reports(FIX_REPORTS)
        self.more = _with_new_edition(self.reports)

    def test_series(self):
        from collector.reports import series_of
        self.assertEqual(series_of({"id": "ai-index-2026"}), "ai-index")
        self.assertEqual(series_of({"id": "msft-diffusion-2026q2"}), "msft-diffusion")
        self.assertEqual(series_of({"id": "iea-energy-ai"}), "iea-energy-ai")
        self.assertEqual(series_of({"id": "iea-energy-ai-2027"}), "iea-energy-ai")
        self.assertEqual(series_of({"id": "x-2027", "series": "custom"}), "custom")

    def test_current_and_superseded(self):
        from collector.reports import check_reports, current_reports, superseded
        # 지금 목록은 판이 하나씩이라 바뀌는 것이 없다
        self.assertEqual(current_reports(self.reports), self.reports)
        self.assertEqual(superseded(self.reports), {})
        cur = current_reports(self.more)
        self.assertEqual(len(cur), len(self.reports))
        self.assertIn("ai-index-2027", [r["id"] for r in cur])
        self.assertNotIn("ai-index-2026", [r["id"] for r in cur])
        self.assertEqual(superseded(self.more), {"ai-index-2026": "ai-index-2027"})
        chs = {r["id"]: {} for r in self.reports}
        self.assertEqual(check_reports(self.more, dict(chs, **{"ai-index-2027": {}})), [])
        self.assertEqual(check_reports(self.more, chs), ["ai-index-2027: chapters.json에 챕터 정리가 없습니다"])
        # 지난 판은 watch 가 없어도 된다(새 소식은 최신판으로만 찾는다)
        bare = [dict(r, watch=[], watchIf=[]) if r["id"] == "ai-index-2026" else r for r in self.more]
        self.assertEqual(check_reports(bare), [])
        same = [dict(r, date="2026-04-13") if r["id"] == "ai-index-2027" else r for r in self.more]
        self.assertTrue(any("발간일" in p for p in check_reports(same)))

    def test_news_links_to_latest_only(self):
        from collector.reports import current_reports
        rm = ReportMatcher(current_reports(self.more))
        self.assertEqual(rm.match("Stanford AI Index 2027 released"), ["ai-index-2027"])
        self.assertEqual(rm.newer("Stanford AI Index 2027 released", ["ai-index-2027"], 2028), [])
        self.assertEqual(rm.newer("Stanford AI Index 2028 preview", ["ai-index-2027"], 2028), ["ai-index-2027"])

    def test_edition_notes(self):
        from collector.reports import edition_notes
        self.assertEqual(edition_notes(self.reports, MET, ISS), [])
        self.assertEqual(edition_notes(self.more, MET, ISS), [
            "metrics.json m1: 지난 판 ai-index-2026 → 최신판 ai-index-2027 로 바꿀지 확인",
            "issues.json q1: 지난 판 ai-index-2026 → 최신판 ai-index-2027 로 바꿀지 확인"])


class NotifyTest(unittest.TestCase):
    """새 판 알림 — 네트워크 없이 '무엇을 할지'만 확인한다."""

    def setUp(self):
        from collector import notify
        self.n = notify
        self.reports = load_reports(FIX_REPORTS)
        self.art = {"title": "Stanford AI Index 2027 released", "url": "https://n.example/1", "source": "N",
                    "date": util.iso(NOW - timedelta(days=1)), "newer": ["ai-index-2026"], "reps": ["ai-index-2026"]}

    def test_due_ym(self):
        self.assertEqual(self.n.due_ym({"nextYM": "2027-04"}), "2027-04")
        self.assertEqual(self.n.due_ym({"nextYM": "2027", "months": [8]}), "2027-08")
        self.assertEqual(self.n.due_ym({"nextYM": "2031"}), "2031-12")
        self.assertEqual(self.n.due_ym({}), "")

    def test_signals(self):
        old = dict(self.art, url="https://n.example/old", date=util.iso(NOW - timedelta(days=60)))
        sig = self.n.signals(self.reports, [self.art, old], NOW, days=21, due=True)
        self.assertEqual([a["url"] for a in sig["ai-index-2026"]["arts"]], ["https://n.example/1"])
        # 2026-11 기준 발간 예정 달이 지난 보고서는 없다(첫 실행에 Issue 가 쏟아지지 않는다)
        self.assertEqual({k for k, v in sig.items() if v["due"]}, set())
        later = NOW + timedelta(days=40)                                  # 2026-12 → 정부 AI 준비도(nextYM 2026-12)
        self.assertEqual(self.n.signals(self.reports, [], later, days=21, due=True)["oxford-gari-2025"]["due"], "2026-12")
        self.assertEqual(self.n.signals(self.reports, [], later, days=21, due=False), {})
        # 새 판이 추가되면 지난 판으로는 신호를 내지 않는다
        self.assertNotIn("ai-index-2026", self.n.signals(_with_new_edition(self.reports), [self.art], NOW, days=21, due=False))

    def test_plan(self):
        sig = self.n.signals(self.reports, [self.art], NOW, days=21, due=False)
        acts = self.n.plan(self.reports, sig, [])
        self.assertEqual([(a["op"], a["rid"]) for a in acts], [("create", "ai-index-2026")])
        body = acts[0]["body"]
        self.assertIn("<!-- arr-edition:ai-index-2026 -->", body)
        self.assertIn("ai-index-2027", body)                               # 새 id 제안
        self.assertIn("https://n.example/1", body)
        issue = {"number": 7, "state": "open", "body": body, "text": body}
        self.assertEqual(self.n.plan(self.reports, sig, [issue]), [])      # 같은 기사로 또 알리지 않는다
        more = dict(self.art, url="https://n.example/2")
        sig2 = self.n.signals(self.reports, [self.art, more], NOW, days=21, due=False)
        acts = self.n.plan(self.reports, sig2, [issue])
        self.assertEqual([(a["op"], a["number"], a["reopen"]) for a in acts], [("comment", 7, False)])
        self.assertNotIn("https://n.example/1", acts[0]["body"])
        acts = self.n.plan(self.reports, sig2, [dict(issue, state="closed")])
        self.assertTrue(acts[0]["reopen"])                                 # 닫아 둔 Issue 는 새 기사가 오면 다시 연다
        due_only = {"ai-index-2026": {"arts": [], "due": "2027-04"}}
        self.assertEqual(self.n.plan(self.reports, due_only, [dict(issue, state="closed")]), [])
        # 새 판이 추가되면 열린 Issue 를 닫는다
        acts = self.n.plan(_with_new_edition(self.reports), {}, [issue])
        self.assertEqual([(a["op"], a["number"]) for a in acts], [("close", 7)])
        from collector.reports import edition_notes
        notes = edition_notes(_with_new_edition(self.reports), MET, ISS)
        acts = self.n.plan(_with_new_edition(self.reports), {}, [issue], notes)
        self.assertIn("issues.json q1", acts[0]["body"])                    # 닫을 때 남은 지난 판 참조를 알린다


if __name__ == "__main__":
    unittest.main()
