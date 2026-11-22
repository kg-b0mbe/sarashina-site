#!/usr/bin/env python3
"""SB IntuitionsのモデルのうちGGUF版がまだないものを洗い出す（Mac miniで実行。サイトには何も書き込まない）。

Hugging Faceにある sbintuitions のモデルを1つずつ見て、
  - GGUF版がすでにあるか（公式・有志。モデルの系図 base_model と、名前での検索の両方で探す）
  - llama.cppで変換できそうか（config.json の architectures から目安を出す）
  - 配り直してよいライセンスか（モデルカードの license）
を一覧にする。結果は画面に出し、~/sarashina-gguf-gap.md にも保存する。

  python3 tools/gguf_gap.py
  python3 tools/gguf_gap.py --all   # GGUFがあるものも含めて全部表示

判定はあくまで目安。変換や公開の前に、モデルカードのライセンスと注意書きを必ず自分で読むこと。
"""
import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://huggingface.co/api/models"
UA = "sarashina-site-tools/1.0"
OUT = Path.home() / "sarashina-gguf-gap.md"

# llama.cppの変換スクリプトがふつうに扱える形
EASY = {
    "LlamaForCausalLM", "MistralForCausalLM", "Qwen2ForCausalLM", "Qwen3ForCausalLM", "Qwen3MoeForCausalLM",
    "MixtralForCausalLM", "GemmaForCausalLM", "Gemma2ForCausalLM", "Gemma3ForCausalLM", "Phi3ForCausalLM",
    "GPTNeoXForCausalLM", "StableLmForCausalLM", "NemotronForCausalLM",
}
NOTES = {
    "WhisperForConditionalGeneration": "音声認識。llama.cppではなく whisper.cpp 用の変換になる",
    "ModernBertModel": "埋め込み・分類の部品。llama.cppの対応状況を要確認",
    "ModernBertForMaskedLM": "埋め込み・分類の部品。llama.cppの対応状況を要確認",
    "BertModel": "埋め込みの部品。llama.cppで変換できることが多い（要確認）",
}
OK_LICENSES = {"mit", "apache-2.0", "cc-by-4.0", "cc-by-sa-4.0"}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def list_models():
    base = f"{API}?author=sbintuitions&limit=1000"
    try:
        return get_json(base + "&expand[]=downloads&expand[]=pipeline_tag&expand[]=config&expand[]=cardData"
                        "&expand[]=gated&expand[]=tags&expand[]=library_name")
    except Exception:
        # まとめて取れなければ、1つずつ詳しく見る
        items = get_json(base)
        out = []
        for it in items:
            try:
                out.append(get_json(f"{API}/{it['id']}"))
            except Exception:
                out.append(it)
            time.sleep(0.3)
        return out


def stem(mid):
    """「mmnga/sarashina2.2-3b-instruct-v0.1-gguf」→「sarashina2.2-3b-instruct-v0.1」のように、GGUFの飾りを外した名前。"""
    s = mid.split("/")[-1].lower()
    s = re.sub(r"[-_.]?(i?q\d\w*|f16|bf16|imatrix|gguf)$", "", s)
    s = re.sub(r"[-_.]?(i?q\d\w*|f16|bf16|imatrix|gguf)$", "", s)
    return s.strip("-_.")


def gguf_versions(rid):
    """このモデルを元にしたGGUF版（自分以外のアカウントも含む）を探す。"""
    found = {}
    name = rid.split("/")[1]
    queries = [
        f"{API}?filter={urllib.parse.quote('base_model:quantized:' + rid)}&limit=100&expand[]=downloads&expand[]=tags",
        f"{API}?search={urllib.parse.quote(name)}&filter=gguf&limit=100&expand[]=downloads&expand[]=tags",
    ]
    for q in queries:
        try:
            items = get_json(q)
        except Exception:
            continue
        for it in items:
            mid = it.get("id") or it.get("modelId")
            tags = it.get("tags") or []
            if mid == rid:
                continue
            # 名前検索は似た名前を拾うので、名前がそのまま入っていて、GGUFのものだけ残す
            if "gguf" not in tags and "gguf" not in mid.lower():
                continue
            if "base_model:quantized:" + rid not in tags and stem(mid) != stem(rid):
                continue
            found[mid] = int(it.get("downloads") or 0)
        time.sleep(0.3)
    return found


def convert_note(archs):
    if not archs:
        return "要確認（config.jsonなし）"
    for a in archs:
        if a in EASY:
            return "変換できそう"
        if a in NOTES:
            return NOTES[a]
        if "Vision" in a or "VL" in a or "Llava" in a:
            return "画像を扱う形。画像部分（mmproj）の対応を要確認"
    return "要確認（" + ", ".join(archs) + "）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    rows = []
    models = list_models()
    print(f"sbintuitions のモデル {len(models)} 個を確認します（少し時間がかかります）…")
    for it in models:
        rid = it.get("id") or it.get("modelId")
        if rid.lower().endswith("gguf"):
            continue  # 公式のGGUF版そのもの
        card = it.get("cardData") or {}
        lic = str(card.get("license") or "").lower() or "不明"
        archs = ((it.get("config") or {}).get("architectures")) or []
        ggufs = gguf_versions(rid)
        official = [m for m in ggufs if m.startswith("sbintuitions/")]
        rows.append({
            "id": rid, "downloads": int(it.get("downloads") or 0), "task": it.get("pipeline_tag") or "",
            "license": lic, "gated": bool(it.get("gated")), "convert": convert_note(archs),
            "ggufs": ggufs, "official": official,
        })

    def score(r):  # 先に見たい順：GGUFがない → 変換できそう → ライセンスOK → ダウンロードが多い
        return (bool(r["ggufs"]), r["convert"] != "変換できそう", r["license"] not in OK_LICENSES, -r["downloads"])

    rows.sort(key=score)
    lines = ["# SarashinaのGGUF版の有無", "",
             "| モデル | 30日DL | 用途 | ライセンス | 変換 | GGUF版 |", "|---|---:|---|---|---|---|"]
    for r in rows:
        if r["ggufs"] and not args.all:
            continue
        if r["ggufs"]:
            top = sorted(r["ggufs"].items(), key=lambda kv: -kv[1])[:3]
            have = ("公式あり・" if r["official"] else "") + "、".join(f"{m}（{d:,}）" for m, d in top)
        else:
            have = "**まだない**"
        lic = r["license"] + ("（利用申請が必要）" if r["gated"] else "")
        if r["license"] not in OK_LICENSES:
            lic += "：配り直しの条件を確認"
        lines.append(f"| [{r['id'].split('/')[1]}](https://huggingface.co/{r['id']}) | {r['downloads']:,} | {r['task']} | "
                     f"{lic} | {r['convert']} | {have} |")
    have_n = sum(1 for r in rows if r["ggufs"])
    lines += ["", f"GGUF版あり：{have_n}個 ／ まだない：{len(rows) - have_n}個（公式のGGUFリポジトリは数に入れていない）",
              "", "判定は目安です。変換・公開の前に、モデルカードのライセンスと注意書きを必ず読んでください。"]
    text = "\n".join(lines) + "\n"
    print(text)
    OUT.write_text(text, encoding="utf-8")
    print("保存しました:", OUT)


if __name__ == "__main__":
    main()
