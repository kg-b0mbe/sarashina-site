#!/usr/bin/env python3
"""茶話の声を試す：最新の回の先頭数行を sarashina3.2-tts で読み上げ、mp3にする。

  python3 updater/tts_sample.py            # 最新の回の先頭6行
  python3 updater/tts_sample.py --lines 10 --speed 1.1

出力は ~/sarashina-tts-sample/ （サイトには出さない。聞き比べ用）。
キーは updater/.env の SARASHINA_CLOUD_API_KEY をそのまま使う。
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update as u  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.path.expanduser("~/sarashina-tts-sample"))
VOICES = {"さら": "ja_f_1", "しな": "ja_m_1"}

# TTSが受け付けない文字を読める形に直す（…→、 ♪☆※など→削除）
REPL = {"…": "、", "‥": "、", "〜": "ー", "～": "ー", "×": "かける", "°": "度", "￥": "円", "♪": "", "☆": "", "★": "", "※": ""}


def speakable(text):
    t = text
    for a, b in REPL.items():
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKC", t)
    t = "".join(ch for ch in t if unicodedata.category(ch)[0] in "LNPZ" or ch in "ー々〇")
    return re.sub(r"\s+", " ", t).strip()


def tts(cfg, text, voice, speed):
    body = json.dumps({"model": "sarashina3.2-tts", "input": text, "voice": voice,
                       "response_format": "mp3", "speed": speed}).encode()
    req = urllib.request.Request(cfg["base_url"] + "/audio/speech", data=body, headers={
        "Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"})
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After") or 15)
                print(f"  混んでいるので {wait} 秒待ちます")
                time.sleep(wait)
                continue
            print(f"  失敗 {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=6)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--sara", default=VOICES["さら"], help="ja_f_1 / ja_f_2 / ja_m_1")
    ap.add_argument("--shina", default=VOICES["しな"])
    a = ap.parse_args()

    u.load_env()
    key = os.environ.get("SARASHINA_CLOUD_API_KEY", "")
    if not key:
        sys.exit("updater/.env に SARASHINA_CLOUD_API_KEY がありません。")
    cfg = {"api_key": key, "base_url": os.environ.get("SARASHINA_CLOUD_BASE_URL", "https://api.sarashina.ai/v1").rstrip("/")}

    eps = json.loads((ROOT / "data" / "chat.json").read_text(encoding="utf-8")).get("episodes", [])
    if not eps:
        sys.exit("data/chat.json に回がありません。")
    ep = max(eps, key=lambda e: e.get("at", ""))
    voice = {"さら": a.sara, "しな": a.shina}
    OUT.mkdir(exist_ok=True)
    print("回:", ep.get("at"), (ep.get("news") or {}).get("title", ""))
    for i, l in enumerate(ep.get("lines", [])[:a.lines], 1):
        text = speakable(l.get("text", ""))
        if len(text) < 2:
            continue
        print(f"{i:02d} {l['who']}（{voice[l['who']]}）: {text}")
        audio = tts(cfg, text[:700], voice[l["who"]], a.speed)
        if audio:
            (OUT / f"{i:02d}_{l['who']}.mp3").write_bytes(audio)
        time.sleep(11)  # 1分6回の上限に合わせる
    print("できました:", OUT)


if __name__ == "__main__":
    main()
