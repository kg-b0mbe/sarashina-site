#!/usr/bin/env bash
# DGX Sparkで実行：Sarashina2.2-3B-instruct を OpenAI互換API（vLLM）で起動する。
#
#   ./serve-sarashina.sh start    # 起動
#   ./serve-sarashina.sh status   # 状態確認
#   ./serve-sarashina.sh stop     # 停止
#   ./serve-sarashina.sh logs     # ログ
#
# メインの大きいモデルと同居させるため、メモリは全体の約1割だけ使う設定にしている。
# 止まっても自動で再起動はしない（原因を確かめてから start する運用）。
set -euo pipefail

NAME=sarashina
PORT="${PORT:-8010}"
MODEL_REPO="sbintuitions/sarashina2.2-3b-instruct-v0.1"
SERVED_NAME="sarashina2.2-3b"
KEY_FILE="${HOME}/.sarashina_api_key"
# 手元で使っているvLLMのイメージを指定（docker images | grep vllm で確認）
VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:latest}"

case "${1:-}" in
  start)
    if [ ! -f "$KEY_FILE" ]; then
      openssl rand -hex 24 > "$KEY_FILE"
      chmod 600 "$KEY_FILE"
      echo "APIキーを作成しました: $KEY_FILE （Mac miniの updater/.env に同じ値を書く）"
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --gpus all --ipc=host \
      -p "${PORT}:8000" \
      -v "${HOME}/.cache/huggingface:/root/.cache/huggingface" \
      "$VLLM_IMAGE" \
      vllm serve "$MODEL_REPO" \
        --served-model-name "$SERVED_NAME" \
        --api-key "$(cat "$KEY_FILE")" \
        --dtype bfloat16 \
        --max-model-len 8192 \
        --max-num-seqs 4 \
        --gpu-memory-utilization 0.10
    echo "起動中… 初回はモデルのダウンロードに数分かかります。 ./serve-sarashina.sh logs で確認"
    ;;
  status)
    if curl -sf -H "Authorization: Bearer $(cat "$KEY_FILE")" "http://localhost:${PORT}/v1/models" >/dev/null; then
      echo "OK: http://$(hostname):${PORT}/v1 で応答しています（モデル名: ${SERVED_NAME}）"
    else
      echo "応答なし。 docker ps -a と ./serve-sarashina.sh logs を確認してください"
      exit 1
    fi
    ;;
  stop) docker rm -f "$NAME" ;;
  logs) docker logs --tail 80 -f "$NAME" ;;
  *) echo "使い方: $0 {start|status|stop|logs}"; exit 1 ;;
esac
