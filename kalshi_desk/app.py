# -*- coding: utf-8 -*-
"""Kalshi 账户管理系统（NiceGUI）。

启动：python -m kalshi_desk
浏览器打开 http://127.0.0.1:8090
"""
from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime
from typing import Optional

from nicegui import events, run, ui

from . import store
from .api import ACTION_LABEL, ENV_LABEL, DeskAPI

CSS = """
body { background: #070b10; }
.kd-bar { background: #0e1620; border: 1px solid #1b2836; border-radius: 12px;
          padding: 10px 14px; width: 100%; }
.kd-stat { background: linear-gradient(180deg, #0f1824, #0b121a);
           border: 1px solid #1c2a38; border-radius: 12px; padding: 14px 16px; }
.kd-mut { color: #7c8ea4; }
.q-table { background: transparent; }
.q-table thead tr { background: #101a26; }
.q-table tbody tr:hover { background: #131f2c !important; cursor: pointer; }
.q-table td, .q-table th { font-variant-numeric: tabular-nums; }
.kd-pos { color: #34d399; }
.kd-neg { color: #f87171; }
.kd-actions .q-btn__content { white-space: nowrap; }
"""

TAB_OVER, TAB_POS, TAB_ORD, TAB_FILL, TAB_SUB, TAB_SET = (
    "概览", "持仓", "订单", "成交", "子账户 / 分片", "设置")


