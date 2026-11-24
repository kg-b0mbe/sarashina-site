#!/usr/bin/env python3
"""茶話の聞き比べ（試作）。サイトには何も書き込まず、pushもしない。

同じ話題・同じあらすじで、作り方の違う台本を並べる。
  A：Sarashina2.2-3B（手元）だけで書く                 … GLMが止まったときの予備の形
  C：GLMが下書きをそのまま使う                         … 参考
  D：GLMの下書き → Sarashina2.2-3B（手元）が言い直す   … いまの茶話の形
  E：Sarashina3.2 mini thinking（API）が、あらすじから書く
  F：GLMの下書き → Sarashina3.2 mini thinking（API）が言い直す
D・E・F は、最後にGLMが「材料にない事実を足していないか」を確かめる。

  python3 updater/chat_compare.py                 # 論文手帖のいちばん新しい論文で比べる
  python3 updater/chat_compare.py --paper <id>    # 論文を指定
  python3 updater/chat_compare.py --news ai       # 番組表の棚（ai / local / season / light）から見出しを1つ
  python3 updater/chat_compare.py --headline "見出し"
  python3 updater/chat_compare.py --only A,D      # 一部だけ比べる

結果は画面に出し、~/sarashina-compare/ にMarkdownでも保存する。

Sarashina3.2（API）を使うときは updater/.env に書く（書かなければ E・F は飛ばす）：
  SARASHINA_CLOUD_API_KEY=…            # sarashina.ai で発行したキー（手元用の SARASHINA_API_KEY とは別）
  SARASHINA_CLOUD_MODEL=sarashina3.2-mini-thinking
  SARASHINA_CLOUD_BASE_URL=https://api.sarashina.ai/v1
  SARASHINA_CLOUD_EXTRA={"reasoning_effort":"low"}   # 思考の深さ low / medium / high（JSON・任意）
無償ベータは入力・出力が学習に使われる場合があるので、個人の情報は送らない（ニュースの見出しと論文の要約だけを送る）。
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
CLOUD_GAP = 3  # 1分あたりの回数に上限がある（mini thinkingは30回）ので、APIを呼ぶ間を少しあける（秒）
_last_cloud = [0.0]


# ---------------------------------------------------------------- Sarashina3.2（API）

def cloud_cfg():
    key = os.environ.get("SARASHINA_CLOUD_API_KEY", "")
    model = os.environ.get("SARASHINA_CLOUD_MODEL", "")
    if not key or not model:
        return None
    extra = os.environ.get("SARASHINA_CLOUD_EXTRA", "").strip()
    return {"base_url": os.environ.get("SARASHINA_CLOUD_BASE_URL", "https://api.sarashina.ai/v1").rstrip("/"),
            "api_key": key, "model": model, "extra": json.loads(extra) if extra else {}}


def ask_cloud(cfg, system, user, schema, max_tokens=16000):
    # 思考モデルは考えた分も max_completion_tokens に数えられる（上限32768）ので、多めにとる
    """APIのSarashinaにJSONで答えさせる。思考の部分は外し、本文からJSONを取り出す。"""
    payload = dict({
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user + "\n\nJSONだけを出力してください。"}],
        "max_completion_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}},
    }, **cfg["extra"])

    def call(p):
        wait = CLOUD_GAP - (time.time() - _last_cloud[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions", data=json.dumps(p).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                body = json.loads(r.read().decode("utf-8"))
        finally:
            _last_cloud[0] = time.time()
        msg = body["choices"][0]["message"]
        return msg.get("content") or "", body.get("usage") or {}

    for attempt in range(3):
        try:
            text, usage = call(payload)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:  # 回数の上限にかかったら、Retry-After の秒数だけ待ってやり直す
                time.sleep(int(e.headers.get("Retry-After") or 30) + 1)
                continue
            if e.code in (400, 422) and "response_format" in payload:  # 構造化出力に未対応なら外す
                payload.pop("response_format")
                continue
            raise RuntimeError(f"Sarashina APIがエラーを返しました: {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
    else:
        raise RuntimeError("Sarashina APIの回数上限が続いたので、あきらめました")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSONが返りませんでした: " + text[:200])
    return json.loads(m.group(0)), usage


def cloud_write(ccfg, news, plan):
    """E：あらすじの要点どおりに、Sarashina3.2がセリフを書く。"""
    n = len(plan["beats"])
    schema = {"type": "object", "additionalProperties": False, "required": ["lines"],
              "properties": {"lines": c.line_schema(n, n)}}
    todo = "\n".join(f"{k + 1}. {b['who']}：{b['point']}" for k, b in enumerate(plan["beats"]))
    user = (f"--- 材料 ---\n{c.material(news)}\n\n全体の流れ：{plan['flow']}\n\n"
            f"次の要点どおりの順番と話者で、二人の話し言葉のセリフにしてください。\n{todo}\n\n"
            "要点は「何を言うか」のメモです。そのまま書き写さず、その人が実際に口にするセリフにしてください。"
            "例：要点「報酬とは何かと質問」→ セリフ「報酬って何？テストの点数みたいなもの？」。"
            "質問の次の行は、その質問にきちんと答えます。1行は60字以内。材料にない数字や事実は足しません。"
            "JSONで {\"lines\": [{\"who\": \"さら\", \"text\": \"...\"}, ...]} の形にします。")
    d, usage = ask_cloud(ccfg, c.SYSTEM, user, schema)
    lines = [{"who": b["who"], "text": c.tidy(l.get("text", ""))} for b, l in zip(plan["beats"], d.get("lines", []))]
    # 要点をそのまま書き写していないか（セリフになっていない）を数える
    import difflib
    copied = sum(1 for b, l in zip(plan["beats"], lines)
                 if difflib.SequenceMatcher(None, b["point"], l["text"]).ratio() > 0.8)
    if copied:
        usage = dict(usage or {}, 要点の書き写し=f"{copied}/{len(lines)}行")
    return lines, usage


def cloud_restyle(ccfg, news, draft):
    """F：GLMの下書きを、Sarashina3.2がさらとしなの口調に言い直す（中身は変えない）。"""
    n = len(draft)
    schema = {"type": "object", "additionalProperties": False, "required": ["lines"],
              "properties": {"lines": c.line_schema(n, n)}}
    todo = "\n".join(f"{k + 1}. {l['who']}：{l['text']}" for k, l in enumerate(draft))
    user = (f"次の{n}行の下書きを、さらとしなの話し言葉に言い直してください。\n{todo}\n\n"
            "守ること：言っている中身・例・数字は一切変えない。新しい例や事実を足さない。英語を混ぜない。"
            "順番と話者はそのまま。言い回しと語尾だけを、その人らしく自然にする。1行は60字以内。"
            "JSONで {\"lines\": [{\"who\": \"さら\", \"text\": \"...\"}, ...]} の形にします。")
    d, usage = ask_cloud(ccfg, c.SYSTEM, user, schema)
    got = d.get("lines", [])
    out = []
    for k, l in enumerate(draft):
        text = c.tidy(got[k].get("text", "")) if k < len(got) else ""
        out.append({"who": l["who"], "text": text if 2 <= len(text) <= 120 else l["text"]})
    return out, usage


# ---------------------------------------------------------------- 話題

def pick(args):
    if args.headline:
        return {"title": args.headline, "link": "", "key": "manual",
                "lang": "ja" if re.search(r"[ぁ-んァ-ン一-龥]", args.headline) else "en", "kind": "ai"}
    if args.news:
        n = c.pick_headline(set(), args.news)
        if not n:
            sys.exit("その棚に新しい見出しが見つかりませんでした。")
        return n
    papers = json.loads(c.PAPERS.read_text(encoding="utf-8"))["papers"]
    p = next((x for x in papers if x["id"] == args.paper), None) if args.paper else \
        sorted(papers, key=lambda x: x["date"], reverse=True)[0]
    if not p:
        sys.exit("その id の論文はありません: " + args.paper)
    link = (p.get("links") or {}).get("paper") or p["sources"][0]
    return {"title": p["title_ja"], "link": link, "key": "paper:" + p["id"], "lang": "ja", "kind": "paper", "paper": p}


# ---------------------------------------------------------------- 出力

def show(lines):
    return "\n".join(f"{l['who']}：{l['text']}" for l in lines) if lines else "（うまく書けませんでした）"


def run(label, fn):
    t = time.time()
    try:
        lines, note = fn()
    except Exception as e:
        lines, note = [], f"失敗: {e}"
    return {"label": label, "lines": lines, "note": note, "sec": time.time() - t}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper")
    ap.add_argument("--news", choices=["ai", "local", "season", "light"])
    ap.add_argument("--headline")
    ap.add_argument("--only", default="A,C,D,E,F")
    args = ap.parse_args()
    want = set(args.only.upper().split(","))

    cfg = u.load_env()
    gcfg = c.glm_cfg()
    ccfg = cloud_cfg()
    if not u.sarashina_alive(cfg):
        sys.exit("手元のSarashinaに接続できません。")
    if not gcfg:
        sys.exit("GLMに接続できません（updater/.env の GLM_BASE_URL を確認）。")
    if not ccfg and want & {"E", "F"}:
        print("（SARASHINA_CLOUD_API_KEY / SARASHINA_CLOUD_MODEL がないので、E・F は飛ばします）\n")
        want -= {"E", "F"}

    news = pick(args)
    print(f"話題：【{news['kind']}】{news['title']}\nGLM：{gcfg['model']} / 手元：{cfg['model']}"
          + (f" / API：{ccfg['model']}" if ccfg else "") + "\n")

    t = time.time()
    plan = c.outline(gcfg, news, "（これが最初の会話です）")
    draft = c.glm_draft(gcfg, news, plan)
    t_plan = time.time() - t
    plan_txt = plan["flow"] + "\n" + "\n".join(f"{i + 1}. {x['who']}：{x['point']}" for i, x in enumerate(plan["beats"]))
    print(f"[GLMのあらすじ（下書きまで {t_plan:.0f}秒）]\n{plan_txt}\n")

    def a():
        c.WHY.clear()
        for _ in range(3):
            try:
                _, lines = c.clean(c.write_episode(cfg, news, []))
            except Exception as e:
                c.WHY.append(str(e))
                continue
            if lines:
                return lines, ""
        return [], "捨てた理由：" + " / ".join(c.WHY)

    def d():
        lines, kept = c.restyle(cfg, news, draft)
        return lines, (f"{kept}行は下書きのまま" if kept else "")

    def e():
        lines, usage = cloud_write(ccfg, news, plan)
        return lines, f"トークン：{usage}" if usage else ""

    def f():
        lines, usage = cloud_restyle(ccfg, news, draft)
        return lines, f"トークン：{usage}" if usage else ""

    jobs = [("A", "Sarashina2.2-3B（手元）だけ", a), ("C", "GLMの下書きのまま（参考）", lambda: (draft, "")),
            ("D", "GLMの下書き → Sarashina2.2-3B（手元）が言い直し", d),
            ("E", "Sarashina3.2（API）があらすじから書く", e), ("F", "GLMの下書き → Sarashina3.2（API）が言い直し", f)]
    results = []
    for key, label, fn in jobs:
        if key not in want:
            continue
        r = run(f"{key}：{label}", fn)
        if key in "DEF" and r["lines"]:
            try:
                ok, problems = c.glm_check(gcfg, news, r["lines"])
                r["check"] = "問題なし" if ok else "\n".join("- " + p for p in problems)
            except Exception as ex:
                r["check"] = f"確認に失敗: {ex}"
        results.append(r)
        print(f"=== {r['label']}（{r['sec']:.0f}秒）===\n{show(r['lines'])}")
        if r["note"]:
            print(f"（{r['note']}）")
        if r.get("check"):
            print(f"[GLMの確認] {r['check']}")
        print()

    OUT.mkdir(exist_ok=True)
    path = OUT / (dt.datetime.now(u.JST).strftime("%Y%m%d-%H%M") + ".md")
    body = [f"# 茶話の聞き比べ\n\n話題：【{news['kind']}】{news['title']}\n",
            f"GLM：{gcfg['model']} / 手元：{cfg['model']}" + (f" / API：{ccfg['model']}" if ccfg else ""),
            f"\n## GLMのあらすじ\n\n{plan_txt}\n"]
    for r in results:
        body.append(f"## {r['label']}（{len(r['lines'])}行・{r['sec']:.0f}秒）\n\n{show(r['lines'])}\n")
        if r["note"]:
            body.append(f"（{r['note']}）\n")
        if r.get("check"):
            body.append(f"GLMの確認：{r['check']}\n")
    path.write_text("\n".join(body), encoding="utf-8")
    print("保存しました:", path)


if __name__ == "__main__":
    main()
