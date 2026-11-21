#!/usr/bin/env bash
# DGX Sparkで実行：Sarashina2.2-3B-instruct（GGUF・Q8_0）を llama.cpp の llama-server で起動する。
# OpenAI互換API（/v1/models, /v1/chat/completions）を http://<このSpark>:8010/v1 で出す。
#
#   ./serve-sarashina.sh start    # 起動
#   ./serve-sarashina.sh status   # 状態確認（メモリの空きも表示）
#   ./serve-sarashina.sh stop     # 停止
#   ./serve-sarashina.sh logs     # ログ
#
# 常駐しているGLMと同じSparkに同居させるため、vLLMではなくllama.cppで、量子化した軽い版を使う。
# 使うメモリは5GBほど（重み3.6GB＋文脈8Kぶん＋CUDAの作業領域）。
# 止まっても自動で再起動はしない（原因を確かめてから start する運用）。
set -euo pipefail

PORT="${PORT:-8010}"
SERVED_NAME="sarashina2.2-3b"
MODEL="${MODEL:-$HOME/models/sarashina/sarashina2.2-3b-instruct-v0.1-Q8_0.gguf}"
LLAMA_SERVER="${LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}"
KEY_FILE="$HOME/.sarashina_api_key"
LOG="$HOME/.sarashina-server.log"
PIDFILE="$HOME/.sarashina-server.pid"
MIN_FREE_GB="${MIN_FREE_GB:-9}"   # これより空きが少なければ起動しない（GLMを守る）

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
avail_gb() { free -g | awk '/^Mem:/ {print $7}'; }

case "${1:-}" in
  start)
    if running; then echo "もう動いています（PID $(cat "$PIDFILE")）"; exit 0; fi
    [ -x "$LLAMA_SERVER" ] || { echo "llama-server がありません: $LLAMA_SERVER"; exit 1; }
    [ -f "$MODEL" ] || { echo "モデルがありません: $MODEL"; exit 1; }
    if [ "$(avail_gb)" -lt "$MIN_FREE_GB" ]; then
      echo "空きメモリが $(avail_gb)GB しかないので起動しません（${MIN_FREE_GB}GB以上必要）。free -h で確認してください"
      exit 1
    fi
    if [ ! -f "$KEY_FILE" ]; then
      openssl rand -hex 24 > "$KEY_FILE"
      chmod 600 "$KEY_FILE"
      echo "APIキーを作成しました: $KEY_FILE （Mac miniの updater/.env に同じ値を書く）"
    fi
    nohup "$LLAMA_SERVER" \
      --model "$MODEL" \
      --alias "$SERVED_NAME" \
      --host 0.0.0.0 --port "$PORT" \
      --api-key-file "$KEY_FILE" \
      --ctx-size 8192 \
      --parallel 1 \
      --n-gpu-layers 99 \
      --jinja \
      > "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "起動しました（PID $!）。数秒後に ./serve-sarashina.sh status で確認してください"
    ;;
  status)
    if running; then echo "プロセス: 動いています（PID $(cat "$PIDFILE")）"; else echo "プロセス: 止まっています"; fi
    echo "空きメモリ: $(avail_gb)GB"
    if curl -sf -H "Authorization: Bearer $(cat "$KEY_FILE" 2>/dev/null)" "http://localhost:${PORT}/v1/models" >/dev/null; then
      echo "API: OK  http://$(hostname):${PORT}/v1 （モデル名: ${SERVED_NAME}）"
    else
      echo "API: 応答なし（読み込み中なら少し待つ。だめなら ./serve-sarashina.sh logs）"
      exit 1
    fi
    ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE" && echo "止めました"; else echo "動いていません"; rm -f "$PIDFILE"; fi
    ;;
  logs) tail -n 80 -f "$LOG" ;;
  *) echo "使い方: $0 {start|status|stop|logs}"; exit 1 ;;
esac
