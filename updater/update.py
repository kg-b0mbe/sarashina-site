#!/usr/bin/env python3
"""Sarashinaファンサイトの自動更新（Mac miniで実行）。

流れ:
  1. 集める   … Hugging Face API・SB Intuitionsのブログ・Google ニュースから新しい情報を探す（スクリプト）
  2. 下書き   … DGX Spark上のSarashina2.2-3Bに、名前・分類・日本語の要約だけを書かせる
  3. 検証     … scripts/build.py でデータを検証し、ページを生成する
  4. 提案     … 新しいブランチにコミットしてプルリクエストを作り、Slackに通知する
                （公開はプルリクエストを人がマージしたとき）

日付・パラメータ数・ライセンスはHugging FaceのAPIから機械的に取り、LLMには書かせない。
外部ライブラリは使わない（macOS標準のpython3で動く）。

使い方:
  python3 updater/update.py --dry-run   # 何が提案されるかを表示するだけ（git・Slackは触らない）
  python3 updater/update.py             # 本番（プルリクエストを作ってSlackに通知）
"""
import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "data" / "models.json"
EVENTS = ROOT / "data" / "events.json"
JST = dt.timezone(dt.timedelta(hours=9))
UA = "sarashina-fan-site-updater/1.0 (+https://github.com/)"


# ---------------------------------------------------------------- 設定

def load_env():
    """updater/.env（なければ環境変数）から設定を読む。"""
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    cfg = {
        "base_url": os.environ.get("SARASHINA_BASE_URL", "").rstrip("/"),
        "api_key": os.environ.get("SARASHINA_API_KEY", ""),
        "model": os.environ.get("SARASHINA_MODEL", "sarashina2.2-3b"),
        "slack": os.environ.get("SLACK_WEBHOOK_URL", ""),
        "branch": os.environ.get("GIT_BASE_BRANCH", "main"),
        "state_dir": Path(os.path.expanduser(os.environ.get("STATE_DIR", "~/.sarashina-site"))),
        "max_items": int(os.environ.get("MAX_ITEMS_PER_RUN", "6")),
        "world": os.environ.get("WATCH_WORLD", "1") == "1",
    }
    if not cfg["base_url"]:
        sys.exit("SARASHINA_BASE_URL が設定されていません（updater/.env を確認）")
    return cfg


# ---------------------------------------------------------------- 通信

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_json(url, timeout=30):
    return json.loads(http_get(url, timeout))


def slack(cfg, text, dry):
    if dry or not cfg["slack"]:
        print("[Slack]", text)
        return
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(cfg["slack"], data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # 通知の失敗で更新全体は止めない
        print("Slack通知に失敗:", e)


# ---------------------------------------------------------------- Sarashina（DGX Spark）

def sarashina_alive(cfg):
    req = urllib.request.Request(cfg["base_url"] + "/models",
                                 headers={"Authorization": "Bearer " + cfg["api_key"]})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        print("Sarashinaに接続できません:", e)
        return False


def ask_sarashina(cfg, system, user, schema, max_tokens=500):
    """JSONスキーマに沿った答えを返させる。vLLMの構造化出力を使い、使えなければ通常生成から抜き出す。"""
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "answer", "schema": schema, "strict": True}},
    }

    def call(p):
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions", data=json.dumps(p).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]

    try:
        text = call(payload)
    except urllib.error.HTTPError:
        payload.pop("response_format")
        payload["messages"][1]["content"] += "\n\nJSONだけを出力してください。"
        text = call(payload)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("SarashinaがJSONを返しませんでした: " + text[:200])
    return json.loads(m.group(0))


SYSTEM = ("あなたは国産AI「Sarashina」の非公式ファンサイトの編集者です。"
          "与えられた資料に書かれていることだけを使い、推測で事実を足さないでください。"
          "わからない項目は null にしてください。文章は体言止め中心で短く書き、「です・ます」は使いません。")


