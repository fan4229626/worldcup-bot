#!/usr/bin/env python3
"""
2026 世界杯实时事件推送

用法：
  python live_events.py --today              # 追踪今天（阿拉木图时间）所有比赛
  python live_events.py --date 2026-06-14   # 追踪指定日期的所有比赛
  python live_events.py --match 537346      # 追踪单场比赛（用于测试）

运行逻辑：
  - 每 30 秒轮询一次 football-data.org
  - 自动等待比赛开始，比赛结束后自动退出
  - 状态保存在内存中，重启后不会重复推送已推过的事件
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError, RetryAfter

# ── 配置 ──────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]
FD_TOKEN   = os.environ["FOOTBALL_DATA_TOKEN"]

ALMATY_TZ     = ZoneInfo("Asia/Almaty")
UTC_TZ        = timezone.utc
POLL_INTERVAL = 30   # 每 30 秒轮询一次
FD_BASE       = "https://api.football-data.org/v4"
FD_HEADERS    = {"X-Auth-Token": FD_TOKEN}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 队名中文映射
TEAM_CN = {
    "Algeria": "阿尔及利亚", "Argentina": "阿根廷", "Australia": "澳大利亚",
    "Austria": "奥地利", "Belgium": "比利时", "Bosnia-Herzegovina": "波黑",
    "Bosnia and Herzegovina": "波黑", "Brazil": "巴西", "Canada": "加拿大",
    "Cape Verde Islands": "佛得角", "Cape Verde": "佛得角",
    "Colombia": "哥伦比亚", "Croatia": "克罗地亚", "Curaçao": "库拉索",
    "Czechia": "捷克", "Czech Republic": "捷克",
    "DR Congo": "刚果民主共和国", "Democratic Republic of the Congo": "刚果民主共和国",
    "Ecuador": "厄瓜多尔", "Egypt": "埃及", "England": "英格兰",
    "France": "法国", "Germany": "德国", "Ghana": "加纳", "Haiti": "海地",
    "Iran": "伊朗", "Iraq": "伊拉克", "Ivory Coast": "科特迪瓦",
    "Japan": "日本", "Jordan": "约旦", "Mexico": "墨西哥", "Morocco": "摩洛哥",
    "Netherlands": "荷兰", "New Zealand": "新西兰", "Norway": "挪威",
    "Panama": "巴拿马", "Paraguay": "巴拉圭", "Portugal": "葡萄牙",
    "Qatar": "卡塔尔", "Saudi Arabia": "沙特阿拉伯", "Scotland": "苏格兰",
    "Senegal": "塞内加尔", "South Africa": "南非", "South Korea": "韩国",
    "Spain": "西班牙", "Sweden": "瑞典", "Switzerland": "瑞士",
    "Tunisia": "突尼斯", "Turkey": "土耳其", "United States": "美国",
    "Uruguay": "乌拉圭", "Uzbekistan": "乌兹别克斯坦",
}

CARD_INFO = {
    "YELLOW_CARD":      ("🟨", "黄牌"),
    "RED_CARD":         ("🟥", "红牌"),
    "YELLOW_RED_CARD":  ("🟥", "第二张黄牌（红牌）"),
}


def tcn(name: str) -> str:
    return TEAM_CN.get(name, name)


# ── 比赛状态跟踪器 ─────────────────────────────────────────────────────────────
@dataclass
class MatchTracker:
    match_id: int
    home: str = ""
    away: str = ""
    # 已推送事件的唯一键，防止重复推送
    sent_goals:    set = field(default_factory=set)
    sent_bookings: set = field(default_factory=set)
    # 特殊状态标志
    ht1_injury_sent: bool = False   # 上半场补时已推
    halftime_sent:   bool = False   # 上半场结束已推
    ht2_injury_sent: bool = False   # 下半场补时已推（加时赛前）
    et1_injury_sent: bool = False   # 加时上半场补时
    finished_sent:   bool = False   # 比赛结束已推
    done: bool = False              # 是否已完全结束


# ── API 调用 ──────────────────────────────────────────────────────────────────
def fetch_match(match_id: int) -> dict:
    resp = httpx.get(
        f"{FD_BASE}/matches/{match_id}",
        headers=FD_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_wc_matches(date_str: str) -> list[dict]:
    """拉指定日期（UTC）的世界杯比赛列表。"""
    resp = httpx.get(
        f"{FD_BASE}/competitions/WC/matches",
        headers=FD_HEADERS,
        params={"dateFrom": date_str, "dateTo": date_str},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("matches", [])


# ── Telegram 推送 ─────────────────────────────────────────────────────────────
async def send_msg(bot: Bot, text: str) -> bool:
    for attempt in range(3):
        try:
            await bot.send_message(chat_id=CHAT_ID, text=text)
            logger.info("推送成功: %s", text[:60].replace("\n", " "))
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError as e:
            logger.error("Telegram 错误: %s", e)
            return False
    return False


# ── 事件处理 ──────────────────────────────────────────────────────────────────
async def process_match(bot: Bot, tracker: MatchTracker, data: dict):
    """对比最新数据和已推送状态，发出新事件通知。"""
    home = tcn(data["homeTeam"]["name"])
    away = tcn(data["awayTeam"]["name"])
    tracker.home = home
    tracker.away = away

    status      = data.get("status", "")
    minute      = data.get("minute")
    injury_time = data.get("injuryTime")
    score       = data.get("score", {})
    ft          = score.get("fullTime", {})
    ht          = score.get("halfTime", {})
    goals       = data.get("goals", []) or []
    bookings    = data.get("bookings", []) or []
    header      = f"【{home} vs {away}】"

    # ── 进球 ──
    # football-data.org 的进球对象不含实时比分，需按时间顺序自行累计
    home_id = data["homeTeam"]["id"]
    sorted_goals = sorted(goals, key=lambda x: (x["minute"], x.get("extraTime") or 0))

    for i, g in enumerate(sorted_goals):
        key = (g["minute"], g["team"]["id"], g["scorer"]["id"])
        if key in tracker.sent_goals:
            continue
        tracker.sent_goals.add(key)

        scorer = g["scorer"]["name"]
        assist = g.get("assist")
        assist_line = f"\n助攻：{assist['name']}" if assist else ""
        goal_type = g.get("type", "REGULAR")
        type_tag = "（点球）" if goal_type == "PENALTY" else \
                   "（乌龙球）" if goal_type == "OWN_GOAL" else ""

        # 累计到当前进球（含）的比分，乌龙球算对方进
        h_score = a_score = 0
        for prev in sorted_goals[:i + 1]:
            is_own      = prev.get("type") == "OWN_GOAL"
            by_home     = prev["team"]["id"] == home_id
            if by_home ^ is_own:   # 主队进球 XOR 乌龙 → 主队得分
                h_score += 1
            else:
                a_score += 1

        msg = (
            f"⚽ 进球{type_tag}！第 {g['minute']} 分钟\n"
            f"{header}\n"
            f"比分：{home} {h_score} : {a_score} {away}\n"
            f"射手：{scorer}{assist_line}"
        )
        await send_msg(bot, msg)

    # ── 红黄牌 ──
    for b in bookings:
        key = (b["minute"], b["player"]["id"], b["card"])
        if key in tracker.sent_bookings:
            continue
        tracker.sent_bookings.add(key)

        emoji, card_cn = CARD_INFO.get(b["card"], ("🟨", b["card"]))
        player = b["player"]["name"]
        team   = tcn(b["team"]["name"])
        msg = (
            f"{emoji} {card_cn}！第 {b['minute']} 分钟\n"
            f"{header}\n"
            f"{player}（{team}）"
        )
        await send_msg(bot, msg)

    # ── 上半场补时 ──
    if (status == "IN_PROGRESS"
            and minute is not None and 45 <= minute < 90
            and injury_time and not tracker.ht1_injury_sent):
        tracker.ht1_injury_sent = True
        await send_msg(bot, f"⏱️ 上半场补时 {injury_time} 分钟\n{header}")

    # ── 上半场结束 ──
    if status == "PAUSED" and not tracker.halftime_sent:
        tracker.halftime_sent = True
        h_score = ht.get("home", 0)
        a_score = ht.get("away", 0)
        await send_msg(
            bot,
            f"🔔 上半场结束\n{header}\n半场比分：{home} {h_score} : {a_score} {away}"
        )

    # ── 下半场/加时赛补时 ──
    if (status == "IN_PROGRESS"
            and minute is not None and minute >= 90
            and injury_time and not tracker.ht2_injury_sent):
        tracker.ht2_injury_sent = True
        phase = "加时赛上半场" if minute >= 105 else "下半场"
        await send_msg(bot, f"⏱️ {phase}补时 {injury_time} 分钟\n{header}")

    # ── 比赛结束 ──
    if status == "FINISHED" and not tracker.finished_sent:
        tracker.finished_sent = True
        tracker.done = True
        duration = score.get("duration", "REGULAR")
        duration_cn = {"REGULAR": "", "EXTRA_TIME": "（加时）", "PENALTY_SHOOTOUT": "（点球大战）"}.get(duration, "")
        await send_msg(
            bot,
            f"🏁 比赛结束{duration_cn}\n{header}\n"
            f"最终比分：{home} {ft.get('home',0)} : {ft.get('away',0)} {away}"
        )


# ── 初始化去重（防止重启后重复推送历史事件）────────────────────────────────────
async def init_tracker(tracker: MatchTracker, data: dict):
    """
    比赛已开始/已结束时，预先把现有事件标记为已推送。
    这样定时任务重启后不会把历史进球/黄牌重复推送。
    """
    status = data.get("status", "")
    if status not in ("IN_PROGRESS", "PAUSED", "FINISHED"):
        return

    for g in data.get("goals", []) or []:
        key = (g["minute"], g["team"]["id"], g["scorer"]["id"])
        tracker.sent_goals.add(key)

    for b in data.get("bookings", []) or []:
        key = (b["minute"], b["player"]["id"], b["card"])
        tracker.sent_bookings.add(key)

    if status in ("PAUSED", "FINISHED"):
        tracker.ht1_injury_sent = True
        tracker.halftime_sent   = True

    if status == "FINISHED":
        tracker.ht2_injury_sent = True
        tracker.finished_sent   = True
        tracker.done            = True

    logger.info(
        "[%d] 初始化完成（状态=%s）：跳过 %d 个进球、%d 张牌",
        tracker.match_id, status,
        len(tracker.sent_goals), len(tracker.sent_bookings),
    )


# ── 主循环 ────────────────────────────────────────────────────────────────────
async def track_matches(match_ids: list[int]):
    bot = Bot(token=BOT_TOKEN)
    trackers = {mid: MatchTracker(match_id=mid) for mid in match_ids}

    logger.info("开始追踪 %d 场比赛：%s", len(match_ids), match_ids)

    # 启动时先初始化——防止定时重启后重复推送已发生的事件
    for tracker in trackers.values():
        try:
            data = fetch_match(tracker.match_id)
            await init_tracker(tracker, data)
        except Exception as e:
            logger.error("[%d] 初始化失败: %s", tracker.match_id, e)

    while True:
        active = [t for t in trackers.values() if not t.done]
        if not active:
            logger.info("所有比赛已结束，退出")
            break

        for tracker in active:
            try:
                data   = fetch_match(tracker.match_id)
                status = data.get("status", "")
                minute = data.get("minute")
                logger.info(
                    "[%d] %s vs %s  状态=%s  分钟=%s",
                    tracker.match_id,
                    tcn(data["homeTeam"]["name"]),
                    tcn(data["awayTeam"]["name"]),
                    status, minute,
                )

                if status in ("TIMED", "SCHEDULED"):
                    # 还没开始，跳过本轮不处理
                    continue

                await process_match(bot, tracker, data)

            except httpx.HTTPStatusError as e:
                logger.error("[%d] API 请求失败: %s", tracker.match_id, e)
            except Exception as e:
                logger.error("[%d] 处理异常: %s", tracker.match_id, e)

            # 多场比赛时，每场之间间隔 2 秒，避免触发频率限制
            if len(active) > 1:
                await asyncio.sleep(2)

        await asyncio.sleep(POLL_INTERVAL)


# ── 命令入口 ──────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="世界杯实时事件追踪")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--today",  action="store_true", help="追踪今天（阿拉木图时间）的所有比赛")
    grp.add_argument("--date",   metavar="YYYY-MM-DD", help="追踪指定日期（阿拉木图时间）的所有比赛")
    grp.add_argument("--match",  type=int, metavar="ID", help="追踪单场比赛（测试用）")
    args = parser.parse_args()

    if args.match:
        match_ids = [args.match]
    else:
        # 用阿拉木图时间确定"今天"，避免凌晨漏掉 UTC 昨天的比赛
        if args.today:
            almaty_today = datetime.now(ALMATY_TZ).date()
        else:
            almaty_today = datetime.strptime(args.date, "%Y-%m-%d").date()

        # 阿拉木图一天 = UTC 前一天19:00 ~ 当天19:00，跨两个UTC日期
        # 所以同时拉"UTC昨天"和"UTC今天"，再按阿拉木图日期过滤
        utc_dates = [
            (almaty_today - timedelta(days=1)).strftime("%Y-%m-%d"),
            almaty_today.strftime("%Y-%m-%d"),
        ]
        all_matches = []
        seen_ids = set()
        for d in utc_dates:
            for m in fetch_wc_matches(d):
                if m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    all_matches.append(m)

        # 过滤：只保留阿拉木图日期等于目标日期的比赛
        def almaty_date(m):
            dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
            return dt.astimezone(ALMATY_TZ).date()

        matches = [m for m in all_matches if almaty_date(m) == almaty_today]

        if not matches:
            logger.info("阿拉木图时间 %s 没有比赛，退出", almaty_today)
            return

        logger.info("阿拉木图时间 %s，共 %d 场比赛：", almaty_today, len(matches))
        match_ids = [m["id"] for m in matches]
        for m in matches:
            logger.info(
                "  [%d] %s vs %s  %s  状态=%s",
                m["id"],
                tcn(m["homeTeam"]["name"]),
                tcn(m["awayTeam"]["name"]),
                m["utcDate"],
                m["status"],
            )

    await track_matches(match_ids)


if __name__ == "__main__":
    asyncio.run(main())
