#!/bin/zsh
# 启动 GamblingOnDogs 机器人（默认 live 模式，自动检测 venv）
if [ -d ".venv" ] && [ -x ".venv/bin/python" ]; then
	PYTHON=".venv/bin/python"
else
	PYTHON="python3"
fi
$PYTHON -m src.bot --live
