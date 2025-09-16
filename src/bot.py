"""CLI 入口脚本

职责：
- 加载 .env 与 YAML/环境变量配置；
- 基于配置构建 OKX 与 AI 客户端；
- 启动主交易循环。
"""
from __future__ import annotations
import argparse
import os
from rich.console import Console
from dotenv import load_dotenv

from .config import load_config
from .okx_client import OkxClient, DummyOkxClient
from .ai_client import OpenAICompatClient, HeuristicAIClient

from .trader import trade_loop
from .logger import operations_logger


console = Console()


def build_ai_client(cfg):
    """构建 AI 客户端：优先使用 OpenAI 兼容客户端，无 Key 则退回启发式。"""
    api_key = cfg.ai.api_key or os.getenv("OPENAI_API_KEY")
    base_url = cfg.ai.base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = cfg.ai.model
    if not api_key:
        console.print("[yellow]缺少 OPENAI_API_KEY，使用启发式AI进行干跑。[/yellow]")
        return HeuristicAIClient()
    return OpenAICompatClient(api_key=api_key, base_url=base_url, model=model)


def build_okx_client(cfg):
    """构建 OKX 客户端：没有凭证时返回 Dummy 客户端以便干跑。"""
    okx_cfg = getattr(cfg, "okx", None) or {}
    api_key = okx_cfg.get("api_key") or os.getenv("OKX_API_KEY")
    api_secret = okx_cfg.get("api_secret") or os.getenv("OKX_API_SECRET")
    passphrase = okx_cfg.get("passphrase") or os.getenv("OKX_PASSPHRASE")
    demo = cfg.environment != "prod"
    if not (api_key and api_secret and passphrase):
        console.print("[yellow]缺少 OKX API 凭证，使用 DummyOkxClient 进行干跑。[/yellow]")
        return DummyOkxClient()
    return OkxClient(api_key, api_secret, passphrase, demo=demo)


def main():
    """解析参数、加载配置并启动交易循环。"""
    # Load .env early
    load_dotenv()
    parser = argparse.ArgumentParser(description="OKX 合约 AI 交易脚本")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径，默认 ./config.yaml")
    parser.add_argument("--live", action="store_true", help="覆盖配置，实时下单（谨慎）")
    parser.add_argument("--dry-run", action="store_true", help="覆盖配置，干跑模式")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.live:
        cfg.trading.dry_run = False
    if args.dry_run:
        cfg.trading.dry_run = True
    operations_logger.info(f"启动bot，参数: live={args.live}, dry_run={args.dry_run}, config={args.config}")

    console.print(f"环境: {cfg.environment}  干跑: {cfg.trading.dry_run}")

    okx = build_okx_client(cfg)
    ai = build_ai_client(cfg)

    try:
        trade_loop(okx, ai, cfg)
    except KeyboardInterrupt:
        console.print("[yellow]检测到用户中断，程序已退出。[/yellow]")
        return


if __name__ == "__main__":
    main()
