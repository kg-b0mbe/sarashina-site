#!/usr/bin/env python3
"""さらとしなの茶話（Mac miniで1日6回、番組表 PROGRAM の時刻に実行）。

番組表（朝：全国の天気・季節／午前：生成AI／昼：ローカルLLM／午後：論文コーナー／夕方と夜：世間話）に沿って
話題を1つ選ぶ。その棚に新しい見出しがない、または最近3日の回と同じ話題なら、別の見出し・別の棚から探す。
季節の回は、月ごとの食べもの（鍋、おでん、かき氷…）を直近と重ならないように1つ選んで話に混ぜる。

台本はGLMがあらすじを作り、セリフは話題の種類で担当を分ける（CLOUD_FIRST）：
  論文・AI     ：Sarashina3.2 mini thinking（sarashina.ai のAPI）が書く → だめなら手元の言い直し
  季節・世間話 ：GLMの下書きを手元のSarashina2.2-3Bが言い直す → だめならSarashina3.2
どちらもGLMが確認する。両方だめなら、手元のSarashina2.2-3Bが下書きから書く（どれかが止まっても茶話は止まらない）。
APIを止めたいときは updater/.env に SARASHINA_CLOUD_ENABLED=0 と書く（手元だけで回る）。
聞き比べの結果（2026-11-22・24）は updater/chat_compare.py で再現できる。

季節の回は7日、世間話は14日で外し、AI・論文の回はふり返れるように残す（KEEP_DAYS）。
"""
import argparse
import datetime as dt
import difflib
import random
import unicodedata
import hashlib
import json
import re
import subprocess
import time
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
# 残す期間（日）。季節の話は1週間で鮮度が落ちるので外し、AI・論文の回はふり返れるように残す。
KEEP_DAYS = {"season": 7, "light": 14}
KEEP_MAX = 3000  # 念のための上限（回）

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
    # 天気・季節（全国の話。特定の地域の天気は LOCAL_WEATHER ではじく。災害の見出しは AVOID ではじく）
    "season": [
        (gnews("全国 天気 寒さ"), False),
        (gnews("全国的に 冷え込み OR 気温"), False),
        (gnews("週末の天気 全国"), False),
        (gnews("紅葉 見頃 OR 初雪 OR 初霜 OR 桜 開花"), False),
        (gnews("季節の話題 暮らし"), False),
    ],
    # 世間話は LIGHT_THEMES（スポーツ・乗り物・生きもの・宇宙と空・旅と行事）から、テーマを回して選ぶ
    "light": [],
}
# 世間話のテーマ。直近に使ったテーマを避けて、順に回す（同じテーマが続かないように）
LIGHT_THEMES = {
    "スポーツ": [gnews("プロ野球 OR Jリーグ OR 大相撲 OR フィギュアスケート"), gnews(topic="SPORTS"),
              gnews("駅伝 OR マラソン OR バレーボール OR バスケットボール")],
    "乗り物": [gnews("新型車両 OR 新幹線 OR 鉄道 新しい"), gnews("新型 飛行機 OR 就航 OR 旅客機"),
             gnews("新型車 発表 OR 自転車 話題 OR フェリー")],
    "生きもの": [gnews("水族館 OR 動物園 赤ちゃん"), gnews("パンダ OR ペンギン OR ラッコ"), gnews("珍しい 生きもの 発見")],
    "宇宙と空": [gnews("流星群 OR 月食 OR 天体ショー"), gnews("ロケット 打ち上げ OR 宇宙"), gnews("オーロラ OR 虹 OR 星空")],
    "旅と行事": [gnews("祭り OR イルミネーション OR 花火"), gnews("紅葉 OR 桜 名所"), gnews("世界遺産 OR 観光地 話題")],
}
# 宣伝・募集・買いもの・ファッションの見出しは、世間話には使わない（一部の人にしか関係しないので）
PROMO = re.compile(r"募集|受講|講座|キャンペーン|セール|クーポン|割引|ポイント|プレゼント|応募|申し?込|予約受付|"
                   r"発売|販売開始|新商品|限定|お得|PR|広告|コーデ|着こなし|ファッション|理由$|ランキング|まとめ|"
                   r"生協|コープ|株価|決算|求人|署名")
