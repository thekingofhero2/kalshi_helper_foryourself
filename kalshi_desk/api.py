# -*- coding: utf-8 -*-
"""Kalshi 私有账户 API 封装。

基于本工程内置的 client.KalshiClient（RSA-PSS 签名），补充本系统需要的
余额分布、子账户、成交、行情与下单语义。
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from .client import KalshiClient, KalshiHTTPError, fp_to_float  # noqa: F401

ENDPOINTS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
ENV_LABEL = {"prod": "生产", "demo": "模拟"}

ACTION_LABEL = {
    "buy_yes": "买入 YES",
    "sell_yes": "卖出 YES",
    "buy_no": "买入 NO",
    "sell_no": "卖出 NO",
}


# ---------------------------------------------------------------------------
# 数值解析：Kalshi 同时存在 _dollars(FixedPointDollars) / _fp(FixedPointCount)
# 与旧版「整数美分」字段，这里统一转成 float 美元 / 浮点数。
# ---------------------------------------------------------------------------
def dec(v) -> Optional[float]:
    """DollarDecimal / 数字字符串 -> float"""
    if v is None or v == "":
        return None
    if isinstance(v, dict):
        for k in ("dollar", "dollars", "value", "amount"):
            if k in v:
                return dec(v[k])
        return None
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dollar_value(v) -> Optional[float]:
    """DollarDecimal 字段：dict/字符串按美元；整数按美分。"""
    if isinstance(v, int) and not isinstance(v, bool):
        return v / 100.0
    return dec(v)


def money(obj: dict, key: str) -> Optional[float]:
    """优先 <key>_dollars；否则 <key> 按整数美分。"""
    if not isinstance(obj, dict):
        return None
    if f"{key}_dollars" in obj:
        return dec(obj.get(f"{key}_dollars"))
    if key in obj and obj.get(key) is not None:
        v = obj.get(key)
        if isinstance(v, dict):
            return dec(v)
        try:
            return float(v) / 100.0
        except (TypeError, ValueError):
            return None
    return None


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _count(obj: dict, *keys) -> float:
    for k in keys:
        if obj.get(k) is not None and obj.get(k) != "":
            return _num(obj.get(k)) or 0.0
    return 0.0


class DeskAPI:
    """面向控制台的账户 API。"""

    def __init__(self, key_id: str, pem_path: str, env: str = "prod"):
        self.key_id = key_id
        self.env = env if env in ENDPOINTS else "prod"
        self.base_url = ENDPOINTS[self.env]
        self.cli = KalshiClient(key_id, pem_path, base_url=self.base_url)

    # ==== 账户 ====
    def balance(self) -> dict:
        d = self.cli.request("GET", "/portfolio/balance")
        shards = []
        for it in d.get("balance_breakdown") or []:
            if not isinstance(it, dict):
                continue
            shards.append({
                "exchange_index": it.get("exchange_index"),
                "balance": _dollar_value(it.get("balance")) or 0.0,
            })
        shards.sort(key=lambda x: (x["exchange_index"] is None, x["exchange_index"]))
        return {
            "available": money(d, "balance") or 0.0,
            "portfolio_value": money(d, "portfolio_value"),
            "payout": money(d, "payout"),
            "shards": shards,
            "updated_ts": d.get("updated_ts"),
            "raw": d,
        }

    # ==== 子账户 / shard ====
    def subaccount_balances(self) -> list:
        d = self.cli.request("GET", "/portfolio/subaccounts/balances")
        rows = []
        for it in d.get("subaccount_balances") or []:
            if not isinstance(it, dict):
                continue
            rows.append({
                "subaccount": it.get("subaccount_number"),
                "exchange_index": it.get("exchange_index"),
                "balance": _dollar_value(it.get("balance")) or 0.0,
                "updated_ts": it.get("updated_ts"),
                "raw": it,
            })
        rows.sort(key=lambda x: ((x["exchange_index"] if x["exchange_index"] is not None else 0),
                                 (x["subaccount"] if x["subaccount"] is not None else 0)))
        return rows

    def create_subaccount(self, exchange_index: Optional[int] = None) -> dict:
        body = {}
        if exchange_index is not None:
            body["exchange_index"] = int(exchange_index)
        return self.cli.request("POST", "/portfolio/subaccounts", json_body=body)

    def transfer_subaccounts(self, from_sub: int, to_sub: int, amount: float,
                             exchange_index: Optional[int] = None) -> dict:
        body = {
            "client_transfer_id": str(uuid.uuid4()),
            "from_subaccount": int(from_sub),
            "to_subaccount": int(to_sub),
            "amount_cents": int(round(float(amount) * 100)),
        }
        if exchange_index is not None:
            body["exchange_index"] = int(exchange_index)
        return self.cli.request("POST", "/portfolio/subaccounts/transfer", json_body=body)

    def subaccount_transfers(self, limit: int = 50) -> list:
        d = self.cli.request("GET", "/portfolio/subaccounts/transfers",
                             params={"limit": int(limit)})
        rows = []
        for t in d.get("transfers") or []:
            rows.append({
                "transfer_id": t.get("transfer_id"),
                "amount": (_num(t.get("amount_cents")) or 0.0) / 100.0,
                "from_subaccount": t.get("from_subaccount"),
                "to_subaccount": t.get("to_subaccount"),
                "created_ts": t.get("created_ts"),
                "raw": t,
            })
        return rows

    def transfer_across_shards(self, from_shard: int, to_shard: int, amount: float,
                               source: str = "event_contract",
                               destination: str = "event_contract",
                               source_subaccount: int = 0,
                               destination_subaccount: int = 0) -> dict:
        """跨 shard（exchange instance）转账；金额单位为 centicents（1 美元=10000）。"""
        body = {
            "source": source,
            "destination": destination,
            "source_exchange_shard": int(from_shard),
            "destination_exchange_shard": int(to_shard),
            "amount": int(round(float(amount) * 10000)),
            "source_subaccount": int(source_subaccount),
            "destination_subaccount": int(destination_subaccount),
        }
        return self.cli.request("POST", "/portfolio/intra_exchange_instance_transfer",
                                json_body=body)

    # ==== 持仓 ====
    def positions(self) -> list:
        out, cursor = [], None
        for _ in range(5):
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            d = self.cli.request("GET", "/portfolio/positions", params=params)
            for p in d.get("market_positions") or d.get("positions") or []:
                pos = _count(p, "position_fp", "position")
                if not pos:
                    continue
                out.append({
                    "ticker": p.get("ticker"),
                    "position": pos,
                    "yes_qty": pos if pos > 0 else 0.0,
                    "no_qty": -pos if pos < 0 else 0.0,
                    "exposure": money(p, "market_exposure") or 0.0,
                    "realized_pnl": money(p, "realized_pnl") or 0.0,
                    "fees": money(p, "fees_paid") or 0.0,
                    "total_traded": money(p, "total_traded") or 0.0,
                    "resting": int(_count(p, "resting_orders_count")),
                    "exchange_index": p.get("exchange_index"),
                    "updated_ts": p.get("last_updated_ts"),
                    "raw": p,
                })
            cursor = d.get("cursor")
            if not cursor:
                break
        out.sort(key=lambda x: abs(x["exposure"]), reverse=True)
        return out

    # ==== 订单 ====
    def orders(self, status: Optional[str] = None, ticker: Optional[str] = None,
               days: Optional[int] = None, max_pages: int = 3) -> list:
        min_ts = int(time.time() - days * 86400) if days else None
        out = []
        for o in self.cli.iter_orders(status=status or None, ticker=ticker or None,
                                      limit=200, max_pages=max_pages,
                                      min_ts=min_ts, timeout=10):
            price = dec(o.get("price"))
            if price is None:
                price = money(o, "yes_price")
            side = o.get("side")
            side_txt = {"bid": "买 YES", "ask": "卖 YES", "yes": "YES", "no": "NO"}.get(side, side or "—")
            out.append({
                "order_id": o.get("order_id"),
                "client_order_id": o.get("client_order_id"),
                "ticker": o.get("ticker"),
                "side": side,
                "side_txt": side_txt,
                "price": price,
                "count": _count(o, "count", "initial_count_fp", "initial_count"),
                "filled": _count(o, "fill_count_fp", "fill_count"),
                "remaining": _count(o, "remaining_count_fp", "remaining_count"),
                "status": o.get("status"),
                "tif": o.get("time_in_force"),
                "fees": (money(o, "taker_fees") or 0.0) + (money(o, "maker_fees") or 0.0),
                "subaccount": o.get("subaccount"),
                "exchange_index": o.get("exchange_index"),
                "created": o.get("created_time") or o.get("created_ts"),
                "raw": o,
            })
        return out

    def cancel(self, order_id: str, ticker: str) -> dict:
        return self.cli.cancel_order(order_id, ticker=ticker)

    def place(self, ticker: str, side: str, yes_price: float, count: int,
              tif: str = "immediate_or_cancel", post_only: bool = False,
              subaccount: Optional[int] = None) -> dict:
        """V2 单 book 下单：side=bid(买 YES)/ask(卖 YES)，price 为 YES 价。"""
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "count": str(int(count)),
            "price": f"{float(yes_price):.4f}",
            "self_trade_prevention_type": "taker_at_cross",
            "time_in_force": tif,
        }
        if post_only:
            body["post_only"] = True
        if subaccount is not None:
            body["subaccount"] = int(subaccount)
        return self.cli.request("POST", "/portfolio/events/orders", json_body=body, timeout=15)

    # ==== 成交 ====
    def fills(self, ticker: Optional[str] = None, days: Optional[int] = None,
              max_pages: int = 3) -> list:
        out, cursor = [], None
        min_ts = int(time.time() - days * 86400) if days else None
        for _ in range(max_pages):
            params = {"limit": 200}
            if ticker:
                params["ticker"] = ticker
            if min_ts:
                params["min_ts"] = min_ts
            if cursor:
                params["cursor"] = cursor
            d = self.cli.request("GET", "/portfolio/fills", params=params)
            for f in d.get("fills") or []:
                side = f.get("side")
                price = money(f, "yes_price") if side != "no" else money(f, "no_price")
                if price is None:
                    price = money(f, "yes_price")
                out.append({
                    "fill_id": f.get("fill_id") or f.get("trade_id"),
                    "trade_id": f.get("trade_id"),
                    "order_id": f.get("order_id"),
                    "ticker": f.get("market_ticker") or f.get("ticker"),
                    "side": side,
                    "action": f.get("action"),
                    "price": price,
                    "count": _count(f, "count_fp", "count"),
                    "is_taker": f.get("is_taker"),
                    "fees": dec(f.get("fees_dollars")) or money(f, "fees"),
                    "created": f.get("created_time") or f.get("ts"),
                    "subaccount": f.get("subaccount"),
                    "exchange_index": f.get("exchange_index"),
                    "raw": f,
                })
            cursor = d.get("cursor")
            if not cursor:
                break
        return out

    # ==== 行情 ====
    def quote(self, ticker: str) -> dict:
        m = self.cli.get_market(ticker) or {}
        q = {
            "ticker": ticker,
            "title": m.get("title") or m.get("subtitle") or "",
            "status": m.get("status"),
            "result": m.get("result"),
            "close_time": m.get("close_time"),
            "yes_bid": money(m, "yes_bid"),
            "yes_ask": money(m, "yes_ask"),
            "no_bid": money(m, "no_bid"),
            "no_ask": money(m, "no_ask"),
            "last": money(m, "last_price"),
        }
        if None in (q["yes_bid"], q["yes_ask"]):
            try:
                book = self._orderbook_quote(ticker)
            except Exception:
                book = {}
            for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
                if q[k] is None and book.get(k) is not None:
                    q[k] = book[k]
        if q["no_bid"] is None and q["yes_ask"] is not None:
            q["no_bid"] = round(1.0 - q["yes_ask"], 4)
        if q["no_ask"] is None and q["yes_bid"] is not None:
            q["no_ask"] = round(1.0 - q["yes_bid"], 4)
        return q

    def _orderbook_quote(self, ticker: str) -> dict:
        ob = (self.cli.get_orderbook(ticker, depth=10) or {}).get("orderbook") or {}

        def prices(side):
            arr = ob.get(side) or ob.get(f"{side}_dollars") or []
            out = []
            for row in arr:
                if not row:
                    continue
                p = row[0]
                if isinstance(p, str):
                    x = _num(p)
                    if x is None:
                        continue
                    if x > 1.0:
                        x /= 100.0
                else:
                    x = float(p)
                    if isinstance(p, int) or x > 1.0:
                        x /= 100.0
                out.append(x)
            return out

        yes, no = prices("yes"), prices("no")
        yes_bid = max(yes) if yes else None
        no_bid = max(no) if no else None
        return {
            "yes_bid": yes_bid,
            "yes_ask": round(1.0 - no_bid, 4) if no_bid is not None else None,
            "no_bid": no_bid,
            "no_ask": round(1.0 - yes_bid, 4) if yes_bid is not None else None,
        }

    # ==== 交易动作 -> book side ====
    @staticmethod
    def to_book(action: str, price: float) -> tuple:
        """把「买入/卖出 YES/NO + 该合约价格」映射为 (book_side, YES 价)。"""
        if action == "buy_yes":
            return "bid", float(price)
        if action == "sell_yes":
            return "ask", float(price)
        if action == "buy_no":
            return "ask", round(1.0 - float(price), 4)
        if action == "sell_no":
            return "bid", round(1.0 - float(price), 4)
        raise ValueError(f"未知交易动作: {action}")

    @staticmethod
    def best_price(q: dict, action: str) -> Optional[float]:
        idx = {
            "buy_yes": "yes_ask",
            "sell_yes": "yes_bid",
            "buy_no": "no_ask",
            "sell_no": "no_bid",
        }[action]
        return q.get(idx)
