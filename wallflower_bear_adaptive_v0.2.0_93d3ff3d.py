"""Wallflower Bear Market Adaptive v0.2.0 — Complete Strategy Code

Regime-adaptive strategy for bear markets with relaxed entry conditions.
Backtested: January 10, 2026 - June 15, 2026
Assets: BTCUSDT, ETHUSDT, SOLUSDT (4H timeframe)

Configuration:
- Starting Balance: $10,000
- Leverage: 2x
- Position Size: 3% per trade
- Max Concurrent Positions: 2

Signal Types:
1. Bear Short: Price within 2% of EMA21 + Volume < 10-period MA
2. Fear Long: RSI < 25 + Volume > 2x 20-period MA
3. Bull Long: EMA9 > EMA21 + Volume > 1.5x 20-period MA + RSI 40-60

Exit Parameters:
- Bear Short: 4% TP, 2% SL, max 8 candles
- Fear Long: 8% TP, 3% SL, max 4 candles
- Bull Long: 6% TP, 3% SL, max 8 candles
- Trailing Stop: 3% profit → move SL to breakeven, 5% profit → trail at 1.5%

Risk Management:
- Weekend Block: No entries Friday 20:00 - Saturday 08:00, Saturday 20:00 - Sunday 08:00
- Macro Override: 5%+ BTC move closes all positions immediately
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy, StrategyConfig


class WallflowerBearAdaptiveConfig(StrategyConfig, frozen=True):
    """Strategy configuration."""

    instrument_ids: tuple = ("BTCUSDT.BINANCE", "ETHUSDT.BINANCE", "SOLUSDT.BINANCE")
    bar_types: tuple = (
        "BTCUSDT.BINANCE-4-HOUR-LAST-EXTERNAL",
        "ETHUSDT.BINANCE-4-HOUR-LAST-EXTERNAL",
        "SOLUSDT.BINANCE-4-HOUR-LAST-EXTERNAL",
    )
    order_id_tag: str = "WBA"
    leverage: int = 2
    max_positions: int = 2
    position_size_pct: float = 0.03


class WallflowerBearAdaptiveStrategy(Strategy):
    """Regime-adaptive strategy for bear markets."""

    def __init__(self, config: WallflowerBearAdaptiveConfig):
        super().__init__(config)
        self._bar_history: Dict[str, list] = {}
        self._bar_counts: Dict[str, int] = {}
        self._indicators: Dict[str, dict] = {}
        self._regime: Dict[str, str] = {}  # inst_id -> "BEAR", "BULL", or "FLAT"
        self._position_info: Dict[str, Optional[PositionInfo]] = {}
        self._signal_pending: Dict[str, Optional[dict]] = {}

        # Per-instrument trade sizes
        self._trade_sizes = {
            "BTCUSDT.BINANCE": "0.001",
            "ETHUSDT.BINANCE": "0.001",
            "SOLUSDT.BINANCE": "0.01",
        }

    def on_start(self) -> None:
        """Initialize strategy on start."""
        self.log.info("WallflowerBearAdaptiveStrategy v0.2.0 starting")
        for bar_type_str in self.config.bar_types:
            bar_type = BarType.from_str(bar_type_str)
            self.subscribe_bars(bar_type)

    def on_bar(self, bar: Bar) -> None:
        """Process each bar."""
        inst_id = str(bar.bar_type.instrument_id)

        # Initialize tracking for new instrument
        if inst_id not in self._bar_history:
            self._bar_history[inst_id] = []
            self._bar_counts[inst_id] = 0
            self._regime[inst_id] = "FLAT"
            self._position_info[inst_id] = None
            self._signal_pending[inst_id] = None
            self._indicators[inst_id] = {
                "ema9": None,
                "ema21": None,
                "ema50": None,
                "rsi": None,
                "vol_ma10": None,
                "vol_ma20": None,
                "high_5d": None,
            }

        self._bar_counts[inst_id] += 1
        bar_count = self._bar_counts[inst_id]

        # Store bar data
        bar_dict = {
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
            "timestamp": int(bar.ts_event),
        }
        self._bar_history[inst_id].append(bar_dict)

        # Keep last 1000 bars
        if len(self._bar_history[inst_id]) > 1000:
            self._bar_history[inst_id] = self._bar_history[inst_id][-1000:]

        # Compute indicators after 50 bars
        if bar_count >= 50:
            self._compute_indicators(inst_id)
            self._detect_regime(inst_id)

        # Check exits first
        self._check_exit(inst_id, bar, bar_count)

        # Check macro override
        if self._check_macro_override(inst_id, bar):
            return

        # Check pending signal confirmation
        if self._signal_pending[inst_id] is not None:
            self._check_signal_confirmation(inst_id, bar, bar_count)

        # Check for new signals (with time filter)
        if (
            self._signal_pending[inst_id] is None
            and self._count_open() < self.config.max_positions
            and bar_count >= 50
            and not self._is_weekend_blocked(bar)
        ):
            self._check_signals(inst_id, bar, bar_count)

    def _is_weekend_blocked(self, bar: Bar) -> bool:
        """Check if current time is within weekend block window.

        Block entries 4 hours before/after 4H candles opening 00:00-04:00 UTC on Sat/Sun.
        This means: Friday 20:00 - Saturday 08:00 UTC, and Saturday 20:00 - Sunday 08:00 UTC.
        """
        ts_ns = int(bar.ts_event)
        ts_s = ts_ns // 1_000_000_000
        dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)

        # Friday 20:00 - Saturday 08:00 UTC
        if dt.weekday() == 4 and dt.hour >= 20:
            return True
        if dt.weekday() == 5 and dt.hour < 8:
            return True

        # Saturday 20:00 - Sunday 08:00 UTC
        if dt.weekday() == 5 and dt.hour >= 20:
            return True
        if dt.weekday() == 6 and dt.hour < 8:
            return True

        return False

    def _compute_indicators(self, inst_id: str) -> None:
        """Compute all technical indicators."""
        bars = self._bar_history[inst_id]
        n = len(bars)
        if n < 50:
            return

        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]
        highs = [b["high"] for b in bars]

        # EMA-9
        k9 = 2.0 / 10.0
        ema9 = closes[0]
        for c in closes[1:]:
            ema9 = c * k9 + ema9 * (1 - k9)
        self._indicators[inst_id]["ema9"] = ema9

        # EMA-21
        k21 = 2.0 / 22.0
        ema21 = closes[0]
        for c in closes[1:]:
            ema21 = c * k21 + ema21 * (1 - k21)
        self._indicators[inst_id]["ema21"] = ema21

        # EMA-50
        k50 = 2.0 / 51.0
        ema50 = closes[0]
        for c in closes[1:]:
            ema50 = c * k50 + ema50 * (1 - k50)
        self._indicators[inst_id]["ema50"] = ema50

        # RSI-14
        if n >= 14:
            deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
            gains = [max(d, 0) for d in deltas[-14:]]
            losses = [max(-d, 0) for d in deltas[-14:]]
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                self._indicators[inst_id]["rsi"] = 100 - (100 / (1 + rs))
            else:
                self._indicators[inst_id]["rsi"] = 100

        # Volume MA-10 (for bear short)
        if n >= 10:
            self._indicators[inst_id]["vol_ma10"] = sum(volumes[-10:]) / 10.0

        # Volume MA-20 (for fear long and bull long)
        if n >= 20:
            self._indicators[inst_id]["vol_ma20"] = sum(volumes[-20:]) / 20.0

        # 5-day high (30 4H candles)
        if n >= 30:
            self._indicators[inst_id]["high_5d"] = max(highs[-30:])

    def _detect_regime(self, inst_id: str) -> None:
        """Detect regime from EMA50 slope over last 2 days (12 4H candles)."""
        bars = self._bar_history[inst_id]
        n = len(bars)
        if n < 62:  # 50 + 12
            return

        # Compute EMA50 for last 12 candles
        closes = [b["close"] for b in bars]
        ema50_values = []
        for i in range(n - 12, n):
            start_idx = max(0, i - 49)
            subset = closes[start_idx : i + 1]
            if len(subset) < 50:
                continue
            k50 = 2.0 / 51.0
            ema = subset[0]
            for c in subset[1:]:
                ema = c * k50 + ema * (1 - k50)
            ema50_values.append(ema)

        if len(ema50_values) < 2:
            return

        # Check slope over last 2 values
        slope = (ema50_values[-1] - ema50_values[-2]) / ema50_values[-2]

        if slope < -0.002:  # Meaningful decline = BEAR
            self._regime[inst_id] = "BEAR"
        elif slope > 0.002:  # Meaningful rise = BULL
            self._regime[inst_id] = "BULL"
        else:
            self._regime[inst_id] = "FLAT"

    def _check_signals(self, inst_id: str, bar: Bar, bar_count: int) -> None:
        """Check for entry signals based on regime."""
        regime = self._regime.get(inst_id, "FLAT")
        if regime == "FLAT":
            return

        ind = self._indicators.get(inst_id, {})
        rsi = ind.get("rsi")
        vol_ma10 = ind.get("vol_ma10")
        vol_ma20 = ind.get("vol_ma20")
        ema21 = ind.get("ema21")
        ema9 = ind.get("ema9")
        high_5d = ind.get("high_5d")

        if rsi is None:
            return

        current_volume = float(bar.volume)
        current_price = float(bar.close)

        # Bear short signal (only in BEAR regime)
        if regime == "BEAR" and vol_ma10 is not None and ema21 is not None:
            # Price within 2% of EMA21 (bounce) AND volume < vol_ma10
            if abs(current_price - ema21) / ema21 <= 0.02 and current_volume < vol_ma10:
                self._signal_pending[inst_id] = {
                    "type": "bear_short",
                    "bar_count": bar_count,
                }
                return

        # Fear long signal (only in BEAR regime)
        if regime == "BEAR" and vol_ma20 is not None and high_5d is not None:
            # RSI < 25 AND volume > 2x vol_ma20
            if rsi < 25 and current_volume >= 2.0 * vol_ma20:
                self._signal_pending[inst_id] = {
                    "type": "fear_long",
                    "bar_count": bar_count,
                }
                return

        # Bull long signal (in BULL regime)
        if regime == "BULL":
            if ema9 and ema21 and ema9 > ema21:
                if vol_ma20 and current_volume >= 1.5 * vol_ma20:
                    if 40 <= rsi <= 60:
                        self._signal_pending[inst_id] = {
                            "type": "bull_long",
                            "bar_count": bar_count,
                        }

    def _check_signal_confirmation(
        self, inst_id: str, bar: Bar, bar_count: int
    ) -> None:
        """Confirm signal on next candle and submit entry."""
        signal = self._signal_pending[inst_id]
        if signal is None:
            return

        # Confirm immediately on next bar
        signal_type = signal["type"]
        entry_price = float(bar.close)

        # Set exit parameters based on signal type
        if signal_type == "bear_short":
            sl_price = entry_price * 1.02  # 2% SL
            tp_price = entry_price * 0.96  # 4% TP
            max_candles = 8
        elif signal_type == "fear_long":
            sl_price = entry_price * 0.97  # 3% SL
            tp_price = entry_price * 1.08  # 8% TP
            max_candles = 4
        elif signal_type == "bull_long":
            sl_price = entry_price * 0.97  # 3% SL
            tp_price = entry_price * 1.06  # 6% TP
            max_candles = 8
        else:
            self._signal_pending[inst_id] = None
            return

        side = "short" if signal_type == "bear_short" else "long"
        self._submit_entry(
            inst_id, side, entry_price, sl_price, tp_price, bar_count, max_candles
        )
        self._signal_pending[inst_id] = None

    def _submit_entry(
        self,
        inst_id: str,
        side: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        bar_count: int,
        max_candles: int,
    ) -> None:
        """Submit market order."""
        if self._position_info.get(inst_id) is not None:
            return

        instrument_id = InstrumentId.from_str(inst_id)
        order_side = OrderSide.BUY if side == "long" else OrderSide.SELL
        quantity = Quantity.from_str(self._trade_sizes[inst_id])

        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=order_side,
            quantity=quantity,
        )

        # Submit the order
        self.submit_order(order)

        # Track position
        self._position_info[inst_id] = PositionInfo(
            instrument_id=inst_id,
            side=side,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_bar=bar_count,
            max_candles=max_candles,
            highest_favorable_close=entry_price,
        )

    def _check_exit(self, inst_id: str, bar: Bar, bar_count: int) -> None:
        """Check SL/TP/max-candles exit with trailing stop logic."""
        pos = self._position_info.get(inst_id)
        if pos is None:
            return

        current_price = float(bar.close)
        bars_held = bar_count - pos.entry_bar

        # Calculate current profit percentage
        if pos.side == "long":
            profit_pct = (current_price - pos.entry_price) / pos.entry_price
            # Update highest favorable close for longs
            if current_price > pos.highest_favorable_close:
                pos.highest_favorable_close = current_price
        else:  # short
            profit_pct = (pos.entry_price - current_price) / pos.entry_price
            # Update highest favorable close for shorts (lowest price is best)
            if current_price < pos.highest_favorable_close:
                pos.highest_favorable_close = current_price

        # Trailing stop logic for winning trades
        if profit_pct >= 0.05:  # 5% profit: trail at 1.5% below highest favorable close
            if pos.side == "long":
                trailing_sl = pos.highest_favorable_close * 0.985
                if trailing_sl > pos.sl_price:
                    pos.sl_price = trailing_sl
            else:  # short
                trailing_sl = pos.highest_favorable_close * 1.015
                if trailing_sl < pos.sl_price:
                    pos.sl_price = trailing_sl
        elif profit_pct >= 0.03:  # 3% profit: move SL to breakeven
            if pos.side == "long" and pos.sl_price < pos.entry_price:
                pos.sl_price = pos.entry_price
            elif pos.side == "short" and pos.sl_price > pos.entry_price:
                pos.sl_price = pos.entry_price

        should_close = False

        # Check exit conditions
        if bars_held >= pos.max_candles:
            should_close = True
        elif pos.side == "long" and current_price <= pos.sl_price:
            should_close = True
        elif pos.side == "short" and current_price >= pos.sl_price:
            should_close = True
        elif pos.side == "long" and current_price >= pos.tp_price:
            should_close = True
        elif pos.side == "short" and current_price <= pos.tp_price:
            should_close = True

        if should_close:
            instrument_id = InstrumentId.from_str(inst_id)
            close_side = OrderSide.SELL if pos.side == "long" else OrderSide.BUY
            quantity = Quantity.from_str(self._trade_sizes[inst_id])

            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=close_side,
                quantity=quantity,
            )
            self.submit_order(order)
            self._position_info[inst_id] = None

    def _check_macro_override(self, inst_id: str, bar: Bar) -> bool:
        """Close all positions if BTC shows 5%+ move on single 4H candle."""
        if inst_id != "BTCUSDT.BINANCE":
            return False

        bars = self._bar_history[inst_id]
        if len(bars) < 2:
            return False

        prev_close = bars[-2]["close"]
        curr_close = float(bar.close)
        move_pct = abs(curr_close - prev_close) / prev_close

        if move_pct >= 0.05:
            # Close all positions
            for pos_inst_id in list(self._position_info.keys()):
                if self._position_info[pos_inst_id] is not None:
                    pos = self._position_info[pos_inst_id]
                    instrument_id = InstrumentId.from_str(pos_inst_id)
                    close_side = OrderSide.SELL if pos.side == "long" else OrderSide.BUY
                    quantity = Quantity.from_str(self._trade_sizes[pos_inst_id])

                    order = self.order_factory.market(
                        instrument_id=instrument_id,
                        order_side=close_side,
                        quantity=quantity,
                    )
                    self.submit_order(order)
                    self._position_info[pos_inst_id] = None
            return True

        return False

    def _count_open(self) -> int:
        """Count open positions."""
        return sum(1 for p in self._position_info.values() if p is not None)

    def on_stop(self) -> None:
        """Clean shutdown."""
        for inst_id_str in self.config.instrument_ids:
            instrument_id = InstrumentId.from_str(inst_id_str)
            self.cancel_all_orders(instrument_id)
            self.close_all_positions(instrument_id)


@dataclass
class PositionInfo:
    """Track position state for SL/TP/trailing stop management."""

    instrument_id: str
    side: str
    entry_price: float
    sl_price: float
    tp_price: float
    entry_bar: int = 0
    max_candles: int = 8
    highest_favorable_close: float = 0.0
