"""Trader loop and position management.

该模块实现了基于 AI 决策的合约交易主循环：
- 全局单一持仓约束：任意时刻只允许账户中存在 1 个合约持仓；
- 固定轮询：默认每 30s 检查一次是否仍有持仓；
- 开仓决策：无持仓时，获取最近 60 分钟(1m*60)K 线交给 AI，AI 输出 long/short 后进行市价开仓；
- 止盈止损：下单时根据当前价与配置的百分比设置触发价；
- 干跑模式：不调用真实下单接口，模拟“持仓等待”以避免紧循环。

注意：合约单位、最小下单量、精度在不同 instId 上存在差异。此处的尺寸估算采用简化逻辑（以名义资金/现价换算为“币量整数”），请按实际品种规则细化。
"""

from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Optional
import datetime
from rich.console import Console
from .logger import operations_logger, orders_logger
from .config import AppConfig, InstrumentConfig
from .okx_client import OkxClient, NetworkError
from .ai_client import AIClient


console = Console()


@dataclass
class SizePlan:
    """下单尺寸规划结果。

    attributes:
        contracts: 估算的合约/币量（这里使用简化后的整数数量）；
        notional: 计划投入的名义本金（USDT），尚未乘以杠杆；
    """
    contracts: int
    notional: float


def plan_size_from_notional(
    okx: OkxClient, inst_id: str, notional_usdt: float, leverage: int
) -> SizePlan:
    """根据名义本金与杠杆估算下单数量。

    算法（简化版）：
    - 读取最新成交价 last；
    - 有效头寸规模 = notional_usdt * leverage；
    - 币量 ≈ 有效头寸规模 / last；
    - 取整得到“contracts”。

    警告：不同合约的合约单位/张面值/精度不同，本实现只是演示用途；实际生产请按 instId 的合约细则严谨换算并做精度截断。
    """
    last = float(getattr(okx, "get_last_price")(inst_id)) or 1.0
    effective = notional_usdt * leverage
    coin_qty = effective / last
    # 先向下取整，保证不超额
    contracts = max(1, int(coin_qty))
    # 如果 contracts+1 也不超额，则取更大值
    next_amt = (contracts + 1) * last / leverage
    if next_amt <= notional_usdt:
        contracts += 1
    return SizePlan(contracts=contracts, notional=notional_usdt)


