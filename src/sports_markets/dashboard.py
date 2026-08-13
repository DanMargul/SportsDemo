import argparse
import time

from nicegui import ui

from sports_markets.dashboard_state import DEFAULT_STATE_PATH, read_state

BACKGROUND = "#0D1520"
PANEL = "#131F2E"
LINE = "#1E3048"
TEXT = "#D8E2EE"
DIM = "#6C7F94"
AMBER = "#E8A33D"
BID_GREEN = "#3DBE8B"
ASK_RED = "#E4566B"

STALE_AFTER_SECONDS = 6.0


def cents(value, decimals=1):
    if value is None:
        return "--"
    return f"{value:.{decimals}f}"


class MarketCard:
    def __init__(self, ticker):
        self.ticker = ticker
        self.mid_cents_history = []
        with ui.card().style(f"background-color:{PANEL};border:1px solid "
                             f"{LINE};padding:12px;width:340px"):
            with ui.row().classes("items-baseline gap-2 w-full"):
                ui.label(ticker[-18:]).style(
                    f"font-size:12px;font-weight:700;color:{TEXT}")
                self.fills_label = ui.label("0 fills").classes("ml-auto").style(
                    f"font-size:11px;color:{DIM}")
            with ui.row().classes("gap-3 w-full").style("flex-wrap:wrap"):
                self.mid_label = self._kv("mid")
                self.pos_label = self._kv("pos")
                self.pnl_label = self._kv("p&l")
                self.fair_label = self._kv("fair")
            self.ladder = ui.column().classes("gap-0 w-full")
            self.chart = ui.echart({
                "grid": {"left": 34, "right": 8, "top": 6, "bottom": 6},
                "xAxis": {"type": "category", "show": False},
                "yAxis": {"type": "value", "scale": True,
                          "axisLabel": {"color": DIM, "fontSize": 9}},
                "series": [{"type": "line", "showSymbol": False,
                            "lineStyle": {"color": AMBER, "width": 1.3},
                            "data": []}],
            }).style("height:70px;width:100%")

    def _kv(self, name):
        with ui.column().classes("gap-0"):
            ui.label(name).style(f"font-size:9px;color:{DIM};"
                                 f"letter-spacing:.1em")
            value = ui.label("--").style(
                f"font-size:14px;font-weight:600;color:{TEXT};"
                f"font-variant-numeric:tabular-nums")
        return value

    def update(self, row):
        self.fills_label.text = f"{row.get('fill_count', 0)} fills"
        self.mid_label.text = f"{cents(row.get('mid_cents'))}c"
        position = row.get("position", 0)
        self.pos_label.text = f"{position:+.0f}"
        self.pos_label.style(f"font-size:14px;font-weight:600;color:{AMBER};"
                             f"font-variant-numeric:tabular-nums")
        pnl_dollars = row.get("pnl_dollars", 0.0)
        self.pnl_label.text = f"${pnl_dollars:+.2f}"
        self.pnl_label.style(
            f"font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;"
            f"color:{BID_GREEN if pnl_dollars > 0 else ASK_RED if pnl_dollars < 0 else TEXT}")
        fair = row.get("fair_cents")
        if fair is None:
            self.fair_label.text = "--"
            self.fair_label.style(f"font-size:14px;font-weight:600;color:{DIM}")
        else:
            age = row.get("fair_age_seconds")
            self.fair_label.text = (f"{fair:.1f}c"
                                    + ("" if age is None else f"·{round(age)}s"))
            self.fair_label.style(f"font-size:14px;font-weight:600;"
                                  f"color:{AMBER}")
        self._render_ladder(row)
        if row.get("mid_cents") is not None:
            self.mid_cents_history.append(row["mid_cents"])
            self.mid_cents_history = self.mid_cents_history[-300:]
            self.chart.options["series"][0]["data"] = self.mid_cents_history
            self.chart.options["xAxis"]["data"] = list(
                range(len(self.mid_cents_history)))
            self.chart.update()

    def _render_ladder(self, row):
        self.ladder.clear()
        book = row.get("book")
        resting = row.get("resting", {})
        if not book:
            with self.ladder:
                ui.label("waiting for book").style(f"color:{DIM}")
            return
        quantities = [level[1] for level in book["bids"] + book["asks"]]
        maximum_contracts = max(quantities) if quantities else 1
        resting_bid, resting_ask = resting.get("bid"), resting.get("ask")
        with self.ladder:
            for price_cents, contracts in reversed(book["asks"]):
                own_ask = (resting_ask if resting_ask
                           and resting_ask[0] == price_cents else None)
                self._ladder_row(price_cents, contracts, maximum_contracts,
                                 ASK_RED, own_ask)
            ui.label(f"spread {row.get('spread_cents')}c").style(
                f"text-align:center;color:{DIM};font-size:9px;"
                f"border-top:1px dashed {LINE};border-bottom:1px dashed {LINE};"
                f"padding:2px 0;width:100%")
            for price_cents, contracts in book["bids"]:
                own_bid = (resting_bid if resting_bid
                           and resting_bid[0] == price_cents else None)
                self._ladder_row(price_cents, contracts, maximum_contracts,
                                 BID_GREEN, own_bid)

    def _ladder_row(self, price_cents, contracts,
                    maximum_contracts, side_color, is_own_order):
        with ui.row().classes("items-center gap-2 w-full").style(
                "padding:0 4px;position:relative"):
            if contracts is not None and maximum_contracts > 0:
                width = min(100, 100 * contracts / maximum_contracts)
                ui.element("div").style(
                    f"position:absolute;right:0;top:1px;bottom:1px;"
                    f"width:{width:.1f}%;background-side_color:{side_color};"
                    f"opacity:0.13;pointer-events:none")
            ui.label(f"{price_cents}c").style(
                f"width:40px;side_color:{side_color};z-index:1;font-size:12px")
            if is_own_order:
                ui.label(f"YOU {is_own_order[1]}").style(
                    f"font-size:9px;side_color:{BACKGROUND};background-side_color:{AMBER};"
                    f"border-radius:2px;padding:0 4px;font-weight:700;z-index:1")
            ui.label("" if contracts is None else f"{round(contracts)}").classes(
                "ml-auto").style(f"side_color:{DIM};z-index:1;font-size:12px")


