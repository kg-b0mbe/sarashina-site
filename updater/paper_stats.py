#!/usr/bin/env python3
"""論文の引用件数をSemantic Scholarから取り込む（Mac miniで実行）。

Google Scholarには公式APIがなく、機械的な取得も規約で禁止されているため、
公式の無料APIがあるSemantic Scholarを使う。ページには各論文の「Google Scholarで見る」リンクも出す。
数字はAPIからそのまま取るだけで、LLMは使わない。変化があれば main に直接コミットしてpushする。

  python3 updater/paper_stats.py --dry-run   # 取得して表示するだけ
  python3 updater/paper_stats.py             # data/paper_stats.json を更新 → 検証 → コミット・push

papers.json の各論文は、次の順で探す。
  1. s2_ids に書いたID（例: "arXiv:2606.25369", "ACL:2025.findings-acl.57", "DOI:..."）
  2. links.paper のURLから arXiv / ACL Anthology のIDを読み取ったもの
  3. 英語タイトルでの完全一致検索（タイトルが一致しなければ採用しない）
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ROOT / "data" / "papers.json"
OUT = ROOT / "data" / "paper_stats.json"
JST = dt.timezone(dt.timedelta(hours=9))
API = "https://api.semanticscholar.org/graph/v1/paper/"
FIELDS = "title,citationCount,influentialCitationCount,url"
UA = "sarashina-fan-site-updater/1.0"
GAP = 3.0  # 1件ごとの間隔（秒）。キーなしのAPIは共用の枠なので、ゆっくり聞く


def api_key():
    """updater/.env に S2_API_KEY があれば使う（Semantic Scholarの無料キー。なくても動く）。"""
    env = Path(__file__).resolve().parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("S2_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


KEY = api_key()


def get_json(url, tries=4):
    """キーなしのAPIは混み合うと429を返すので、だんだん長く待って再試行する。"""
    for i in range(tries):
        headers = {"User-Agent": UA}
        if KEY:
            headers["x-api-key"] = KEY
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 and i < tries - 1:
                time.sleep((15, 45, 90)[min(i, 2)])
                continue
            raise
    return None


def norm(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def candidate_ids(p):
    ids = list(p.get("s2_ids") or [])
    url = (p.get("links") or {}).get("paper", "")
    m = re.search(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})", url)
    if m:
        ids.append("arXiv:" + m.group(1))
    m = re.search(r"aclanthology\.org/([0-9]{4}\.[a-z0-9-]+\.\d+)", url)
    if m:
        ids.append("ACL:" + m.group(1))
    return ids


def lookup(p):
    for pid in candidate_ids(p):
        d = get_json(API + urllib.parse.quote(pid, safe=":") + "?fields=" + FIELDS)
        time.sleep(GAP)
        if d and d.get("citationCount") is not None:
            return d, pid
    q = urllib.parse.quote(p["title"])
    d = get_json(API + "search/match?query=" + q + "&fields=" + FIELDS)
    time.sleep(GAP)
    if d and d.get("data"):
        best = d["data"][0]
        if norm(best.get("title")) == norm(p["title"]):
            return best, "title"
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
            sys.exit("作業ツリーに未コミットの変更があります。片付けてから実行してください。")
        subprocess.run(["git", "checkout", "main"], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["git", "pull", "--ff-only"], cwd=ROOT, check=True, capture_output=True)

    papers = json.loads(PAPERS.read_text(encoding="utf-8"))["papers"]
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8")).get("papers", {}) if OUT.exists() else {}
    except Exception:
        prev = {}
    stats, missing, kept = {}, [], []
    for p in papers:
        try:
            d, how = lookup(p)
        except Exception as e:
            # 混雑などで聞けなかったときは、前回の数字をそのまま残す（消さない）
            print(f"{p['id']}: 取得エラー {e}（前回の数字を残します）" if p["id"] in prev else f"{p['id']}: 取得エラー {e}")
            if p["id"] in prev:
                stats[p["id"]] = prev[p["id"]]
                kept.append(p["id"])
            continue
        if not d:
            missing.append(p["id"])
            continue
        stats[p["id"]] = {
            "citations": int(d.get("citationCount") or 0),
            "influential": int(d.get("influentialCitationCount") or 0),
            "s2_url": d.get("url"),
            "matched_by": how,
        }

    doc = {
        "fetched_at": dt.datetime.now(JST).strftime("%Y-%m-%d"),
        "source": "Semantic Scholar Academic Graph API",
        "papers": dict(sorted(stats.items())),
        "not_found": sorted(missing),
    }

    if args.dry_run:
        for pid, s in sorted(stats.items(), key=lambda kv: -kv[1]["citations"]):
            print(f"{s['citations']:>6}  {pid}（{s['matched_by']}）")
        if missing:
            print("見つからなかった論文:", ", ".join(missing))
        if kept:
            print("混雑で聞けず、前回の数字を残した論文:", ", ".join(kept))
        return

    def strip_date(t):
        try:
            j = json.loads(t); j.pop("fetched_at", None); return j
        except Exception:
            return None
    old = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    new = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if strip_date(old) == strip_date(new):
        print("引用件数に変化はありません。")
        return
    OUT.write_text(new, encoding="utf-8")
    build = subprocess.run([sys.executable, "scripts/build.py"], cwd=ROOT, capture_output=True, text=True)
    if build.returncode != 0:
        subprocess.run(["git", "checkout", "--", "data", "docs"], cwd=ROOT)
        sys.exit("検証に失敗したので元に戻しました:\n" + build.stdout[-1000:])
    subprocess.run(["git", "add", "data/paper_stats.json", "docs"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f"論文の引用件数を更新（{doc['fetched_at']}）"], cwd=ROOT, check=True)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)
    print("更新してpushしました。")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lock import single_run
    with single_run():
        main()
