import asyncio
import json
import random
import re
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

# 每个位置最多取多少个英雄
TOP_N_PER_LANE = 40

# 过滤登场率太低的数据，避免冷门样本太少
MIN_PICK_RATE = 0.3

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)


LANES = {
    "top": "上路",
    "jungle": "打野",
    "mid": "中路",
    "adc": "下路",
    "support": "辅助",
}


@dataclass
class ChampionLaneStat:
    champion_name: str
    lane: str
    lane_cn: str
    rank_no: int
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
    await page.wait_for_timeout(2500)

    # 再等到网络空闲。失败也不致命。
    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass


async def scrape_lane(page: Page, lane: str) -> List[ChampionLaneStat]:
    url = build_url(lane)
    print(f"[SCRAPE] {lane.upper()} -> {url}")

    await safe_goto(page, url)

    page_text = await page.locator("body").inner_text(timeout=30_000)
    patch_version = extract_patch_version(page_text)

    # 抓所有指向 champion detail 的 a 标签。
    # OP.GG 表格可能不是标准 table，所以不要只依赖 tr。
    raw_items = await page.locator("a[href*='/lol/champions/']").evaluate_all(
        """
        anchors => {
            const items = [];

            for (const a of anchors) {
                const href = a.href || "";
                const name = (a.innerText || "").trim();

                if (!href.includes("/lol/champions/")) continue;
                if (!name) continue;

                let node = a;
                let container = null;

                // 往上找一个看起来像“榜单行”的父节点
                for (let i = 0; i < 8 && node; i++) {
                    const text = (node.innerText || "").trim();
                    const percentCount = (text.match(/\\d+(?:\\.\\d+)?%/g) || []).length;

                    if (percentCount >= 3) {
                        container = node;
                        break;
                    }

                    node = node.parentElement;
                }

                if (!container) continue;

                items.push({
                    championName: name,
                    href,
                    rowText: (container.innerText || "").trim()
                });
            }

            return items;
        }
        """
    )

    stats: List[ChampionLaneStat] = []
    seen = set()

    for item in raw_items:
        champion_name = item["championName"].strip()
        row_text = item["rowText"].strip()

        if champion_name in seen:
            continue

        percentages = re.findall(r"\d+(?:\.\d+)?%", row_text)

        if len(percentages) < 3:
            continue

        win_rate = parse_percent(percentages[0])
        pick_rate = parse_percent(percentages[1])
        ban_rate = parse_percent(percentages[2])

        if pick_rate is not None and pick_rate < MIN_PICK_RATE:
            continue

        seen.add(champion_name)

        stats.append(
            ChampionLaneStat(
                champion_name=champion_name,
                lane=lane.upper(),
                lane_cn=LANES[lane],
                rank_no=len(stats) + 1,
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

        if len(stats) >= TOP_N_PER_LANE:
            break

    print(f"[OK] {lane.upper()} scraped {len(stats)} champions, patch={patch_version}")
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
            viewport={"width": 1440, "height": 1200},
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


if __name__ == "__main__":
    asyncio.run(main())