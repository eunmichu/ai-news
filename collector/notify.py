"""새 판 알림 — 새 판 신호가 잡힌 보고서마다 GitHub Issue 를 하나 열어 둔다(갱신은 사람이 한다).

    python -m collector.notify --dry-run     # 무엇을 할지 보여 주기만(토큰 불필요)
    python -m collector.notify               # Actions: GITHUB_TOKEN·GITHUB_REPOSITORY 로 Issue 열기·댓글·닫기

신호
  1) '새 판 가능성' 기사 — 제목에 최신판보다 뒤의 연도가 나온 보고서 소식(최근 NOTIFY_DAYS 일)
  2) 발간 예정 달 — reports.json 의 nextYM 이 이번 달이 되었거나 지났다(연도만 있으면 months 의 첫 달로 본다)
하는 일
  - 신호가 있는데 Issue 가 없으면 연다. 이미 있으면 처음 보는 기사만 댓글로 더한다.
    닫아 둔 Issue 는 처음 보는 기사가 올 때만 다시 연다(발간 예정 달 신호로는 다시 열지 않는다).
  - reports.json 에 새 판이 추가되어 지난 판이 된 보고서의 열린 Issue 는 자동으로 닫는다.
Issue 는 본문의 표지(<!-- arr-edition:보고서 id -->)와 라벨(new-edition)로 알아본다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from . import config, store, util
from .reports import edition_notes, edition_year, editions, load_reports, series_of, status, superseded

LABEL = "new-edition"
MARK = "<!-- arr-edition:{} -->"
_MARK_RX = re.compile(r"<!-- arr-edition:([\w.\-]+) -->")
MAX_ARTICLES = 10
MAX_CREATE = 10            # 한 번에 너무 많이 열지 않는다(설정을 잘못 고쳤을 때 대비)
STEP = {"biennial": 2, "5y": 5}


# ── 신호 ─────────────────────────────────────────────────────────
def due_ym(r: dict) -> str:
    """발간 예정 달(YYYY-MM). nextYM 이 연도뿐이면 months 의 첫 달, 그것도 없으면 12월로 본다."""
    n = str(r.get("nextYM") or "")
    if re.fullmatch(r"\d{4}-\d{2}", n):
        return n
    if re.fullmatch(r"\d{4}", n):
        months = r.get("months") or [12]
        return f"{n}-{int(months[0]):02d}"
    return ""


def signals(reports: list[dict], items: list[dict], now: datetime, days: int | None = None,
            due: bool | None = None) -> dict[str, dict]:
    """보고서 id → {arts: [기사], due: 'YYYY-MM' 또는 ''} — 최신판·수집 중인 보고서만."""
    days = config.NOTIFY_DAYS if days is None else days
    due = config.NOTIFY_DUE if due is None else due
    old = superseded(reports)
    live = {r["id"]: r for r in reports if r["id"] not in old and status(r) == "active"}
    cut = util.iso(now - timedelta(days=days))
    out: dict[str, dict] = {}
    for it in sorted(items, key=lambda i: i.get("date", ""), reverse=True):
        if it.get("date", "") < cut:
            continue
        for rid in it.get("newer") or []:
            if rid in live:
                arts = out.setdefault(rid, {"arts": [], "due": ""})["arts"]
                if len(arts) < MAX_ARTICLES and all(a["url"] != it["url"] for a in arts):
                    arts.append({k: it.get(k, "") for k in ("title", "url", "source", "date")})
    if due:
        ym = now.astimezone(util.KST).strftime("%Y-%m")
        for rid, r in live.items():
            d = due_ym(r)
            if d and d <= ym:
                out.setdefault(rid, {"arts": [], "due": ""})["due"] = d
    return out


# ── Issue 본문 ───────────────────────────────────────────────────
def suggest_id(r: dict) -> str:
    s = series_of(r)
    if r.get("cycle") == "quarterly":
        return f"{s}-연도q분기 (예: {s}-{edition_year(r) or '2027'}q3)"
    y = edition_year(r)
    return f"{s}-{y + STEP.get(r.get('cycle'), 1)}" if y else f"{s}-연도"


def _art_line(a: dict) -> str:
    title = re.sub(r"[\[\]]", "", a.get("title", "")).strip() or a["url"]
    return f"- [{title}]({a['url']}) — {a.get('source', '')} · {str(a.get('date', ''))[:10]}"


def issue_title(r: dict) -> str:
    return f"[새 판 확인] {r.get('short') or r['id']} — {r.get('title', '')} 다음 판"


def issue_body(r: dict, sig: dict, past: list[str]) -> str:
    lines = [MARK.format(r["id"]),
             f"**{r.get('short', r['id'])}**({r.get('org', '')})의 다음 판이 나왔을 수 있습니다.", "",
             f"- 지금 사이트의 최신판: {r.get('title', '')} ({r.get('date', '')}) · id `{r['id']}`",
             f"- 예상 발간: {r.get('next', '미정')}" + (f" (nextYM {r['nextYM']})" if r.get("nextYM") else ""),
             f"- 원문 페이지: {r.get('url', '')}"]
    if past:
        lines.append(f"- 쌓여 있는 지난 판: {', '.join(f'`{x}`' for x in past)}")
    lines += ["", "### 감지된 신호"]
    lines += [_art_line(a) for a in sig.get("arts", [])]
    if sig.get("due"):
        lines.append(f"- 발간 예정 달({sig['due']})이 되었습니다. 원문 페이지에서 새 판이 나왔는지 확인하세요.")
    lines += ["", "### 갱신 순서(지난 판은 지우지 않고 쌓습니다)",
              "1. 원문 페이지에서 새 판이 실제로 나왔는지 확인합니다. 아니면 이 Issue 를 닫으세요(새 기사가 잡히면 다시 열립니다).",
              f"2. `assets/reports.json` 의 `reports` 에 새 판 항목을 **추가**합니다. 지난 판 `{r['id']}` 항목은 그대로 둡니다.",
              f"   - 새 id 예: `{suggest_id(r)}` — 같은 보고서로 묶이도록 id 를 `{series_of(r)}-` 로 시작하게 하거나 `\"series\": \"{series_of(r)}\"` 를 적습니다.",
              "   - `date` 는 새 판 발간일 — 같은 묶음에서 발간일이 가장 늦은 판이 최신판이 됩니다.",
              "3. `assets/chapters.json` 의 `reports` 에 같은 새 id 로 챕터 정리를 추가합니다.",
              "4. 필요하면 `metrics.json` 에 새 판 수치를 더하고, `issues.json` 의 행(`r`)을 새 id 로 바꿉니다.",
              "5. 올리면 사이트에 '판 선택'이 생기고 이 Issue 는 자동으로 닫힙니다. 닫을 때 아직 지난 판을 가리키는 지표·쟁점을 댓글로 알려 줍니다.",
              "",
              "> Claude 에게 맡길 때: 새 판 PDF(또는 링크)와 이 Issue 를 주고 "
              f"\"`{r['id']}` 항목과 같은 형식으로 새 판의 reports.json·chapters.json 항목을 만들어 줘\" 라고 요청하세요."]
    return "\n".join(lines) + "\n"


# ── 계획(네트워크 없음) ──────────────────────────────────────────
def issue_rid(issue: dict) -> str:
    m = _MARK_RX.search(issue.get("body") or "")
    return m.group(1) if m else ""


def plan(reports: list[dict], sigs: dict[str, dict], issues: list[dict], notes: list[str] | None = None) -> list[dict]:
    """할 일 목록. issues: [{number, state, body, text(본문+댓글)}] — 같은 보고서는 가장 최근 Issue 하나만 본다.
    notes: edition_notes() 결과 — Issue 를 닫을 때 아직 지난 판을 가리키는 지표·쟁점을 함께 알린다."""
    rep = {r["id"]: r for r in reports}
    old = superseded(reports)
    eds = editions(reports)
    by_rid: dict[str, dict] = {}
    for it in sorted(issues, key=lambda i: i["number"]):
        rid = issue_rid(it)
        if rid:
            by_rid[rid] = it
    acts: list[dict] = []
    for rid, it in by_rid.items():
        if rid in old and it["state"] == "open":
            new = rep[old[rid]]
            left = [n for n in notes or [] if f"지난 판 {rid} " in n]
            body = (f"새 판 `{new['id']}`({new.get('title', '')}, {new.get('date', '')})이 reports.json 에 추가되어 닫습니다. "
                    f"지난 판 `{rid}` 는 사이트의 '판 선택'에서 계속 볼 수 있습니다.")
            if left:
                body += ("\n\n아직 지난 판을 가리키는 곳입니다. 새 판 수치로 바꿀 곳만 고치세요"
                         "(일부러 지난 판을 인용한 곳은 그대로 두어도 됩니다).\n\n" + "\n".join(f"- {n}" for n in left))
            acts.append({"op": "close", "number": it["number"], "rid": rid, "body": body + "\n"})
    created = 0
    for rid, sig in sigs.items():
        r = rep.get(rid)
        if not r:
            continue
        it = by_rid.get(rid)
        if it is None:
            if created >= MAX_CREATE:
                continue
            created += 1
            past = [x["id"] for x in eds[series_of(r)][1:]]
            acts.append({"op": "create", "rid": rid, "title": issue_title(r), "body": issue_body(r, sig, past)})
            continue
        fresh = [a for a in sig.get("arts", []) if a["url"] not in (it.get("text") or it.get("body") or "")]
        if fresh:
            acts.append({"op": "comment", "number": it["number"], "rid": rid, "reopen": it["state"] != "open",
                         "body": "새 판 신호가 더 잡혔습니다.\n\n" + "\n".join(_art_line(a) for a in fresh) + "\n"})
    return acts


# ── GitHub ───────────────────────────────────────────────────────
class GitHub:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self.token, self.api = repo, token, api.rstrip("/")
        util.register_secret(token)

    def _req(self, method: str, path: str, data: dict | None = None):
        req = urllib.request.Request(f"{self.api}{path}", method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "User-Agent": util.UA,
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(util.redact(f"GitHub {method} {path} → {e.code} {detail}")) from None

    def issues(self) -> list[dict]:
        out = []
        for page in range(1, 11):
            rows = self._req("GET", f"/repos/{self.repo}/issues?state=all&labels={LABEL}&per_page=100&page={page}") or []
            out += [r for r in rows if "pull_request" not in r]
            if len(rows) < 100:
                break
        return [{"number": r["number"], "state": r["state"], "body": r.get("body") or ""} for r in out]

    def text(self, number: int, body: str) -> str:
        rows = self._req("GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100") or []
        return body + "\n" + "\n".join(c.get("body") or "" for c in rows)

    def ensure_label(self) -> None:
        try:
            self._req("POST", f"/repos/{self.repo}/labels",
                      {"name": LABEL, "color": "5b6cff", "description": "보고서 새 판 확인 필요(AI Report Radar)"})
        except RuntimeError as e:
            if "422" not in str(e):      # 이미 있음
                raise

    def run(self, act: dict) -> str:
        if act["op"] == "create":
            r = self._req("POST", f"/repos/{self.repo}/issues", {"title": act["title"], "body": act["body"], "labels": [LABEL]})
            return f"#{r['number']} 열기 — {act['title']}"
        n = act["number"]
        self._req("POST", f"/repos/{self.repo}/issues/{n}/comments", {"body": act["body"]})
        if act["op"] == "close":
            self._req("PATCH", f"/repos/{self.repo}/issues/{n}", {"state": "closed", "state_reason": "completed"})
            return f"#{n} 닫기 — {act['rid']} 새 판 반영됨"
        if act.get("reopen"):
            self._req("PATCH", f"/repos/{self.repo}/issues/{n}", {"state": "open"})
            return f"#{n} 다시 열고 댓글 — {act['rid']}"
        return f"#{n} 댓글 — {act['rid']}"


def _describe(act: dict) -> str:
    if act["op"] == "create":
        return f"열기: {act['title']}"
    return {"close": "닫기", "comment": "댓글" + ("(다시 열기)" if act.get("reopen") else "")}[act["op"]] + f": #{act['number']} {act['rid']}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m collector.notify", description="새 판 알림(GitHub Issue)")
    ap.add_argument("--data-dir", default=str(config.DATA_DIR))
    ap.add_argument("--dry-run", action="store_true", help="Issue 를 건드리지 않고 할 일만 보여 준다")
    args = ap.parse_args(argv)

    from pathlib import Path
    now = util.now_utc()
    reports = load_reports()
    items, _ = store.load(Path(args.data_dir))
    sigs = signals(reports, items, now)
    print(f"새 판 신호: {len(sigs)}종" + "".join(
        f"\n  · {rid}: 기사 {len(s['arts'])}건" + (f", 발간 예정 달 {s['due']}" if s["due"] else "") for rid, s in sigs.items()))

    token, repo = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    gh = GitHub(repo, token) if token and repo else None
    if gh is None and not args.dry_run:
        print("GITHUB_TOKEN·GITHUB_REPOSITORY 가 없어 할 일만 보여 줍니다(--dry-run 과 같음).")
    issues = gh.issues() if gh else []
    for it in issues:
        if gh and issue_rid(it) in sigs:
            it["text"] = gh.text(it["number"], it["body"])
    assets = config.ROOT / "assets"
    notes = edition_notes(reports, *(json.loads((assets / f).read_text(encoding="utf-8")) for f in ("metrics.json", "issues.json")))
    acts = plan(reports, sigs, issues, notes)
    if not acts:
        print("할 일 없음")
    done = []
    for a in acts:
        if gh and not args.dry_run:
            if a["op"] == "create" and not any(d.startswith("label") for d in done):
                gh.ensure_label()
                done.append("label")
            msg = gh.run(a)
        else:
            msg = "(미리보기) " + _describe(a)
        done.append(msg)
        print("  " + msg)

    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n### 새 판 알림\n\n" + ("\n".join(f"- {d}" for d in done if d != "label") or "- 할 일 없음") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