class DashboardView:
    def __init__(self, state_path, refresh_seconds):
        self.state_path = state_path
        self.cards = {}
        self._logged_lines = 0
        self._build()
        ui.timer(refresh_seconds, self.refresh)

    def _build(self):
        ui.query("body").style(f"background-color:{BACKGROUND}")
        with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-3"):
            with ui.row().classes("items-baseline gap-3 w-full"):
                ui.label("kalshi mm").style(
                    f"font-size:20px;font-weight:700;color:{TEXT}")
                self.mode_chip = ui.label("DRY RUN").style(
                    f"font-size:11px;padding:2px 9px;border:1px solid {LINE};"
                    f"border-radius:3px;color:{DIM}")
                self.env_chip = ui.label("").style(f"font-size:11px;color:{DIM}")
                self.stale_chip = ui.label("STALE").style(
                    f"font-size:11px;padding:2px 9px;border:1px solid {ASK_RED};"
                    f"border-radius:3px;color:{ASK_RED}")
                self.stale_chip.visible = False
                self.countdown = ui.label("").classes("ml-auto").style(
                    f"font-size:12px;color:{DIM}")
            with ui.row().classes("gap-2 w-full"):
                self.total_pnl = self._stat("Total P&L")
                self.market_count = self._stat("Markets")
                self.total_fills = self._stat("Fills")
            self.grid = ui.row().classes("gap-3 w-full").style("flex-wrap:wrap")
            with ui.card().style(f"background-color:{PANEL};border:1px solid "
                                 f"{LINE};width:100%"):
                ui.label("EVENT LOG").style(
                    f"font-size:10px;letter-spacing:.18em;color:{DIM}")
                self.log_view = ui.log().classes("w-full h-40").style(
                    f"font-size:11px;color:{DIM};background-color:{BACKGROUND}")

    def _stat(self, name):
        with ui.card().style(f"background-color:{PANEL};border:1px solid "
                             f"{LINE};padding:9px 12px;min-width:130px"):
            ui.label(name.upper()).style(
                f"font-size:10px;letter-spacing:.16em;color:{DIM}")
            value = ui.label("--").style(
                f"font-size:19px;font-weight:600;color:{TEXT};"
                f"font-variant-numeric:tabular-nums")
        return value

    def refresh(self):
        state = read_state(self.state_path)
        if not state:
            return
        served_at = time.time()
        self.stale_chip.visible = served_at - state.get("published_timestamp", 0) > STALE_AFTER_SECONDS
        live = state.get("live")
        self.mode_chip.text = "LIVE" if live else "DRY RUN"
        self.mode_chip.style(
            f"font-size:11px;padding:2px 9px;border:1px solid "
            f"{AMBER if live else LINE};border-radius:3px;"
            f"color:{AMBER if live else DIM}")
        self.env_chip.text = state.get("env", "")
        stop_ts = state.get("stop_ts")
        if stop_ts:
            left = max(0, stop_ts - served_at)
            self.countdown.text = f"stops in {int(left // 60)}m {int(left % 60)}s"
        total = state.get("total_pnl_dollars", 0.0)
        self.total_pnl.text = f"${total:+.2f}"
        self.total_pnl.style(
            f"font-size:19px;font-weight:600;font-variant-numeric:tabular-nums;"
            f"color:{BID_GREEN if total > 0 else ASK_RED if total < 0 else TEXT}")
        self.market_count.text = str(state.get("market_count", 0))
        self.total_fills.text = str(state.get("fill_count", 0))

        rows = {row["ticker"]: row for row in state.get("markets", [])}
        for ticker, row in rows.items():
            if ticker not in self.cards:
                with self.grid:
                    self.cards[ticker] = MarketCard(ticker)
            self.cards[ticker].update(row)

        log_lines = state.get("log", [])
        for line in log_lines[self._logged_lines:]:
            self.log_view.push(line)
        self._logged_lines = len(log_lines)


def main():
    parser = argparse.ArgumentParser(
        description="NiceGUI dashboard for the multi-market maker.")
    parser.add_argument("--state-file", default=DEFAULT_STATE_PATH)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--refresh", type=float, default=1.0)
    args = parser.parse_args()
    DashboardView(args.state_file, args.refresh)
    ui.run(host="127.0.0.1", port=args.port, reload=False, show=False,
           title="kalshi mm", dark=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()
