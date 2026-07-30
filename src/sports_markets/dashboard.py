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


class DashboardView:
    def __init__(self, state_path, refresh_seconds):
        self.state_path = state_path
        self.mid_cents_history = []
        self._build()
        ui.timer(refresh_seconds, self.refresh)

    def _build(self):
        ui.query("body").style(f"background-color:{BACKGROUND}")
        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-3"):
            with ui.row().classes("items-baseline gap-3 w-full"):
                self.ticker_label = ui.label("--").style(
                    f"font-size:20px;font-weight:700;color:{TEXT}")
                self.mode_chip = ui.label("DRY RUN").style(
                    f"font-size:11px;padding:2px 9px;border:1px solid {LINE};"
                    f"border-radius:3px;color:{DIM}")
                self.env_chip = ui.label("").style(
                    f"font-size:11px;color:{DIM}")
                self.stale_chip = ui.label("STALE").style(
                    f"font-size:11px;padding:2px 9px;border:1px solid {ASK_RED};"
                    f"border-radius:3px;color:{ASK_RED}")
                self.stale_chip.visible = False
                self.countdown_label = ui.label("").classes("ml-auto").style(
                    f"font-size:12px;color:{DIM}")

            with ui.row().classes("w-full gap-2"):
                self.mid_cents_stat = self._stat_card("Mid")
                self.spread_stat = self._stat_card("Spread")
                self.position_stat = self._stat_card("Position")
                self.pnl_stat = self._stat_card("Session P&L")
                self.fair_stat = self._stat_card("Ext Fair")
                self.fills_stat = self._stat_card("Fills")

            with ui.card().style(
                    f"background-color:{PANEL};border:1px solid {LINE};"
                    f"width:100%"):
                ui.label("BOOK · YOUR QUOTES PINNED").style(
                    f"font-size:10px;letter-spacing:.18em;color:{DIM}")
                self.ladder_container = ui.column().classes("gap-0 w-full")

            with ui.card().style(
                    f"background-color:{PANEL};border:1px solid {LINE};"
                    f"width:100%"):
                ui.label("MID, THIS SESSION").style(
                    f"font-size:10px;letter-spacing:.18em;color:{DIM}")
                self.mid_chart = ui.echart({
                    "grid": {"left": 40, "right": 12, "top": 10, "bottom": 20},
                    "xAxis": {"type": "category", "show": False},
                    "yAxis": {"type": "value", "scale": True,
                              "axisLabel": {"color": DIM}},
                    "series": [{"type": "line", "showSymbol": False,
                                "lineStyle": {"color": AMBER}, "data": []}],
                }).style("height:120px;width:100%")

            with ui.card().style(
                    f"background-color:{PANEL};border:1px solid {LINE};"
                    f"width:100%"):
                ui.label("EVENT LOG").style(
                    f"font-size:10px;letter-spacing:.18em;color:{DIM}")
                self.log_view = ui.log().classes("w-full h-40").style(
                    f"font-size:11px;color:{DIM};background-color:{BACKGROUND}")
                self._logged_lines = 0

    def _stat_card(self, title):
        with ui.card().style(
                f"background-color:{PANEL};border:1px solid {LINE};"
                f"padding:9px 12px;min-width:120px"):
            ui.label(title.upper()).style(
                f"font-size:10px;letter-spacing:.18em;color:{DIM}")
            value = ui.label("--").style(
                f"font-size:19px;font-weight:600;color:{TEXT}")
        return value

    def _ladder_row(self, price_cents, contracts,
                    maximum_contracts, side_color, is_own_order):
        with ui.row().classes("items-center gap-2 w-full").style(
                "padding:1px 4px;position:relative"):
            if contracts is not None and maximum_contracts > 0:
                bar_width = min(100, 100 * contracts / maximum_contracts)
                ui.element("div").style(
                    f"position:absolute;right:0;top:2px;bottom:2px;"
                    f"width:{bar_width:.1f}%;background-color:{side_color};"
                    f"opacity:0.13;pointer-events:none")
            ui.label(f"{price_cents}c").style(
                f"width:44px;color:{side_color};z-index:1")
            if is_own_order:
                ui.label(f"YOU {is_own_order[1]}").style(
                    f"font-size:9px;color:{BACKGROUND};background-color:{AMBER};"
                    f"border-radius:2px;padding:0 4px;font-weight:700;z-index:1")
            ui.label("" if contracts is None else f"{round(contracts)}").classes(
                "ml-auto").style(f"color:{DIM};z-index:1")

    def refresh(self):
        state = read_state(self.state_path)
        if not state:
            return
        served_at = time.time()
        is_stale = served_at - state.get("published_timestamp", 0) > STALE_AFTER_SECONDS
        self.stale_chip.visible = is_stale

        self.ticker_label.text = state.get("ticker", "--")
        self.env_chip.text = state.get("env", "")
        self.mode_chip.text = "LIVE" if state.get("live") else "DRY RUN"
        self.mode_chip.style(
            f"font-size:11px;padding:2px 9px;border:1px solid "
            f"{AMBER if state.get('live') else LINE};border-radius:3px;"
            f"color:{AMBER if state.get('live') else DIM}")

        stop_ts = state.get("stop_ts")
        if stop_ts:
            remaining = max(0, stop_ts - served_at)
            self.countdown_label.text = (
                f"stops in {int(remaining // 60)}m {int(remaining % 60)}s")

        self.mid_cents_stat.text = cents(state.get("mid_cents")) + "c"
        spread = state.get("spread_cents")
        self.spread_stat.text = ("--" if spread is None else f"{spread}") + "c"
        position = state.get("position", 0)
        self.position_stat.text = f"{position:+.0f}"
        self.position_stat.style(f"font-size:19px;font-weight:600;color:{AMBER}")
        pnl_dollars = state.get("pnl_dollars", 0.0)
        self.pnl_stat.text = f"${pnl_dollars:+.2f}"
        self.pnl_stat.style(
            f"font-size:19px;font-weight:600;"
            f"color:{BID_GREEN if pnl_dollars > 0 else ASK_RED if pnl_dollars < 0 else TEXT}")
        self.fills_stat.text = str(state.get("fill_count", 0))

        fair = state.get("fair_cents")
        if fair is None:
            self.fair_stat.text = "--"
            self.fair_stat.style(
                f"font-size:19px;font-weight:600;color:{DIM}")
        else:
            age = state.get("fair_age_seconds")
            age_text = "" if age is None else f" · {round(age)}s"
            self.fair_stat.text = f"{fair:.1f}c{age_text}"
            self.fair_stat.style(
                f"font-size:19px;font-weight:600;color:{AMBER}")

        self._render_ladder(state)

        if state.get("mid_cents") is not None:
            self.mid_cents_history.append(state["mid_cents"])
            self.mid_cents_history = self.mid_cents_history[-600:]
            self.mid_chart.options["series"][0]["data"] = self.mid_cents_history
            self.mid_chart.options["xAxis"]["data"] = list(
                range(len(self.mid_cents_history)))
            self.mid_chart.update()

        log_lines = state.get("log", [])
        for line in log_lines[self._logged_lines:]:
            self.log_view.push(line)
        self._logged_lines = len(log_lines)

    def _render_ladder(self, state):
        self.ladder_container.clear()
        book = state.get("book")
        resting = state.get("resting", {})
        if not book:
            with self.ladder_container:
                ui.label("waiting for book").style(f"color:{DIM}")
            return
        level_contracts = [level[1] for level in book["bids"] + book["asks"]]
        maximum_contracts = max(level_contracts) if level_contracts else 1
        resting_bid = resting.get("bid")
        resting_ask = resting.get("ask")
        with self.ladder_container:
            for price_cents, contracts in reversed(book["asks"]):
                own_ask = (resting_ask if resting_ask
                           and resting_ask[0] == price_cents else None)
                self._ladder_row(price_cents, contracts, maximum_contracts,
                                 ASK_RED, own_ask)
            ui.label(f"spread {state.get('spread_cents')}c · "
                     f"micro {cents(state.get('microprice_cents'))}c").style(
                f"text-align:center;color:{DIM};font-size:10px;"
                f"border-top:1px dashed {LINE};border-bottom:1px dashed {LINE};"
                f"padding:3px 0;width:100%")
            for price_cents, contracts in book["bids"]:
                own_bid = (resting_bid if resting_bid
                           and resting_bid[0] == price_cents else None)
                self._ladder_row(price_cents, contracts, maximum_contracts,
                                 BID_GREEN, own_bid)


def main():
    parser = argparse.ArgumentParser(
        description="NiceGUI dashboard for the market maker. Reads the state "
                    "file the maker writes each tick; run as a separate "
                    "process from market_maker.py.")
    parser.add_argument("--state-file", default=DEFAULT_STATE_PATH)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--refresh", type=float, default=1.0)
    args = parser.parse_args()
    DashboardView(args.state_file, args.refresh)
    ui.run(host="127.0.0.1", port=args.port, reload=False, show=False,
           title="kalshi mm", dark=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()