def open_position(
    okx: OkxClient,
    ai: AIClient,
    cfg: AppConfig,
    inst: InstrumentConfig,
    dry_run: bool,
    candles_override=None,
) -> Optional[str]:
    """根据最近 60m K 线由 AI 决策方向并尝试开仓。

    参数：
        okx: 真实或 Dummy 的 OKX 客户端实例；
        ai: AI 客户端（OpenAI 兼容或启发式）；
        cfg: 全局配置；
        inst: 单个标的的个性化配置；
        dry_run: 干跑标志；
        candles_override: 可选，外部传入的 K 线数据（例如持仓刚结束后现拉的 1 小时数据）。

    返回：
        ordId 字符串或 "dry-run-order-id"；如果开仓失败返回 None。
    """

    # 默认读取最近 60m（1m*60）K 线；如果调用方已准备好数据，则使用其覆盖
    candles = candles_override or okx.get_candles(inst.inst_id, bar="1m", limit=60)
    direction = ai.decide_direction(inst.inst_id, candles)
    # 根据账户持仓模式与 margin_mode 决定 posSide：
    # - 若账户是净持仓模式，则强制 posSide=net（OKX 要求），TP/SL 方向由下单 side 推断
    # - 若账户是双向持仓模式，则根据 AI 方向设定 long/short
    acc_pos_mode = getattr(okx, "get_position_mode", lambda: "net")()
    if acc_pos_mode == "net":
        pos_side = "net"
    else:
        pos_side = "long" if direction == "long" else "short"
    side = "buy" if direction == "long" else "sell"

    leverage = inst.leverage or cfg.trading.default_leverage
    tp = inst.tp_percent if inst.tp_percent is not None else cfg.trading.default_tp_percent
    sl = inst.sl_percent if inst.sl_percent is not None else cfg.trading.default_sl_percent
    base_notional = inst.base_notional_usdt or cfg.trading.base_notional_usdt

    # 余额 & 本金：不足本金时使用全部可用余额
    balance = okx.get_usdt_balance()
    open_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    operations_logger.info(f"获取余额: {balance}")
    console.print(f"[调试] 获取到 balance: {balance}")
    if balance <= 0:
        msg = f"余额不足，无法开仓 ({balance} USDT)"
        operations_logger.warning(msg)
        console.print(f"[red]{msg}[/red]")
        return None
    notional = min(balance, base_notional)
    operations_logger.info(f"计算notional: {notional}, base_notional: {base_notional}")
    sz_plan = plan_size_from_notional(okx, inst.inst_id, notional, leverage)
    # 若存在最大可持仓量缓存，则预先钳制 contracts，避免 51004
    max_cap = None
    if hasattr(okx, "get_cached_position_cap"):
        try:
            max_cap = okx.get_cached_position_cap(inst.inst_id, leverage)
        except Exception:
            max_cap = None
    # 应用 fixed_contracts 或 max_contracts（优先级：fixed > instrument.max > global.max > cap）
    # 1) fixed_contracts 直接覆盖
    if getattr(inst, 'fixed_contracts', None):
        sz_plan.contracts = int(getattr(inst, 'fixed_contracts'))
        operations_logger.info(f"固定张数生效: contracts={sz_plan.contracts}")
    else:
        # 2) instrument.max_contracts
        inst_max = getattr(inst, 'max_contracts', None)
        # 3) global max_contracts
        global_max = getattr(cfg.trading, 'max_contracts', None)
        # 从候选中取最严格上限
        candidates = [c for c in [inst_max, global_max, max_cap] if c]
        if candidates:
            hard_cap = int(min(candidates))
            if sz_plan.contracts > hard_cap:
                operations_logger.info(f"触发上限钳制: 原 contracts={sz_plan.contracts}, cap={hard_cap} (inst_max={inst_max}, global_max={global_max}, pos_cap={max_cap})")
                sz_plan.contracts = hard_cap
    operations_logger.info(f"下单计划: contracts={sz_plan.contracts}, notional={sz_plan.notional}")
    console.print(f"[调试] sz_plan: contracts={sz_plan.contracts}, notional={sz_plan.notional}")
    console.print(f"[调试] candles(前2): {candles[:2]} ... 共{len(candles)}条")
    console.print(f"[调试] direction: {direction}, pos_side: {pos_side}, side: {side}, acc_pos_mode: {acc_pos_mode}")
    console.print(f"[调试] leverage: {leverage}, tp: {tp}, sl: {sl}")

    msg = (
        f"准备开仓 {inst.inst_id} 方向={pos_side} 杠杆={leverage} 本金={notional}USDT 计划合约数={sz_plan.contracts} TP={tp*100:.1f}% SL={sl*100:.1f}%"
    )
    operations_logger.info(msg)
    console.print(msg)

    # 干跑：不实际下单，仅打印；真实：设置杠杆并市价单下单（带 TP/SL 触发参数）

    if dry_run:
        msg = f"DRY-RUN: 不实际下单，仅打印参数: {inst.inst_id}, direction={direction}, contracts={sz_plan.contracts}, notional={sz_plan.notional}"
        operations_logger.info(msg)
        console.print("[yellow]DRY-RUN: 不实际下单，仅打印参数。[/yellow]")
        return "dry-run-order-id"


    okx.set_leverage(inst.inst_id, leverage, cfg.trading.margin_mode, pos_side=pos_side)
    r = okx.place_order(
        inst_id=inst.inst_id,
        side=side,
        td_mode=cfg.trading.margin_mode,
        sz=str(sz_plan.contracts),
        ord_type="market",
        pos_side=pos_side,
        tp_ratio=tp,
        sl_ratio=sl,
        tp_trigger_type=getattr(cfg.trading, "tp_trigger_type", "last"),
        sl_trigger_type=getattr(cfg.trading, "sl_trigger_type", "last"),
        lever=leverage,
    )
    # 标准化返回判定：只有 code=="0" 且 data[0].ordId 存在才算成功
    ord_id = None
    msg = f"下单返回: {r}"
    operations_logger.info(msg)
    console.print(msg)
    if isinstance(r, dict) and str(r.get("code")) == "0":
        data = r.get("data")
        if isinstance(data, list) and data:
            ord_id = data[0].get("ordId") or None
    if not ord_id:
        # 失败则返回 None
        return None
    return ord_id



def poll_until_no_positions(okx: OkxClient, poll_sec: int) -> None:
    """阻塞等待直到账户层面无任何持仓（全局单仓约束）。"""
    while True:
        positions = okx.get_positions()
        any_open = any(abs(float(p.get("pos", 0))) > 0 for p in positions)
        if not any_open:
            return
        time.sleep(poll_sec)

