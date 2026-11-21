#!/usr/bin/env python3
"""data/*.json を検証して、GitHub Pages用の docs/ にページを生成する。

Sarashinaなどのローカルモデルが data/ のJSONを書き換えたあと、必ずこれを通す。
検証に失敗したら exit code 1 で止まるので、Hermesはpushしないこと。

生成物: docs/models.html（モデル目録）, docs/timeline.html（年譜）, docs/papers.html（論文手帖）, docs/utaawase.html（国産AI歌合）, docs/chat.html（茶話）, docs/about.html（はしがき）, docs/index.html（ホーム）
サイト名・ナビ・奥付はこのファイルの SITE_NAME / NAV / FOOTER で一括管理する。

使い方: python3 scripts/build.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "models.json"
EVENTS = ROOT / "data" / "events.json"
PAPERS = ROOT / "data" / "papers.json"
KOKUSAN = ROOT / "data" / "kokusan.json"
HF_STATS = ROOT / "data" / "hf_stats.json"   # updater/hf_stats.py が作る。なければ数字は表示しない
CHAT = ROOT / "data" / "chat.json"   # updater/chat.py が2時間ごとに追加する
MEDIA = ROOT / "data" / "media.json"            # 研究者の本・出演動画・インタビュー（論文手帖の下に出す）
PAPER_STATS = ROOT / "data" / "paper_stats.json"   # updater/paper_stats.py が作る。なければ引用件数は表示しない
TEMPLATES = ROOT / "templates"
OUT_DIR = ROOT / "docs"
PAGES = {"home.template.html": "index.html", "models.template.html": "models.html", "timeline.template.html": "timeline.html",
         "papers.template.html": "papers.html", "utaawase.template.html": "utaawase.html",
         "chat.template.html": "chat.html", "about.template.html": "about.html"}
SITE_NAME = "Sarashina 帖"
NAV = [("models.html", "モデル目録"), ("timeline.html", "年譜"), ("papers.html", "論文手帖"),
       ("utaawase.html", "歌合"), ("chat.html", "茶話"),
       ("about.html", "はしがき")]
TOPICS = {"pretraining", "evaluation", "speech", "dialogue", "trust"}


def header(current):
    parts = []
    for href, label in NAV:
        cur = ' aria-current="page"' if href == current else ""
        parts.append(f'<a href="{href}"{cur}>{label}</a>')
    links = "".join(parts)
    return (f'<header class="masthead"><div class="masthead-inner">'
            f'<a class="site-name" href="index.html">{SITE_NAME}<small>非公式ファンサイト</small></a>'
            f'<nav class="site-nav" aria-label="サイト内">{links}</nav></div></header>')


FOOTER = ('<footer class="colophon"><div class="colophon-inner">'
          '国産AI「Sarashina」を応援する個人の非公式ファンサイトです。SB Intuitions株式会社とは関係ありません。'
          '正確な最新情報は公式サイトとHugging Faceをご確認ください。<br>'
          'データ更新日：<span id="updated"></span>'
          '<div class="irome-title">このサイトの色　紅梅と縹の匂（上から）</div>'
          '<div class="irome">'
          '<span style="--c:var(--suou)"><i></i>蘇芳</span>'
          '<span style="--c:var(--kobai)"><i></i>紅梅</span>'
          '<span style="--c:var(--usubeni)"><i></i>薄紅</span>'
          '<span style="--c:var(--shiro)"><i></i>白</span>'
          '<span style="--c:var(--usuhanada)"><i></i>薄縹</span>'
          '<span style="--c:var(--hanada)"><i></i>縹</span>'
          '</div>'
          '</div></footer>')


def validate_papers(doc, model_ids):
    errors = []
    ids = set()
    for i, p in enumerate(doc.get("papers", [])):
        where = "paper:" + str(p.get("id", f"#{i}"))
        for k in ["id", "title", "title_ja", "authors", "venue", "date", "topic", "summary",
                  "points", "sarashina", "models", "links", "sources"]:
            if k not in p:
                errors.append(f"{where}: 項目 '{k}' がありません")
        if p.get("id") in ids:
            errors.append(f"{where}: id が重複しています")
        ids.add(p.get("id"))
        if not DATE_RE.match(str(p.get("date"))):
            errors.append(f"{where}: date は YYYY-MM か YYYY-MM-DD で書いてください")
        if p.get("topic") not in TOPICS:
            errors.append(f"{where}: topic '{p.get('topic')}' は不正です")
        for mid in p.get("models", []):
            if mid not in model_ids:
                errors.append(f"{where}: models.json に存在しないモデル '{mid}' を参照しています")
        urls = list((p.get("links") or {}).values()) + list(p.get("sources") or [])
        if not p.get("sources") or not all(str(u).startswith("https://") for u in urls):
            errors.append(f"{where}: リンクと出典は https:// で、出典は最低1つ必要です")
    return errors
EVENT_KINDS = {"company", "release", "service", "milestone"}
COMPANIES = {"openai", "anthropic", "google", "deepseek", "alibaba", "moonshot", "zhipu"}  # 後ろ4つは中国

KINDS = {"base", "instruct", "thinking", "vision", "ocr", "tts",
         "embedding", "rerank", "guard", "translate"}
AVAIL = {"open_weights", "api"}
SPARK = {"ok", "quant", "no", "api"}
REQUIRED = ["id", "name", "generation", "kind", "modality", "params_b", "architecture",
            "training_tokens", "context_length", "license", "availability", "released",
            "hf_repo", "spark", "notes", "sources"]
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def validate(doc):
    errors = []
    ids = set()
    for i, m in enumerate(doc.get("models", [])):
        where = m.get("id", f"#{i}")
        for k in REQUIRED:
            if k not in m:
                errors.append(f"{where}: 項目 '{k}' がありません")
        if m.get("id") in ids:
            errors.append(f"{where}: id が重複しています")
        ids.add(m.get("id"))
        if m.get("kind") not in KINDS:
            errors.append(f"{where}: kind '{m.get('kind')}' は不正です")
        if m.get("availability") not in AVAIL:
            errors.append(f"{where}: availability '{m.get('availability')}' は不正です")
        if m.get("spark") not in SPARK:
            errors.append(f"{where}: spark '{m.get('spark')}' は不正です")
        if m.get("availability") == "api" and m.get("spark") != "api":
            errors.append(f"{where}: APIのみのモデルは spark を 'api' にしてください")
        r = m.get("released")
        if r is not None and not DATE_RE.match(str(r)):
            errors.append(f"{where}: released '{r}' は YYYY-MM-DD か YYYY-MM で書いてください")
        p = m.get("params_b")
        if p is not None and not (isinstance(p, (int, float)) and 0 < p < 5000):
            errors.append(f"{where}: params_b '{p}' が数値として不正です")
        lic = m.get("license")
        if not isinstance(lic, dict) or "name" not in lic or "commercial" not in lic:
            errors.append(f"{where}: license は {{name, commercial}} の形にしてください")
        srcs = m.get("sources")
        if not srcs or not all(str(u).startswith("https://") for u in srcs):
            errors.append(f"{where}: 出典URL（https://）が最低1つ必要です")
        repo = m.get("hf_repo")
        if repo is not None and not str(repo).startswith("sbintuitions/"):
            errors.append(f"{where}: hf_repo は sbintuitions/ で始まる必要があります")
    return errors


def validate_events(ev, model_ids):
    errors = []
    ids = set()
    used = {}
    for i, e in enumerate(ev.get("events", [])):
        where = "event:" + str(e.get("id", f"#{i}"))
        for k in ["id", "date", "kind", "title", "summary", "models", "sources"]:
            if k not in e:
                errors.append(f"{where}: 項目 '{k}' がありません")
        if e.get("id") in ids:
            errors.append(f"{where}: id が重複しています")
        ids.add(e.get("id"))
        if not DATE_RE.match(str(e.get("date"))):
            errors.append(f"{where}: date '{e.get('date')}' は YYYY-MM-DD か YYYY-MM で書いてください")
        if e.get("kind") not in EVENT_KINDS:
            errors.append(f"{where}: kind '{e.get('kind')}' は不正です")
        for mid in e.get("models", []):
            if mid not in model_ids:
                errors.append(f"{where}: models.json に存在しないモデル '{mid}' を参照しています")
            if mid in used:
                errors.append(f"{where}: モデル '{mid}' は {used[mid]} にも入っています（1つの出来事にだけ入れる）")
            used[mid] = where
        srcs = e.get("sources")
        if not srcs or not all(str(u).startswith("https://") for u in srcs):
            errors.append(f"{where}: 出典URL（https://）が最低1つ必要です")
    for i, w in enumerate(ev.get("world", [])):
        where = "world:" + str(w.get("id", f"#{i}"))
        for k in ["id", "date", "company", "title", "summary", "sources"]:
            if k not in w:
                errors.append(f"{where}: 項目 '{k}' がありません")
        if w.get("id") in ids:
            errors.append(f"{where}: id が重複しています")
        ids.add(w.get("id"))
        if not DATE_RE.match(str(w.get("date"))):
            errors.append(f"{where}: date '{w.get('date')}' は YYYY-MM-DD か YYYY-MM で書いてください")
        if w.get("company") not in COMPANIES:
            errors.append(f"{where}: company '{w.get('company')}' は {', '.join(sorted(COMPANIES))} のどれかにしてください")
        srcs = w.get("sources")
        if not srcs or not all(str(u).startswith("https://") for u in srcs):
            errors.append(f"{where}: 出典URL（https://）が最低1つ必要です")
    return errors


def validate_kokusan(doc):
    errors = []
    ids = set()
    for i, m in enumerate(doc.get("models", [])):
        where = "kokusan:" + str(m.get("id", f"#{i}"))
        for k in ["id", "name", "maker", "latest", "method", "base", "size", "tokens", "context",
                  "open", "access", "gennai", "note", "sources"]:
            if k not in m:
                errors.append(f"{where}: 項目 '{k}' がありません")
        if m.get("id") in ids:
            errors.append(f"{where}: id が重複しています")
        ids.add(m.get("id"))
        if m.get("method") not in {"scratch", "continual", "unknown"}:
            errors.append(f"{where}: method '{m.get('method')}' は不正です")
        if not m.get("sources") or not all(str(u).startswith("https://") for u in m["sources"]):
            errors.append(f"{where}: 出典URL（https://）が最低1つ必要です")
    for i, r in enumerate(doc.get("rounds", [])):
        where = "round:" + str(r.get("id", f"#{i}"))
        if r.get("result") not in {"left", "right", "draw"}:
            errors.append(f"{where}: result は left / right / draw のどれか")
        for side in ("left", "right"):
            if (r.get(side) or {}).get("model") not in ids:
                errors.append(f"{where}: {side} のモデルが名寄せ（models）にありません")
        if not r.get("sources") or not all(str(u).startswith("https://") for u in r["sources"]):
            errors.append(f"{where}: 出典URL（https://）が最低1つ必要です")
    if len(doc.get("rounds", [])) > 10:
        errors.append("rounds は10番までにしてください（漢数字の表示が十番まで）")
    return errors


def validate_hf(doc):
    errors = []
    if not DATE_RE.match(str(doc.get("fetched_at"))):
        errors.append("hf_stats: fetched_at が日付ではありません")
    for rid, st in (doc.get("repos") or {}).items():
        if not isinstance(st.get("downloads"), int) or not isinstance(st.get("likes"), int):
            errors.append(f"hf_stats: {rid} の数値が不正です")
    return errors


def validate_paper_stats(doc, paper_ids):
    errors = []
    if not DATE_RE.match(str(doc.get("fetched_at"))):
        errors.append("paper_stats: fetched_at が日付ではありません")
    for pid, st in (doc.get("papers") or {}).items():
        if pid not in paper_ids:
            errors.append(f"paper_stats: papers.json にない論文 '{pid}' があります")
        if not isinstance(st.get("citations"), int):
            errors.append(f"paper_stats: {pid} の引用件数が不正です")
    return errors


MEDIA_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def validate_media(doc):
    errors, seen = [], set()
    for i, it in enumerate(doc.get("items", [])):
        where = "media:" + str(it.get("id", f"#{i}"))
        if not it.get("id") or it["id"] in seen:
            errors.append(f"{where}: id がないか重複しています")
        seen.add(it.get("id"))
        if it.get("kind") not in ("book", "video", "article"):
            errors.append(f"{where}: kind は book / video / article のどれかにしてください")
        for k in ("title", "where", "summary"):
            if not str(it.get(k, "")).strip():
                errors.append(f"{where}: {k} がありません")
        if not MEDIA_DATE_RE.match(str(it.get("date", ""))):
            errors.append(f"{where}: date は YYYY / YYYY-MM / YYYY-MM-DD で書いてください")
        if not it.get("people") or not all(str(x.get("name", "")).strip() for x in it["people"]):
            errors.append(f"{where}: people（SB Intuitionsの人）がありません")
        urls = [it.get("url", "")] + list(it.get("sources") or [])
        if not it.get("sources") or not all(str(u).startswith("https://") for u in urls):
            errors.append(f"{where}: url と sources は https:// で、出典は1つ以上必要です")
        if it.get("kind") == "video" and not re.match(r"^[A-Za-z0-9_-]{11}$", str(it.get("youtube", ""))):
            errors.append(f"{where}: 動画には YouTube の動画ID（11文字）を youtube に入れてください")
    return errors


def validate_chat(doc):
    errors = []
    for i, ep in enumerate(doc.get("episodes", [])):
        where = "chat:" + str(ep.get("id", f"#{i}"))
        if not str((ep.get("news") or {}).get("link", "")).startswith("https://"):
            errors.append(f"{where}: 元の記事のURL（https://）がありません")
        lines = ep.get("lines") or []
        if not (2 <= len(lines) <= 16):
            errors.append(f"{where}: 会話の行数が不正です")
        for ln in lines:
            if ln.get("who") not in ("さら", "しな") or not str(ln.get("text", "")).strip():
                errors.append(f"{where}: 話者か台詞が不正です")
                break
    return errors


def main():
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    ev = json.loads(EVENTS.read_text(encoding="utf-8"))
    papers = json.loads(PAPERS.read_text(encoding="utf-8"))
    model_ids = {m.get("id") for m in doc.get("models", [])}
    errors = validate(doc)
    errors += validate_events(ev, model_ids)
    errors += validate_papers(papers, model_ids)
    kokusan = json.loads(KOKUSAN.read_text(encoding="utf-8"))
    errors += validate_kokusan(kokusan)
    hf = json.loads(HF_STATS.read_text(encoding="utf-8")) if HF_STATS.exists() else None
    if hf:
        errors += validate_hf(hf)
    chat = json.loads(CHAT.read_text(encoding="utf-8")) if CHAT.exists() else {"episodes": []}
    errors += validate_chat(chat)
    pstats = json.loads(PAPER_STATS.read_text(encoding="utf-8")) if PAPER_STATS.exists() else None
    if pstats:
        errors += validate_paper_stats(pstats, {p.get("id") for p in papers.get("papers", [])})
    media = json.loads(MEDIA.read_text(encoding="utf-8")) if MEDIA.exists() else {"items": []}
    errors += validate_media(media)
    if errors:
        print("検証エラー（ページは生成していません）:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    site = {
        "updated_at": max(doc["updated_at"], ev["updated_at"], papers["updated_at"], kokusan["updated_at"]),
        "models": doc["models"],
        "events": ev["events"],
        "world": ev.get("world", []),
        "papers": papers["papers"],
        "kokusan": kokusan,
        "hf": hf,
        "paper_stats": pstats,
        "chat": chat,
        "media": media["items"],
    }
    base_css = (TEMPLATES / "_base.css").read_text(encoding="utf-8")
    payload = json.dumps(site, ensure_ascii=False).replace("</", "<\\/")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for tpl, out in PAGES.items():
        html = ((TEMPLATES / tpl).read_text(encoding="utf-8")
                .replace("/*__BASE_CSS__*/", base_css)
                .replace("<!--HEADER-->", header(out))
                .replace("<!--FOOTER-->", FOOTER)
                .replace("<!--BOOTH-->", (TEMPLATES / "_booth.svg").read_text(encoding="utf-8"))
                .replace("/*__SITE_JSON__*/", payload))
        (OUT_DIR / out).write_text(html, encoding="utf-8")
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    # イラスト（assets/ の画像。SVGは tools/draw_characters.py が描いた予備の絵）
    (OUT_DIR / "img").mkdir(exist_ok=True)
    for f in sorted((ROOT / "assets").iterdir()):
        if f.suffix in (".svg", ".webp", ".png", ".jpg"):
            (OUT_DIR / "img" / f.name).write_bytes(f.read_bytes())
    # 茶話のページが、開いたまま新しい会話を読み込むためのファイル
    (OUT_DIR / "chat.json").write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
    print(f"OK: {len(doc['models'])}モデル・{len(ev['events'])}件の出来事・{len(ev.get('world', []))}件の海外の動き・"
          f"{len(papers['papers'])}本の論文・{len(kokusan['rounds'])}番の歌合 → docs/")


if __name__ == "__main__":
    main()
