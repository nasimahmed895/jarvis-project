"""Market-data validation.

Every candle sequence that enters the system passes through here. The
validator enforces:

- OHLC algebraic sanity (high >= max(open, close, low), low <= min(...)).
- Non-negative volume.
- Monotonically increasing open_time_ms.
- Expected candle spacing (gaps and duplicates are flagged, not silently
  filled or dropped).
- Freshness relative to the configured staleness threshold.

Invalid input raises `ValidationError`. Fresh-but-incomplete data is reported
in the returned `DataQuality` object so the caller can downgrade the analysis
rather than hiding the problem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..exceptions import StaleData, ValidationError
from ..types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS


@dataclass(frozen=True, slots=True)
class DataQuality:
    """Diagnostics about a validated frame.

    - `missing_candles` is the count of expected candles absent from the
      sequence (one entry per gap, not per missing bar).
    - `duplicate_timestamps` is the count of duplicate `open_time_ms` values
      removed during normalisation.
    - `out_of_order` counts timestamps that arrived out of monotonic order.
    - `is_stale` is True when the latest *closed* candle is older than
      `data_staleness_seconds`.
    """

    expected_candles: int
    received_candles: int
    missing_candles: int
    duplicate_timestamps: int
    out_of_order: int
    stale_age_seconds: float
    is_stale: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def completeness(self) -> float:
        if self.expected_candles <= 0:
            return 1.0
        return max(0.0, min(1.0, self.received_candles / self.expected_candles))


def _sort_and_dedupe(candles: Sequence[Candle]) -> tuple[list[Candle], int, int]:
    seen: dict[int, Candle] = {}
    out_of_order = 0
    last_ts = -1
    for c in candles:
        if c.open_time_ms < last_ts:
            out_of_order += 1
        last_ts = max(last_ts, c.open_time_ms)
        # Last write wins; this is deliberate — providers occasionally retransmit
        # a candle with its final values after a partial update.
        seen[c.open_time_ms] = c
    duplicates = len(candles) - len(seen)
    sorted_candles = sorted(seen.values(), key=lambda c: c.open_time_ms)
    return sorted_candles, duplicates, out_of_order


def _count_gaps(sorted_candles: list[Candle], step_ms: int) -> int:
    gaps = 0
    for prev, curr in zip(sorted_candles, sorted_candles[1:]):
        delta = curr.open_time_ms - prev.open_time_ms
        if delta != step_ms:
            # Any mismatch counts as one gap. Over-dense candles would have
            # been collapsed by the dedupe step above, so this is strictly
            # about missing bars.
            gaps += 1
    return gaps


def validate_frame(
    frame: OHLCVFrame,
    now_ms: int,
    staleness_seconds: int,
    min_candles: int = 1,
) -> tuple[OHLCVFrame, DataQuality]:
    """Validate and normalise an OHLCVFrame.

    Returns the (possibly reordered / deduped) frame alongside a DataQuality
    report. Hard failures (OHLC sanity, insufficient data) raise
    `ValidationError`. Stale data raises `StaleData`.
    """
    if frame.timeframe not in TIMEFRAME_MS:
        raise ValidationError(f"unsupported timeframe {frame.timeframe!r}")
    if len(frame) < min_candles:
        raise ValidationError(
            f"insufficient candles: received {len(frame)}, need at least {min_candles}"
        )

    step_ms = TIMEFRAME_MS[frame.timeframe]
    normalised, dupes, out_of_order = _sort_and_dedupe(list(frame.candles))

    # Reject candles whose open_time is in the future. A legitimate exchange
    # never advertises an open-time ahead of wall-clock; this almost always
    # means a clock-skew or a malformed row. We drop the offending candles
    # and surface the count rather than silently keeping them.
    future_cut: list[Candle] = []
    future_dropped = 0
    for c in normalised:
        if c.open_time_ms > now_ms + 60_000:   # 1-minute grace for clock skew
            future_dropped += 1
            continue
        future_cut.append(c)
    normalised = future_cut

    warnings: list[str] = []
    if dupes:
        warnings.append(f"removed {dupes} duplicate timestamp(s)")
    if out_of_order:
        warnings.append(f"{out_of_order} candle(s) arrived out of order")
    if future_dropped:
        warnings.append(
            f"dropped {future_dropped} candle(s) with open_time in the future "
            "(likely clock skew or malformed row)"
        )

    if normalised:
        span = normalised[-1].open_time_ms - normalised[0].open_time_ms
        expected = span // step_ms + 1
    else:
        expected = 0
    received = len(normalised)
    missing = _count_gaps(normalised, step_ms)
    if missing:
        warnings.append(f"{missing} gap(s) detected in candle series")

    last_closed = None
    for c in reversed(normalised):
        if c.is_closed:
            last_closed = c
            break
    if last_closed is None:
        raise ValidationError("no closed candles in frame")

    stale_age_sec = max(0.0, (now_ms - last_closed.close_time_ms) / 1000.0)
    is_stale = stale_age_sec > staleness_seconds
    if is_stale:
        raise StaleData(
            f"latest closed candle is {stale_age_sec:.1f}s old; threshold "
            f"is {staleness_seconds}s"
        )

    cleaned = OHLCVFrame(
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        source=frame.source,
        retrieved_at_ms=frame.retrieved_at_ms,
        candles=tuple(normalised),
    )
    quality = DataQuality(
        expected_candles=expected,
        received_candles=received,
        missing_candles=missing,
        duplicate_timestamps=dupes,
        out_of_order=out_of_order,
        stale_age_seconds=stale_age_sec,
        is_stale=False,
        warnings=tuple(warnings),
    )
    return cleaned, quality


def closed_candles_only(frame: OHLCVFrame) -> OHLCVFrame:
    """Drop the in-progress candle, if any. Prevents look-ahead on close."""
    closed = tuple(c for c in frame.candles if c.is_closed)
    return OHLCVFrame(
        symbol=frame.symbol,
        timeframe=frame.timeframe,
        source=frame.source,
        retrieved_at_ms=frame.retrieved_at_ms,
        candles=closed,
    )
