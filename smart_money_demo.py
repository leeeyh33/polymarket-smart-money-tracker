import json
import re
import requests
import time
from collections import defaultdict
from datetime import datetime

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DATA_TRADES_URL = "https://data-api.polymarket.com/trades"


def fetch_closed_markets(limit=10):
    """
    Fetch recently closed markets.
    Gamma currently accepts `order=id` here; `closed_time` returns 422.
    If ordering params are rejected again in the future, retry with the
    minimal closed-market filter instead of crashing.
    """
    page_size = min(limit, 500)
    markets = []

    for offset in range(0, limit, page_size):
        params = {
            "closed": "true",
            "limit": min(page_size, limit - len(markets)),
            "order": "id",
            "ascending": "false",
            "offset": offset,
        }
        resp = requests.get(GAMMA_MARKETS_URL, params=params, timeout=10)

        try:
            resp.raise_for_status()
        except requests.HTTPError:
            if resp.status_code != 422:
                raise

            fallback_params = {
                "closed": "true",
                "limit": min(page_size, limit - len(markets)),
                "offset": offset,
            }
            resp = requests.get(GAMMA_MARKETS_URL, params=fallback_params, timeout=10)
            resp.raise_for_status()

        page_markets = resp.json()
        if not page_markets:
            break

        markets.extend(page_markets)
        if len(page_markets) < page_size:
            break

    return markets[:limit]