def log_close_order(inst, direction, contracts, open_balance, ord_id, okx, orders_logger):
    close_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    close_balance = okx.get_usdt_balance()
    leverage = getattr(inst, 'leverage', 1) if inst else 1
    # 投入资金
    invested = getattr(inst, 'base_notional_usdt', open_balance) if inst else open_balance
    profit = close_balance - open_balance
    # 盈亏率直接用OKX接口返回的实现收益率
    realized_pnl_ratio = '-'
    if inst and hasattr(okx, 'get_position_summary'):
        pos_summary = okx.get_position_summary(getattr(inst, 'inst_id', ''))
        if pos_summary:
            realized_pnl_ratio = pos_summary.get('realizedPnlRatio', '-')
            if realized_pnl_ratio not in ('-', None):
                try:
                    realized_pnl_ratio = f"{float(realized_pnl_ratio)*100:.2f}%"
                except Exception:
                    realized_pnl_ratio = '-'
    # 只在文件为空时写表头
    import os
    log_path = getattr(orders_logger.handlers[0], 'baseFilename', None)
    need_header = False
    if log_path and (not os.path.exists(log_path) or os.path.getsize(log_path) == 0):
        need_header = True
    if need_header:
        orders_logger.info("| 时间 | 类型 | 标的 | 开仓余额 | 平仓余额 | 盈亏 | 盈亏率 | 订单ID |")
        orders_logger.info("|------|------|------|----------|----------|------|--------|--------|")
    orders_logger.info(f"| {close_time} | 平仓 | {getattr(inst, 'inst_id', '-') if inst else '-'} | {open_balance if open_balance is not None else '-':.2f} | {close_balance:.2f} | {profit:.2f} | {realized_pnl_ratio} | {ord_id} |")


def trade_loop(okx: OkxClient, ai: AIClient, cfg: AppConfig) -> None:
    """主交易循环。

    逻辑：
    - 若账户存在任意持仓，则仅等待下一轮轮询；
    - 若无持仓，则按 instruments 顺序轮转一个标的，拉取最近 60 根 1m K 线交给 AI 决策方向并尝试开仓；
    - 真实模式：下单后每 poll_sec 秒检查是否已无持仓；
    - 干跑模式：用一次 sleep 模拟“持仓中”，避免忙等；
    - 合约结束后，继续下一轮。
    """
    poll_sec = cfg.trading.poll_interval_sec
    if not cfg.instruments:
        msg = "配置中缺少 instruments"
        operations_logger.error(msg)
        console.print(f"[red]{msg}[/red]")
        return
    idx = 0
    console.rule("启动 AI 合约交易循环（全局仅持有一个合约）")
    operations_logger.info("启动 AI 合约交易循环（全局仅持有一个合约）")
    while True:
        try:
            dry_run = cfg.trading.dry_run
            poll_sec_local = poll_sec
            # 全局单一持仓：若任意合约持仓中，则仅轮询（不再开新仓）
            positions = okx.get_positions()
            any_open = any(abs(float(p.get("pos", 0))) > 0 for p in positions)
            if any_open:
                msg = f"已有持仓，{poll_sec_local}s 后再次检查"
                console.print(msg)
                time.sleep(poll_sec_local)
                continue

            # 无持仓：轮到下一个标的（轮转）
            inst = cfg.instruments[idx % len(cfg.instruments)]
            idx += 1
            # 获取最近 1 小时(60根1m)数据传给 AI 进行方向判定
            candles_1h = okx.get_candles(inst.inst_id, bar="1m", limit=60)
            operations_logger.info(f"尝试开仓: inst_id={inst.inst_id}")
            # 记录开仓前余额
            open_balance = okx.get_usdt_balance()
            ord_id = open_position(okx, ai, cfg, inst, dry_run, candles_override=candles_1h)
            if ord_id is None:
                msg = "开仓失败，稍后重试"
                operations_logger.warning(msg)
                console.print(msg)
                time.sleep(poll_sec_local)
                continue

            # 下单后等待到全局无持仓（或在干跑中用 sleep 模拟）
            if dry_run:
                msg = f"干跑：模拟持仓中，等待{poll_sec_local}s 再继续"
                operations_logger.info(msg)
                console.print(msg)
                time.sleep(poll_sec_local)
                # DRY-RUN平仓记录
                log_close_order(inst, '-', '-', open_balance, ord_id, okx, orders_logger)
            else:
                msg = f"已开仓 {inst.inst_id}，开始轮询账户持仓状态（每{poll_sec_local}s）"
                operations_logger.info(msg)
                console.print(msg)
                poll_until_no_positions(okx, poll_sec_local)
                # 平仓后记录
                log_close_order(inst, '-', '-', open_balance, ord_id, okx, orders_logger)
        except NetworkError as e:
            msg = f"网络异常：{e}，本轮跳过，{poll_sec}s 后重试"
            operations_logger.error(msg)
            console.print(f"[red]{msg}[/red]")
            time.sleep(poll_sec)
            continue
