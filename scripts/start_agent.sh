#!/usr/bin/env bash
# Travel Agent local launcher (macOS / Linux)
# --- LLM (DeepSeek, OpenAI-compatible) ---
# 注意：若在管理后台(8788)「大语言模型设置」里保存过配置，以后台设置为准，此处只是兜底
# 安全提示：不要把真实 API Key 提交进仓库，填你自己的 Key 或改用管理后台保存。
export OPENAI_API_KEY="sk-在这里填你自己的KEY"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="deepseek-chat"
export OPENAI_TIMEOUT="60"

# src 布局：让 python -m 找到 travel_agent 包；运行时数据统一落在项目根 data/
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
python3 -m travel_agent.server --open
