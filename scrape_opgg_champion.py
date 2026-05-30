import asyncio
import json
import random
import re
import os
import psycopg2
from dotenv import load_dotenv
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Page

BASE_URL = "https://op.gg/zh-cn/lol/champions"

REGION = "kr"
REGION_LABEL = "Korea"

TIER = "emerald_plus"
TIER_LABEL = "Emerald+"

QUEUE = "ranked"

# 过滤tier太低的数据，避免冷门样本太少
EXCLUDE_OPGG_TIER_5 = True

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
AUTO_IMPORT_TO_DB = True

LANES = {
    "top": "上路",
    "jungle": "打野",
    "mid": "中路",
    "adc": "下路",
    "support": "辅助",
}

LANE_DB_MAP = {
    "上路": "TOP",
    "打野": "JUNGLE",
    "中路": "MID",
    "下路": "ADC",
    "辅助": "SUPPORT",
}


def normalize_lane_for_db(lane_cn: Optional[str]) -> Optional[str]:
    if lane_cn is None:
        return None

    if lane_cn not in LANE_DB_MAP:
        raise ValueError(f"Unknown lane: {lane_cn}")

    return LANE_DB_MAP[lane_cn]


@dataclass
class ChampionLaneStat:
    champion_name: str
    lane: str
    lane_cn: str
    rank_no: int
    opgg_tier: Optional[int]
    win_rate: Optional[float]
    pick_rate: Optional[float]
    ban_rate: Optional[float]
    source_url: str
    region: str
    tier: str
    queue: str
    patch_version: Optional[str]
    scraped_at: str


def parse_percent(text: str) -> Optional[float]:
    """
    '51.23%' -> 51.23
    """
    if not text:
        return None

    match = re.search(r"(\d+(?:\.\d+)?)%", text)
    if not match:
        return None

    return float(match.group(1))


def extract_patch_version(page_text: str) -> Optional[str]:
    """
    从页面文本中提取类似 16.11 的版本号。
    """
    match = re.search(r"补丁\s*(\d+\.\d+)", page_text)
    if match:
        return match.group(1)

    match = re.search(r"Version:\s*(\d+\.\d+)", page_text)
    if match:
        return match.group(1)

    return None


def build_url(lane: str) -> str:
    """
    构造 OP.GG 查询 URL。

    注意：
    OP.GG 前端参数可能会变化。
    如果某天抓不到，优先检查 URL 参数是否变了。
    """
    params = {
        "region": REGION,
        "tier": TIER,
        "position": lane,
        "queue": QUEUE,
    }
    return f"{BASE_URL}?{urlencode(params)}"


async def safe_goto(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    # 页面有时会继续异步渲染，稍微等一下
    await page.wait_for_timeout(3000)

    # 再等到网络空闲。失败也不致命。
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass


async def scroll_to_bottom(page: Page) -> None:
    """
    OP.GG 榜单可能是滚动加载/懒加载。
    不滚动的话，Playwright 可能只抓到当前视口附近的部分英雄。
    """
    previous_height = 0
    stable_count = 0

    for _ in range(30):
        current_height = await page.evaluate("document.body.scrollHeight")

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)

        new_height = await page.evaluate("document.body.scrollHeight")

        if new_height == previous_height or new_height == current_height:
            stable_count += 1
        else:
            stable_count = 0

        previous_height = new_height

        if stable_count >= 3:
            break

    # 回到顶部不是必须，但方便后续 debug
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)


