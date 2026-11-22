#!/usr/bin/env python3
"""茶話の聞き比べ（試作）。サイトには何も書き込まず、pushもしない。

同じ話題で、2とおりの台本を作って並べる。
  A：いまのやり方（Sarashinaだけで8行前後）
  B：GLMがあらすじ（だれが、何を話すか）を作り、Sarashinaがそれに沿ってセリフを書く。
     最後にGLMが「見出しや材料にない事実を足していないか」を確かめる。
  C：参考として、同じあらすじでセリフもGLMが書く。

  python3 updater/chat_compare.py                 # 論文手帖のいちばん新しい論文で比べる
  python3 updater/chat_compare.py --paper <id>    # 論文を指定
  python3 updater/chat_compare.py --news          # いまのニュースの見出しから1つ選んで比べる
  python3 updater/chat_compare.py --headline "見出し"  # 見出しを手で渡す

結果は画面に出し、~/sarashina-compare/ にMarkdownでも保存する。
GLMの接続先は updater/.env に GLM_BASE_URL / GLM_API_KEY / GLM_MODEL（空なら自動）を書く。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as u  # noqa: E402
import chat as c  # noqa: E402

OUT = Path.home() / "sarashina-compare"
CHUNK = 4  # Sarashinaに一度に書かせるセリフの数（小さいモデルなので少しずつ）


# ---------------------------------------------------------------- GLM

def glm_cfg():
    base = os.environ.get("GLM_BASE_URL", "").rstrip("/")
    if not base:
        sys.exit("updater/.env に GLM_BASE_URL がありません（例：http://spark-a436:8888/v1）")
    cfg = {"base_url": base, "api_key": os.environ.get("GLM_API_KEY", ""), "model": os.environ.get("GLM_MODEL", "")}
    if not cfg["model"]:  # 指定がなければ、サーバーが出しているモデルの1つ目を使う
        req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + cfg["api_key"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            cfg["model"] = json.loads(r.read().decode("utf-8"))["data"][0]["id"]
    return cfg


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
            msg = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]
        return msg.get("content") or ""

    try:
        text = call(payload)
    except urllib.error.HTTPError:
        payload.pop("response_format")
        payload["messages"][1]["content"] += "\n\n考えた過程は書かず、JSONだけを出力してください。"
        text = call(payload)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # 考える途中が混ざっていたら外す
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("GLMがJSONを返しませんでした: " + text[:200])
    return json.loads(m.group(0))


# ---------------------------------------------------------------- 材料

def material(news):
    if news["kind"] == "paper":
        p = news["paper"]
        return (f"題名：{p['title_ja']}（原題：{p['title']}）\n発表：{p['venue']}（{p['date']}）\n"
                f"ひとことで：{p['summary']}\nポイント：" + " / ".join(p["points"]) +
                f"\nSarashinaとのつながり：{p['sarashina']}")
    return f"見出し：「{news['title']}」（{'英語' if news['lang'] == 'en' else '日本語'}のニュース）"


def pick(args):
    if args.headline:
        return {"title": args.headline, "link": "", "key": "manual", "lang": "ja" if c.JA.search(args.headline) else "en",
                "kind": "ai"}
    if args.news:
        n = c.pick_headline(set())
        if not n:
            sys.exit("見出しが見つかりませんでした。")
        return n
    papers = json.loads(c.PAPERS.read_text(encoding="utf-8"))["papers"]
    if args.paper:
        ps = [p for p in papers if p["id"] == args.paper]
        if not ps:
            sys.exit("その id の論文はありません: " + args.paper)
        p = ps[0]
    else:
        p = sorted(papers, key=lambda x: x["date"], reverse=True)[0]
    link = (p.get("links") or {}).get("paper") or p["sources"][0]
    return {"title": p["title_ja"], "link": link, "key": "paper:" + p["id"], "lang": "ja", "kind": "paper", "paper": p}


# ---------------------------------------------------------------- A：いまのやり方

def version_a(cfg, news):
    c.WHY.clear()
    for _ in range(3):
        try:
            head, lines = c.clean(c.write_episode(cfg, news, []))
        except Exception as e:
            c.WHY.append(f"下書きに失敗: {e}")
            continue
        if lines:
            return lines, list(c.WHY)
    return None, list(c.WHY)


# ---------------------------------------------------------------- B：GLMのあらすじ → Sarashinaのセリフ

OUTLINE_SYSTEM = (
    "あなたはラジオの構成作家です。「さら」と「しな」の二人が話す短い番組のあらすじを作ります。\n"
    "さら：好奇心旺盛な聞き役。ちょっとおっちょこちょいで、素朴な質問をする。\n"
    "しな：落ち着いた物知りのツッコミ役。知らないことは「わからない」と正直に言う。\n"
    "セリフそのものは書かず、一行ごとに「だれが」「何を言うか（要点）」だけを決めます。"
    "材料に書かれていない数字・人名・出来事は要点に入れません。誰かを悪く言いません。")


def outline(gcfg, news):
    schema = {"type": "object", "additionalProperties": False, "required": ["flow", "beats"], "properties": {
        "flow": {"type": "string"},
        "beats": {"type": "array", "minItems": 10, "maxItems": 16, "items": {
            "type": "object", "additionalProperties": False, "required": ["who", "point"],
            "properties": {"who": {"type": "string", "enum": c.SPEAKERS}, "point": {"type": "string"}}}}}}
    corner = ("今日は「論文コーナー」。しなが解説役、さらが聞き役。難しい言葉は、さらが質問し、しながたとえ話で言い換える流れを入れる。"
              if news["kind"] == "paper" else "見出しをきっかけにした雑談。感想や身近な話への脱線は自由。")
    user = (f"{corner}\n\n--- 材料 ---\n{material(news)}\n\n"
            "12行前後のあらすじを作ってください。最初はさらが話題を紹介し、途中で一度は話が身近なことに脱線して戻り、"
            "最後は次の話題につながる一言で終えます。前の行を受けて返す、掛け合いになるようにします。"
            "flow には全体の流れを1〜2文で、beats の point には各行の要点を30字以内で書きます。")
    return ask_glm(gcfg, OUTLINE_SYSTEM, user, schema)


def lines_from_outline(cfg, news, plan):
    beats = plan["beats"]
    lines = []
    for i in range(0, len(beats), CHUNK):
        part = beats[i:i + CHUNK]
        prev = "\n".join(f"{l['who']}：{l['text']}" for l in lines[-4:]) or "（ここから始まります）"
        todo = "\n".join(f"{k + 1}. {b['who']}：{b['point']}" for k, b in enumerate(part))
        schema = {"type": "object", "additionalProperties": False, "required": ["lines"], "properties": {
            "lines": {"type": "array", "minItems": len(part), "maxItems": len(part), "items": {
                "type": "object", "additionalProperties": False, "required": ["who", "text"],
                "properties": {"who": {"type": "string", "enum": c.SPEAKERS}, "text": {"type": "string"}}}}}}
        user = (f"--- 材料 ---\n{material(news)}\n\n全体の流れ：{plan['flow']}\n\n"
                f"直前までの会話：\n{prev}\n\n"
                f"続きの{len(part)}行を、次の要点どおりの順番と話者で、二人の話し言葉のセリフにしてください。\n{todo}\n\n"
                "1行は60字以内。材料にない数字や事実は足しません。"
                "JSONで {\"lines\": [{\"who\": \"さら\", \"text\": \"...\"}, ...]} の形にします。")
        got = u.ask_sarashina(cfg, c.SYSTEM, user, schema, max_tokens=600).get("lines", [])
        for b, l in zip(part, got):
            text = re.sub(r"\s+", " ", str(l.get("text", ""))).replace("</s>", "").strip()
            text = re.sub(r"^(さら|しな)[:：]\s*", "", text)
            text = re.sub(r"Sarashinar(?=\d)", "Sarashina", text)
            lines.append({"who": b["who"], "text": text})  # 話者はあらすじのほうを正とする
    return lines


def glm_lines(gcfg, news, plan):
    """C：同じあらすじのまま、セリフもGLMが書く（比べるための参考）。"""
    schema = {"type": "object", "additionalProperties": False, "required": ["lines"], "properties": {
        "lines": {"type": "array", "minItems": len(plan["beats"]), "maxItems": len(plan["beats"]), "items": {
            "type": "object", "additionalProperties": False, "required": ["who", "text"],
            "properties": {"who": {"type": "string", "enum": c.SPEAKERS}, "text": {"type": "string"}}}}}}
    todo = "\n".join(f"{k + 1}. {b['who']}：{b['point']}" for k, b in enumerate(plan["beats"]))
    user = (f"--- 材料 ---\n{material(news)}\n\n全体の流れ：{plan['flow']}\n\n"
            f"次の要点どおりの順番と話者で、二人の話し言葉のセリフにしてください。\n{todo}\n\n"
            "1行は60字以内。材料にない数字や事実は足しません。")
    got = ask_glm(gcfg, c.SYSTEM, user, schema).get("lines", [])
    return [{"who": b["who"], "text": str(l.get("text", "")).strip()} for b, l in zip(plan["beats"], got)]


def check(gcfg, news, lines):
    schema = {"type": "object", "additionalProperties": False, "required": ["ok", "problems"], "properties": {
        "ok": {"type": "boolean"}, "problems": {"type": "array", "items": {"type": "string"}}}}
    talk = "\n".join(f"{l['who']}：{l['text']}" for l in lines)
    user = (f"--- 材料 ---\n{material(news)}\n\n--- 会話 ---\n{talk}\n\n"
            "この会話について、(1)材料にない数字・人名・出来事を事実として話していないか、"
            "(2)話がかみ合わない行や、文が途中で切れている行がないか、を確かめてください。"
            "問題があれば problems に「何行目：どこがおかしいか」を短く書き、なければ空にして ok を true にします。")
    return ask_glm(gcfg, "あなたは丁寧な校閲者です。", user, schema, max_tokens=3000)


# ---------------------------------------------------------------- 出力

def show(lines):
    return "\n".join(f"{l['who']}：{l['text']}" for l in lines) if lines else "（うまく書けませんでした）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper")
    ap.add_argument("--news", action="store_true")
    ap.add_argument("--headline")
    args = ap.parse_args()
    cfg = u.load_env()
    gcfg = glm_cfg()
    if not u.sarashina_alive(cfg):
        sys.exit("Sarashinaに接続できません。")
    news = pick(args)
    print(f"話題：【{news['kind']}】{news['title']}\nGLM：{gcfg['model']}\n")

    t = time.time()
    a, why = version_a(cfg, news)
    ta = time.time() - t
    print(f"=== A：Sarashinaだけ（{ta:.0f}秒）===\n{show(a)}\n")
    if why:
        print("（捨てた下書きの理由：" + " / ".join(why) + "）\n")

    t = time.time()
    plan = outline(gcfg, news)
    b = lines_from_outline(cfg, news, plan)
    review = check(gcfg, news, b)
    tb = time.time() - t
    plan_txt = plan["flow"] + "\n" + "\n".join(f"{i + 1}. {x['who']}：{x['point']}" for i, x in enumerate(plan["beats"]))
    review_txt = "問題なし" if review.get("ok") and not review.get("problems") else "\n".join("- " + p for p in review["problems"])
    print(f"=== B：GLMのあらすじ → Sarashinaのセリフ（{tb:.0f}秒）===\n[あらすじ]\n{plan_txt}\n\n[セリフ]\n{show(b)}\n\n"
          f"[GLMの確認]\n{review_txt}\n")

    t = time.time()
    try:
        cl = glm_lines(gcfg, news, plan)
    except Exception as e:
        print("Cの生成に失敗:", e)
        cl = []
    tc = time.time() - t
    print(f"=== C：あらすじもセリフもGLM（参考・{tc:.0f}秒）===\n{show(cl)}\n")

    OUT.mkdir(exist_ok=True)
    path = OUT / (dt.datetime.now(u.JST).strftime("%Y%m%d-%H%M") + ".md")
    path.write_text(
        f"# 茶話の聞き比べ\n\n話題：【{news['kind']}】{news['title']}\n\nGLM：{gcfg['model']} / Sarashina：{cfg['model']}\n\n"
        f"## A：Sarashinaだけ（{len(a or [])}行・{ta:.0f}秒）\n\n{show(a)}\n\n"
        f"## B：GLMのあらすじ → Sarashinaのセリフ（{len(b)}行・{tb:.0f}秒）\n\n### あらすじ\n\n{plan_txt}\n\n"
        f"### セリフ\n\n{show(b)}\n\n### GLMの確認\n\n{review_txt}\n\n"
        f"## C：あらすじもセリフもGLM（参考・{len(cl)}行・{tc:.0f}秒）\n\n{show(cl)}\n", encoding="utf-8")
    print("保存しました:", path)


if __name__ == "__main__":
    main()