def usd(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "—"
    body = f"${abs(x):,.2f}"
    if x < 0:
        return f"-{body}"
    return f"+{body}" if signed and x > 0 else body


def fmt_ts(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        if isinstance(v, (int, float)):
            t = float(v)
            if t > 2e10:
                t /= 1000.0
            return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S")
        s = str(v).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone().strftime("%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(v)[:19]


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DeskUI:
    def __init__(self):
        self.api: Optional[DeskAPI] = None
        self.sess: Optional[dict] = None
        self.auto = True
        self.busy = False
        self.ticks = 0
        self.status_filter: Optional[str] = "resting"
        self.orders_ticker = ""
        self.fills_ticker = ""
        self.fills_days: Optional[int] = 3
        self._pem_text: Optional[str] = None
        self.state = {"balance": {}, "positions": [], "orders": [], "fills": [], "subs": []}
        self.pos_by_ticker: dict = {}
        self.order_by_id: dict = {}

    # ================= 页面骨架 =================
    async def start(self) -> None:
        ui.dark_mode(True)
        ui.add_css(CSS)

        self.trade_dlg = ui.dialog().props("persistent")
        self.order_dlg = ui.dialog().props("persistent")
        with ui.column().classes("w-full max-w-[1560px] mx-auto p-3 gap-3") as self.root:
            pass

        sess = store.load_session()
        if sess:
            try:
                api = DeskAPI(sess["key_id"], sess["pem_path"], sess["env"])
                await run.io_bound(api.balance)
                self.api, self.sess = api, sess
            except Exception as ex:  # 自动登录失败时回落到登录页
                self.login_error = f"自动登录失败：{ex}"

        self.rebuild()
        ui.timer(5.0, self.tick)

    def rebuild(self) -> None:
        self.root.clear()
        with self.root:
            if self.api is None:
                self.build_login()
            else:
                self.build_main()

    # ================= 登录 =================
    def build_login(self) -> None:
        with ui.column().classes("w-full items-center pt-16"):
            with ui.card().classes("w-[580px] max-w-[96vw] p-6 gap-3"):
                ui.label("Kalshi 账户管理系统").classes("text-2xl font-bold")
                ui.label("上传 API 私钥（.pem）后，可同步余额、分片/子账户、订单、"
                         "实时持仓与历史成交，并直接下单/平仓。").classes("text-sm kd-mut")
                if getattr(self, "login_error", ""):
                    ui.label(self.login_error).classes("text-red-400 text-sm")
                self.pem_name = ui.label("未选择文件").classes("text-xs kd-mut")
                ui.upload(label="上传 PEM 私钥", auto_upload=True, max_file_size=2_000_000,
                          on_upload=self.on_pem_upload).props("accept=.pem,.key,.txt flat bordered")
                self.key_input = ui.input(
                    "API Key ID", placeholder="Kalshi 控制台创建 API Key 时生成的 Key ID"
                ).classes("w-full").props("dense outlined")
                self.env_select = ui.select(
                    {"prod": "生产环境", "demo": "模拟环境"},
                    value=(self.sess or {}).get("env", "prod"),
                    label="环境",
                ).classes("w-48")
                ui.button("连接账户", icon="login", on_click=self.do_login) \
                    .props("unelevated color=primary")
                ui.separator()
                ui.label(f"私钥仅保存在本机 {store.DATA_DIR}，不会发送到任何第三方服务。") \
                    .classes("text-xs kd-mut")

    async def on_pem_upload(self, e: events.UploadEventArguments) -> None:
        data = await e.file.read()
        try:
            text = data.decode("utf-8")
            store.validate_pem(text)
        except Exception as ex:
            ui.notify(f"PEM 解析失败：{ex}", type="negative")
            return
        self._pem_text = text
        self.pem_name.set_text(f"已选择：{e.file.name}")
        ui.notify("私钥已载入", type="positive")

    async def do_login(self) -> None:
        key_id = (self.key_input.value or "").strip()
        env = self.env_select.value or "prod"
        await self._connect(key_id, env)

    async def _connect(self, key_id: str, env: str) -> None:
        if not self._pem_text:
            ui.notify("请先上传 .pem 私钥文件", type="warning")
            return
        staged = None
        try:
            staged = store.stage_pem(self._pem_text)
            api = DeskAPI(key_id, str(staged), env)
            await run.io_bound(api.balance)
            sess = store.save_session(key_id, self._pem_text, env)
            api = DeskAPI(key_id, sess["pem_path"], env)
        except Exception as ex:
            ui.notify(f"连接失败：{ex}", type="negative")
            return
        finally:
            if staged is not None:
                try:
                    staged.unlink()
                except OSError:
                    pass
        self.api, self.sess = api, sess
        self.login_error = ""
        self._pem_text = None
        self.rebuild()
        ui.notify("已连接 Kalshi 账户", type="positive")

    # ================= 主界面 =================
    def build_main(self) -> None:
        env = self.api.env
        with ui.row().classes("kd-bar items-center gap-3"):
            ui.icon("account_balance").classes("text-2xl text-sky-400")
            ui.label("Kalshi 交易工作台").classes("text-xl font-bold")
            ui.badge(ENV_LABEL[env]).props("color=negative" if env == "prod" else "color=info")
            ui.label(store.mask_key(self.sess["key_id"])).classes("text-xs kd-mut font-mono")
            ui.space()
            ui.switch("自动刷新", value=self.auto,
                      on_change=lambda e: setattr(self, "auto", bool(e.value)))
            ui.button("刷新", icon="refresh",
                      on_click=lambda: self.refresh_all(manual=True)).props("flat dense")
            self.updated_label = ui.label("同步中…").classes("text-xs kd-mut")

        with ui.tabs() as tabs:
            ui.tab(TAB_OVER, icon="dashboard").props("inline-label")
            ui.tab(TAB_POS, icon="account_balance_wallet").props("inline-label")
            ui.tab(TAB_ORD, icon="receipt_long").props("inline-label")
            ui.tab(TAB_FILL, icon="history").props("inline-label")
            ui.tab(TAB_SUB, icon="call_split").props("inline-label")
            ui.tab(TAB_SET, icon="settings").props("inline-label")

        with ui.tab_panels(tabs, value=TAB_OVER).classes("w-full"):
            with ui.tab_panel(TAB_OVER):
                self.build_overview()
            with ui.tab_panel(TAB_POS):
                self.build_positions()
            with ui.tab_panel(TAB_ORD):
                self.build_orders()
            with ui.tab_panel(TAB_FILL):
                self.build_fills()
            with ui.tab_panel(TAB_SUB):
                self.build_subaccounts()
            with ui.tab_panel(TAB_SET):
                self.build_settings()

        tabs.on_value_change(lambda e: self.on_tab(e.value))
        asyncio.create_task(self.refresh_all(manual=True))

    def on_tab(self, name: str) -> None:
        if not self.api:
            return
        if name == TAB_ORD:
            asyncio.create_task(self.refresh_orders())
        elif name == TAB_FILL:
            asyncio.create_task(self.refresh_fills())
        elif name == TAB_SUB:
            asyncio.create_task(self.refresh_transfers())

    # ---------- 概览 ----------
    def _stat_card(self, title: str):
        with ui.card().classes("kd-stat flex-1 min-w-[165px] gap-0"):
            ui.label(title).classes("text-xs kd-mut")
            lbl = ui.label("—").classes("text-2xl font-bold tabular-nums")
        return lbl

    def build_overview(self) -> None:
        with ui.row().classes("w-full gap-3"):
            self.c_avail = self._stat_card("可用余额")
            self.c_expo = self._stat_card("持仓敞口")
            self.c_pnl = self._stat_card("已实现盈亏")
            self.c_rest = self._stat_card("在挂订单")
            self.c_subs = self._stat_card("子账户数")

        with ui.row().classes("w-full gap-3 items-stretch"):
            with ui.card().classes("flex-1 min-w-[420px] gap-2"):
                ui.label("分片余额分布（exchange shard）").classes("font-semibold")
                self.shard_table = ui.table(
                    columns=[
                        {"name": "shard", "label": "shard", "field": "shard", "align": "center"},
                        {"name": "balance", "label": "余额", "field": "balance", "align": "right"},
                        {"name": "pct", "label": "占比", "field": "pct", "align": "right"},
                    ],
                    rows=[], row_key="id", pagination=5,
                ).classes("w-full")
            with ui.card().classes("flex-1 min-w-[420px] gap-2"):
                ui.label("子账户余额").classes("font-semibold")
                self.sub_mini = ui.table(
                    columns=[
                        {"name": "shard", "label": "shard", "field": "shard", "align": "center"},
                        {"name": "sub", "label": "子账户", "field": "sub", "align": "center"},
                        {"name": "balance", "label": "余额", "field": "balance", "align": "right"},
                        {"name": "updated", "label": "更新时间", "field": "updated", "align": "left"},
                    ],
                    rows=[], row_key="id", pagination=5,
                ).classes("w-full")

    # ---------- 持仓 ----------
    def build_positions(self) -> None:
        with ui.row().classes("w-full items-center gap-2"):
            ui.button("手动交易", icon="add", on_click=lambda: self.open_trade()) \
                .props("unelevated dense")
            ui.label("选中任意持仓行 → 查看实时行情并直接平仓 / 买入 / 卖出").classes("text-xs kd-mut")
        self.pos_table = ui.table(
            columns=[
                {"name": "ticker", "label": "市场", "field": "ticker", "align": "left", "sortable": True},
                {"name": "side", "label": "方向", "field": "side", "align": "center"},
                {"name": "qty", "label": "数量", "field": "qty", "align": "right"},
                {"name": "exposure", "label": "敞口", "field": "exposure", "align": "right"},
                {"name": "pnl", "label": "已实现盈亏", "field": "pnl", "align": "right"},
                {"name": "resting", "label": "在挂", "field": "resting", "align": "right"},
                {"name": "shard", "label": "shard", "field": "shard", "align": "center"},
                {"name": "act", "label": "操作", "field": "act", "align": "center"},
            ],
            rows=[], row_key="id", pagination=12, selection="single",
            on_select=self.on_pos_select,
        ).classes("w-full")
        self.pos_table.add_slot("body-cell-ticker", '''
            <q-td :props="props">
                <span class="cursor-pointer" @click="e => e.target.closest('tr')
                    .querySelector('.q-checkbox').click()">{{ props.value }}</span>
            </q-td>
        ''')
        self.pos_table.add_slot("body-cell-act", '''
            <q-td :props="props" class="text-center">
                <q-btn dense flat size="sm" color="primary" label="交易"
                    @click="e => e.target.closest('tr').querySelector('.q-checkbox').click()" />
            </q-td>
        ''')

    def on_pos_select(self, e) -> None:
        if not e.selection:
            return
        row = e.selection[0]
        self.pos_table.selected = []
        p = self.pos_by_ticker.get(row.get("id"))
        if not p:
            return
        leg = "yes" if p["yes_qty"] > 0 else "no"
        qty = int(p["yes_qty"] or p["no_qty"])
        asyncio.create_task(self.open_trade(p["ticker"], leg=leg, qty=qty))

    # ---------- 订单 ----------
    def build_orders(self) -> None:
        with ui.row().classes("w-full items-center gap-2"):
            self.status_toggle = ui.toggle(
                {"all": "全部", "resting": "在挂", "executed": "已成交", "canceled": "已撤"},
                value="resting",
                on_change=self._on_status_change,
            ).props("dense")
            self.ord_ticker_input = ui.input("市场 ticker 过滤", placeholder="留空 = 全部") \
                .classes("w-64").props("dense outlined clearable")
            ui.button("查询", icon="search", on_click=self._on_order_query).props("dense flat")
            ui.label("选中在挂订单行（左侧单选）即可撤单").classes("text-xs kd-mut")
        self.order_table = ui.table(
            columns=[
                {"name": "time", "label": "时间", "field": "time", "align": "left"},
                {"name": "ticker", "label": "市场", "field": "ticker", "align": "left"},
                {"name": "side", "label": "方向", "field": "side", "align": "center"},
                {"name": "price", "label": "价格", "field": "price", "align": "right"},
                {"name": "count", "label": "数量", "field": "count", "align": "right"},
                {"name": "filled", "label": "成交", "field": "filled", "align": "right"},
                {"name": "remaining", "label": "剩余", "field": "remaining", "align": "right"},
                {"name": "status", "label": "状态", "field": "status", "align": "center"},
                {"name": "shard", "label": "shard", "field": "shard", "align": "center"},
                {"name": "act", "label": "操作", "field": "act", "align": "center"},
            ],
            rows=[], row_key="id", pagination=15, selection="single",
            on_select=self.on_order_select,
        ).classes("w-full")
        self.order_table.add_slot("body-cell-ticker", '''
            <q-td :props="props">
                <span class="cursor-pointer" @click="e => e.target.closest('tr')
                    .querySelector('.q-checkbox').click()">{{ props.value }}</span>
            </q-td>
        ''')
        self.order_table.add_slot("body-cell-act", '''
            <q-td :props="props" class="text-center">
                <q-btn dense flat size="sm" color="primary" label="详情"
                    @click="e => e.target.closest('tr').querySelector('.q-checkbox').click()" />
            </q-td>
        ''')

    async def _on_status_change(self, e) -> None:
        v = e.value
        self.status_filter = None if v == "all" else v
        await self.refresh_orders()

    async def _on_order_query(self) -> None:
        self.orders_ticker = (self.ord_ticker_input.value or "").strip()
        await self.refresh_orders()

    def on_order_select(self, e) -> None:
        if not e.selection:
            return
        row = e.selection[0]
        self.order_table.selected = []
        asyncio.create_task(self.open_order(row.get("id")))

    # ---------- 成交 ----------
    def build_fills(self) -> None:
        with ui.row().classes("w-full items-center gap-2"):
            self.fill_ticker_input = ui.input("市场 ticker 过滤", placeholder="留空 = 全部") \
                .classes("w-64").props("dense outlined clearable")
            self.fill_days_select = ui.select(
                {3: "近 3 天", 7: "近 7 天", 30: "近 30 天", 0: "不限"},
                value=3, label="时间范围",
            ).classes("w-36")
            ui.button("查询", icon="search", on_click=self._on_fill_query).props("dense flat")
        self.fill_table = ui.table(
            columns=[
                {"name": "time", "label": "时间", "field": "time", "align": "left"},
                {"name": "ticker", "label": "市场", "field": "ticker", "align": "left"},
                {"name": "action", "label": "动作", "field": "action", "align": "center"},
                {"name": "side", "label": "方向", "field": "side", "align": "center"},
                {"name": "price", "label": "成交价", "field": "price", "align": "right"},
                {"name": "count", "label": "数量", "field": "count", "align": "right"},
                {"name": "role", "label": "角色", "field": "role", "align": "center"},
                {"name": "order", "label": "订单", "field": "order", "align": "left"},
            ],
            rows=[], row_key="id", pagination=20,
        ).classes("w-full")

    async def _on_fill_query(self) -> None:
        self.fills_ticker = (self.fill_ticker_input.value or "").strip()
        days = self.fill_days_select.value
        self.fills_days = None if not days else int(days)
        await self.refresh_fills()

    # ---------- 子账户 / 分片 ----------
    def build_subaccounts(self) -> None:
        with ui.card().classes("w-full gap-2"):
            ui.label("子账户余额（含各 shard）").classes("font-semibold")
            self.sub_table = ui.table(
                columns=[
                    {"name": "shard", "label": "shard", "field": "shard", "align": "center"},
                    {"name": "sub", "label": "子账户", "field": "sub", "align": "center"},
                    {"name": "balance", "label": "余额", "field": "balance", "align": "right"},
                    {"name": "updated", "label": "更新时间", "field": "updated", "align": "left"},
                ],
                rows=[], row_key="id", pagination=10,
            ).classes("w-full")

        with ui.row().classes("w-full gap-3 items-start"):
            with ui.card().classes("flex-1 min-w-[380px] gap-2"):
                ui.label("同 shard 子账户转账").classes("font-semibold")
                with ui.row().classes("gap-2"):
                    self.t_from = ui.number("从子账户", value=0, min=0, precision=0).classes("w-28")
                    self.t_to = ui.number("到子账户", value=1, min=0, precision=0).classes("w-28")
                    self.t_shard = ui.number("shard（可空）", value=None, precision=0).classes("w-32")
                self.t_amt = ui.number("金额（美元）", value=10.0, min=0.01, step=1.0,
                                       precision=2).classes("w-44")
                ui.button("提交转账", icon="swap_horiz", on_click=self.do_transfer_sub) \
                    .props("unelevated")
                ui.label("amount_cents 为整数美分，由系统自动换算。").classes("text-xs kd-mut")

            with ui.card().classes("flex-1 min-w-[380px] gap-2"):
                ui.label("跨 shard 转账").classes("font-semibold")
                with ui.row().classes("gap-2"):
                    self.x_from = ui.number("源 shard", value=0, min=0, precision=0).classes("w-28")
                    self.x_to = ui.number("目标 shard", value=2, min=0, precision=0).classes("w-28")
                    self.x_amt = ui.number("金额（美元）", value=10.0, min=0.01, step=1.0,
                                           precision=2).classes("w-40")
                with ui.row().classes("gap-2"):
                    self.x_src = ui.select({"event_contract": "event_contract", "margined": "margined"},
                                           value="event_contract", label="源类型").classes("w-40")
                    self.x_dst = ui.select({"event_contract": "event_contract", "margined": "margined"},
                                           value="event_contract", label="目标类型").classes("w-40")
                ui.button("提交跨 shard 转账", icon="sync_alt", on_click=self.do_transfer_shard) \
                    .props("unelevated")
                ui.label("金额按 centicents（1 美元 = 10000）提交。").classes("text-xs kd-mut")

        with ui.card().classes("w-full gap-2"):
            with ui.row().classes("items-center gap-2"):
                ui.label("创建子账户").classes("font-semibold")
                self.new_sub_shard = ui.number("指定 shard（可空）", value=None, precision=0) \
                    .classes("w-44")
                ui.button("创建", icon="add", on_click=self.do_create_subaccount).props("dense")
                ui.space()
                ui.button("刷新转账记录", icon="refresh",
                          on_click=self.refresh_transfers).props("dense flat")
            self.tr_table = ui.table(
                columns=[
                    {"name": "time", "label": "时间", "field": "time", "align": "left"},
                    {"name": "amount", "label": "金额", "field": "amount", "align": "right"},
                    {"name": "from", "label": "从", "field": "from", "align": "center"},
                    {"name": "to", "label": "到", "field": "to", "align": "center"},
                ],
                rows=[], row_key="id", pagination=8,
            ).classes("w-full")

    # ---------- 设置 ----------
    def build_settings(self) -> None:
        with ui.card().classes("w-full max-w-[720px] gap-2"):
            ui.label("当前连接").classes("font-semibold")
            ui.label(f"环境：{ENV_LABEL[self.api.env]}　({self.api.base_url})").classes("text-sm")
            ui.label(f"Key ID：{store.mask_key(self.sess['key_id'])}").classes("text-sm font-mono")
            ui.label(f"私钥路径：{self.sess['pem_path']}").classes("text-xs kd-mut")
            with ui.row().classes("gap-2"):
                ui.button("测试连接", icon="wifi_tethering", on_click=self.test_conn).props("outline")
                ui.button("解绑并清除本地私钥", icon="delete_forever",
                          on_click=self.do_unbind).props("outline color=negative")

        with ui.card().classes("w-full max-w-[720px] gap-2"):
            ui.label("重新绑定私钥").classes("font-semibold")
            self.pem_name2 = ui.label("未选择文件").classes("text-xs kd-mut")
            ui.upload(label="上传新的 PEM", auto_upload=True, max_file_size=2_000_000,
                      on_upload=self.on_pem_upload2).props("accept=.pem,.key,.txt flat bordered")
            self.key_input2 = ui.input("API Key ID").classes("w-full").props("dense outlined")
            self.env_select2 = ui.select({"prod": "生产环境", "demo": "模拟环境"},
                                         value=self.api.env, label="环境").classes("w-48")
            ui.button("保存并连接", icon="save", on_click=self.do_rebind).props("unelevated")

    async def on_pem_upload2(self, e: events.UploadEventArguments) -> None:
        data = await e.file.read()
        try:
            text = data.decode("utf-8")
            store.validate_pem(text)
        except Exception as ex:
            ui.notify(f"PEM 解析失败：{ex}", type="negative")
            return
        self._pem_text = text
        self.pem_name2.set_text(f"已选择：{e.file.name}")
        ui.notify("私钥已载入", type="positive")

    async def do_rebind(self) -> None:
        await self._connect((self.key_input2.value or "").strip(), self.env_select2.value or "prod")

    async def test_conn(self) -> None:
        try:
            bal = await run.io_bound(self.api.balance)
            ui.notify(f"连接正常，可用余额 {usd(bal['available'])}", type="positive")
        except Exception as ex:
            ui.notify(f"连接失败：{ex}", type="negative")

    async def do_unbind(self) -> None:
        if not await self.confirm("确定解除绑定？本机保存的私钥文件将被删除。"):
            return
        store.clear_session()
        self.api, self.sess = None, None
        self.state = {"balance": {}, "positions": [], "orders": [], "fills": [], "subs": []}
        self.rebuild()
        ui.notify("已解绑并清除本地私钥", type="positive")

    # ================= 数据刷新 =================
    async def tick(self) -> None:
        if self.api and self.auto and not self.busy:
            await self.refresh_all()

    async def refresh_all(self, manual: bool = False) -> None:
        if not self.api or self.busy:
            return
        self.busy = True
        try:
            jobs = [
                run.io_bound(self.api.balance),
                run.io_bound(self.api.positions),
                run.io_bound(self.api.orders, self.status_filter, self.orders_ticker or None, None),
                run.io_bound(self.api.subaccount_balances),
            ]
            with_fills = manual or self.ticks % 2 == 0
            if with_fills:
                jobs.append(run.io_bound(self.api.fills, self.fills_ticker or None, self.fills_days))
            results = await asyncio.gather(*jobs, return_exceptions=True)

            errs = []

            def take(i: int, key: str) -> bool:
                r = results[i]
                if isinstance(r, Exception):
                    errs.append(f"{key}: {r}")
                    return False
                self.state[key] = r
                return True

            if take(0, "balance"):
                self.render_balance()
            if take(1, "positions"):
                self.render_positions()
            if take(2, "orders"):
                self.render_orders()
            if take(3, "subs"):
                self.render_subs()
            if with_fills and take(4, "fills"):
                self.render_fills()

            self.ticks += 1
            if errs:
                self.updated_label.set_text("部分同步失败")
                if manual:
                    ui.notify(errs[0], type="negative")
            else:
                self.updated_label.set_text(f"已同步 {datetime.now().strftime('%H:%M:%S')}")
        finally:
            self.busy = False

    async def refresh_orders(self) -> None:
        if not self.api:
            return
        try:
            rows = await run.io_bound(self.api.orders, self.status_filter,
                                      self.orders_ticker or None, None)
            self.state["orders"] = rows
            self.render_orders()
        except Exception as ex:
            ui.notify(f"订单查询失败：{ex}", type="negative")

    async def refresh_fills(self) -> None:
        if not self.api:
            return
        try:
            rows = await run.io_bound(self.api.fills, self.fills_ticker or None, self.fills_days)
            self.state["fills"] = rows
            self.render_fills()
        except Exception as ex:
            ui.notify(f"成交查询失败：{ex}", type="negative")

    async def refresh_transfers(self) -> None:
        if not self.api:
            return
        try:
            rows = await run.io_bound(self.api.subaccount_transfers, 50)
            self.tr_table.rows = [{
                "id": r.get("transfer_id") or f"tr-{i}",
                "time": fmt_ts(r.get("created_ts")),
                "amount": usd(r.get("amount")),
                "from": str(r.get("from_subaccount")),
                "to": str(r.get("to_subaccount")),
            } for i, r in enumerate(rows)]
            self.tr_table.update()
        except Exception as ex:
            ui.notify(f"转账记录查询失败：{ex}", type="negative")

    # ================= 渲染 =================
    def render_balance(self) -> None:
        b = self.state.get("balance") or {}
        self.c_avail.set_text(usd(b.get("available")))
        pos = self.state.get("positions") or []
        expo = sum(p.get("exposure") or 0 for p in pos)
        pnl = sum(p.get("realized_pnl") or 0 for p in pos)
        self.c_expo.set_text(usd(expo))
        self.c_pnl.set_text(usd(pnl, signed=True))
        resting = sum(1 for o in (self.state.get("orders") or [])
                      if (o.get("status") == "resting"))
        self.c_rest.set_text(str(resting))
        self.c_subs.set_text(str(len(self.state.get("subs") or [])))

        shards = b.get("shards") or []
        total = sum(s.get("balance") or 0 for s in shards)
        self.shard_table.rows = [{
            "id": str(s.get("exchange_index")),
            "shard": str(s.get("exchange_index")),
            "balance": usd(s.get("balance")),
            "pct": f"{(s.get('balance') or 0) / total * 100:.1f}%" if total else "—",
        } for s in shards]
        self.shard_table.update()

    def render_subs(self) -> None:
        subs = self.state.get("subs") or []
        rows = [{
            "id": f"{s.get('exchange_index')}-{s.get('subaccount')}",
            "shard": "—" if s.get("exchange_index") is None else str(s.get("exchange_index")),
            "sub": "—" if s.get("subaccount") is None else str(s.get("subaccount")),
            "balance": usd(s.get("balance")),
            "updated": fmt_ts(s.get("updated_ts")),
        } for s in subs]
        self.sub_table.rows = rows
        self.sub_table.update()
        self.sub_mini.rows = rows[:50]
        self.sub_mini.update()
        self.c_subs.set_text(str(len(subs)))

    def render_positions(self) -> None:
        positions = self.state.get("positions") or []
        self.pos_by_ticker = {p["ticker"]: p for p in positions}
        self.pos_table.rows = [{
            "id": p["ticker"],
            "ticker": p["ticker"],
            "side": "YES" if p["yes_qty"] > 0 else "NO",
            "qty": f"{int(p['yes_qty'] or p['no_qty'])}",
            "exposure": usd(p.get("exposure")),
            "pnl": usd(p.get("realized_pnl"), signed=True),
            "resting": str(p.get("resting") or 0),
            "shard": "—" if p.get("exchange_index") is None else str(p.get("exchange_index")),
        } for p in positions]
        self.pos_table.update()
        self.c_expo.set_text(usd(sum(p.get("exposure") or 0 for p in positions)))
        self.c_pnl.set_text(usd(sum(p.get("realized_pnl") or 0 for p in positions), signed=True))

    def render_orders(self) -> None:
        orders = self.state.get("orders") or []
        self.order_by_id = {o["order_id"]: o for o in orders if o.get("order_id")}
        self.order_table.rows = [{
            "id": o["order_id"],
            "time": fmt_ts(o.get("created")),
            "ticker": o.get("ticker") or "",
            "side": o.get("side_txt") or "",
            "price": usd(o.get("price")),
            "count": f"{(o.get('count') or 0):g}",
            "filled": f"{(o.get('filled') or 0):g}",
            "remaining": f"{(o.get('remaining') or 0):g}",
            "status": o.get("status") or "",
            "shard": "—" if o.get("exchange_index") is None else str(o.get("exchange_index")),
        } for o in orders]
        self.order_table.update()
        self.c_rest.set_text(str(sum(1 for o in orders if o.get("status") == "resting")))

    def render_fills(self) -> None:
        fills = self.state.get("fills") or []
        self.fill_table.rows = [{
            "id": f.get("fill_id") or f.get("trade_id") or f"f-{i}",
            "time": fmt_ts(f.get("created")),
            "ticker": f.get("ticker") or "",
            "action": "买入" if f.get("action") == "buy" else ("卖出" if f.get("action") == "sell" else "—"),
            "side": (f.get("side") or "—").upper(),
            "price": usd(f.get("price")),
            "count": f"{(f.get('count') or 0):g}",
            "role": "Taker" if f.get("is_taker") else "Maker",
            "order": (f.get("order_id") or "")[:18],
        } for i, f in enumerate(fills)]
        self.fill_table.update()

    # ================= 下单 / 平仓 =================
    async def confirm(self, text: str) -> bool:
        with ui.dialog() as d, ui.card().classes("w-[440px] gap-2"):
            ui.label("请确认").classes("font-semibold")
            ui.label(text).classes("text-sm whitespace-pre-line")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=lambda: d.submit(False)).props("flat")
                ui.button("确认", on_click=lambda: d.submit(True)).props("unelevated color=negative")
        result = bool(await d)
        d.delete()
        return result

    async def open_trade(self, ticker: str = "", leg: Optional[str] = None,
                         qty: Optional[int] = None) -> None:
        if not self.api:
            return
        self.trade_dlg.clear()
        quote = None
        qerr = ""
        if ticker:
            try:
                quote = await run.io_bound(self.api.quote, ticker)
            except Exception as ex:
                qerr = f"行情获取失败：{ex}"
        pos = self.pos_by_ticker.get(ticker)

        with self.trade_dlg, ui.card().classes("w-[820px] max-w-[96vw] gap-3"):
            with ui.row().classes("w-full items-center"):
                ui.label("下单 / 平仓").classes("text-lg font-semibold")
                ui.space()
                ui.button(icon="close", on_click=self.trade_dlg.close).props("flat round dense")

            self.q_ticker = ui.input("市场 ticker", value=ticker) \
                .classes("w-full").props("dense outlined")
            self.q_info = ui.label(qerr or (quote or {}).get("title") or
                                   "输入 ticker 后点击“刷新行情”").classes("text-xs kd-mut")
            self.q_line = ui.label("").classes("text-sm font-mono")
            self.q_pos = ui.label("").classes("text-sm")

            default_action = "sell_yes" if leg == "yes" else ("sell_no" if leg == "no" else "buy_yes")
            self.act_toggle = ui.toggle(dict(ACTION_LABEL), value=default_action) \
                .props("spread dense no-caps").classes("kd-actions")
            with ui.row().classes("w-full gap-3 items-end"):
                self.px_input = ui.number("价格（所选合约，美元）", value=0.5,
                                          min=0.01, max=0.99, step=0.01,
                                          precision=2).classes("w-52")
                self.qty_input = ui.number("数量（张）", value=qty or 1, min=1,
                                           precision=0).classes("w-36")
                self.sub_input = ui.number("子账户", value=0, min=0,
                                           precision=0).classes("w-32")
                self.tif_toggle = ui.toggle(
                    {"immediate_or_cancel": "IOC 立即成交", "good_till_canceled": "GTC 挂单"},
                    value="immediate_or_cancel").props("dense")
            with ui.row().classes("w-full gap-3 items-center"):
                self.post_only = ui.checkbox("只做 maker（post-only）")
                self.est_label = ui.label("").classes("text-sm kd-mut")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("刷新行情", icon="refresh", on_click=self.reload_quote).props("flat")
                ui.button("提交订单", icon="send", on_click=self.submit_trade).props("unelevated")

            self._set_quote(quote)
            self.act_toggle.on_value_change(lambda e: self._on_action_change(e.value))
            for el in (self.px_input, self.qty_input):
                el.on_value_change(lambda _: self._update_est())

        self._on_action_change(default_action)
        self._update_est()
        self.trade_dlg.open()

    def _set_quote(self, quote: Optional[dict]) -> None:
        self._quote = quote
        if not quote:
            self.q_line.set_text("")
            return
        p = self.pos_by_ticker.get(quote.get("ticker") or "")
        self.q_line.set_text(
            f"YES 买一 {usd(quote.get('yes_bid'))} / 卖一 {usd(quote.get('yes_ask'))}    "
            f"NO 买一 {usd(quote.get('no_bid'))} / 卖一 {usd(quote.get('no_ask'))}    "
            f"最新 {usd(quote.get('last'))}"
        )
        self.q_info.set_text(
            f"{quote.get('title') or ''}　状态：{quote.get('status') or '—'}"
            f"　收盘：{fmt_ts(quote.get('close_time'))}")
        if p:
            self.q_pos.set_text(
                f"当前持仓：YES {int(p['yes_qty'])} 张 / NO {int(p['no_qty'])} 张"
                f"　敞口 {usd(p.get('exposure'))}")
        else:
            self.q_pos.set_text("当前持仓：无")

    def _on_action_change(self, action: str) -> None:
        best = DeskAPI.best_price(self._quote or {}, action)
        if best is not None and 0.01 <= best <= 0.99:
            self.px_input.value = round(best, 2)
        self._update_est()

    def _update_est(self) -> None:
        try:
            count = int(self.qty_input.value or 0)
            price = float(self.px_input.value or 0)
            if count > 0 and price > 0:
                verb = "支出" if str(self.act_toggle.value).startswith("buy") else "收入"
                self.est_label.set_text(f"预计{verb} ≈ {usd(count * price)}（未含手续费）")
            else:
                self.est_label.set_text("")
        except (TypeError, ValueError):
            self.est_label.set_text("")

    async def reload_quote(self) -> None:
        ticker = (self.q_ticker.value or "").strip()
        if not ticker:
            ui.notify("请输入 ticker", type="warning")
            return
        try:
            quote = await run.io_bound(self.api.quote, ticker)
        except Exception as ex:
            ui.notify(f"行情获取失败：{ex}", type="negative")
            return
        self._set_quote(quote)
        self._on_action_change(self.act_toggle.value)

    async def submit_trade(self) -> None:
        ticker = (self.q_ticker.value or "").strip()
        action = self.act_toggle.value
        price = _num(self.px_input.value)
        count = _num(self.qty_input.value)
        if not ticker:
            ui.notify("请输入市场 ticker", type="warning")
            return
        if price is None or not (0.01 <= price <= 0.99):
            ui.notify("价格需在 0.01 ~ 0.99 之间", type="warning")
            return
        if not count or count < 1:
            ui.notify("数量至少 1 张", type="warning")
            return
        count = int(count)
        if self.post_only.value and self.tif_toggle.value != "good_till_canceled":
            ui.notify("post-only 订单必须使用 GTC", type="warning")
            return
        if action.startswith("sell"):
            p = self.pos_by_ticker.get(ticker)
            held = 0
            if p:
                held = int(p["yes_qty"] if action == "sell_yes" else p["no_qty"])
            if count > held:
                ui.notify(f"可卖数量不足：当前持仓 {held} 张", type="warning")
                return
        side, yes_px = DeskAPI.to_book(action, price)
        summary = (f"{ACTION_LABEL[action]}　{ticker}\n"
                   f"价格 {price:.2f}　数量 {count} 张　"
                   f"金额 ≈ {usd(count * price)}\n"
                   f"TIF {self.tif_toggle.value}　子账户 {int(self.sub_input.value or 0)}")
        if not await self.confirm(summary):
            return
        tif = self.tif_toggle.value
        post_only = bool(self.post_only.value) and tif == "good_till_canceled"
        sub = int(self.sub_input.value or 0)
        try:
            r = await run.io_bound(
                lambda: self.api.place(ticker, side, yes_px, count, tif, post_only, sub))
            filled = _num(r.get("fill_count_fp") or r.get("fill_count")) or 0.0
            ui.notify(f"已提交：成交 {filled:g}/{count} 张，状态 {r.get('status') or '—'}",
                      type="positive")
            self.trade_dlg.close()
            await self.refresh_all(manual=True)
        except Exception as ex:
            ui.notify(f"下单失败：{ex}", type="negative")

    # ================= 订单详情 / 撤单 =================
    async def open_order(self, order_id: Optional[str]) -> None:
        o = self.order_by_id.get(order_id)
        if not o:
            return
        self.order_dlg.clear()
        with self.order_dlg, ui.card().classes("w-[620px] max-w-[96vw] gap-2"):
            with ui.row().classes("w-full items-center"):
                ui.label("订单详情").classes("text-lg font-semibold")
                ui.space()
                ui.button(icon="close", on_click=self.order_dlg.close).props("flat round dense")
            for label, value in (
                ("订单号", o.get("order_id")),
                ("client_order_id", o.get("client_order_id")),
                ("市场", o.get("ticker")),
                ("方向", o.get("side_txt")),
                ("价格", usd(o.get("price"))),
                ("数量", f"{(o.get('count') or 0):g}"),
                ("已成交 / 剩余", f"{(o.get('filled') or 0):g} / {(o.get('remaining') or 0):g}"),
                ("状态", o.get("status")),
                ("TIF", o.get("tif")),
                ("手续费", usd(o.get("fees"))),
                ("shard", o.get("exchange_index")),
                ("创建时间", fmt_ts(o.get("created"))),
            ):
                with ui.row().classes("w-full justify-between"):
                    ui.label(label).classes("text-xs kd-mut")
                    ui.label("—" if value is None else str(value)).classes("text-sm font-mono")
            if o.get("status") == "resting":
                with ui.row().classes("w-full justify-end"):
                    ui.button("撤单", icon="cancel",
                              on_click=lambda: asyncio.create_task(
                                  self.cancel_order(o))).props("unelevated color=negative")
        self.order_dlg.open()

    async def cancel_order(self, o: dict) -> None:
        if not await self.confirm(f"撤销订单？\n{o.get('ticker')}  {o.get('side_txt')}  "
                                  f"剩余 {(o.get('remaining') or 0):g} 张"):
            return
        try:
            await run.io_bound(self.api.cancel, o.get("order_id"), o.get("ticker"))
            ui.notify("已撤单", type="positive")
            self.order_dlg.close()
            await self.refresh_all(manual=True)
        except Exception as ex:
            ui.notify(f"撤单失败：{ex}", type="negative")

    # ================= 资金操作 =================
    async def do_transfer_sub(self) -> None:
        try:
            frm = int(self.t_from.value or 0)
            to = int(self.t_to.value or 0)
            amt = float(self.t_amt.value or 0)
            shard = self.t_shard.value
            shard = None if shard is None or shard == "" else int(shard)
        except (TypeError, ValueError):
            ui.notify("转账参数不合法", type="warning")
            return
        if amt <= 0:
            ui.notify("金额必须大于 0", type="warning")
            return
        if frm == to and shard is None:
            ui.notify("同 shard 内不能转给同一子账户", type="warning")
            return
        text = f"子账户 {frm} → {to}，金额 {usd(amt)}" + (f"，shard {shard}" if shard is not None else "")
        if not await self.confirm(text):
            return
        try:
            await run.io_bound(lambda: self.api.transfer_subaccounts(frm, to, amt, shard))
            ui.notify("转账已提交", type="positive")
            await self.refresh_all(manual=True)
            await self.refresh_transfers()
        except Exception as ex:
            ui.notify(f"转账失败：{ex}", type="negative")

    async def do_transfer_shard(self) -> None:
        try:
            frm = int(self.x_from.value or 0)
            to = int(self.x_to.value or 0)
            amt = float(self.x_amt.value or 0)
        except (TypeError, ValueError):
            ui.notify("转账参数不合法", type="warning")
            return
        if frm == to:
            ui.notify("源 shard 与目标 shard 不能相同", type="warning")
            return
        if amt <= 0:
            ui.notify("金额必须大于 0", type="warning")
            return
        src, dst = self.x_src.value, self.x_dst.value
        if not await self.confirm(f"跨 shard 转账：shard {frm} → {to}\n金额 {usd(amt)}"
                                  f"（{src} → {dst}）"):
            return
        try:
            await run.io_bound(lambda: self.api.transfer_across_shards(
                frm, to, amt, src, dst))
            ui.notify("跨 shard 转账已提交", type="positive")
            await self.refresh_all(manual=True)
        except Exception as ex:
            ui.notify(f"跨 shard 转账失败：{ex}", type="negative")

    async def do_create_subaccount(self) -> None:
        shard = self.new_sub_shard.value
        shard = None if shard is None or shard == "" else int(shard)
        if not await self.confirm("创建一个新的子账户？" + (f"（shard {shard}）" if shard is not None else "")):
            return
        try:
            r = await run.io_bound(lambda: self.api.create_subaccount(shard))
            ui.notify(f"子账户已创建：{r}", type="positive")
            await self.refresh_all(manual=True)
        except Exception as ex:
            ui.notify(f"创建子账户失败：{ex}", type="negative")


@ui.page("/")
async def _index() -> None:
    await DeskUI().start()


def _pick_port(start: int = 8090) -> int:
    """优先用环境变量 KALSHI_DESK_PORT，否则从 8090 起找一个空闲端口。"""
    env = os.environ.get("KALSHI_DESK_PORT", "").strip()
    if env:
        return int(env)
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main() -> None:
    ui.run(title="Kalshi 交易工作台", host="0.0.0.0", port=_pick_port(), dark=True,
           reload=False, show=True)
