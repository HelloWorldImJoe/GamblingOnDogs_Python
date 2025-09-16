"""OKX 客户端封装

提供对 OKX 账户、行情、交易等接口的薄封装，统一给策略侧使用：
- OkxClient：真实客户端，依赖 python-okx；
- DummyOkxClient：干跑用的假客户端，不发网络请求，返回合成数据。

公开方法的“契约”尽量简洁：get_usdt_balance/get_candles/get_last_price/get_positions/
set_leverage/place_order/cancel_all_open_orders/close_position_market/get_position_summary
这样策略代码无需直接访问 sdk 内部对象，替换实现更方便。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


import os
import yaml
from okx import Account, MarketData, PublicData, Trade, Funding
import random

# 优先从 config.yaml 读取 DEBUG_OKX_CLIENT，否则回退到环境变量
def _get_debug_flag():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        val = config.get("DEBUG_OKX_CLIENT", None)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("1", "true", "yes", "on")
    except Exception:
        pass
    return os.getenv("DEBUG_OKX_CLIENT", "False").lower() in ("1", "true", "yes", "on")

DEBUG_OKX_CLIENT = _get_debug_flag()

class NetworkError(Exception):
    """自定义网络异常，表示与 OKX 通信失败。"""
    pass

class OkxClient:
    _cap_cache: dict
    def get_position_mode(self) -> str:
        """获取账户持仓模式。

        返回值：
        - "net": 单向持仓（净持仓模式）
        - "long_short": 双向持仓（多空分开）
        - 获取失败时默认返回 "net"
        """
        try:
            if DEBUG_OKX_CLIENT:
                print("[调试] 调用 get_position_mode() -> account.get_account_config()")
            r = self.account.get_account_config()
            # 典型返回: { data: [ { posMode: 'net_mode' | 'long_short_mode', ... } ] }
            data = (r or {}).get("data", [])
            if data:
                pos_mode = data[0].get("posMode")
                if DEBUG_OKX_CLIENT:
                    print(f"[调试] get_position_mode 返回 posMode={pos_mode}")
                if pos_mode in ("net_mode", "net"):
                    return "net"
                if pos_mode in ("long_short_mode", "long_short"):
                    return "long_short"
            return "net"
        except Exception as e:
            if DEBUG_OKX_CLIENT:
                print(f"[get_position_mode] 网络/解析异常: {e}")
            return "net"
    def cancel_all_algo_orders(self, inst_id: str) -> None:
        """取消该合约下所有计划委托单（止盈止损等）。"""
        try:
            # 查询所有未触发的计划单
            algos = self.trade.get_algo_list(instType="SWAP", instId=inst_id).get("data", [])
            algo_ids = [a["algoId"] for a in algos if a.get("state") == "live"]
            if not algo_ids:
                return
            self.trade.cancel_algos(instId=inst_id, algoIds=algo_ids)
            if DEBUG_OKX_CLIENT:
                print(f"[调试] 已取消计划单: {algo_ids}")
        except Exception as e:
            if DEBUG_OKX_CLIENT:
                print(f"[cancel_all_algo_orders] 网络请求异常: {e}")
    def __init__(self, api_key: str, api_secret: str, passphrase: str, demo: bool = True):
        if DEBUG_OKX_CLIENT:
            print(f"[调试] 初始化 OkxClient: api_key={api_key[:4]}***, demo={demo}")
        # demo True = 交易模拟盘；OKX SDK 的 flag "1" 是 demo；"0" 是实盘
        self.flag = "1" if demo else "0"
        domain = "https://www.okx.me" if not demo else "https://www.okx.com"
        self.account = Account.AccountAPI(api_key, api_secret, passphrase, False, self.flag, domain=domain)
        self.market = MarketData.MarketAPI(api_key, api_secret, passphrase, False, self.flag, domain=domain)
        self.trade = Trade.TradeAPI(api_key, api_secret, passphrase, False, self.flag, domain=domain)
        self.public = PublicData.PublicAPI(api_key, api_secret, passphrase, False, self.flag, domain=domain)
        self.funding = Funding.FundingAPI(api_key, api_secret, passphrase, False, self.flag, domain=domain)
        # (inst_id, lever) -> max contracts
        self._cap_cache = {}

    def get_cached_position_cap(self, inst_id: str, lever: Optional[int]) -> Optional[int]:
        try:
            return self._cap_cache.get((inst_id, int(lever))) if lever is not None else None
        except Exception:
            return None

    def update_position_cap(self, inst_id: str, lever: Optional[int], cap: Optional[int]) -> None:
        if inst_id and lever and cap:
            try:
                self._cap_cache[(inst_id, int(lever))] = int(cap)
                if DEBUG_OKX_CLIENT:
                    print(f"[调试] 缓存 {inst_id} 在 {lever}x 的最大可持仓量: {cap}")
            except Exception:
                pass

    # --- Account helpers ---
    def get_usdt_balance(self) -> float:
        if DEBUG_OKX_CLIENT:
            print("[调试] 调用 get_usdt_balance()")
        # 账户余额（交易账户）。注意 OKX 有资金账户与交易账户，此处取交易账户可用余额
        try:
            if DEBUG_OKX_CLIENT:
                print("[调试] 请求 OKX: account.get_account_balance()")
            r = self.account.get_account_balance()
            if DEBUG_OKX_CLIENT:
                print(f"[调试] OKX 返回: {r}")
            details = r.get("data", [{}])[0].get("details", [])
            for d in details:
                if d.get("ccy") == "USDT":
                    # availBal 为可用余额
                    return float(d.get("availBal", 0))
            return 0.0
        except KeyboardInterrupt:
            raise
        except Exception as e:
            raise NetworkError(f"[get_usdt_balance] 网络请求异常: {e}")

    # --- Market data ---
    def get_candles(self, inst_id: str, bar: str = "1m", limit: int = 60) -> List[List[str]]:
        if DEBUG_OKX_CLIENT:
            print(f"[调试] 调用 get_candles(inst_id={inst_id}, bar={bar}, limit={limit})")
        # 返回 [[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm, ...]]
        try:
            if DEBUG_OKX_CLIENT:
                print(f"[调试] 请求 OKX: market.get_history_candlesticks(instId={inst_id}, bar={bar}, limit={limit})")
            r = self.market.get_history_candlesticks(instId=inst_id, bar=bar, limit=str(limit))
            if DEBUG_OKX_CLIENT:
                print(f"[调试] OKX 返回: {str(r)[:300]}..." if r else "[调试] OKX 返回: None")
            return r.get("data", [])
        except KeyboardInterrupt:
            raise
        except Exception as e:
            raise NetworkError(f"[get_candles] 网络请求异常: {e}")

    # --- Positions & orders ---
    def get_positions(self, inst_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if DEBUG_OKX_CLIENT:
            print(f"[调试] 调用 get_positions(inst_id={inst_id})")
        try:
            if DEBUG_OKX_CLIENT:
                print(f"[调试] 请求 OKX: account.get_positions(instId={inst_id})" if inst_id else "[调试] 请求 OKX: account.get_positions()")
            r = self.account.get_positions(instId=inst_id) if inst_id else self.account.get_positions()
            if DEBUG_OKX_CLIENT:
                print(f"[调试] OKX 返回: {str(r)[:300]}..." if r else "[调试] OKX 返回: None")
            return r.get("data", [])
        except KeyboardInterrupt:
            raise
        except Exception as e:
            raise NetworkError(f"[get_positions] 网络请求异常: {e}")

    def get_last_price(self, inst_id: str) -> float:
        if DEBUG_OKX_CLIENT:
            print(f"[调试] 调用 get_last_price(inst_id={inst_id})")
        try:
            if DEBUG_OKX_CLIENT:
                print(f"[调试] 请求 OKX: market.get_ticker(instId={inst_id})")
            ticker = self.market.get_ticker(instId=inst_id).get("data", [{}])[0]
            if DEBUG_OKX_CLIENT:
                print(f"[调试] OKX 返回: {ticker}")
            return float(ticker.get("last", 0))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            raise NetworkError(f"[get_last_price] 网络请求异常: {e}")

    def has_open_position(self, inst_id: str) -> bool:
        if DEBUG_OKX_CLIENT:
            print(f"[调试] 调用 has_open_position(inst_id={inst_id})")
        try:
            pos = self.get_positions(inst_id)
            if DEBUG_OKX_CLIENT:
                print(f"[调试] has_open_position 获取到 pos: {pos}")
            return any(abs(float(p.get("pos", 0))) > 0 for p in pos)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            raise NetworkError(f"[has_open_position] 网络请求异常: {e}")

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "cross", pos_side: str = "net") -> None:
        # posSide = net 表示净持仓模式；cross/isolated 由 mgn_mode 控制
        try:
            if DEBUG_OKX_CLIENT:
                print(f"[调试] set_leverage: inst_id={inst_id}, lever={lever}, mgn_mode={mgn_mode}, pos_side={pos_side}")
            self.account.set_leverage(instId=inst_id, lever=str(lever), mgnMode=mgn_mode, posSide=pos_side)
        except Exception as e:
            if DEBUG_OKX_CLIENT:
                print(f"[set_leverage] 网络请求异常: {e}")

    def place_order(
        self,
        inst_id: str,
        side: str,  # buy/sell
        td_mode: str,  # cross/isolated
        sz: str,  # size in contracts
        ord_type: str = "market",
        pos_side: str = "long",  # long/short for tp/sl
        tp_ratio: Optional[float] = None,
        sl_ratio: Optional[float] = None,
        tp_trigger_type: str = "last",
        sl_trigger_type: str = "last",
        lever: Optional[int] = None,
    ) -> Dict[str, Any]:
        """市价下单（可附带 TP/SL 触发参数，自动组装 attachAlgoOrds）。

        说明：OKX 的止盈止损可通过 attachAlgoOrds 实现。
        """
        params = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        # 在净持仓（buy/sell）模式下不应传 posSide；双向持仓模式才传 long/short
        if pos_side in ("long", "short"):
            params["posSide"] = pos_side
        attach_algo = {}
        last = None
        if tp_ratio is not None or sl_ratio is not None:
            last = self.get_last_price(inst_id)
        computed = {"last": last}
        if tp_ratio is not None:
            # 兼容 net/long/short 三种模式，自动修正止盈方向
            ratio = abs(tp_ratio)
            # 判断方向依据：
            # 1. pos_side=long/short 用pos_side
            # 2. pos_side=net 用side（buy=多，sell=空）
            is_long = None
            if pos_side == "long":
                is_long = True
            elif pos_side == "short":
                is_long = False
            elif pos_side == "net":
                is_long = (side == "buy")
            else:
                # 兜底，默认为多
                is_long = True
            if is_long:
                tp_px = last * (1 + ratio)
                if tp_px <= last:
                    tp_px = last * 1.002
            else:
                tp_px = last * (1 - ratio)
                if tp_px >= last:
                    tp_px = last * 0.998
            attach_algo["tpTriggerPx"] = f"{tp_px:.6f}"
            attach_algo["tpTriggerPxType"] = tp_trigger_type
            attach_algo["tpOrdPx"] = "-1"  # 市价
            computed.update({"tp_px": tp_px, "tp_is_long": is_long, "tp_ratio": ratio})
        if sl_ratio is not None:
            # 兼容 net/long/short 三种模式，自动修正止损方向
            ratio = abs(sl_ratio)
            is_long = None
            if pos_side == "long":
                is_long = True
            elif pos_side == "short":
                is_long = False
            elif pos_side == "net":
                is_long = (side == "buy")
            else:
                is_long = True
            if is_long:
                sl_px = last * (1 - ratio)
                if sl_px >= last:
                    sl_px = last * 0.998
            else:
                sl_px = last * (1 + ratio)
                if sl_px <= last:
                    sl_px = last * 1.002
            attach_algo["slTriggerPx"] = f"{sl_px:.6f}"
            attach_algo["slTriggerPxType"] = sl_trigger_type
            attach_algo["slOrdPx"] = "-1"
            computed.update({"sl_px": sl_px, "sl_is_long": is_long, "sl_ratio": ratio})
        if attach_algo:
            params["attachAlgoOrds"] = [attach_algo]
        if DEBUG_OKX_CLIENT:
            print(f"[调试] place_order 参数: {params}")
            if computed:
                print(f"[调试] 价格计算: {computed}")
        try:
            result = self.trade.place_order(**params)
            if DEBUG_OKX_CLIENT:
                print(f"[调试] place_order 返回: {result}")
            # 针对 TP 方向相关错误，做一次性自愈重试：
            try_retry = False
            if isinstance(result, dict):
                data_list = result.get("data") or []
                # OKX 批量返回在 data 数组中包含 sCode/sMsg
                if isinstance(data_list, list) and data_list:
                    item = data_list[0]
                    s_code = str(item.get("sCode", ""))
                    s_msg = (item.get("sMsg", "") or "").lower()
                    # 51052: Your TP price should be lower than the primary order price.
                    # 51051: Your TP price should be higher than the primary order price.（常见文案）
                    if s_code in {"51052", "51051"} or ("tp price" in s_msg and ("lower" in s_msg or "higher" in s_msg)):
                        try_retry = True
            if try_retry and attach_algo and "tpTriggerPx" in attach_algo and last:
                if DEBUG_OKX_CLIENT:
                    print("[调试] 触发 TP 自愈重试：翻转 TP 方向并放宽 0.2% 余量")
                # 翻转 TP 方向：若原先按多向上，则改为向下；若按空向下，则改为向上
                orig_tp = float(attach_algo.get("tpTriggerPx"))
                ratio = float(computed.get("tp_ratio", 0.01) or 0.01)
                # 判断是原来向上还是向下
                if orig_tp >= last:
                    # 原为向上，改为向下
                    new_tp = last * (1 - max(0.001, ratio))
                else:
                    # 原为向下，改为向上
                    new_tp = last * (1 + max(0.001, ratio))
                # 放入新的参数并重试
                params_retry = dict(params)
                params_retry["attachAlgoOrds"] = [dict(params["attachAlgoOrds"][0])]
                params_retry["attachAlgoOrds"][0]["tpTriggerPx"] = f"{new_tp:.6f}"
                if DEBUG_OKX_CLIENT:
                    print(f"[调试] 自愈重试参数: {params_retry}")
                result_retry = self.trade.place_order(**params_retry)
                if DEBUG_OKX_CLIENT:
                    print(f"[调试] 自愈重试返回: {result_retry}")
                return result_retry

            # 针对持仓/订单数量超过上限 51004，做一次性缩量重试
            try_retry_cap = False
            cap_value = None
            if isinstance(result, dict):
                data_list = result.get("data") or []
                if isinstance(data_list, list) and data_list:
                    item = data_list[0]
                    s_code = str(item.get("sCode", ""))
                    s_msg = item.get("sMsg", "") or ""
                    if s_code == "51004":
                        try_retry_cap = True
                        # 更稳健地从文案中提取最大合约数（例如 1,500(contracts)）
                        import re
                        # 先找所有带 (contracts) 的数字
                        m_all = re.findall(r"([0-9,]+)\(contracts\)", s_msg)
                        if m_all:
                            try:
                                cap_value = int(m_all[0].replace(",", ""))
                            except Exception:
                                cap_value = None
                        # 回退：匹配包含 maximum position amount 的任意数字
                        if cap_value is None:
                            m2 = re.search(r"maximum position amount[^0-9]*([0-9,]+)", s_msg, re.IGNORECASE)
                            if m2:
                                try:
                                    cap_value = int(m2.group(1).replace(",", ""))
                                except Exception:
                                    cap_value = None
            if try_retry_cap:
                try:
                    cur_sz = int(float(sz))
                except Exception:
                    cur_sz = None
                if cap_value and cur_sz:
                    new_sz = max(1, min(cur_sz, cap_value))
                    # 缓存上限，便于后续预先钳制
                    self.update_position_cap(inst_id, lever, cap_value)
                elif cur_sz:
                    # 无法解析上限时，保守缩半
                    new_sz = max(1, cur_sz // 2)
                else:
                    new_sz = None
                if new_sz and new_sz != int(float(sz)):
                    params_retry2 = dict(params)
                    params_retry2["sz"] = str(new_sz)
                    if DEBUG_OKX_CLIENT:
                        print(f"[调试] 命中 51004，缩量重试: {params_retry2}")
                    result_retry2 = self.trade.place_order(**params_retry2)
                    if DEBUG_OKX_CLIENT:
                        print(f"[调试] 缩量重试返回: {result_retry2}")
                    return result_retry2
            # 针对保证金不足 51008，做一次性缩量 50% 重试
            try_retry_margin = False
            if isinstance(result, dict):
                data_list = result.get("data") or []
                if isinstance(data_list, list) and data_list:
                    item = data_list[0]
                    s_code = str(item.get("sCode", ""))
                    if s_code == "51008":
                        try_retry_margin = True
            if try_retry_margin:
                try:
                    cur_sz = int(float(sz))
                except Exception:
                    cur_sz = None
                if cur_sz and cur_sz > 1:
                    new_sz = max(1, cur_sz // 2)
                    params_retry3 = dict(params)
                    params_retry3["sz"] = str(new_sz)
                    if DEBUG_OKX_CLIENT:
                        print(f"[调试] 命中 51008，保证金不足，缩量50%重试: {params_retry3}")
                    result_retry3 = self.trade.place_order(**params_retry3)
                    if DEBUG_OKX_CLIENT:
                        print(f"[调试] 51008 缩量重试返回: {result_retry3}")
                    return result_retry3
            return result
        except Exception as e:
            if DEBUG_OKX_CLIENT:
                print(f"[place_order] 网络请求异常: {e}")
            return {"code": "-1", "msg": str(e), "data": []}

    def cancel_all_open_orders(self, inst_id: str) -> None:
        # 仅取消挂单；已成交的仓位需要通过相反方向下市价单或关闭接口处理
        try:
            if DEBUG_OKX_CLIENT:
                print(f"[调试] cancel_all_open_orders: inst_id={inst_id}")
            open_orders = self.trade.get_order_list(instId=inst_id).get("data", [])
            for o in open_orders:
                if o.get("state") in {"live", "partially_filled"}:
                    try:
                        self.trade.cancel_order(instId=inst_id, ordId=o.get("ordId"))
                    except Exception as e:
                        if DEBUG_OKX_CLIENT:
                            print(f"[cancel_order] 网络请求异常: {e}")
        except Exception as e:
            if DEBUG_OKX_CLIENT:
                print(f"[cancel_all_open_orders] 网络请求异常: {e}")

    def close_position_market(self, inst_id: str, pos_side: str, sz: Optional[str] = None) -> Dict[str, Any]:
        # Place market order opposite to position direction
        side = "sell" if pos_side == "long" else "buy"
        params = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": "market",
        }
        if sz:
            params["sz"] = sz
        if DEBUG_OKX_CLIENT:
            print(f"[调试] close_position_market 参数: {params}")
        try:
            result = self.trade.place_order(**params)
            if DEBUG_OKX_CLIENT:
                print(f"[调试] close_position_market 返回: {result}")
            # 平仓后自动取消所有计划单
            self.cancel_all_algo_orders(inst_id)
            return result
        except Exception as e:
            if DEBUG_OKX_CLIENT:
                print(f"[close_position_market] 网络请求异常: {e}")
            return {"code": "-1", "msg": str(e), "data": []}

    def get_position_summary(self, inst_id: str) -> Optional[Dict[str, Any]]:
        try:
            if DEBUG_OKX_CLIENT:
                print(f"[调试] get_position_summary: inst_id={inst_id}")
            for p in self.get_positions(inst_id):
                if p.get("instId") == inst_id:
                    if DEBUG_OKX_CLIENT:
                        print(f"[调试] get_position_summary 返回: {p}")
                    return p
            return None
        except Exception as e:
            if DEBUG_OKX_CLIENT:
                print(f"[get_position_summary] 网络请求异常: {e}")
            return None


class DummyOkxClient:
    """干跑最小实现：不发网络请求，返回合成数据。"""

    def __init__(self):
        self._balance_usdt = 1000.0

    # --- Account helpers ---
    def get_usdt_balance(self) -> float:
        return self._balance_usdt

    # --- Market data ---
    def get_candles(self, inst_id: str, bar: str = "1m", limit: int = 60):
        # 生成简单的随机游走 K 线数据
        price = 30000.0 if inst_id.startswith("BTC") else 2000.0
        out = []
        for i in range(limit):
            drift = (random.random() - 0.5) * 0.002
            price = max(1.0, price * (1 + drift))
            o = price * (1 - 0.0005)
            h = price * (1 + 0.001)
            l = price * (1 - 0.001)
            c = price
            out.append([str(0), f"{o:.2f}", f"{h:.2f}", f"{l:.2f}", f"{c:.2f}", "0", "0", "0", "1"])
        return out

    # --- Positions & orders ---
    def get_positions(self, inst_id: Optional[str] = None):
        return []

    def has_open_position(self, inst_id: str) -> bool:
        return False

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str = "cross", pos_side: str = "net") -> None:
        return None

    def get_last_price(self, inst_id: str) -> float:
        return 30000.0 if inst_id.startswith("BTC") else 2000.0

    def place_order(
        self,
        inst_id: str,
        side: str,
        td_mode: str,
        sz: str,
        ord_type: str = "market",
        pos_side: str = "long",
        tp_ratio: Optional[float] = None,
        sl_ratio: Optional[float] = None,
    ):
        return {"data": [{"ordId": "dry-run-order"}], "code": "0", "msg": ""}

    def cancel_all_open_orders(self, inst_id: str) -> None:
        return None

    def close_position_market(self, inst_id: str, pos_side: str, sz: Optional[str] = None):
        return {"code": "0", "msg": ""}

    def get_position_summary(self, inst_id: str):
        return None
