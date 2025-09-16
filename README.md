# OKX 合约 AI 交易脚本

> 开箱即用的 Python 脚本：使用 OKX API（python-okx）+ OpenAI 兼容接口，让 AI 决策做多/做空，并按固定轮询管理合约生命周期。

## 功能概述
- 通过环境变量设置 API Key（OKX 与 OpenAI）
- 通过 `config.yaml` 指定交易对与参数（止盈/止损、本金USDT、杠杆、轮询间隔、环境等）
- 使用 AI 分析最近K线数据，决策下一次做多/做空
- 单一持仓约束：同一时间仅持有一个合约，固定间隔检查；持仓结束后再让 AI 决策并开新仓
- 若余额不足配置本金，则使用全部可用余额
- 默认参数：杠杆 100x、止盈 20%、止损 10%、每 30s 查询一次状态
- 合约结束后拉取最近 1 小时数据再次喂给 AI 做出下一步方向
- 支持 OpenAI 兼容的 `base_url`，可接入不同厂商

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置
- 复制示例配置：

```bash
cp config.yaml.example config.yaml
```

- 填写环境变量（可放入 `.env` 或系统环境中）：
  - `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_PASSPHRASE`
  - `OPENAI_API_KEY`
  - 可选：`OPENAI_BASE_URL`（兼容其他厂商）、`OPENAI_MODEL`
  - 可选：`ENV=prod|demo`（默认 demo）

配置文件关键段落：
```yaml
environment: demo
trading:
  poll_interval_sec: 30
  default_leverage: 100
  default_tp_percent: 0.20
  default_sl_percent: 0.10
  base_notional_usdt: 10
  margin_mode: cross
  dry_run: true

instruments:
  - inst_id: BTC-USDT-SWAP
    leverage: 100
    tp_percent: 0.20
    sl_percent: 0.10
    base_notional_usdt: 10

ai:
  provider: openai
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
```

## 运行

> 默认 `dry_run: true`，不会实际下单。需 `--live` 或配置改为 `dry_run: false` 才会实盘下单（请务必谨慎）。

```bash
python -m src.bot --config config.yaml --dry-run
# 或
python -m src.bot --config config.yaml --live
```

## 实现说明
- `src/config.py`：加载 YAML + 环境变量，提供默认值并校验
- `src/okx_client.py`：封装账户余额、K线、设置杠杆、下单、查询持仓等
- `src/ai_client.py`：OpenAI 兼容实现，通过 `/chat/completions` 返回 long/short；支持 `base_url` 自定义
- `src/trader.py`：主交易循环，单一持仓约束、30s 轮询、持仓结束后重新用 AI 决策并开仓；余额不足则使用全部余额
- `src/bot.py`：入口脚本，解析CLI参数、组装依赖并启动循环

## 重要提示与免责声明
- 本仓库仅用于技术研究与演示，不构成任何投资建议。加密资产及衍生品具有高风险，请谨慎评估并自行承担风险。
- 实盘前请在 `demo` 环境充分测试；使用 `--live` 前请反复确认参数。
- 不同合约的合约单位、最小下单量、精度等可能不同，示例实现采用简化估算逻辑，请根据实际产品规则完善尺寸计算。

## 后续提升
- 更精准的合约张数/币量换算与精度适配
- 引入回测与指标（MA/RSI等）作为提示词辅助
- 增加持仓止盈/止损的动态追踪与移动止损
- 完善日志、异常恢复与持久化状态
