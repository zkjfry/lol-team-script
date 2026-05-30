import json
import os
from datetime import datetime
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL in .env")


LANE_MAP = {
    "上路": "TOP",
    "打野": "JUNGLE",
    "中路": "MID",
    "下路": "ADC",
    "辅助": "SUPPORT",
}


def normalize_lane(lane_cn):
    if lane_cn is None:
        return None

    if lane_cn not in LANE_MAP:
        raise ValueError(f"Unknown lane: {lane_cn}")

    return LANE_MAP[lane_cn]


def main():
    json_path = Path("output/opgg_champion_mapping.json")

    if not json_path.exists():
        json_path = Path("opgg_champion_mapping.json")

    if not json_path.exists():
        raise FileNotFoundError("Cannot find opgg_champion_mapping.json")

    data = json.loads(json_path.read_text(encoding="utf-8"))

    source = data.get("source", "OP.GG")
    region = data.get("region", "Korea")
    tier = data.get("tier", "Emerald+")
    queue = data.get("queue", "ranked")
    scraped_at_text = data.get("scrapedAt")

    scraped_at = None
    if scraped_at_text:
        scraped_at = datetime.fromisoformat(scraped_at_text.replace("Z", "+00:00"))

    champion_mapping = data.get("championMapping", [])

    print(f"[INFO] source={source}, region={region}, tier={tier}, queue={queue}")
    print(f"[INFO] champion count={len(champion_mapping)}")

    conn = psycopg2.connect(DATABASE_URL)

    try:
        with conn:
            with conn.cursor() as cur:
                for item in champion_mapping:
                    champion_name = item["championName"]
                    primary_lane = normalize_lane(item["primaryLane"])
                    secondary_lane = normalize_lane(item.get("secondaryLane"))

                    cur.execute(
                        """
                        INSERT INTO champion (
                            champion_name_cn,
                            created_at,
                            updated_at
                        )
                        VALUES (
                            %s,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (champion_name_cn)
                        DO UPDATE SET
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING id;
                        """,
                        (champion_name,),
                    )

                    champion_id = cur.fetchone()[0]

                    cur.execute(
                        """
                        INSERT INTO champion_lane_mapping (
                            champion_id,
                            primary_lane,
                            secondary_lane,
                            source,
                            region,
                            tier,
                            queue_type,
                            scraped_at,
                            updated_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (champion_id, source, region, tier, queue_type)
                        DO UPDATE SET
                            primary_lane = EXCLUDED.primary_lane,
                            secondary_lane = EXCLUDED.secondary_lane,
                            scraped_at = EXCLUDED.scraped_at,
                            updated_at = CURRENT_TIMESTAMP;
                        """,
                        (
                            champion_id,
                            primary_lane,
                            secondary_lane,
                            source,
                            region,
                            tier,
                            queue,
                            scraped_at,
                        ),
                    )

        print("[DONE] Import completed.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()