async def extract_raw_items_from_current_dom(page: Page) -> List[Dict]:
    """
    只解析 OP.GG 右侧 Ranking Table。

    修复点：
    - rank 不再从 cells[0].innerText 取；
    - 改成 cells[0].querySelector("span.w-5")；
    - 避免把排名和涨跌数字拼成 11 / 23 / 59。
    """
    return await page.evaluate(
        """
        () => {
            function textOf(el) {
                return (el && el.innerText ? el.innerText.trim() : "");
            }

            function findRankingTable() {
                const tables = Array.from(document.querySelectorAll("table"));

                for (const table of tables) {
                    const caption = table.querySelector("caption");
                    const captionText = textOf(caption);

                    if (captionText === "Ranking Table") {
                        return table;
                    }
                }

                for (const table of tables) {
                    const tableText = textOf(table);

                    if (
                        tableText.includes("排名") &&
                        tableText.includes("英雄") &&
                        tableText.includes("胜率") &&
                        tableText.includes("登场率") &&
                        tableText.includes("禁用率")
                    ) {
                        return table;
                    }
                }

                return null;
            }

            function parseRankFromCell(cell) {
                /*
                 * OP.GG 第一列里有两个数字：
                 * - 第一个 span.w-5 是真正排名
                 * - 后面的 green/red span 是上升/下降位数
                 *
                 * 所以必须优先取 span.w-5。
                 */
                const rankSpan = cell.querySelector("span.w-5");

                if (rankSpan) {
                    const rankText = textOf(rankSpan);
                    const n = Number(rankText);

                    if (Number.isInteger(n) && n >= 1 && n <= 300) {
                        return n;
                    }
                }

                /*
                 * fallback：取第一个 span 的纯数字。
                 */
                const spans = Array.from(cell.querySelectorAll("span"));

                for (const span of spans) {
                    const t = textOf(span);

                    if (/^\\d{1,3}$/.test(t)) {
                        const n = Number(t);

                        if (n >= 1 && n <= 300) {
                            return n;
                        }
                    }
                }

                return null;
            }

            const table = findRankingTable();

            if (!table) {
                return [];
            }

            const rows = Array.from(table.querySelectorAll("tbody tr"));
            const items = [];

            for (const row of rows) {
                if (row.classList.contains("ad")) continue;

                const cells = Array.from(row.querySelectorAll("td"));

                if (cells.length < 7) continue;

                const rankNo = parseRankFromCell(cells[0]);

                if (!rankNo) continue;

                const championStrong = cells[1].querySelector("strong");
                const championName = textOf(championStrong);

                if (!championName) continue;

                const championLink = cells[1].querySelector("a[href*='/lol/champions/']");
                const href = championLink ? championLink.href : "";

                const tierText = textOf(cells[2]);
                const tierMatch = tierText.match(/[1-5]/);
                const opggTier = tierMatch ? Number(tierMatch[0]) : null;
                
                const rowText = textOf(row);
                const percentages = rowText.match(/\d+(?:\.\d+)?%/g) || [];
                
                if (percentages.length < 3) continue;
                
                items.push({
                    championName,
                    href,
                    rowText,
                    rankNo,
                    opggTier,
                    percentages: percentages.slice(0, 3)
                });
            }

            return items;
        }
        """
    )


async def collect_raw_items_by_scrolling(page: Page) -> List[Dict]:
    """
    viewport 已经设置成 1920x6000 后，OP.GG 榜单通常会一次性渲染完整。
    这里只做少量滚动和多次采集，避免复杂滚动导致漏数据。
    """
    collected = {}

    async def collect_once():
        current_items = await extract_raw_items_from_current_dom(page)

        for item in current_items:
            rank_no = item.get("rankNo")
            champion_name = item.get("championName", "").strip()

            if rank_no is None or not champion_name:
                continue

            collected[rank_no] = item

    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1500)
    await collect_once()

    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(2000)
    await collect_once()

    await page.keyboard.press("End")
    await page.wait_for_timeout(1500)
    await collect_once()

    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)

    return [collected[k] for k in sorted(collected.keys())]