FRESH_DAYS = {"ai": 2, "local": 3, "season": 2, "light": 3}

# 1日の番組表（時:話題）。launchd の時刻（jp.sarashina-site.chat.plist）と合わせる。
# その棚に新しい見出しがなければ、後ろに書いた順に別の棚を探す。
PROGRAM = {
    7: ["season", "light", "ai"],     # 朝：天気・季節
    10: ["ai", "local", "light"],     # 午前：生成AIのニュース
    13: ["local", "ai", "light"],     # 昼：ローカルLLM
    16: ["paper", "ai", "local"],     # 午後：論文コーナー（新しい論文がなければAI）
    19: ["light", "season", "ai"],    # 夕方：よもやま（世間話）
    22: ["light", "ai", "local"],     # 夜：世間話（なければAI）
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

# 天気の話は全国の話にしたいので、地方局の見出しや、県名・地方名が主役の見出しは使わない
PREFS = ("北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|東京|神奈川|新潟|富山|石川|福井|山梨|長野|"
         "岐阜|静岡|愛知|三重|滋賀|京都|大阪|兵庫|奈良|和歌山|鳥取|島根|岡山|広島|山口|徳島|香川|愛媛|高知|福岡|"
         "佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄|札幌|仙台|横浜|名古屋|神戸|那覇")
LOCAL_WEATHER = re.compile(rf"(?:{PREFS})(?:県|府|都|市|の天気|天気)?|【[^】]*天気】|放送|テレビ|NHK\s*[^ ]+局")

# 話題がかぶらないように見るときの、ありふれた言葉
STOP = {"ai", "生成", "生成ai", "発表", "公開", "開発", "研究", "研究者", "モデル", "ニュース", "日本", "技術", "新た",
        "最新", "提供", "開始", "天気", "予報", "今日", "明日", "全国", "話題", "利用", "可能", "対応", "企業"}


def topic_words(title):
    t = unicodedata.normalize("NFKC", title).lower()
    words = set(re.findall(r"[ァ-ヴー]{3,}|[一-龥]{2,}|[a-z][a-z0-9.\-]{2,}", t))
    return {w for w in words if w not in STOP}


def similar(title, recent):
    """最近の回と同じ話題か。固有の言葉が2つ以上重なるか、文字の並びがかなり似ていれば同じとみなす。"""
    a = topic_words(title)
    na = unicodedata.normalize("NFKC", title)
    for r in recent:
        if len(a & topic_words(r)) >= 2:
            return True
        if difflib.SequenceMatcher(None, na, unicodedata.normalize("NFKC", r)).ratio() >= 0.6:
            return True
    return False


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


def _candidate(it, state_seen, days, need_ai, recent=(), shelf=""):
    title = clean_title(re.sub(r"\s+-\s+[^-]+$", "", it["title"]).strip())  # 末尾の「 - 媒体名」なども外す
    key = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    if key in state_seen or AVOID.search(title) or len(title) < 8 or not _fresh(it.get("date"), days):
        return None
    if shelf == "season" and LOCAL_WEATHER.search(title) and "全国" not in title:
        return None
    if shelf == "light" and PROMO.search(title):
        return None
    if similar(title, recent):  # 最近話したのと同じ話題（リーマン予想が続く、など）は避ける
        return None
    if need_ai and not AI_WORDS.search(title):
        return None
    return {"title": title, "link": it["link"], "key": key, "lang": "ja" if JA.search(title) else "en"}


# 季節の回で話す食べもの（月ごと）。直近に使ったものは避けて選ぶ。
SEASON_FOODS = {
    (12, 1, 2): ["鍋", "おでん", "焼き芋", "みかん", "肉まん", "ぜんざい", "湯豆腐", "シチュー", "甘酒", "グラタン", "雑炊", "鯛焼き"],
    (3, 4, 5): ["いちご", "桜餅", "たけのこご飯", "春キャベツ", "新じゃが", "柏餅", "菜の花のおひたし", "お花見団子"],
    (6, 7, 8): ["かき氷", "アイス", "そうめん", "スイカ", "冷やし中華", "とうもろこし", "麦茶", "うなぎ", "トマト"],
    (9, 10, 11): ["焼き芋", "栗ごはん", "さんま", "きのこ", "かぼちゃ", "柿", "梨", "豚汁", "鍋", "新米のおにぎり"],
}


def pick_food(state, month):
    foods = next(v for k, v in SEASON_FOODS.items() if month in k)
    used = state.get("chat_foods", [])[-6:]
    choice = random.choice([f for f in foods if f not in used] or foods)
    state.setdefault("chat_foods", []).append(choice)
    state["chat_foods"] = state["chat_foods"][-30:]
    return choice


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


def pick_headline(state_seen, shelf, recent=()):
    for feed, need_ai in FEEDS[shelf]:
        for it in u.rss_items(feed):
            cand = _candidate(it, state_seen, FRESH_DAYS[shelf], need_ai, recent, shelf)
            if cand:
                # 「local」もページ上は「AIのはなし」として出す
                return dict(cand, kind="ai" if shelf in ("ai", "local") else shelf)
    return None


def pick_light(state, seen, recent=()):
    """世間話：直近に使ったテーマを後回しにして、テーマごとに見出しを探す。"""
    used = state.get("light_themes", [])
    themes = sorted(LIGHT_THEMES, key=lambda t: (used[::-1].index(t) * -1 if t in used else -99, random.random()))
    for theme in themes:
        for feed in LIGHT_THEMES[theme]:
            for it in u.rss_items(feed):
                cand = _candidate(it, seen, FRESH_DAYS["light"], False, recent, "light")
                if cand:
                    state["light_themes"] = (used + [theme])[-10:]
                    return dict(cand, kind="light", theme=theme)
    return None


def pick(state, seen, order, recent=()):
    for shelf in order:
        news = (pick_paper(state) if shelf == "paper" else pick_light(state, seen, recent) if shelf == "light"
                else pick_headline(seen, shelf, recent))
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
    topic = ("AIやローカルLLMの話題です。" + ("小さなモデルなら、ふつうのパソコンでも手元で動かせることに、1行だけさらりと触れてよい（特別な機械でないと動かない、とは言わない。機械の名前は出さない）。" if news.get("spark") else "自分たちがどんな機械（DGX Sparkなど）で動いているかの話はしない。")
             if news["kind"] == "ai" else
             "天気や季節の話題です。寒さや暮らしの話に脱線してかまいません。" if news["kind"] == "season" else
             f"肩の力を抜いた世間話です（テーマ：{news.get('theme', 'よもやま')}）。誰でも楽しめる明るい話にしてください。")
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
    text = text.replace("\u2011", "-").replace("\u2010", "-")  # 見えないハイフンの違い（2.2‑TTS など）をそろえる
    text = re.sub(r"[\s\"'{}\[\],]+$", "", text)            # 行末に紛れ込んだ } " ] など
    text = re.sub(r"(?<=[ぁ-んァ-ン一-龥ー〜！？])\.$", "。", text)  # 「〜.」→「〜。」
    text = re.sub(r"〜。$", "〜", text)
    return text.strip()


def clean_title(title):
    """見出しの頭の【動画】【速報】や、末尾の（22日19時更新）を外す。"""
    title = re.sub(r"^(【[^】]{1,8}】\s*)+", "", title)
    title = re.sub(r"\s*[（(][^）)]*更新[）)]\s*$", "", title)
    parts = re.split(r"\s*[|｜]\s*", title)  # 「見出し | 長崎のニュース | NBC長崎放送」→「見出し」
    if len(parts) > 1 and len(parts[0]) >= 8:
        title = parts[0]
    return title.strip()


def prune(episodes, now):
    """季節・世間話は鮮度の落ちた回を外す。AI・論文の回は残す。"""
    keep = []
    for ep in episodes:
        days = KEEP_DAYS.get(ep["news"]["kind"])
        if days:
            try:
                at = dt.datetime.strptime(ep["at"], "%Y-%m-%d %H:%M").date()
            except ValueError:
                at = now.date()
            if (now.date() - at).days > days:
                continue
        keep.append(ep)
    return keep[:KEEP_MAX]


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
        corner = ("AIやローカルLLMの見出しをきっかけにした雑談。感想や身近な話への脱線は自由。"
                  + ("小さなモデルなら、ふつうのパソコンでも手元で動かせることに、1行だけさらりと触れてよい（特別な機械でないと動かない、とは言わない。機械の名前は出さない）。" if news.get("spark") else "自分たちがどんな機械（DGX Sparkなど）で動いているかの話はしない。"))
    elif news["kind"] == "season":
        food = news.get("food") or "季節の食べもの"
        corner = ("天気や季節の見出しをきっかけにした雑談。特定の地域の話にせず、全国的な季節の移り変わりとして話す"
                  "（見出しに地名があっても、そこだけの話にしない）。"
                  f"途中で、今日は「{food}」の話に自然につなげる（毎回「鍋」にしない）。"
                  + ("二人はAIなので寒さや暑さを肌で感じない、という小ネタを1行だけ入れてよい。" if news.get("gag")
                     else "二人がAIで寒さや暑さを感じない、という話は今回は入れない。"))
    else:
        corner = (f"肩の力を抜いた世間話（今日のテーマ：{news.get('theme', 'よもやま')}）。見出しをきっかけに、"
                  "「見てみたい」「乗ってみたい」「応援したい」のような素直な感想や、好きなもの、ちょっとした思い出話へ"
                  "脱線は自由。誰でも楽しめる明るい話にする。AIや技術の話には寄せない。")
    if news.get("avoid"):
        corner += "\n最近の回で使った言い回し（下）と同じ表現・同じ冗談・同じ食べものは避ける：\n" + news["avoid"]
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
    # 話者はGLMが書いたほうを使う（あらすじから1行ずれても、セリフと話者が食い違わないように）
    return [{"who": l.get("who") if l.get("who") in SPEAKERS else b["who"],
             "text": re.sub(r"\s+", " ", str(l.get("text", ""))).strip()}
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
            "(3)話がかみ合わない行や、文が途中で切れている行がないか、"
            "(4)質問した次の行が、その質問に答えているか、"
            "(5)セリフではなく「〜と説明」「〜と質問」のような台本のメモがそのまま残っていないか、を確かめてください。"
            "さらの失敗談のような、明らかに冗談や雑談とわかる脱線は問題にしません。"
            "問題があれば problems に「何行目：どこがおかしいか」を短く書き、なければ空にして ok を true にします。")
    r = ask_glm(gcfg, "あなたは丁寧な校閲者です。", user, schema, max_tokens=3000)
    return bool(r.get("ok")) and not r.get("problems"), r.get("problems") or []


# ---------------------------------------------------------------- Sarashina3.2（sarashina.ai のAPI）

CLOUD_LABEL = "Sarashina3.2"
_last_cloud = [0.0]


def cloud_cfg():
    """sarashina.ai のAPI。キーとモデルIDがなければ None（使わない）。"""
    key = os.environ.get("SARASHINA_CLOUD_API_KEY", "")
    model = os.environ.get("SARASHINA_CLOUD_MODEL", "")
    if not key or not model or os.environ.get("SARASHINA_CLOUD_ENABLED", "1") == "0":
        return None
    extra = os.environ.get("SARASHINA_CLOUD_EXTRA", "").strip()
    return {"base_url": os.environ.get("SARASHINA_CLOUD_BASE_URL", "https://api.sarashina.ai/v1").rstrip("/"),
            "api_key": key, "model": model, "extra": json.loads(extra) if extra else {}}


def ask_cloud(cfg, system, user, schema, max_tokens=16000, gap=3):
    """APIのSarashinaにJSONで答えさせる。思考モデルは考えた分も max_completion_tokens に数えられる（上限32768）。
    回数上限（429）は Retry-After だけ待って1回やり直す。ほかの失敗はそのまま例外にして、呼び出し側が予備に切り替える。"""
    payload = dict({
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user + "\n\nJSONだけを出力してください。"}],
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}},
    }, **cfg["extra"])

    def call(p):
        wait = gap - (time.time() - _last_cloud[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions", data=json.dumps(p).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                body = json.loads(r.read().decode("utf-8"))
        finally:
            _last_cloud[0] = time.time()
        return body["choices"][0]["message"].get("content") or "", body.get("usage") or {}

    for attempt in range(3):
        try:
            text, usage = call(payload)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(min(int(e.headers.get("Retry-After") or 30), 90) + 1)
                continue
            if e.code in (400, 422) and "response_format" in payload:
                payload.pop("response_format")
                continue
            raise RuntimeError(f"Sarashina API {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    else:
        raise RuntimeError("Sarashina APIの回数上限が続いた")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Sarashina APIがJSONを返さなかった: " + text[:120])
    return json.loads(m.group(0)), usage


def cloud_write(ccfg, news, plan):
    """あらすじの要点どおりに、Sarashina3.2がセリフを書く。要点の書き写しが多ければ失敗とする。"""
    n = len(plan["beats"])
    schema = {"type": "object", "additionalProperties": False, "required": ["lines"],
              "properties": {"lines": line_schema(n, n)}}
    todo = "\n".join(f"{k + 1}. {b['who']}：{b['point']}" for k, b in enumerate(plan["beats"]))
    user = (f"--- 材料 ---\n{material(news)}\n\n全体の流れ：{plan['flow']}\n\n"
            f"次の要点どおりの順番と話者で、二人の話し言葉のセリフにしてください。\n{todo}\n\n"
            "要点は「何を言うか」のメモです。そのまま書き写さず、その人が実際に口にするセリフにしてください。"
            "例：要点「報酬とは何かと質問」→ セリフ「報酬って何？テストの点数みたいなもの？」。"
            "質問の次の行は、その質問にきちんと答えます。1行は60字以内。材料にない数字や事実は足しません。"
            "JSONで {\"lines\": [{\"who\": \"さら\", \"text\": \"...\"}, ...]} の形にします。")
    d, usage = ask_cloud(ccfg, SYSTEM, user, schema)
    lines = [{"who": b["who"], "text": tidy(l.get("text", ""))} for b, l in zip(plan["beats"], d.get("lines", []))]
    copied = sum(1 for b, l in zip(plan["beats"], lines)
                 if difflib.SequenceMatcher(None, b["point"], l["text"]).ratio() > 0.8)
    if copied > 2 or len(lines) < n:
        raise ValueError(f"セリフになっていない（要点の書き写し {copied}行、{len(lines)}/{n}行）")
    return lines, usage


# 話題の種類ごとに、セリフをどちらに先に任せるか（聞き比べの結果、2026-11-24）
#   論文・AI：正確さが大事 → Sarashina3.2（API）が、あらすじから中身を外さずに書く
#   季節・世間話：楽しさが大事 → 手元のSarashina2.2が、GLMの下書きをくだけた口調に言い直す
CLOUD_FIRST = {"paper", "ai"}


def write_with_glm(cfg, gcfg, ccfg, news, last_lines, local_ok=True):
    """台本づくり。GLMがあらすじを作り、セリフは次の2つの方法を、話題の種類で決めた順に試す：
      cloud：Sarashina3.2（API）があらすじからセリフを書く
      local：GLMが下書き → 手元のSarashina2.2が言い直す
    どちらもGLMが確認し、通ったものを使う。2回やって通らなければ (None, None, None)。
    APIが止まっていても（ベータ終了・回数上限・障害）、local に回るので茶話は止まらない。"""
    prev = "\n".join(f"{l['who']}：{l['text']}" for l in last_lines) or "（これが最初の会話です）"
    head_of = lambda plan: plan.get("headline_ja") or news["title"]  # noqa: E731
    order = ["cloud", "local"] if news["kind"] in CLOUD_FIRST else ["local", "cloud"]
    order = [w for w in order if (w == "cloud" and ccfg) or (w == "local" and local_ok)]

    def by_cloud(plan):
        lines, _ = cloud_write(ccfg, news, plan)
        ok, problems = glm_check(gcfg, news, lines)
        if not ok:
            raise ValueError("GLMの確認で問題: " + " / ".join(problems)[:200])
        return lines, {"plan": gcfg["model"], "writer": ccfg["model"], "writer_label": CLOUD_LABEL}

    def by_local(plan):
        draft = glm_draft(gcfg, news, plan)
        voiced, kept = restyle(cfg, news, draft)
        if kept:
            print(f"（{kept}行はSarashinaの言い直しがおかしかったので、GLMの下書きのまま）")
        ok, problems = glm_check(gcfg, news, voiced)
        if not ok:
            raise ValueError("GLMの確認で問題: " + " / ".join(problems)[:200])
        return voiced, {"draft": gcfg["model"], "voice": cfg["model"]}

    names = {"cloud": "Sarashina3.2", "local": "手元のSarashina2.2の言い直し"}
    for _ in range(2):
        try:
            plan = outline(gcfg, news, prev)
        except Exception as e:
            WHY.append(f"GLMのあらすじに失敗: {e}")
            continue
        for way in order:
            try:
                lines, made = (by_cloud if way == "cloud" else by_local)(plan)
            except Exception as e:
                WHY.append(f"{names[way]}：{e}")
                continue
            head, lines = clean({"headline_ja": head_of(plan), "lines": lines}, hi=16)
            if lines:
                return head, lines, made
    return None, None, None

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
        local_ok = u.sarashina_alive(cfg)
        gcfg = glm_cfg()
        ccfg = cloud_cfg() if gcfg else None
        if not local_ok and not ccfg:
            print("手元のSarashinaが応答せず、Sarashina3.2も使えないので、今回はお休みします。")
            return

        doc = json.loads(CHAT.read_text(encoding="utf-8"))
        state = u.load_state(cfg)
        seen = set(state.get("chat_keys", []))
        order = [args.slot] + [k for k in ("ai", "local", "light") if k != args.slot] if args.slot \
            else lineup(dt.datetime.now(u.JST).hour)
        now0 = dt.datetime.now(u.JST)
        recent_eps = [e for e in doc["episodes"]
                      if (now0 - dt.datetime.strptime(e["at"], "%Y-%m-%d %H:%M").replace(tzinfo=now0.tzinfo)).days < 3]
        recent = [e["news"].get("title_ja") or e["news"]["title"] for e in recent_eps]
        news = pick(state, seen, order, recent)
        if not news:
            print("話題になる見出しが見つかりませんでした。")
            return
        if news["kind"] == "ai":
            # 「手元で動く」話は、たまに（6回に1回くらい）。Sparkを言いすぎると、Sparkでないと動かないと誤解されるので
            news["spark"] = random.random() < 0.15
        if news["kind"] == "season":
            news["food"] = pick_food(state, now0.month)
            news["gag"] = random.random() < 0.25  # 「AIだから寒さを感じない」は4回に1回くらい
        if news["kind"] in ("season", "light"):
            same = [e for e in doc["episodes"] if e["news"]["kind"] == news["kind"]][:2]
            news["avoid"] = "\n".join(l["text"] for e in same for l in e["lines"])[:600]
        last = doc["episodes"][0]["lines"][-2:] if doc["episodes"] else []
        lines, head, made = None, None, {"voice": cfg["model"]}
        if gcfg:
            head, lines, got = write_with_glm(cfg, gcfg, ccfg, news, last, local_ok)
            if lines:
                made = got
            else:
                print("GLMとの台本が通らなかったので、" + ("手元のSarashinaだけで書きます。" if local_ok else "今回はお休みします。")
                      + "理由:", " / ".join(WHY))
        for _ in range(0 if lines or not local_ok else 3):  # Sarashinaだけで書く。形が崩れたら2回まで書き直させる
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
                       "lang": news["lang"], "kind": news["kind"], "link": news["link"]}, "lines": lines, "model": made.get("writer") or cfg["model"], "made": made}
        if args.dry_run:
            print("あらすじ：" + made["plan"] + " ／ セリフ：" + made["writer"] if made.get("writer") else
                  "下書き：" + made["draft"] + " ／ 言い直し：" + made["voice"] if made.get("draft") else "下書きから：" + made["voice"])
            if news.get("spark"):
                print("（今回は「手元で動く」話に1行だけ触れてよい回）")
            if news.get("food"):
                print("（今日の食べもの：" + news["food"] + ("・AIの小ネタあり" if news.get("gag") else "") + "）")
            print(f"【{news['kind']}】{news['title']}" + (f"\n（日本語：{head}）" if news["lang"] == "en" else ""))
            for l in lines:
                print(f"{l['who']}：{l['text']}")
            return

        doc["episodes"] = prune([ep] + doc["episodes"], now)
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
