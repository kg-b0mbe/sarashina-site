#!/usr/bin/env python3
"""さらとしなの茶話（Mac miniで1日6回、番組表 PROGRAM の時刻に実行）。

番組表（朝：天気・季節／午前：生成AI／昼：ローカルLLM／午後：論文コーナー／夕方：よもやま／夜：生成AI）に
沿って話題を1つ選び（その棚に新しい見出しがなければ別の棚から）、台本を作って data/chat.json に追加する。台本の作り方は2とおり。
  - GLMが使えるとき（updater/.env に GLM_BASE_URL があり、応答するとき）：
    GLMがあらすじと下書きを書き → Sarashinaがさらとしなの口調に言い直し → GLMが事実を確かめる。
    確認で問題が出たら1回だけ作り直し、それでもだめならSarashinaだけのやり方に切り替える。
  - GLMが使えないとき：Sarashina2.2-3Bが台本から書く（これまでのやり方）。
聞き比べの結果（2026-11-22）、この分担がいちばん自然で間違いも少なかった（updater/chat_compare.py）。
遊びのページなので、人の承認は挟まずに main へ直接pushする。
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
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
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


# 話題の棚。(フィード, AIの話題だけ拾うか) の並び
FEEDS = {
    # 生成AIの新しいモデルやサービス
    "ai": [
        (gnews("生成AI 新モデル"), False),
        (gnews("生成AI 発表"), False),
        (gnews("AI model release", lang="en"), False),
        ("https://hnrss.org/frontpage", True),
    ],
    # ローカルLLM・オープンウェイト
    "local": [
        (gnews("ローカルLLM"), False),
        (gnews("オープンウェイト モデル"), False),
        (gnews('"local LLM" OR "open-weight model"', lang="en"), False),
        (gnews("llama.cpp OR Ollama OR vLLM", lang="en"), False),
    ],
    # 天気・季節（寒くなってきた、紅葉、初雪など。災害の見出しは AVOID ではじく）
    "season": [
        (gnews("冷え込み 天気"), False),
        (gnews("紅葉 見頃 OR 初雪 OR 初霜"), False),
        (gnews("週末 天気 気温"), False),
        (gnews("季節 話題"), False),
    ],
    # 国内外のまったりした話題
    "light": [
        (gnews(topic="SCIENCE"), False),
        (gnews("ほっこり"), False),
        (gnews(topic="SCIENCE", lang="en"), False),
        ("https://rss.upi.com/news/odd_news.rss", False),
        (gnews("quirky news", lang="en"), False),
    ],
}
FRESH_DAYS = {"ai": 2, "local": 3, "season": 2, "light": 3}

# 1日の番組表（時:話題）。launchd の時刻（jp.sarashina-site.chat.plist）と合わせる。
# その棚に新しい見出しがなければ、後ろに書いた順に別の棚を探す。
PROGRAM = {
    7: ["season", "light", "ai"],     # 朝：天気・季節
    10: ["ai", "local", "light"],     # 午前：生成AIのニュース
    13: ["local", "ai", "light"],     # 昼：ローカルLLM
    16: ["paper", "ai", "local"],     # 午後：論文コーナー（新しい論文がなければAI）
    19: ["light", "season", "ai"],    # 夕方：よもやま
    22: ["ai", "local", "light"],     # 夜：AIのニュース
}


def lineup(hour):
    """いまの時刻にいちばん近い（その時刻以前の）枠の並びを返す。"""
    slots = [h for h in sorted(PROGRAM) if h <= hour] or [max(PROGRAM)]
    return PROGRAM[slots[-1]]


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
    "二人は仲のいい友だちで、気楽な話し言葉で、ゆるく雑談します。相手のことは「さら」「しな」と名前で呼び、「あなた」とは言いません。\n"
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
    title = clean_title(re.sub(r"\s+-\s+[^-]+$", "", it["title"]).strip())  # 末尾の「 - 媒体名」なども外す
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


def pick_headline(state_seen, shelf):
    for feed, need_ai in FEEDS[shelf]:
        for it in u.rss_items(feed):
            cand = _candidate(it, state_seen, FRESH_DAYS[shelf], need_ai)
            if cand:
                # 「local」もページ上は「AIのはなし」として出す
                return dict(cand, kind="ai" if shelf in ("ai", "local") else shelf)
    return None


def pick(state, seen, order):
    for shelf in order:
        news = pick_paper(state) if shelf == "paper" else pick_headline(seen, shelf)
        if news:
            return news
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
             if news["kind"] == "ai" else
             "天気や季節の話題です。寒さや暮らしの話に脱線してかまいません。" if news["kind"] == "season" else
             "肩の力を抜いた、まったりした話題です。")
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


def tidy(text):
    """小さいモデルの書き損じを直す（JSONの記号の混入、和文のあとの半角ピリオドなど）。"""
    text = re.sub(r"\s+", " ", str(text)).replace("</s>", "").strip()
    text = re.sub(r"^(さら|しな)[:：]\s*", "", text)
    text = re.sub(r"Sarashinar(?=\d)", "Sarashina", text)
    text = re.sub(r"[\s\"'{}\[\],]+$", "", text)            # 行末に紛れ込んだ } " ] など
    text = re.sub(r"(?<=[ぁ-んァ-ン一-龥ー〜！？])\.$", "。", text)  # 「〜.」→「〜。」
    text = re.sub(r"〜。$", "〜", text)
    return text.strip()


def clean_title(title):
    """見出しの頭の【動画】【速報】や、末尾の（22日19時更新）を外す。"""
    title = re.sub(r"^(【[^】]{1,8}】\s*)+", "", title)
    title = re.sub(r"\s*[（(][^）)]*更新[）)]\s*$", "", title)
    return title.strip()


def clean(d, hi=12):
    head = re.sub(r"\s+", " ", str((d or {}).get("headline_ja", ""))).strip().strip("「」")
    if not (4 <= len(head) <= 80) or re.search(r"https?://|www\.", head):
        return reject(f"見出しの形が不正: {head[:40]!r}")
    lines = []
    for l in (d or {}).get("lines", []):
        who, text = l.get("who"), tidy(l.get("text", ""))
        if re.match(r"^[を、。」』）]", text):  # 文の頭が欠けた台詞は使わない（「へえ」「でも」で始まるのはふつう）
            return reject(f"頭が欠けた台詞: {text[:30]!r}")
        if who not in SPEAKERS:
            return reject(f"話者が不正: {who!r}")
        if not (2 <= len(text) <= 120) or re.search(r"https?://|www\.", text):
            return reject(f"台詞の長さかURL: {len(text)}字 {text[:30]!r}")
        lines.append({"who": who, "text": text})
    if not (6 <= len(lines) <= hi):
        return reject(f"行数が {len(lines)} 行")
    if len({l["who"] for l in lines}) < 2:
        return reject("一人しか話していない")
    return head, lines


# ---------------------------------------------------------------- GLM（台本の下書きと確認）

LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.\-]+")
CHUNK = 4  # Sarashinaに一度に言い直させる行数（小さいモデルなので少しずつ）


def line_schema(n_min, n_max):
    return {"type": "array", "minItems": n_min, "maxItems": n_max, "items": {
        "type": "object", "additionalProperties": False, "required": ["who", "text"],
        "properties": {"who": {"type": "string", "enum": SPEAKERS}, "text": {"type": "string"}}}}


def glm_cfg():
    """GLMの接続先。設定がない・応答しないときは None（Sarashinaだけで書く）。"""
    base = os.environ.get("GLM_BASE_URL", "").rstrip("/")
    if not base:
        return None
    cfg = {"base_url": base, "api_key": os.environ.get("GLM_API_KEY", ""), "model": os.environ.get("GLM_MODEL", "")}
    try:
        req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + cfg["api_key"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            models = json.loads(r.read().decode("utf-8"))["data"]
        if not cfg["model"]:
            cfg["model"] = models[0]["id"]
        return cfg
    except Exception as e:
        print("GLMに接続できないので、Sarashinaだけで書きます:", e)
        return None


def ask_glm(cfg, system, user, schema, max_tokens=6000):
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.6,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}},
    }

    def call(p):
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions", data=json.dumps(p).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode("utf-8"))["choices"][0]["message"].get("content") or ""

    try:
        text = call(payload)
    except urllib.error.HTTPError:
        payload.pop("response_format")
        payload["messages"][1]["content"] += "\n\n考えた過程は書かず、JSONだけを出力してください。"
        text = call(payload)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("GLMがJSONを返しませんでした: " + text[:200])
    return json.loads(m.group(0))


def material(news):
    if news["kind"] == "paper":
        p = news["paper"]
        return (f"題名：{p['title_ja']}（原題：{p['title']}）\n発表：{p['venue']}（{p['date']}）\n"
                f"ひとことで：{p['summary']}\nポイント：" + " / ".join(p["points"]) +
                f"\nSarashinaとのつながり：{p['sarashina']}")
    return f"見出し：「{news['title']}」（{'英語' if news['lang'] == 'en' else '日本語'}のニュース）"


OUTLINE_SYSTEM = (
    "あなたはラジオの構成作家です。「さら」と「しな」の二人が話す短い番組のあらすじを作ります。\n"
    "さら：好奇心旺盛な聞き役。ちょっとおっちょこちょいで、素朴な質問をする。\n"
    "しな：落ち着いた物知りのツッコミ役。知らないことは「わからない」と正直に言う。\n"
    "セリフそのものは書かず、一行ごとに「だれが」「何を言うか（要点）」だけを決めます。"
    "材料に書かれていない数字・人名・出来事は要点に入れません。誰かを悪く言いません。")


def outline(gcfg, news, prev):
    schema = {"type": "object", "additionalProperties": False, "required": ["headline_ja", "flow", "beats"], "properties": {
        "headline_ja": {"type": "string"}, "flow": {"type": "string"},
        "beats": {"type": "array", "minItems": 10, "maxItems": 16, "items": {
            "type": "object", "additionalProperties": False, "required": ["who", "point"],
            "properties": {"who": {"type": "string", "enum": SPEAKERS}, "point": {"type": "string"}}}}}}
    if news["kind"] == "paper":
        corner = ("今日は「論文コーナー」。しなが解説役、さらが聞き役。"
                  "難しい言葉は、さらが質問し、しながたとえ話で言い換える流れを入れる。")
    elif news["kind"] == "ai":
        corner = ("AIやローカルLLMの見出しをきっかけにした雑談。二人もDGX Sparkの上で動くAIなので、"
                  "そのことに軽く触れてもよい。感想や身近な話への脱線は自由。")
    elif news["kind"] == "season":
        corner = ("天気や季節の見出しをきっかけにした雑談。寒さ・食べもの・暮らしの話への脱線は自由。"
                  "二人はAIなので寒さを肌で感じるわけではない、という小ネタを入れてもよい。")
    else:
        corner = "肩の力を抜いた、まったりした見出しをきっかけにした雑談。感想や身近な話への脱線は自由。"
    user = (f"前回の会話の終わり：\n{prev}\n\n{corner}\n\n--- 材料 ---\n{material(news)}\n\n"
            "12行前後のあらすじを作ってください。最初はさらが話題を紹介し、途中で一度は話が身近なことに脱線して戻り、"
            "最後は「さて、次は何の話かな」くらいの軽い一言で終えます（「〜な話へ進もう」のような説明っぽい締めは避ける）。"
            "前の行を受けて返す、掛け合いになるようにします。headline_ja には見出しを自然な日本語で40字以内に（日本語の見出し・論文の題名ならそのまま）、"
            "flow には全体の流れを1〜2文で、beats の point には各行の要点を30字以内で書きます。")
    return ask_glm(gcfg, OUTLINE_SYSTEM, user, schema)


def glm_draft(gcfg, news, plan):
    n = len(plan["beats"])
    schema = {"type": "object", "additionalProperties": False, "required": ["lines"],
              "properties": {"lines": line_schema(n, n)}}
    todo = "\n".join(f"{k + 1}. {b['who']}：{b['point']}" for k, b in enumerate(plan["beats"]))
    user = (f"--- 材料 ---\n{material(news)}\n\n全体の流れ：{plan['flow']}\n\n"
            f"次の要点どおりの順番と話者で、二人の話し言葉のセリフにしてください。\n{todo}\n\n"
            "1行は60字以内。材料にない数字や事実は足しません。")
    got = ask_glm(gcfg, SYSTEM, user, schema).get("lines", [])
    return [{"who": b["who"], "text": re.sub(r"\s+", " ", str(l.get("text", ""))).strip()}
            for b, l in zip(plan["beats"], got)]


def restyle(cfg, news, draft):
    """GLMの下書きを、Sarashinaがさらとしなの口調に言い直す。意味と事実は変えない。
    言い直しがおかしい行（英単語が増えた・短すぎる・長すぎる）は下書きのまま使う。"""
    out, kept = [], 0
    for i in range(0, len(draft), CHUNK):
        part = draft[i:i + CHUNK]
        prev = "\n".join(f"{l['who']}：{l['text']}" for l in out[-4:]) or "（ここから始まります）"
        todo = "\n".join(f"{k + 1}. {l['who']}：{l['text']}" for k, l in enumerate(part))
        schema = {"type": "object", "additionalProperties": False, "required": ["lines"],
                  "properties": {"lines": line_schema(len(part), len(part))}}
        user = (f"直前までの会話：\n{prev}\n\n"
                f"次の{len(part)}行の下書きを、さらとしなの話し言葉に言い直してください。\n{todo}\n\n"
                "守ること：言っている中身・例・数字は一切変えない。新しい例や事実を足さない。英語を混ぜない。"
                "順番と話者はそのまま。言い回しと語尾だけを、その人らしく自然にする。1行は60字以内。"
                "JSONで {\"lines\": [{\"who\": \"さら\", \"text\": \"...\"}, ...]} の形にします。")
        try:
            got = u.ask_sarashina(cfg, SYSTEM, user, schema, max_tokens=600).get("lines", [])
        except Exception:
            got = []
        for k, d in enumerate(part):
            text = tidy(got[k].get("text", "")) if k < len(got) else ""
            new_latin = set(LATIN.findall(text)) - set(LATIN.findall(d["text"] + material(news)))
            if not (2 <= len(text) <= 120) or new_latin:
                text, kept = d["text"], kept + 1
            out.append({"who": d["who"], "text": text})
    return out, kept


def glm_check(gcfg, news, lines):
    schema = {"type": "object", "additionalProperties": False, "required": ["ok", "problems"], "properties": {
        "ok": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}}}}
    talk = "\n".join(f"{i + 1}. {l['who']}：{l['text']}" for i, l in enumerate(lines))
    user = (f"--- 材料 ---\n{material(news)}\n\n--- 会話 ---\n{talk}\n\n"
            "この会話について、(1)材料にない数字・人名・出来事を事実として話していないか、"
            "(2)材料の内容を言い過ぎたり、違う意味に変えたりしていないか、"
            "(3)話がかみ合わない行や、文が途中で切れている行がないか、を確かめてください。"
            "さらの失敗談のような、明らかに冗談や雑談とわかる脱線は問題にしません。"
            "問題があれば problems に「何行目：どこがおかしいか」を短く書き、なければ空にして ok を true にします。")
    r = ask_glm(gcfg, "あなたは丁寧な校閲者です。", user, schema, max_tokens=3000)
    return bool(r.get("ok")) and not r.get("problems"), r.get("problems") or []


def write_with_glm(cfg, gcfg, news, last_lines):
    """GLMが下書き → Sarashinaが言い直し → GLMが確認。2回までやって、通らなければ (None, None)。"""
    prev = "\n".join(f"{l['who']}：{l['text']}" for l in last_lines) or "（これが最初の会話です）"
    for _ in range(2):
        try:
            plan = outline(gcfg, news, prev)
            draft = glm_draft(gcfg, news, plan)
            voiced, kept = restyle(cfg, news, draft)
            ok, problems = glm_check(gcfg, news, voiced)
        except Exception as e:
            WHY.append(f"GLMとの台本づくりに失敗: {e}")
            continue
        if kept:
            print(f"（{kept}行はSarashinaの言い直しがおかしかったので、GLMの下書きのまま）")
        if not ok:
            WHY.append("GLMの確認で問題: " + " / ".join(problems)[:300])
            continue
        head, lines = clean({"headline_ja": plan.get("headline_ja") or news["title"], "lines": voiced}, hi=16)
        if lines:
            return head, lines
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slot", choices=["paper", "ai", "local", "season", "light"],
                    help="番組表を無視して、この話題から探す（試すとき用）")
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
        order = [args.slot] + [k for k in ("ai", "local", "light") if k != args.slot] if args.slot \
            else lineup(dt.datetime.now(u.JST).hour)
        news = pick(state, seen, order)
        if not news:
            print("話題になる見出しが見つかりませんでした。")
            return
        last = doc["episodes"][0]["lines"][-2:] if doc["episodes"] else []
        lines, head, made = None, None, {"voice": cfg["model"]}
        gcfg = glm_cfg()
        if gcfg:
            head, lines = write_with_glm(cfg, gcfg, news, last)
            if lines:
                made = {"draft": gcfg["model"], "voice": cfg["model"]}
            else:
                print("GLMとの台本が通らなかったので、Sarashinaだけで書きます。理由:", " / ".join(WHY))
        for _ in range(0 if lines else 3):  # Sarashinaだけで書く。形が崩れたら2回まで書き直させる
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
                       "lang": news["lang"], "kind": news["kind"], "link": news["link"]}, "lines": lines, "model": cfg["model"], "made": made}
        if args.dry_run:
            print("下書き：" + made["draft"] + " ／ 言い直し：" + made["voice"] if made.get("draft") else "下書きから：" + made["voice"])
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