async def scrape_lane(page: Page, lane: str) -> List[ChampionLaneStat]:
    url = build_url(lane)
    print(f"[SCRAPE] {lane.upper()} -> {url}")

    await safe_goto(page, url)

    page_text = await page.locator("body").inner_text(timeout=30_000)
    patch_version = extract_patch_version(page_text)

    raw_items = await collect_raw_items_by_scrolling(page)

    print(f"[DEBUG] {lane.upper()} raw_items count = {len(raw_items)}")

    stats: List[ChampionLaneStat] = []
    seen_ranks = set()

    for item in raw_items:
        champion_name = item.get("championName", "").strip()
        row_text = item.get("rowText", "").strip()
        rank_no = item.get("rankNo")
        opgg_tier = item.get("opggTier")

        if not champion_name:
            continue

        if rank_no is None:
            continue

        if rank_no in seen_ranks:
            continue

        if EXCLUDE_OPGG_TIER_5 and opgg_tier == 5:
            continue

        percentages = item.get("percentages") or re.findall(r"\d+(?:\.\d+)?%", row_text)

        if len(percentages) < 3:
            continue

        win_rate = parse_percent(percentages[0])
        pick_rate = parse_percent(percentages[1])
        ban_rate = parse_percent(percentages[2])

        seen_ranks.add(rank_no)

        stats.append(
            ChampionLaneStat(
                champion_name=champion_name,
                lane=lane.upper(),
                lane_cn=LANES[lane],
                rank_no=rank_no,
                opgg_tier=opgg_tier,
                win_rate=win_rate,
                pick_rate=pick_rate,
                ban_rate=ban_rate,
                source_url=url,
                region=REGION_LABEL,
                tier=TIER_LABEL,
                queue=QUEUE,
                patch_version=patch_version,
                scraped_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    stats.sort(key=lambda x: x.rank_no)

    print(f"[OK] {lane.upper()} scraped {len(stats)} champions, patch={patch_version}")

    debug_ranks = sorted(set(x.rank_no for x in stats))
    print(f"[DEBUG] {lane.upper()} parsed ranks = {debug_ranks}")

    if debug_ranks:
        expected_max = max(debug_ranks)
        missing = [i for i in range(1, expected_max + 1) if i not in debug_ranks]
        print(f"[DEBUG] {lane.upper()} missing ranks = {missing}")
        print(f"[DEBUG] {lane.upper()} first 10 champions = {[x.champion_name for x in stats[:10]]}")

    return stats


def build_champion_mapping(raw_stats: List[ChampionLaneStat]) -> List[Dict]:
    """
    生成你现在前端 HTML 可以直接导入的 championMapping。

    规则：
    - 一个英雄如果只出现在一个位置：primaryLane = 该位置，secondaryLane = null
    - 一个英雄如果出现在多个位置：
      - 按 rank_no 更高的位置作为 primaryLane
      - 第二个位置作为 secondaryLane
    """
    grouped: Dict[str, List[ChampionLaneStat]] = {}

    for stat in raw_stats:
        grouped.setdefault(stat.champion_name, []).append(stat)

    champion_mapping = []

    for champion_name, rows in grouped.items():
        rows_sorted = sorted(
            rows,
            key=lambda x: (
                x.rank_no,
                -(x.pick_rate or 0),
                -(x.win_rate or 0),
            ),
        )

        primary = rows_sorted[0].lane_cn
        secondary = rows_sorted[1].lane_cn if len(rows_sorted) >= 2 else None

        champion_mapping.append(
            {
                "championName": champion_name,
                "primaryLane": primary,
                "secondaryLane": secondary,
            }
        )

    lane_order = {
        "上路": 1,
        "打野": 2,
        "中路": 3,
        "下路": 4,
        "辅助": 5,
    }

    champion_mapping.sort(
        key=lambda x: (
            lane_order.get(x["primaryLane"], 99),
            x["championName"],
        )
    )

    return champion_mapping


def save_champion_mapping_to_db(mapping_output: Dict) -> None:
    """
    把 championMapping 自动写入 PostgreSQL。

    注意：
    1. 会 upsert champion 表。
    2. 会 upsert champion_lane_mapping 表。
    3. 不会覆盖 champion.damage_type / is_enabled / manual_note。
    """
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL in .env")

    source = mapping_output.get("source", "OP.GG")
    region = mapping_output.get("region", "Korea")
    tier = mapping_output.get("tier", "Emerald+")
    queue = mapping_output.get("queue", "ranked")
    scraped_at_text = mapping_output.get("scrapedAt")
    champion_mapping = mapping_output.get("championMapping", [])

    scraped_at = None
    if scraped_at_text:
        scraped_at = datetime.fromisoformat(scraped_at_text.replace("Z", "+00:00"))

    print()
    print("[DB] Start importing champion mapping...")
    print(f"[DB] source={source}, region={region}, tier={tier}, queue={queue}")
    print(f"[DB] champion count={len(champion_mapping)}")

    conn = psycopg2.connect(DATABASE_URL)

    inserted_or_updated_champions = 0
    inserted_or_updated_mappings = 0

    try:
        with conn:
            with conn.cursor() as cur:
                # 每次导入前，先删除旧的英雄池 mapping
                # 注意：只删 champion_lane_mapping，不删 champion 表，避免 damage_type 丢失
                cur.execute(
                    """
                    DELETE FROM champion_lane_mapping
                    WHERE source = %s
                      AND region = %s
                      AND tier = %s
                      AND queue_type = %s;
                    """,
                    (
                        source,
                        region,
                        tier,
                        queue,
                    ),
                )

                print(f"[DB] old mappings deleted: {cur.rowcount}")
                for item in champion_mapping:
                    champion_name = item["championName"]
                    primary_lane = normalize_lane_for_db(item["primaryLane"])
                    secondary_lane = normalize_lane_for_db(item.get("secondaryLane"))

                    # upsert champion
                    # 不覆盖 damage_type / is_enabled / manual_note
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
                    inserted_or_updated_champions += 1

                    # upsert champion_lane_mapping
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
                        );
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

                    inserted_or_updated_mappings += 1

        print(f"[DB] champions upserted: {inserted_or_updated_champions}")
        print(f"[DB] mappings upserted: {inserted_or_updated_mappings}")
        print("[DB] Import completed.")

    finally:
        conn.close()


async def main():
    all_stats: List[ChampionLaneStat] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        page = await browser.new_page(
            locale="zh-CN",
            viewport={"width": 1920, "height": 6000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )

        for lane in LANES.keys():
            lane_stats = await scrape_lane(page, lane)
            all_stats.extend(lane_stats)

            # 不要请求太快
            await page.wait_for_timeout(random.randint(1200, 2500))

        await browser.close()

    raw_output = {
        "source": "OP.GG",
        "region": REGION_LABEL,
        "tier": TIER_LABEL,
        "queue": QUEUE,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "totalRows": len(all_stats),
        "data": [asdict(x) for x in all_stats],
    }

    champion_mapping = build_champion_mapping(all_stats)

    mapping_output = {
        "source": "OP.GG",
        "region": REGION_LABEL,
        "tier": TIER_LABEL,
        "queue": QUEUE,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "championCount": len(champion_mapping),
        "championMapping": champion_mapping,
    }

    raw_path = OUTPUT_DIR / "opgg_raw_stats.json"
    mapping_path = OUTPUT_DIR / "opgg_champion_mapping.json"

    raw_path.write_text(
        json.dumps(raw_output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    mapping_path.write_text(
        json.dumps(mapping_output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"[DONE] raw stats saved to: {raw_path}")
    print(f"[DONE] champion mapping saved to: {mapping_path}")
    print(f"[DONE] total raw rows: {len(all_stats)}")
    print(f"[DONE] total unique champions: {len(champion_mapping)}")

    if AUTO_IMPORT_TO_DB:
        save_champion_mapping_to_db(mapping_output)


if __name__ == "__main__":
    asyncio.run(main())
