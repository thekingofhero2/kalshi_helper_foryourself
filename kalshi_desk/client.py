# -*- coding: utf-8 -*-
"""
Kalshi Trade API v2 客户端（本工程内置，无外部依赖）
====================================================
- 认证: RSA-PSS 签名（timestamp + METHOD + path）
- 下单: V2 单 book 模型（side=bid/ask + price 为 YES 价）
- 订单: fill_count_fp / remaining_count_fp（FixedPointCount 字符串）
- 分页: 所有列表接口支持 cursor 游标
"""
import base64
import time
from typing import Iterator, Optional

import requests

ENDPOINTS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "prod_shared": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "demo_shared": "https://demo-api.kalshi.co/trade-api/v2",
}

ORDER_STATUS = ("created", "resting", "filled", "canceled", "executed")
MARKET_STATUS = ("initialized", "inactive", "active", "closed", "determined", "disputed", "amended", "finalized")


class KalshiHTTPError(RuntimeError):
    """HTTP 错误响应（携带 status_code，调用方可区分 404 终态与网络故障）"""

    def __init__(self, method: str, path: str, status_code: int, text: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.text = text
        super().__init__(f"Kalshi {method} {path} -> {status_code}: {text[:400]}")


class KalshiClient:
    """Kalshi Trade API V2 客户端（官方 OpenAPI 规范）"""

    def __init__(self, key_id: str, pem_path: str, base_url: str = ENDPOINTS["demo_shared"],
                 timeout: int = 20):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        self._hashes = hashes
        self._padding = padding
        self.key_id = key_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        with open(pem_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    # ==== 签名 ====
    def _sign(self, method: str, path: str) -> dict:
        """RSA-PSS 签名: message = ts_ms + METHOD + path(含 /trade-api/v2 前缀, 不含 query)"""
        from urllib.parse import urlparse

        full_path = urlparse(self.base_url + path).path
        ts_ms = str(int(time.time() * 1000))
        message = (ts_ms + method.upper() + full_path).encode("utf-8")
        signature = self.private_key.sign(
            message,
            self._padding.PSS(
                mgf=self._padding.MGF1(self._hashes.SHA256()),
                salt_length=self._padding.PSS.DIGEST_LENGTH,
            ),
            self._hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    def request(self, method: str, path: str, params: dict = None, json_body: dict = None,
                timeout: float = None) -> dict:
        headers = self._sign(method, path)
        url = self.base_url + path
        resp = self.session.request(method, url, headers=headers, params=params,
                                    json=json_body,
                                    timeout=self.timeout if timeout is None else timeout)
        if resp.status_code >= 400:
            raise KalshiHTTPError(method, path, resp.status_code, resp.text or "")
        return resp.json() if resp.text else {}

    # ==== 市场数据 ====
    def get_markets_page(self, series_ticker: str, status: str = None,
                         cursor: str = None, limit: int = 1000) -> dict:
        """拉一页市场列表，返回 {markets, cursor}"""
        params = {"series_ticker": series_ticker, "limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/markets", params=params)

    def iter_markets(self, series_ticker: str, status: str = None, limit: int = 1000) -> Iterator[dict]:
        """分页迭代市场列表"""
        cursor = None
        while True:
            page = self.get_markets_page(series_ticker, status, cursor, limit)
            for m in page.get("markets", []):
                yield m
            cursor = page.get("cursor", "")
            if not cursor:
                break

    def get_market(self, ticker: str) -> dict:
        """获取单个市场详情（返回 market 字典）"""
        resp = self.request("GET", f"/markets/{ticker}")
        return resp.get("market", resp)

    def get_market_status(self, ticker: str) -> str:
        m = self.get_market(ticker)
        return m.get("status", "")

    def get_orderbook(self, ticker: str, depth: int = 2) -> dict:
        """获取订单簿（orderbook 包含 yes/no 各档 bid/ask）"""
        return self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_trades(self, ticker: str = None, min_ts: str = None, max_ts: str = None,
                   cursor: str = None, limit: int = 1000) -> dict:
        """
        获取成交（官方端点 /markets/trades，批量接口）
        Args:
            ticker: 按市场过滤
            min_ts/max_ts: ISO8601 时间过滤
            cursor: 分页
        Returns:
            {trades: [...], cursor: str}
        """
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/markets/trades", params=params)

    # ==== 账户 ====
    def get_balance(self, exchange_index: int = None) -> dict:
        """账户余额（cents）。exchange_index: 0=主, 2=交易 shard"""
        params = {}
        if exchange_index is not None:
            params["exchange_index"] = exchange_index
        return self.request("GET", "/portfolio/balance", params=params or None)

    def get_positions(self, cursor: str = None) -> dict:
        """持仓（GetPositionsResponse 字段归一化）

        官方 v2 返回 market_positions + event_positions，旧字段 positions 已
        下线；这里统一成 positions，避免调用方读到空列表。
        """
        params = {}
        if cursor:
            params["cursor"] = cursor
        d = self.request("GET", "/portfolio/positions", params=params or None)
        if "positions" not in d:
            d["positions"] = d.get("market_positions") or []
        return d

    # ==== 订单 ====
    def get_orders_page(self, status: str = None, ticker: str = None,
                        event_ticker: str = None, cursor: str = None, limit: int = 200,
                        min_ts: int = None,
                        timeout: float = None) -> dict:
        """拉一页订单，返回 {orders, cursor}

        ticker: **单个**市场 ticker（官方 TickerQuery 是单值；实测传
        "A,B,C" 逗号列表恒返回 0 单）。
        event_ticker: 逗号分隔的事件 ticker 列表（官方
        MultipleEventTickerQuery，上限 100）。多市场一次查询必须用这个参数，
        实测 9 个事件可正常返回。
        min_ts: unix 秒（官方 MinTsQuery），按 created_time 过滤下限。
        账户历史订单会无限累积，启动基线必须用它限窗，否则翻页截断。
        """
        params = {"limit": limit}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if min_ts:
            params["min_ts"] = int(min_ts)
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/portfolio/orders", params=params, timeout=timeout)

    def iter_orders(self, status: str = None, ticker: str = None,
                    event_ticker: str = None, limit: int = 200,
                    max_pages: int = 30, min_ts: int = None,
                    timeout: float = None) -> Iterator[dict]:
        """分页迭代订单（Kalshi 限流/瞬时可能返回 orders=null，跳过该页不崩溃）

        账户历史订单会累积（canceled/executed 在 historical cutoff 前一直可查），
        不设上限会拖住主循环，故默认最多翻 max_pages 页并给单页 shorter timeout。
        """
        cursor = None
        n = 0
        for _ in range(max_pages):
            page = self.get_orders_page(status=status, ticker=ticker,
                                        event_ticker=event_ticker,
                                        cursor=cursor, limit=limit,
                                        min_ts=min_ts,
                                        timeout=timeout)
            if not page or not isinstance(page, dict):
                break
            for o in (page.get("orders") or []):
                yield o
                n += 1
            cursor = page.get("cursor", "")
            if not cursor:
                return
        import logging
        _et = (event_ticker or '')[:80] or None
        logging.getLogger("kalshi").warning(
            f"iter_orders 达到页数上限 {max_pages}（status={status} "
            f"ticker={(ticker or '')[:60] or None} event_ticker={_et} "
            f"min_ts={min_ts}），"
            f"已读 {n} 条即截断，剩余订单未读取")

    def get_order(self, order_id: str) -> dict:
        """按 order_id 取订单（返回 order 字典）"""
        resp = self.request("GET", f"/portfolio/orders/{order_id}")
        return resp.get("order", resp)

    def get_order_by_client_id(self, client_order_id: str, status: str = None) -> Optional[dict]:
        """按 client_order_id 取订单（遍历分页，按需传 status 过滤）"""
        for o in self.iter_orders(status=status):
            if o.get("client_order_id") == client_order_id:
                return o
        return None

    def place_order(self, ticker: str, outcome_side: str, count: int,
                    client_order_id: str, price_dollars: str = None,
                    time_in_force: str = "immediate_or_cancel",
                    post_only: bool = False) -> dict:
        """
        下单（官方新格式）
        Args:
            ticker: 市场 ticker
            outcome_side: 'yes' 或 'no'（买 YES 传 'yes'，买 NO 传 'no'）
            count: 合约张数（整数）
            client_order_id: 客户端订单 ID
            price_dollars: 限价（FixedPointDollars 字符串，如 "0.9900"）; 不传=市价
            time_in_force: fill_or_kill / good_till_canceled / immediate_or_cancel
            post_only: 是否只挂单
        Returns:
            order 字典（含 order_id/status/fill_count_fp/remaining_count_fp）
        """
        # V2 单 book 模型: side=bid(买YES)/ask(卖YES=买NO), 单一 price(YES价)
        if not price_dollars:
            raise ValueError("place_order: V2 需显式 price_dollars（限价吃单），不支持市价")
        if outcome_side == "yes":
            body = {
                "ticker": ticker,
                "client_order_id": client_order_id,
                "side": "bid",
                "count": str(int(count)),
                "price": price_dollars,
            }
        else:  # 买 NO: 等价卖出 YES @ (1 - no价)
            body = {
                "ticker": ticker,
                "client_order_id": client_order_id,
                "side": "ask",
                "count": str(int(count)),
                "price": f"{1.0 - float(price_dollars):.4f}",
            }
        body.update({
            "self_trade_prevention_type": "taker_at_cross",
            "time_in_force": time_in_force,
        })
        if post_only:
            body["post_only"] = True
        resp = self.request("POST", "/portfolio/events/orders", json_body=body)
        return resp.get("order", resp)

    def cancel_order(self, order_id: str, exchange_index: int = 2,
                     ticker: str = None) -> dict:
        """取消订单（DELETE /portfolio/events/orders/{id}）。

        撤单必须指定订单所在的交易分片（exchange shard），否则交易所
        在错误分片找不到该单 → 404 not_found。两种指定方式：
        - 传 ticker（推荐）：官方自动路由 exchange_index=-1 + market_ticker，
          无需知道具体分片号；
        - 不传 ticker：用显式 exchange_index（默认 2），分片不符会 404。
        """
        if ticker:
            params = {"exchange_index": -1, "market_ticker": ticker}
        else:
            params = {"exchange_index": exchange_index}
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}",
                            params=params)

    # ==== 结算 ====
    def get_settlements(self, ticker: str = None, cursor: str = None, limit: int = 200) -> dict:
        """获取订单结算记录"""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        return self.request("GET", "/portfolio/settlements", params=params)


# ==== 便捷解析（FixedPoint 字符串转 float） ====
def fp_to_float(v) -> Optional[float]:
    """FixedPointDollars/FixedPointCount 字符串 -> float；空返回 None"""
    if v is None or v == "":
        return None
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def order_status(o: dict) -> str:
    return o.get("status", "")


def order_filled_count(o: dict) -> float:
    """成交张数（优先 fill_count_fp 字符串，兼容旧 fill_count）"""
    v = o.get("fill_count_fp")
    if v is None or v == "":
        v = o.get("fill_count", 0)
    return fp_to_float(v) or 0.0


def order_remaining_count(o: dict) -> float:
    v = o.get("remaining_count_fp")
    if v is None or v == "":
        v = o.get("remaining_count", 0)
    return fp_to_float(v) or 0.0


def order_fees_dollars(o: dict) -> float:
    """订单累计费用（美元）"""
    return (fp_to_float(o.get("taker_fees_dollars")) or 0.0) + \
           (fp_to_float(o.get("maker_fees_dollars")) or 0.0)


def order_cost_dollars(o: dict) -> float:
    """订单累计成交成本（美元）"""
    return (fp_to_float(o.get("taker_fill_cost_dollars")) or 0.0) + \
           (fp_to_float(o.get("maker_fill_cost_dollars")) or 0.0)
