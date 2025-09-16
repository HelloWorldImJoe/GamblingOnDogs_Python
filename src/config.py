from __future__ import annotations

"""配置加载与校验

来源：
- YAML 文件（默认 ./config.yaml，可通过 --config 或环境变量 CONFIG_PATH 指定）；
- 环境变量（优先级更高，覆盖 YAML）。

校验：
- 使用 pydantic 进行字段校验，提供默认值（如 100x 杠杆、TP 20%、SL 10%、30s 轮询等）。
"""

import os
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError


class InstrumentConfig(BaseModel):
    """单个交易标的的个性化配置。"""
    inst_id: str
    leverage: Optional[int] = None
    tp_percent: Optional[float] = None
    sl_percent: Optional[float] = None
    base_notional_usdt: Optional[float] = None
    # 合约数控制：fixed_contracts 固定张数；max_contracts 为该标的上限
    fixed_contracts: Optional[int] = None
    max_contracts: Optional[int] = None


class TradingConfig(BaseModel):
    """交易相关全局配置与默认值。"""
    poll_interval_sec: int = 30
    default_leverage: int = 100
    default_tp_percent: float = 0.02
    default_sl_percent: float = 0.01
    base_notional_usdt: float = 10
    margin_mode: str = Field(default="cross", pattern=r"^(cross|isolated)$")
    dry_run: bool = True
    # 全局合约数上限（若 instrument 未单独设置）
    max_contracts: Optional[int] = None


class AIConfig(BaseModel):
    """AI 模块配置（支持 OpenAI 兼容）。"""
    provider: str = "openai"
    base_url: Optional[str] = None
    model: str = "gpt-4o-mini"
    api_key: Optional[str] = None  # prefer env OPENAI_API_KEY


class LogConfig(BaseModel):
    """日志配置。"""
    level: str = "INFO"


class AppConfig(BaseModel):
    """应用顶层配置对象。"""
    environment: str = Field(default="demo", pattern=r"^(demo|prod)$")
    trading: TradingConfig = TradingConfig()
    instruments: List[InstrumentConfig] = Field(default_factory=list)
    ai: AIConfig = AIConfig()
    log: LogConfig = LogConfig()


def load_yaml_config(path: str) -> dict:
    """从 YAML 读取配置，文件不存在则返回空 dict。"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def merge_env(config: dict) -> dict:
    """将环境变量合并到配置，覆盖同名字段。

    支持的环境变量：
    - OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE
    - OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
    - ENV（demo|prod）、LOG_LEVEL
    """
    # OKX creds
    okx_api_key = os.getenv("OKX_API_KEY")
    okx_api_secret = os.getenv("OKX_API_SECRET")
    okx_passphrase = os.getenv("OKX_PASSPHRASE")
    config.setdefault("okx", {})
    if okx_api_key:
        config["okx"]["api_key"] = okx_api_key
    if okx_api_secret:
        config["okx"]["api_secret"] = okx_api_secret
    if okx_passphrase:
        config["okx"]["passphrase"] = okx_passphrase

    # AI env
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    model = os.getenv("OPENAI_MODEL")
    config.setdefault("ai", {})
    if api_key:
        config["ai"]["api_key"] = api_key
    if base_url:
        config["ai"]["base_url"] = base_url
    if model:
        config["ai"]["model"] = model

    # ENV runtime
    env = os.getenv("ENV")
    if env:
        config["environment"] = env

    # Log level
    log_level = os.getenv("LOG_LEVEL")
    if log_level:
        config.setdefault("log", {})["level"] = log_level

    return config


def load_config(config_path: str | None = None) -> AppConfig:
    """加载并校验最终配置。

    顺序：YAML -> 环境变量覆盖 -> pydantic 校验。
    """
    path = config_path or os.getenv("CONFIG_PATH") or "config.yaml"
    raw = load_yaml_config(path)
    merged = merge_env(raw)
    try:
        app = AppConfig(**merged)
    except ValidationError as e:
        raise RuntimeError(f"配置文件校验失败: {e}")
    return app