# ---------------------------------------------------------------- 状態（提案済みのものは二度出さない）

def load_state(cfg):
    p = cfg["state_dir"] / "seen.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"hf": [], "urls": [], "world_keys": []}


def save_state(cfg, state):
    state["urls"] = state["urls"][-3000:]
    cfg["state_dir"].mkdir(parents=True, exist_ok=True)
    (cfg["state_dir"] / "seen.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 1. 集める

def known_repos(models):
    repos = set()
    for m in models:
        if m.get("hf_repo"):
            repos.add(m["hf_repo"])
        for u in m.get("sources", []):
            hit = re.match(r"https://huggingface\.co/(sbintuitions/[^/?#]+)", u)
            if hit:
                repos.add(hit.group(1))
    return repos


def collect_hf(models, state):
    """sbintuitionsの新しいSarashinaリポジトリを探す。"""
    listing = http_json("https://huggingface.co/api/models?author=sbintuitions&limit=500")
    known = known_repos(models) | set(state["hf"])
    found = []
    for item in listing:
        rid = item.get("id") or item.get("modelId")
        if rid and "sarashina" in rid.lower() and rid not in known:
            found.append(rid)
    return sorted(found)


def rss_items(url):
    try:
        root = ET.fromstring(http_get(url))
    except Exception as e:
        print("RSSを取得できません:", url, e)
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        pub = it.findtext("pubDate")
        date = None
        if pub:
            try:
                date = email.utils.parsedate_to_datetime(pub).astimezone(JST).date().isoformat()
            except Exception:
                pass
        if title and link.startswith("https://"):
            out.append({"title": title, "link": link, "text": re.sub(r"\s+", " ", desc)[:600], "date": date})
    # Atom（はてなブログ）
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        link_el = e.find("a:link[@rel='alternate']", ns)
        if link_el is None:
            link_el = e.find("a:link", ns)
        link = link_el.get("href", "") if link_el is not None else ""
        summ = e.findtext("a:summary", default="", namespaces=ns) or ""
        date = (e.findtext("a:published", default="", namespaces=ns) or "")[:10] or None
        if title and link.startswith("https://"):
            out.append({"title": title, "link": link, "text": re.sub(r"\s+", " ", summ)[:600], "date": date})
    return out


def gnews(query):
    q = urllib.parse.quote(query)
    return rss_items(f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja")


def collect_news(state):
    """Sarashina・SB Intuitionsの新しい記事。"""
    items = rss_items("https://www.sbintuitions.co.jp/blog/feed")
    items += gnews("Sarashina SB Intuitions")
    seen = set(state["urls"])
    uniq, titles = [], set()
    for it in items:
        key = re.sub(r"\W", "", it["title"])[:40]
        if it["link"] in seen or key in titles:
            continue
        titles.add(key)
        uniq.append(it)
    return uniq


WORLD_QUERIES = {
    "openai": ("OpenAI 新モデル 発表", r"\bGPT-\d+(\.\d+)?(\s?[A-Z][a-z]+)?"),
    "anthropic": ("Anthropic Claude 新モデル 発表", r"\bClaude\s(?:Opus|Sonnet|Haiku|Fable|Mythos)\s\d+(\.\d+)?"),
    "google": ("Google Gemini 新モデル 発表", r"\bGemini\s\d+(\.\d+)?(\s(?:Pro|Flash|Ultra|Flash-Lite))?"),
}


def collect_world(events_doc, state):
    """海外の主要モデル発表。タイトルからモデル名を取り出し、既に載っているものは除く。"""
    existing = " ".join(w["title"] for w in events_doc.get("world", []))
    seen_keys = set(state["world_keys"])
    picks = []
    for company, (query, pattern) in WORLD_QUERIES.items():
        for it in gnews(query):
            m = re.search(pattern, it["title"])
            if not m:
                continue
            key = m.group(0).strip()
            if key in existing or key in seen_keys or it["link"] in state["urls"]:
                continue
            picks.append({**it, "company": company, "key": key})
            seen_keys.add(key)
            break  # 1社につき1回1件まで
    return picks


# ---------------------------------------------------------------- 2. 下書き

KIND_ENUM = ["base", "instruct", "thinking", "vision", "ocr", "tts", "embedding", "rerank", "guard", "translate"]
PIPE_MODALITY = {"image-to-text": ["text", "image"], "image-text-to-text": ["text", "image"],
                 "text-to-speech": ["text", "audio"], "automatic-speech-recognition": ["text", "audio"]}


def hf_facts(rid):
    """日付・サイズ・ライセンスはAPIから機械的に決める（LLMには書かせない）。"""
    q = "expand[]=safetensors&expand[]=cardData&expand[]=createdAt&expand[]=pipeline_tag"
    info = http_json(f"https://huggingface.co/api/models/{rid}?{q}")
    try:
        readme = http_get(f"https://huggingface.co/{rid}/raw/main/README.md")
    except Exception:
        readme = ""
    readme = re.sub(r"\n{3,}", "\n\n", re.sub(r"^---.*?---", "", readme, flags=re.S))
    total = (info.get("safetensors") or {}).get("total")
    params_b = round(total / 1e9, 1) if total else None
    lic = ((info.get("cardData") or {}).get("license") or "").lower()
    if lic == "mit":
        license_ = {"name": "MIT", "commercial": True}
    elif "noncommercial" in readme.lower().replace(" ", "") or "non-commercial" in readme.lower():
        license_ = {"name": "Sarashina Model NonCommercial License", "commercial": False}
    elif lic == "apache-2.0":
        license_ = {"name": "Apache-2.0", "commercial": True}
    else:
        license_ = {"name": None, "commercial": None}
    if params_b is None:
        spark = "ok"
    elif params_b * 2 <= 100:
        spark = "ok"
    elif params_b * 0.6 <= 110:
        spark = "quant"
    else:
        spark = "no"
    return {
        "hf_repo": rid,
        "released": (info.get("createdAt") or "")[:10] or None,
        "params_b": params_b,
        "license": license_,
        "modality": PIPE_MODALITY.get(info.get("pipeline_tag"), ["text"]),
        "spark": spark,
        "readme": readme[:2500],
    }


def draft_model(cfg, rid):
    f = hf_facts(rid)
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["name", "generation", "kind", "architecture", "notes"],
        "properties": {
            "name": {"type": "string"},
            "generation": {"type": "string"},
            "kind": {"type": "string", "enum": KIND_ENUM},
            "architecture": {"type": ["string", "null"]},
            "notes": {"type": "string"},
        },
    }
    user = (f"Hugging Faceのリポジトリ「{rid}」のモデルカードです。\n"
            "次の項目をJSONで答えてください。\n"
            "- name: 表示名（例: Sarashina2.2-OCR）\n"
            "- generation: 世代（例: Sarashina2.2、Sarashina3、Embedding）\n"
            f"- kind: 種類。次から1つ: {', '.join(KIND_ENUM)}\n"
            "- architecture: 構造やベースモデル。書かれていなければ null\n"
            "- notes: 何ができるモデルかを日本語で80字以内\n\n"
            f"--- モデルカード ---\n{f['readme']}")
    d = ask_sarashina(cfg, SYSTEM, user, schema)
    mid = rid.split("/", 1)[1].lower()
    return {
        "id": mid, "name": d["name"].strip(), "generation": d["generation"].strip(), "kind": d["kind"],
        "modality": f["modality"], "params_b": f["params_b"], "architecture": d.get("architecture"),
        "training_tokens": None, "context_length": None, "license": f["license"],
        "availability": "open_weights", "released": f["released"], "hf_repo": rid, "spark": f["spark"],
        "notes": d["notes"].strip()[:160], "sources": [f"https://huggingface.co/{rid}"],
        "drafted_by": cfg["model"],
    }


def draft_event(cfg, item, existing_titles):
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["is_new_event", "kind", "title", "summary"],
        "properties": {
            "is_new_event": {"type": "boolean"},
            "kind": {"type": "string", "enum": ["company", "release", "service", "milestone"]},
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
    }
    user = ("次の記事を読んで、Sarashina年表に載せるべき「新しい出来事」かどうか判断してください。\n"
            "新しい出来事とは、SB Intuitionsによるモデルの発表・公開、サービスの提供開始、会社の大きな節目です。"
            "技術解説や使ってみた記事、すでに年表にある出来事の再報道は false にします。\n\n"
            "年表にすでにある出来事:\n- " + "\n- ".join(existing_titles) + "\n\n"
            f"--- 記事 ---\nタイトル: {item['title']}\n本文の一部: {item['text']}\n\n"
            "JSONの項目: is_new_event（true/false）, kind（company/release/service/milestone）, "
            "title（年表の見出し。30字以内）, summary（日本語で80字以内）")
    d = ask_sarashina(cfg, SYSTEM, user, schema)
    if not d.get("is_new_event"):
        return None
    eid = "auto-" + (item["date"] or dt.date.today().isoformat()) + "-" + hashlib.sha1(item["link"].encode("utf-8")).hexdigest()[:6]
    return {"id": eid, "date": item["date"] or dt.datetime.now(JST).date().isoformat(), "kind": d["kind"],
            "title": d["title"].strip()[:60], "summary": d["summary"].strip()[:160], "models": [],
            "sources": [item["link"]], "drafted_by": cfg["model"]}


def draft_world(cfg, item):
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["is_model_launch", "title", "summary"],
        "properties": {
            "is_model_launch": {"type": "boolean"},
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
    }
    user = (f"次の記事は「{item['key']}」というAIモデルの新しい発表についての記事ですか？"
            "噂・予想・比較記事・使い方の記事なら false です。\n\n"
            f"タイトル: {item['title']}\n本文の一部: {item['text']}\n\n"
            "JSONの項目: is_model_launch（true/false）, title（例:「GPT-6 Astra 発表」。20字以内）, "
            "summary（どんなモデルかを日本語で60字以内）")
    d = ask_sarashina(cfg, SYSTEM, user, schema)
    if not d.get("is_model_launch"):
        return None
    wid = re.sub(r"[^a-z0-9]+", "-", item["key"].lower()).strip("-")
    return {"id": wid, "date": item["date"] or dt.datetime.now(JST).date().isoformat(), "company": item["company"],
            "title": d["title"].strip()[:40], "summary": d["summary"].strip()[:120],
            "sources": [item["link"]], "drafted_by": cfg["model"]}


# ---------------------------------------------------------------- 3〜4. 検証・提案

def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def write_json(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="提案内容を表示するだけ（ファイル・git・Slackは変えない）")
    args = ap.parse_args()
    dry = args.dry_run
    cfg = load_env()

    if not dry:
        if git("status", "--porcelain").stdout.strip():
            sys.exit("作業ツリーに未コミットの変更があります。片付けてから実行してください。")
        git("checkout", cfg["branch"])
        git("pull", "--ff-only")

    if not sarashina_alive(cfg):
        slack(cfg, ":warning: Sarashinaファンサイト更新：DGX SparkのSarashinaが応答しません。"
                   "今回の更新はスキップしました（自動再起動はしていません）。", dry)
        sys.exit(1)

    models_doc = json.loads(MODELS.read_text(encoding="utf-8"))
    events_doc = json.loads(EVENTS.read_text(encoding="utf-8"))
    state = load_state(cfg)
    budget = cfg["max_items"]
    new_models, new_events, new_world, skipped = [], [], [], []

    # モデル
    for rid in collect_hf(models_doc["models"], state):
        if budget <= 0:
            break
        try:
            new_models.append(draft_model(cfg, rid))
            budget -= 1
        except Exception as e:
            skipped.append(f"{rid}（下書き失敗: {e}）")
        state["hf"].append(rid)

    # Sarashina関連のニュース
    titles = [e["title"] for e in events_doc["events"]]
    for it in collect_news(state):
        if budget <= 0:
            break
        state["urls"].append(it["link"])
        try:
            ev = draft_event(cfg, it, titles + [e["title"] for e in new_events])
        except Exception as e:
            skipped.append(f"{it['title']}（下書き失敗: {e}）")
            continue
        if ev:
            new_events.append(ev)
            budget -= 1

    # 海外の動き
    if cfg["world"]:
        for it in collect_world(events_doc, state):
            if budget <= 0:
                break
            state["urls"].append(it["link"])
            state["world_keys"].append(it["key"])
            try:
                w = draft_world(cfg, it)
            except Exception as e:
                skipped.append(f"{it['title']}（下書き失敗: {e}）")
                continue
            if w:
                new_world.append(w)
                budget -= 1

    total = len(new_models) + len(new_events) + len(new_world)
    lines = ([f"・モデル: {m['name']}（{m['hf_repo']}）" for m in new_models] +
             [f"・出来事: {e['date']} {e['title']}" for e in new_events] +
             [f"・海外: {w['date']} {w['title']}" for w in new_world])

    if dry:
        print(json.dumps({"models": new_models, "events": new_events, "world": new_world},
                         ensure_ascii=False, indent=2))
        print("\n".join(lines) or "新しい提案はありません。")
        if skipped:
            print("スキップ:", *skipped, sep="\n  ")
        return

    if total == 0:
        save_state(cfg, state)
        print("新しい提案はありません。")
        return

    today = dt.datetime.now(JST).date().isoformat()
    models_doc["models"] += new_models
    models_doc["updated_at"] = today
    events_doc["events"] += new_events
    events_doc.setdefault("world", []).extend(new_world)
    events_doc["updated_at"] = today
    write_json(MODELS, models_doc)
    write_json(EVENTS, events_doc)

    build = subprocess.run([sys.executable, "scripts/build.py"], cwd=ROOT, capture_output=True, text=True)
    if build.returncode != 0:
        git("checkout", "--", "data", "docs")
        slack(cfg, ":x: Sarashinaファンサイト更新：Sarashinaの下書きが検証を通りませんでした。"
                   "公開はしていません。\n```" + build.stdout[-1500:] + "```", dry)
        save_state(cfg, state)
        sys.exit(1)

    branch = "update/" + dt.datetime.now(JST).strftime("%Y%m%d-%H%M")
    git("checkout", "-b", branch)
    git("add", "data", "docs")
    git("commit", "-m", f"Sarashinaによる更新提案（{total}件）\n\n" + "\n".join(lines))
    git("push", "-u", "origin", branch)
    body = ("DGX Spark上の " + cfg["model"] + " が下書きした更新提案です。\n"
            "日付・サイズ・ライセンスはHugging Face APIから機械的に取得、名前・分類・要約はSarashinaが作成。\n"
            "内容と出典を確認して、問題なければマージしてください（マージすると公開されます）。\n\n"
            + "\n".join(lines) + ("\n\nスキップ:\n" + "\n".join(skipped) if skipped else ""))
    pr = subprocess.run(["gh", "pr", "create", "--base", cfg["branch"], "--head", branch,
                         "--title", f"Sarashinaによる更新提案（{today}・{total}件）", "--body", body],
                        cwd=ROOT, capture_output=True, text=True)
    git("checkout", cfg["branch"])
    save_state(cfg, state)
    url = pr.stdout.strip() if pr.returncode == 0 else "(プルリクエスト作成に失敗: " + pr.stderr.strip()[:200] + ")"
    slack(cfg, f":memo: Sarashinaが{total}件の更新を下書きしました。確認してマージすると公開されます。\n"
               + "\n".join(lines) + f"\n{url}", dry)
    print(url)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lock import single_run
    with single_run():
        main()
