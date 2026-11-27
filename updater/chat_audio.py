#!/usr/bin/env python3
"""茶話の音声を sarashina3.2-tts で作り、音声用の別リポジトリに置く。

ふだんは chat.py から呼ばれる。CHAT_AUDIO_ENABLED=1 のときだけ動く（既定は止めておく）。
音声はサイトのリポジトリには入れない。履歴に残り続けて重くなるので、
別リポジトリ（例：kg-b0mbe/sarashina-audio、GitHub Pagesで公開）に
「最近の回の分だけ・履歴を残さない1コミット」で上書きする。

  python3 updater/chat_audio.py --latest     # 最新の回に音声を付けて、音声リポジトリへ送る（サイトは触らない）
  python3 updater/chat_audio.py --check      # 設定の確認だけ

updater/.env:
  CHAT_AUDIO_ENABLED=1
  CHAT_AUDIO_DIR=~/sarashina-audio                        # 音声リポジトリを clone した場所
  CHAT_AUDIO_URL=https://kg-b0mbe.github.io/sarashina-audio
  CHAT_AUDIO_SARA=ja_f_2   CHAT_AUDIO_SHINA=ja_m_1   CHAT_AUDIO_SPEED=1.15
  CHAT_AUDIO_KEEP_DAYS=7
キーは SARASHINA_CLOUD_API_KEY をそのまま使う。
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as u  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CHAT = ROOT / "data" / "chat.json"
MODEL = "sarashina3.2-tts"
LABEL = "Sarashina3.2 TTS"
GAP = 11  # 1分6回の上限に合わせて、1行ごとに空ける秒数

# TTSが受け付けない文字を、読める形に直す
REPL = {"…": "、", "‥": "、", "〜": "ー", "～": "ー", "×": "かける", "°": "度", "℃": "度",
        "￥": "円", "♪": "", "☆": "", "★": "", "※": "", "→": "、", "・": "、"}


def speakable(text):
    t = text
    for a, b in REPL.items():
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKC", t)
    t = "".join(ch for ch in t if unicodedata.category(ch)[0] in "LNPZ" or ch in "ー々〇")
    return re.sub(r"\s+", " ", t).strip()[:700]


def audio_cfg():
    """使える設定がそろっていれば dict、そうでなければ None。"""
    if os.environ.get("CHAT_AUDIO_ENABLED", "0") != "1":
        return None
    key = os.environ.get("SARASHINA_CLOUD_API_KEY", "")
    d = os.environ.get("CHAT_AUDIO_DIR", "")
    url = os.environ.get("CHAT_AUDIO_URL", "").rstrip("/")
    if not key or not d or not url.startswith("https://"):
        print("音声：CHAT_AUDIO_DIR / CHAT_AUDIO_URL / SARASHINA_CLOUD_API_KEY のどれかが足りないので作りません。")
        return None
    path = Path(os.path.expanduser(d))
    if not (path / ".git").exists():
        print(f"音声：{path} が git リポジトリではないので作りません。")
        return None
    return {"api_key": key, "dir": path, "url": url,
            "base_url": os.environ.get("SARASHINA_CLOUD_BASE_URL", "https://api.sarashina.ai/v1").rstrip("/"),
            "voice": {"さら": os.environ.get("CHAT_AUDIO_SARA", "ja_f_2"), "しな": os.environ.get("CHAT_AUDIO_SHINA", "ja_m_1")},
            "speed": float(os.environ.get("CHAT_AUDIO_SPEED", "1.15")),
            "keep_days": int(os.environ.get("CHAT_AUDIO_KEEP_DAYS", "7"))}


def tts(acfg, text, voice):
    body = json.dumps({"model": MODEL, "input": text, "voice": voice,
                       "response_format": "mp3", "speed": acfg["speed"]}).encode()
    req = urllib.request.Request(acfg["base_url"] + "/audio/speech", data=body, headers={
        "Authorization": "Bearer " + acfg["api_key"], "Content-Type": "application/json"})
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 15))
                continue
            print(f"音声：失敗 {e.code} {e.read()[:200].decode('utf-8', 'replace')}")
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            print("音声：つながりません", e)
            return None
    return None


def make(acfg, ep):
    """1回分の音声を作る。0番は番組の頭（しなが今日の話題を読む）、1番からがセリフ。
    途中で1行でも失敗したら、その回は音声なし（ページはブラウザの読み上げに戻る）。"""
    title = ep["news"].get("title_ja") or ep["news"]["title"]
    texts = [("しな", "さらとしなの茶話。今日の話題は、" + title + "。")] + [(l["who"], l["text"]) for l in ep["lines"]]
    out = acfg["dir"] / ep["id"]
    tmp = acfg["dir"] / (ep["id"] + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    files = []
    for i, (who, text) in enumerate(texts):
        if i:
            time.sleep(GAP)
        t = speakable(text)
        audio = tts(acfg, t if len(t) >= 2 else "うん。", acfg["voice"][who])
        if not audio:
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        name = f"{i:02d}.mp3"
        (tmp / name).write_bytes(audio)
        files.append(name)
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return {"base": acfg["url"] + "/" + ep["id"] + "/", "files": files, "by": LABEL}


def strip_old(acfg, episodes, now):
    """音声を残す日数を過ぎた回からは audio を外す（ファイルも publish で消える）。"""
    keep = set()
    for ep in episodes:
        if not ep.get("audio"):
            continue
        try:
            at = dt.datetime.strptime(ep["at"], "%Y-%m-%d %H:%M").date()
        except (KeyError, ValueError):
            at = now.date()
        if (now.date() - at).days >= acfg["keep_days"]:
            ep.pop("audio", None)
        else:
            keep.add(ep["id"])
    return keep


def publish(acfg, keep_ids):
    """残す回の音声だけを、履歴なしの1コミットにして force push する。"""
    d = acfg["dir"]
    for p in d.iterdir():
        if p.is_dir() and not p.name.startswith(".") and p.name not in keep_ids:
            shutil.rmtree(p, ignore_errors=True)
    (d / ".nojekyll").write_text("", encoding="utf-8")
    (d / "README.md").write_text(
        "# Sarashina 帖・茶話の音声\n\nhttps://kg-b0mbe.github.io/sarashina-site/chat.html の読み上げ音声です。"
        "声は " + LABEL + "（SB Intuitions）で生成しています。最近の回の分だけを置いています。\n", encoding="utf-8")

    def g(*a):
        return subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
    branch = (g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main")
    if branch == "HEAD":
        branch = "main"
    g("checkout", "--orphan", "audio-tmp")
    g("add", "-A")
    r = g("commit", "-q", "-m", "茶話の音声（最近の回）")
    if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
        print("音声：commit に失敗", r.stderr[-300:])
        return False
    g("branch", "-D", branch)
    g("branch", "-m", branch)
    r = g("push", "-f", "-q", "origin", branch)
    if r.returncode != 0:
        print("音声：push に失敗", r.stderr[-300:])
        return False
    return True


def attach(ep, episodes, now):
    """chat.py から呼ぶ。ep に audio を付け、古い回の audio を外し、音声リポジトリへ送る。
    うまくいかなくても茶話そのものは止めない。"""
    acfg = audio_cfg()
    if not acfg:
        return
    print("音声を作っています（1行ごとに少し待つので数分かかります）")
    a = make(acfg, ep)
    if a:
        ep["audio"] = a
    keep = strip_old(acfg, episodes, now)
    if a:
        keep.add(ep["id"])
    if not publish(acfg, keep):
        ep.pop("audio", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--latest", action="store_true", help="最新の回に音声を付けて音声リポジトリへ送る")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    u.load_env()
    acfg = audio_cfg()
    if not acfg:
        sys.exit("音声は止まっています（CHAT_AUDIO_ENABLED=1 と設定を確認してください）。")
    if a.check:
        print("音声の設定はそろっています:", acfg["dir"], acfg["url"], acfg["voice"], acfg["speed"])
        return
    if a.latest:
        doc = json.loads(CHAT.read_text(encoding="utf-8"))
        if not doc.get("episodes"):
            sys.exit("茶話の回がありません。")
        now = dt.datetime.now(u.JST)
        ep = doc["episodes"][0]
        attach(ep, doc["episodes"], now)
        print("音声:", "付きました " + ep["audio"]["base"] if ep.get("audio") else "付きませんでした")
        if ep.get("audio"):
            CHAT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("data/chat.json を更新しました。build して commit・push するとページに出ます。")


if __name__ == "__main__":
    main()
