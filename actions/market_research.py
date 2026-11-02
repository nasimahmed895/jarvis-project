"""Voice-command surface for the crypto market research module.

Auto-discovered by `core.action_loader.discover_actions` through its TOOL
dict. No edit to `main.py` is required.

Spoken examples the model will route here:
    "Jarvis, research Bitcoin."
    "Analyze Ethereum on the four-hour timeframe."
    "Give me a market check on BTC."

The handler is intentionally thin: it validates arguments, calls the
`market.pipeline.research_asset` function, and formats a concise spoken
summary. The detailed report lives on disk — the handler returns its path
so JARVIS can read it aloud for the user.
"""
from __future__ import annotations

import threading


def _run_research(parameters, player=None, **_):
    # Deferred import so a missing optional subdependency inside market/
    # cannot prevent action discovery of the rest of the actions.
    try:
        from market.pipeline import research_asset
    except Exception as e:  # noqa: BLE001
        return f"Research module not loaded: {type(e).__name__}: {e}"

    asset = str((parameters or {}).get("asset", "")).strip().upper()
    horizon = str((parameters or {}).get("horizon", "both")).strip().lower()
    if horizon not in ("intraday", "swing", "both"):
        horizon = "both"
    if not asset:
        return "Please tell me which asset to research (for example, Bitcoin or Ethereum)."

    # Research can be slow — the action loader runs this in an executor so
    # the voice session stays responsive. We still use a watchdog so a
    # provider stall does not block the executor forever.
    out: dict = {}

    def _work() -> None:
        try:
            out["result"] = research_asset(asset, horizon=horizon)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout=90.0)
    if t.is_alive():
        return f"Research for {asset} timed out. Please try again in a moment."

    if "error" in out:
        return f"Research failed for {asset}: {out['error']}"

    r = out["result"]
    adv = getattr(r, "advisory", None)
    if adv is not None and adv.voice_summary_bn:
        # Primary: spoken Bengali response from the advisory composer.
        spoken = adv.voice_summary_bn
    else:
        # Fallback: English headline if advisory unavailable (should not
        # happen in normal operation; kept so the action never silently
        # fails to produce a sentence).
        spoken = (
            f"{r.asset} research complete. Conclusion: {r.conclusion} on the "
            f"{r.headline_timeframe} timeframe. Reference price "
            f"{r.reference_price:.4f}. Heuristic confidence {r.heuristic_confidence:.2f}."
        )
    if r.report_markdown_path:
        spoken += f" বিস্তারিত report: {r.report_markdown_path}."
    if r.warnings:
        spoken += f" Warnings: {'; '.join(r.warnings[:2])}."
    return spoken


TOOL = {
    "name": "market_research",
    "description": (
        "Perform cryptocurrency market research for a given asset. "
        "Fetches public spot-market OHLCV from the configured exchange, "
        "runs multi-timeframe analysis, applies the baseline strategy, "
        "produces a LONG BIAS / SHORT BIAS / NO TRADE conclusion, and "
        "saves a detailed Markdown + JSON report. Use when the user says "
        "'research <asset>', 'analyze <asset>', 'market check on <asset>'. "
        "Does NOT place orders or enable live trading."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "asset": {
                "type": "STRING",
                "description": "Base-asset symbol (e.g. BTC, ETH, SOL).",
            },
            "horizon": {
                "type": "STRING",
                "description": (
                    "'intraday' | 'swing' | 'both' (default). "
                    "'intraday' uses 1m-1h timeframes; 'swing' uses 4h-1d."
                ),
            },
        },
        "required": ["asset"],
    },
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
    "handler": _run_research,
}
