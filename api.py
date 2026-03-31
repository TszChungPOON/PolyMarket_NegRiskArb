import logging
from typing import List

import requests

from config import FETCH_COUNT, MAX_MARKETS

logger = logging.getLogger(__name__)


def get_YES_NO_And_Condition(
    fetch_count: int = FETCH_COUNT,
    max_markets: int = MAX_MARKETS,
) -> List:
    """Fetch active neg-risk events from the Polymarket Gamma API with pagination.

    Returns a list where each element is:
        [title, yes_ids, no_ids, condition_ids, daily_rates, min_sizes, max_spreads, min_ticks]
    """
    all_events: List = []
    offset = 0

    while offset < max_markets:
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/events"
                f"?limit={fetch_count}&closed=false&active=true"
                f"&offset={offset}",
                timeout=30,
            )
            r.raise_for_status()
            response = r.json()
        except requests.RequestException as exc:
            logger.error("[api] Request failed at offset %d: %s", offset, exc)
            break

        if not response:
            logger.info("[api] No more events at offset %d, stopping", offset)
            break

        for event in response:
            if not event.get("enableNegRisk"):
                continue

            title = event["title"]
            current_event = [title, [], [], [], [], [], [], []]

            for x in event.get("markets", []):
                if not (x.get("active") and not x.get("closed")):
                    continue

                raw_ids = x["clobTokenIds"]
                parts = raw_ids.replace("[", "").replace("]", "").replace("'", "").replace('"', "").split(",")
                YES_ID = parts[0].strip()
                NO_ID = parts[1].strip()

                current_event[1].append(YES_ID)
                current_event[2].append(NO_ID)
                current_event[3].append(x["conditionId"])
                current_event[4].append(x.get("clobRewards", [{}])[0].get("rewardsDailyRate", 0))
                current_event[5].append(x["rewardsMinSize"])
                current_event[6].append(x["rewardsMaxSpread"])
                current_event[7].append(float(x["orderPriceMinTickSize"]))

            all_events.append(current_event)

        logger.info("[api] Fetched offset %d — total events so far: %d", offset, len(all_events))
        offset += fetch_count

    return all_events


def send_telegram(tg_token: str, chat_id: str, message: str) -> None:
    """Fire-and-forget Telegram notification."""
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    try:
        requests.get(url, params={"chat_id": chat_id, "text": message}, timeout=10)
    except requests.RequestException as exc:
        logger.warning("[telegram] Failed to send message: %s", exc)