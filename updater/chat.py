#!/usr/bin/env python3
"""さらとしなの茶話（Mac miniで2時間ごとに実行）。

話題を1つ選び（1日1回は論文手帖の論文を解説する「論文コーナー」、ほかはAI・ローカルLLMの
ニュースを優先し、なければ国内外のまったりした話題）、
DGX SparkのSarashina2.2-3Bに「さら」と「しな」の雑談を書かせて、
data/chat.json に追加する。遊びのページなので、人の承認は挟まずに main へ直接pushする。
そのぶん、次の安全策を入れている。
  - 事件・事故・災害・政治などの見出しは雑談のネタにしない
  - 見出しに書かれていない事実は話さないよう指示する
  - 形式が崩れた会話（話者・長さ・行数・URLなど）は捨てる
  - ページには「AI同士の雑談で、内容は正確とは限らない」と明記する

  python3 updater/chat.py --dry-run   # 1回分を作って表示するだけ
  python3 updater/chat.py             # 追加 → 検証 → コミット・push

止めたいときは updater/.env に CHAT_ENABLED=0 と書く。
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as u  # noqa: E402  設定の読み込み・RSS・Sarashina呼び出しを共用
from lock import single_run  # noqa: E402

ROOT = u.ROOT
CHAT = ROOT / "data" / "chat.json"
PAPERS = ROOT / "data" / "papers.json"
KEEP = 120  # ページに残す回数

def gnews(q=None, topic=None, lang="ja"):
    loc = "hl=ja&gl=JP&ceid=JP:ja" if lang == "ja" else "hl=en-US&gl=US&ceid=US:en"
    if topic:
        return f"https://news.google.com/rss/headlines/section/topic/{topic}?{loc}"
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{loc}"


# 1. まずはAI・ローカルLLMの話題（2日以内のもの）
AI_FEEDS = [
    (gnews("ローカルLLM"), False),
    (gnews("生成AI 新モデル"), False),
    (gnews('"local LLM" OR "open-weight model"', lang="en"), False),
    (gnews("AI model release", lang="en"), False),
    ("https://hnrss.org/frontpage", True),   # True = タイトルがAIの話題のものだけ拾う
]
# 2. AIの新しい話題がなければ、国内外のまったりした話題（3日以内のもの）
LIGHT_FEEDS = [
    gnews(topic="SCIENCE"),
    gnews("ほっこり"),
    gnews(topic="SCIENCE", lang="en"),
    "https://rss.upi.com/news/odd_news.rss",
    gnews("quirky news", lang="en"),
]
AI_WORDS = re.compile(r"\b(AI|LLMs?|GPT|Claude|Gemini|Llama|Qwen|DeepSeek|Mistral|GLM|Gemma|open[- ]weights?|inference|transformer|model)\b", re.I)

# 雑談のネタにしない見出し（人の不幸や対立を茶化さないため）
AVOID = re.compile(
    "死|亡くな|訃報|殺|事故|事件|逮捕|容疑|被害|災害|地震|津波|火災|豪雨|台風|戦争|紛争|攻撃|テロ|爆発|"
    "自殺|負傷|けが|病|感染|裁判|判決|選挙|政党|首相|大臣|政権|議員|大統領|制裁|デモ|差別|性的|"
    r"\b(dead|death|dies|died|kill\w*|murder\w*|shoot\w*|crash\w*|accident|disaster|earthquake|flood\w*|wildfire|"
    r"war|attack\w*|terror\w*|bomb\w*|election|president|minister|senator|congress|lawsuit|court|arrest\w*|police|"
    r"suicide|abuse|sex\w*|virus|outbreak|cancer|tariff\w*|sanction\w*|layoff\w*|trump|gun\w*)\b", re.I)

JA = re.compile(r"[ぁ-んァ-ン一-龥]")


SPEAKERS = ["さら", "しな"]

SYSTEM = (
    "あなたは「さら」と「しな」という二人の会話を書く脚本家です。\n"
    "さら：新しいものにすぐわくわくする、好奇心旺盛な聞き役。ちょっとおっちょこちょい。\n"
    "しな：落ち着いた物知りのツッコミ役。知らないことは「わからない」と正直に言う。\n"
    "二人は仲のいい友だちで、気楽な話し言葉で、ゆるく雑談します。\n"
    "守ること：ニュースについては見出しに書いてあることだけを事実として扱い、"
    "数字や人名、出来事を付け足さない。感想、連想、身近な話への脱線は自由。"
    "誰かを悪く言わない。URLは書かない。")


def _fresh(date, days):
    if not date:
        return True
    try:
        return dt.date.fromisoformat(date[:10]) >= dt.datetime.now(u.JST).date() - dt.timedelta(days=days)
    except ValueError:
        return True


def _candidate(it, state_seen, days, need_ai):
    title = re.sub(r"\s+-\s+[^-]+$", "", it["title"]).strip()  # 末尾の「 - 媒体名」を外す
    key = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    if key in state_seen or AVOID.search(title) or len(title) < 8 or not _fresh(it.get("date"), days):
        return None
    if need_ai and not AI_WORDS.search(title):
        return None
    return {"title": title, "link": it["link"], "key": key, "lang": "ja" if JA.search(title) else "en"}


def pick_paper(state):
    """論文コーナー：論文手帖に載っている論文のうち、まだ話していないものを新しい順に。1日1回まで。"""
    today = dt.datetime.now(u.JST).date().isoformat()
    if state.get("chat_paper_day") == today:
        return None
    done = set(state.get("chat_papers", []))
    papers = json.loads(PAPERS.read_text(encoding="utf-8"))["papers"]
    for p in sorted(papers, key=lambda x: x["date"], reverse=True):
        if p["id"] in done:
            continue
        link = (p.get("links") or {}).get("paper") or p["sources"][0]
        return {"title": p["title_ja"], "link": link, "key": "paper:" + p["id"], "lang": "ja", "kind": "paper",
                "paper": p}
    return None


def pick_headline(state_seen):
    for feed, need_ai in AI_FEEDS:
        for it in u.rss_items(feed):
            c = _candidate(it, state_seen, 2, need_ai)
            if c:
                return dict(c, kind="ai")
    for feed in LIGHT_FEEDS:
        for it in u.rss_items(feed):
            c = _candidate(it, state_seen, 3, False)
            if c:
                return dict(c, kind="light")
    return None


def write_episode(cfg, news, last_lines):
    schema = {
        "type": "object", "additionalProperties": False, "required": ["headline_ja", "lines"],
        "properties": {"headline_ja": {"type": "string"}, "lines": {"type": "array", "minItems": 6, "maxItems": 12, "items": {
            "type": "object", "additionalProperties": False, "required": ["who", "text"],
            "properties": {"who": {"type": "string", "enum": SPEAKERS}, "text": {"type": "string"}}}}},
    }
    prev = "\n".join(f"{l['who']}：{l['text']}" for l in last_lines) or "（これが最初の会話です）"
    if news["kind"] == "paper":
        p = news["paper"]
        material = (f"題名：{p['title_ja']}（原題：{p['title']}）\n発表：{p['venue']}（{p['date']}）\n"
                    f"ひとことで：{p['summary']}\nポイント：" + " / ".join(p["points"]) +
                    f"\nSarashinaとのつながり：{p['sarashina']}")
        user = (f"前回の会話の終わり：\n{prev}\n\n"
                "今日は「論文コーナー」です。SB Intuitionsの研究者の論文を、しなが解説役、さらが聞き役になって、"
                "やさしく紹介します。下の材料に書かれていることだけを事実として話し、数字や結果を付け足さないでください。"
                "難しい言葉は、さらが質問して、しながたとえ話で言い換えます。\n\n"
                f"--- 材料 ---\n{material}\n\n"
                "8行前後で書きます。最初はさらが「今日は論文コーナー！」のように始め、最後は次の話題につながる一言で終えます。"
                "1行は60字以内。headline_ja には題名をそのまま書きます。"
                "JSONで {\"headline_ja\": \"...\", \"lines\": [{\"who\": \"さら\", \"text\": \"...\"}, ...]} の形にします。")
        return u.ask_sarashina(cfg, SYSTEM, user, schema, max_tokens=900)
    where = "海外のニュース（英語）" if news["lang"] == "en" else "ニュース"
    topic = ("AIやローカルLLMの話題です。二人もDGX Sparkの上で動くAIなので、そのことに軽く触れてもかまいません。"
             if news["kind"] == "ai" else "肩の力を抜いた、まったりした話題です。")
    user = (f"前回の会話の終わり：\n{prev}\n\n"
            f"さらが、さっき見つけた{where}の見出しを持ってきました。{topic}\n見出し：「{news['title']}」\n\n"
            "この見出しをきっかけに、二人の雑談を8行前後で書いてください。"
            "最初はさらが見出しを紹介し、最後は次の話題につながるような一言で終えます。"
            "1行は60字以内。headline_ja には見出しを自然な日本語で40字以内に書きます（日本語の見出しならそのまま）。"
            "JSONで {\"headline_ja\": \"...\", \"lines\": [{\"who\": \"さら\", \"text\": \"...\"}, ...]} の形にします。")
    return u.ask_sarashina(cfg, SYSTEM, user, schema, max_tokens=900)


WHY = []  # 捨てた理由（dry-runで表示する）


def reject(reason):
    WHY.append(reason)
    return None, None


def clean(d):
    head = re.sub(r"\s+", " ", str((d or {}).get("headline_ja", ""))).strip().strip("「」")
    if not (4 <= len(head) <= 80) or re.search(r"https?://|www\.", head):
        return reject(f"見出しの形が不正: {head[:40]!r}")
    lines = []
    for l in (d or {}).get("lines", []):
        who, text = l.get("who"), re.sub(r"\s+", " ", str(l.get("text", ""))).strip()
        text = re.sub(r"^(さら|しな)[:：]\s*", "", text)
        text = re.sub(r"Sarashinar(?=\d)", "Sarashina", text.replace("</s>", "")).strip()  # 小さいモデルの書き損じを直す
        if re.match(r"^[を、。」』）]", text):  # 文の頭が欠けた台詞は使わない（「へえ」「でも」で始まるのはふつう）
            return reject(f"頭が欠けた台詞: {text[:30]!r}")
        if who not in SPEAKERS:
            return reject(f"話者が不正: {who!r}")
        if not (2 <= len(text) <= 120) or re.search(r"https?://|www\.", text):
            return reject(f"台詞の長さかURL: {len(text)}字 {text[:30]!r}")
        lines.append({"who": who, "text": text})
    if not (6 <= len(lines) <= 12):
        return reject(f"行数が {len(lines)} 行")
    if len({l["who"] for l in lines}) < 2:
        return reject("一人しか話していない")
    return head, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = u.load_env()
    import os
    if os.environ.get("CHAT_ENABLED", "1") == "0":
        print("茶話は停止中です（CHAT_ENABLED=0）。")
        return
    with single_run():
        if not args.dry_run:
            if u.git("status", "--porcelain").stdout.strip():
                sys.exit("作業ツリーに未コミットの変更があります。")
            u.git("checkout", cfg["branch"])
            u.git("pull", "--ff-only")
        if not u.sarashina_alive(cfg):
            print("Sarashinaが応答しないので、今回はお休みします。")
            return

        doc = json.loads(CHAT.read_text(encoding="utf-8"))
        state = u.load_state(cfg)
        seen = set(state.get("chat_keys", []))
        news = pick_paper(state) or pick_headline(seen)
        if not news:
            print("話題になる見出しが見つかりませんでした。")
            return
        last = doc["episodes"][0]["lines"][-2:] if doc["episodes"] else []
        lines, head = None, None
        for _ in range(3):  # 形が崩れたら2回まで書き直させる
            try:
                head, lines = clean(write_episode(cfg, news, last))
            except Exception as e:
                WHY.append(f"下書きに失敗: {e}")
            if lines:
                break
        if news["kind"] == "paper":
            # 論文は、うまく書けなかった日も「今日の分」として数える（同じ日に何度も挑戦しない）
            state["chat_paper_day"] = dt.datetime.now(u.JST).date().isoformat()
            if lines:
                state.setdefault("chat_papers", []).append(news["paper"]["id"])
        state.setdefault("chat_keys", []).append(news["key"])
        state["chat_keys"] = state["chat_keys"][-2000:]
        if not lines:
            if not args.dry_run:
                u.save_state(cfg, state)
            print("会話の形が整わなかったので、今回は捨てました。理由:", " / ".join(WHY))
            return

        now = dt.datetime.now(u.JST)
        ep = {"id": now.strftime("%Y%m%d-%H%M"), "at": now.strftime("%Y-%m-%d %H:%M"),
              "news": {"title": news["title"], "title_ja": head if news["lang"] == "en" else news["title"],
                       "paper_id": news["paper"]["id"] if news["kind"] == "paper" else None,
                       "lang": news["lang"], "kind": news["kind"], "link": news["link"]}, "lines": lines, "model": cfg["model"]}
        if args.dry_run:
            print(f"【{news['kind']}】{news['title']}" + (f"\n（日本語：{head}）" if news["lang"] == "en" else ""))
            for l in lines:
                print(f"{l['who']}：{l['text']}")
            return

        doc["episodes"] = ([ep] + doc["episodes"])[:KEEP]
        doc["updated_at"] = now.strftime("%Y-%m-%d")
        CHAT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        build = subprocess.run([sys.executable, "scripts/build.py"], cwd=ROOT, capture_output=True, text=True)
        if build.returncode != 0:
            u.git("checkout", "--", "data", "docs")
            sys.exit("検証に失敗したので元に戻しました:\n" + build.stdout[-800:])
        u.git("add", "data/chat.json", "docs")
        u.git("commit", "-m", f"茶話 {ep['at']}：{news['title'][:30]}")
        u.git("push")
        u.save_state(cfg, state)
        print("追加してpushしました:", news["title"])


if __name__ == "__main__":
    main()
