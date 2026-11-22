#!/usr/bin/env python3
"""Hugging Faceのダウンロード数・いいね数を取り込む（Mac miniで実行）。

数字はHugging FaceのAPIからそのまま取るだけで、LLMは使わない。
そのため、プルリクエストにはせず、変わっていれば main に直接コミットしてpushする。

  python3 updater/hf_stats.py --dry-run   # 取得して表示するだけ
  python3 updater/hf_stats.py             # data/hf_stats.json を更新 → 検証 → コミット・push

外部ライブラリは使わない（macOS標準のpython3で動く）。
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "hf_stats.json"
MODELS = ROOT / "data" / "models.json"
JST = dt.timezone(dt.timedelta(hours=9))
UA = "sarashina-site-updater/1.0"

# 比べる国産オープンモデルの公開元（Hugging Faceのアカウント名）
ORGS = {
    "sbintuitions": "SB Intuitions（Sarashina）",
    "llm-jp": "LLM-jp（国立情報学研究所）",
    "pfnet": "Preferred Networks（PLaMo）",
    "tokyotech-llm": "Swallow（東京科学大学など）",
    "elyza": "ELYZA",
    "stockmark": "ストックマーク",
}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def list_models(author):
    base = f"https://huggingface.co/api/models?author={author}&limit=1000"
    try:
        return get_json(base + "&expand[]=downloads&expand[]=downloadsAllTime&expand[]=likes")
    except Exception:
        return get_json(base)  # 拡張項目が使えなければ、標準の downloads（30日）と likes だけ


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
            sys.exit("作業ツリーに未コミットの変更があります。片付けてから実行してください。")
        subprocess.run(["git", "checkout", "main"], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["git", "pull", "--ff-only"], cwd=ROOT, check=True, capture_output=True)

    repos, orgs = {}, {}
    for author, label in ORGS.items():
        try:
            items = list_models(author)
        except Exception as e:
            print(f"{author} を取得できませんでした: {e}")
            continue
        total = {"label": label, "models": 0, "downloads": 0, "downloads_all_time": 0, "likes": 0}
        per = []  # 公開元の中で、どのモデルが数字を稼いでいるかを見るため
        for it in items:
            rid = it.get("id") or it.get("modelId")
            d30 = int(it.get("downloads") or 0)
            dall = it.get("downloadsAllTime")
            likes = int(it.get("likes") or 0)
            total["models"] += 1
            total["downloads"] += d30
            total["likes"] += likes
            if dall is not None:
                total["downloads_all_time"] += int(dall)
            per.append({"id": rid, "downloads": d30, "downloads_all_time": None if dall is None else int(dall), "likes": likes})
            if author == "sbintuitions":
                repos[rid] = {"downloads": d30, "downloads_all_time": None if dall is None else int(dall), "likes": likes}
        if total["downloads_all_time"] == 0:
            total["downloads_all_time"] = None
        # 物差しごとの上位3モデル
        total["top"] = {}
        for metric in ("downloads", "downloads_all_time", "likes"):
            ranked = sorted((r for r in per if r[metric]), key=lambda r: -r[metric])[:3]
            total["top"][metric] = [{"id": r["id"], "value": r[metric]} for r in ranked]
        orgs[author] = total

    if "sbintuitions" not in orgs:
        sys.exit("SB Intuitionsのデータが取れなかったので、今回は更新しません。")

    doc = {
        "fetched_at": dt.datetime.now(JST).strftime("%Y-%m-%d"),
        "note": "downloads は直近30日、downloads_all_time は累計（Hugging Face API）。",
        "repos": dict(sorted(repos.items())),
        "orgs": orgs,
    }

    if args.dry_run:
        top = sorted(repos.items(), key=lambda kv: -kv[1]["downloads"])[:10]
        for rid, s in top:
            print(f"{s['downloads']:>9,}  {rid}")
        for a, o in orgs.items():
            print(f"{o['downloads']:>9,}  {o['label']}（{o['models']}モデル）")
            for t in o["top"]["downloads"]:
                share = t["value"] / o["downloads"] * 100 if o["downloads"] else 0
                print(f"{'':>11}{t['value']:>9,}（{share:.0f}%）  {t['id']}")
        return

    old = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    new = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    # 日付だけが違う場合はコミットしない
    def strip_date(t):
        try:
            j = json.loads(t); j.pop("fetched_at", None); return j
        except Exception:
            return None
    if strip_date(old) == strip_date(new):
        print("数字に変化はありません。")
        return
    OUT.write_text(new, encoding="utf-8")
    build = subprocess.run([sys.executable, "scripts/build.py"], cwd=ROOT, capture_output=True, text=True)
    if build.returncode != 0:
        subprocess.run(["git", "checkout", "--", "data", "docs"], cwd=ROOT)
        sys.exit("検証に失敗したので元に戻しました:\n" + build.stdout[-1000:])
    subprocess.run(["git", "add", "data/hf_stats.json", "docs"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f"Hugging Faceの数字を更新（{doc['fetched_at']}）"], cwd=ROOT, check=True)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)
    print("更新してpushしました。")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lock import single_run
    with single_run():
        main()