def fetch_trades_for_market(condition_id, limit=500, offset=0):
    """
    Fetch trades for one market using the Data API market filter.
    Supports pagination with offset.
    """
    resp = requests.get(
        DATA_TRADES_URL,
        params={
            "market": condition_id,
            "limit": limit,
            "offset": offset,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def resolve_trader_id(trade):
    return (
        trade.get("proxyWallet")
        or trade.get("userAddress")
        or trade.get("maker_address")
        or trade.get("taker_address")
        or trade.get("owner")
        or "UNKNOWN"
    )


def shorten_id(value):
    if not value:
        return "UNKNOWN"
    if len(value) <= 12:
        return value
    return value[:6] + "..." + value[-4:]


def resolve_trader_label(trade, trader_id):
    alias = trade.get("pseudonym") or trade.get("name")
    if alias:
        return f"{alias} ({shorten_id(trader_id)})"
    return shorten_id(trader_id)


def parse_json_list(value):
    """
    Some Gamma fields may come back as JSON-encoded strings.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


def resolve_winning_outcome(market):
    """
    Try to resolve the winning outcome from market data.

    Preferred:
      market["tokens"] -> find token where token["winner"] is True

    Fallback:
      market["outcomes"] + market["outcomePrices"] -> pick the resolved 1.0 outcome

    Fallback:
      if the market has a direct resolved outcome field, use it
    """
    tokens = market.get("tokens", [])
    if isinstance(tokens, list):
        for token in tokens:
            if token.get("winner") is True:
                return token.get("outcome")

    outcomes = parse_json_list(market.get("outcomes"))
    outcome_prices = parse_json_list(market.get("outcomePrices"))
    if outcomes and outcome_prices and len(outcomes) == len(outcome_prices):
        winning_indexes = []
        for i, price in enumerate(outcome_prices):
            try:
                numeric_price = float(price)
            except (TypeError, ValueError):
                continue
            if numeric_price >= 0.999:
                winning_indexes.append(i)

        if len(winning_indexes) == 1:
            return outcomes[winning_indexes[0]]

    # Fallbacks in case the API shape differs
    for field in ["winningOutcome", "resolvedOutcome", "winner", "result"]:
        value = market.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def parse_iso_to_timestamp(value):
    if not value or not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def resolve_market_start_timestamp(market):
    for field in ["startDate", "createdAt", "endDate"]:
        timestamp = parse_iso_to_timestamp(market.get(field))
        if timestamp is not None:
            return timestamp
    return None


def resolve_market_group_key(market):
    question = market.get("question")
    normalized_question = None
    if isinstance(question, str) and question.strip():
        normalized_question = question.strip().lower()
        # Collapse ladder markets such as "Total Kills Over/Under 57.5" into one family.
        normalized_question = re.sub(r"-?\d+(?:\.\d+)?", "<num>", normalized_question)
        normalized_question = re.sub(r"\s+", " ", normalized_question)

    event_slug = market.get("events", [{}])[0].get("slug") if market.get("events") else None
    if isinstance(event_slug, str) and event_slug.strip():
        if normalized_question:
            return f"{event_slug.strip()}::{normalized_question}"
        return event_slug.strip()

    if normalized_question:
        return normalized_question

    return market.get("conditionId")


def aggregate_win_rates(
    markets,
    trades_limit_per_market=500,
    max_pages=10,
    early_window_minutes=60,
):
    """
    For each closed market:
      - resolve winning outcome
      - fetch trades for that market in descending timestamp order
      - keep only trades from the opening time window
      - count at most one early BUY per trader per market
      - count win/loss by wallet after filtering out near-certain entries

    To keep the demo simpler and avoid side inversion issues,
    only BUY trades are counted.
    """

    stats = defaultdict(
        lambda: {
            "label": "",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "volume": 0.0,
        }
    )

    skipped_markets = 0
    processed_markets = 0
    seen_market_groups = set()
    filter_stats = {
        "total_trades": 0,
        "opening_window_trades": 0,
        "late_trades_filtered": 0,
        "non_buy_filtered": 0,
        "near_certain_filtered": 0,
        "duplicate_market_groups_filtered": 0,
        "counted_trades": 0,
    }

    for market in markets:
        condition_id = market.get("conditionId")
        winning_outcome = resolve_winning_outcome(market)
        market_start_ts = resolve_market_start_timestamp(market)
        market_group_key = resolve_market_group_key(market)

        if not condition_id or not winning_outcome or market_start_ts is None:
            skipped_markets += 1
            continue

        if market_group_key in seen_market_groups:
            filter_stats["duplicate_market_groups_filtered"] += 1
            continue
        seen_market_groups.add(market_group_key)

        early_cutoff_ts = market_start_ts + (early_window_minutes * 60)

        # 1. fetch multiple pages of trades
        all_trades = []
        try:
            for page in range(max_pages):
                offset = page * trades_limit_per_market
                page_trades = fetch_trades_for_market(
                    condition_id,
                    limit=trades_limit_per_market,
                    offset=offset,
                )

                if not page_trades:
                    break

                all_trades.extend(page_trades)

                oldest_trade_ts = min(
                    trade.get("timestamp", 0) or 0 for trade in page_trades
                )

                # The API returns newest trades first. Once a page reaches the
                # opening window, we have enough history to evaluate early users.
                if oldest_trade_ts and oldest_trade_ts <= early_cutoff_ts:
                    break

                if len(page_trades) < trades_limit_per_market:
                    break

        except requests.RequestException:
            skipped_markets += 1
            continue

        if not all_trades:
            skipped_markets += 1
            continue

        processed_markets += 1

        filter_stats["total_trades"] += len(all_trades)

        # Keep only the opening time window, then process earliest first.
        early_trades = [
            trade
            for trade in all_trades
            if market_start_ts <= (trade.get("timestamp", 0) or 0) <= early_cutoff_ts
        ]
        filter_stats["opening_window_trades"] += len(early_trades)
        filter_stats["late_trades_filtered"] += len(all_trades) - len(early_trades)
        early_trades.sort(key=lambda x: x.get("timestamp", 0))
        seen_traders = set()

        for trade in early_trades:
            # Simplify the demo: only evaluate BUY trades
            if trade.get("side") != "BUY":
                filter_stats["non_buy_filtered"] += 1
                continue

            trader_id = resolve_trader_id(trade)
            if trader_id in seen_traders:
                continue

            price = trade.get("price", 0)
            size = trade.get("size", 0)

            try:
                price = float(price)
            except Exception:
                price = 0.0

            try:
                size = float(size)
            except Exception:
                size = 0.0

            # Filter out near-certain trades
            if price >= 0.99 or price <= 0.01:
                filter_stats["near_certain_filtered"] += 1
                continue

            trade_outcome = trade.get("outcome")

            stats[trader_id]["trades"] += 1
            stats[trader_id]["volume"] += price * size
            filter_stats["counted_trades"] += 1
            seen_traders.add(trader_id)

            if not stats[trader_id]["label"]:
                stats[trader_id]["label"] = resolve_trader_label(trade, trader_id)

            if trade_outcome == winning_outcome:
                stats[trader_id]["wins"] += 1
            else:
                stats[trader_id]["losses"] += 1

    return stats, processed_markets, skipped_markets, filter_stats


def build_leaderboard_rows(stats, min_trades=2):
    rows = []

    for trader_id, s in stats.items():
        trades = s["trades"]
        if trades < min_trades:
            continue

        win_rate = (s["wins"] / trades) * 100 if trades > 0 else 0.0
        rows.append(
            (
                trader_id,
                s["label"] or shorten_id(trader_id),
                trades,
                s["wins"],
                s["losses"],
                win_rate,
                s["volume"],
            )
        )

    rows.sort(key=lambda x: (x[5], x[2], x[6]), reverse=True)
    return rows


def print_winrate_leaderboard(stats, top_n=10, min_trades=2):
    rows = build_leaderboard_rows(stats, min_trades=min_trades)

    rank_width = 3
    trader_width = 38
    trades_width = 8
    wins_width = 6
    losses_width = 8
    rate_width = 10

    print("\nTop Smart Money Candidates")
    print("-" * 92)
    print(
        f"{'#':>{rank_width}} "
        f"{'Trader':<{trader_width}} "
        f"{'Trades':>{trades_width}} "
        f"{'Wins':>{wins_width}} "
        f"{'Losses':>{losses_width}} "
        f"{'Win Rate':>{rate_width}}"
    )
    print("-" * 92)

    for i, (_, label, trades, wins, losses, win_rate, _) in enumerate(rows[:top_n], start=1):
        if len(label) > trader_width:
            label = label[:trader_width - 3] + "..."
        print(
            f"{str(i) + '.':>{rank_width}} "
            f"{label:<{trader_width}} "
            f"{trades:>{trades_width}} "
            f"{wins:>{wins_width}} "
            f"{losses:>{losses_width}} "
            f"{win_rate:>{rate_width - 1}.1f}%"
        )


def print_filter_stats(filter_stats):
    print("\nFilter Summary")
    print("-" * 60)
    print(f"Fetched trades:           {filter_stats['total_trades']}")
    print(f"Opening-window trades:    {filter_stats['opening_window_trades']}")
    print(f"Filtered late trades:     {filter_stats['late_trades_filtered']}")
    print(f"Filtered non-BUY trades:  {filter_stats['non_buy_filtered']}")
    print(f"Filtered near-certain:    {filter_stats['near_certain_filtered']}")
    print(f"Skipped duplicate groups: {filter_stats['duplicate_market_groups_filtered']}")
    print(f"Counted trades:           {filter_stats['counted_trades']}")


def save_snapshot(
    leaderboard_rows,
    processed_markets,
    skipped_markets,
    filter_stats,
    path="snapshot.jsonl",
):
    data = {
        "savedAt": datetime.utcnow().isoformat() + "Z",
        "processed_markets": processed_markets,
        "skipped_markets": skipped_markets,
        "filter_stats": filter_stats,
        "leaderboard": [
            {
                "rank": i,
                "trader_id": trader_id,
                "label": label,
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate": win_rate,
                "volume": volume,
            }
            for i, (trader_id, label, trades, wins, losses, win_rate, volume) in enumerate(
                leaderboard_rows,
                start=1,
            )
        ],
    }
    with open(path, "a", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    print("Fetching recently closed markets...")
    markets = fetch_closed_markets(limit=1000)
    print(f"Fetched closed markets: {len(markets)}")

    if markets:
        sample = markets[0]
        print("Sample closed market:", sample.get("question"))
        print("Sample winning outcome:", resolve_winning_outcome(sample))

    print("\nEvaluating wallet performance...")
    stats, processed_markets, skipped_markets, filter_stats = aggregate_win_rates(
        markets,
        trades_limit_per_market=500,
        max_pages=12,
        early_window_minutes=120,
    )

    print(f"Processed markets: {processed_markets}")
    print(f"Skipped markets:   {skipped_markets}")
    print_filter_stats(filter_stats)

    leaderboard_rows = build_leaderboard_rows(stats, min_trades=3)
    print_winrate_leaderboard(stats, top_n=30, min_trades=3)
    save_snapshot(
        leaderboard_rows[:30],
        processed_markets,
        skipped_markets,
        filter_stats,
    )

if __name__ == "__main__":
    while True:
        main()
        print("Sleeping 600 seconds...\n")
        time.sleep(600)
