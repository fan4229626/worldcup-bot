#!/usr/bin/env python3
"""
2026 世界杯赛程 Telegram 推送机器人

用法（本地测试）:
  python worldcup.py --print-today      # 只打印今日赛程，不推送（干跑验证）
  python worldcup.py --today            # 推送今日赛程到 Telegram
  python worldcup.py --test-reminder    # 找最近一场未来的比赛，发送提醒测试
  python worldcup.py --check-reminder   # 正常检查：开赛前 10~90 分钟内推送提醒
                                        # （窗口宽 45 分钟，抗 GitHub Actions 延迟）

GitHub Actions 定时任务说明:
  - 今日赛程：每天 03:00 UTC（= 08:00 阿拉木图）运行 --today
  - 赛前提醒：每 5 分钟运行 --check-reminder
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo  # Python 3.9+

import httpx
from dotenv import load_dotenv
import os
from telegram import Bot
from telegram.error import TelegramError, RetryAfter

# ── 配置 ────────────────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

API_URL = "https://worldcup26.ir/get/games"

# 阿拉木图时区 UTC+5
ALMATY_TZ = ZoneInfo("Asia/Almaty")
UTC_TZ = timezone.utc

# API 的 local_date 存的是各场馆的本地时间，不同城市时区不同。
# 16个场馆 → 各自 ZoneInfo：
#   墨西哥 (1-3)：2023年起取消夏令时，常年 UTC-6 → America/Mexico_City
#   美国中部 (4-6)：夏令时 CDT = UTC-5 → America/Chicago
#   美国东部 (7-12) + 多伦多：夏令时 EDT = UTC-4 → America/New_York
#   美国西部 (13-16) + 温哥华：夏令时 PDT = UTC-7 → America/Los_Angeles
STADIUM_TZ: dict = {
    "1":  ZoneInfo("America/Mexico_City"),   # 墨西哥城
    "2":  ZoneInfo("America/Mexico_City"),   # 瓜达拉哈拉
    "3":  ZoneInfo("America/Mexico_City"),   # 蒙特雷
    "4":  ZoneInfo("America/Chicago"),       # 达拉斯
    "5":  ZoneInfo("America/Chicago"),       # 休斯顿
    "6":  ZoneInfo("America/Chicago"),       # 堪萨斯城
    "7":  ZoneInfo("America/New_York"),      # 亚特兰大
    "8":  ZoneInfo("America/New_York"),      # 迈阿密
    "9":  ZoneInfo("America/New_York"),      # 波士顿
    "10": ZoneInfo("America/New_York"),      # 费城
    "11": ZoneInfo("America/New_York"),      # 纽约/新泽西
    "12": ZoneInfo("America/Toronto"),       # 多伦多
    "13": ZoneInfo("America/Vancouver"),     # 温哥华
    "14": ZoneInfo("America/Los_Angeles"),   # 西雅图
    "15": ZoneInfo("America/Los_Angeles"),   # 旧金山湾区
    "16": ZoneInfo("America/Los_Angeles"),   # 洛杉矶
}

# 比赛类型中文映射
MATCH_TYPE_CN = {
    "group": "小组赛",
    "r32":   "32强赛",
    "r16":   "16强赛",
    "qf":    "四分之一决赛",
    "sf":    "半决赛",
    "third": "三四名决赛",
    "final": "决赛",
}

# 队名英文→中文映射（2026世界杯全部48支球队）
TEAM_CN = {
    "Algeria":                        "阿尔及利亚",
    "Argentina":                      "阿根廷",
    "Australia":                      "澳大利亚",
    "Austria":                        "奥地利",
    "Belgium":                        "比利时",
    "Bosnia and Herzegovina":         "波黑",
    "Brazil":                         "巴西",
    "Canada":                         "加拿大",
    "Cape Verde":                     "佛得角",
    "Colombia":                       "哥伦比亚",
    "Croatia":                        "克罗地亚",
    "Curaçao":                        "库拉索",
    "Czech Republic":                 "捷克",
    "Democratic Republic of the Congo": "刚果民主共和国",
    "Ecuador":                        "厄瓜多尔",
    "Egypt":                          "埃及",
    "England":                        "英格兰",
    "France":                         "法国",
    "Germany":                        "德国",
    "Ghana":                          "加纳",
    "Haiti":                          "海地",
    "Iran":                           "伊朗",
    "Iraq":                           "伊拉克",
    "Ivory Coast":                    "科特迪瓦",
    "Japan":                          "日本",
    "Jordan":                         "约旦",
    "Mexico":                         "墨西哥",
    "Morocco":                        "摩洛哥",
    "Netherlands":                    "荷兰",
    "New Zealand":                    "新西兰",
    "Norway":                         "挪威",
    "Panama":                         "巴拿马",
    "Paraguay":                       "巴拉圭",
    "Portugal":                       "葡萄牙",
    "Qatar":                          "卡塔尔",
    "Saudi Arabia":                   "沙特阿拉伯",
    "Scotland":                       "苏格兰",
    "Senegal":                        "塞内加尔",
    "South Africa":                   "南非",
    "South Korea":                    "韩国",
    "Spain":                          "西班牙",
    "Sweden":                         "瑞典",
    "Switzerland":                    "瑞士",
    "Tunisia":                        "突尼斯",
    "Turkey":                         "土耳其",
    "United States":                  "美国",
    "Uruguay":                        "乌拉圭",
    "Uzbekistan":                     "乌兹别克斯坦",
}


def team_cn(name_en: str) -> str:
    """返回队名中文，找不到则保留英文。"""
    return TEAM_CN.get(name_en, name_en)

# ── 日志 ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── 数据获取 ─────────────────────────────────────────────────────────────────
def fetch_games() -> list[dict]:
    """从 worldcup26.ir 拉取全部赛程数据。"""
    try:
        resp = httpx.get(API_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # API 返回格式：{"games": [...]}
        games = data.get("games", data) if isinstance(data, dict) else data
        logger.info("成功拉取 %d 场比赛数据", len(games))
        return games
    except Exception as e:
        logger.error("拉取赛程失败: %s", e)
        return []


def parse_game_time(game: dict) -> Optional[datetime]:
    """
    解析 local_date 字段（格式：MM/DD/YYYY HH:MM）为带时区的 datetime（UTC）。
    根据 stadium_id 确定场馆本地时区，再转为 UTC。
    """
    raw = game.get("local_date", "")
    if not raw:
        return None
    try:
        dt_naive = datetime.strptime(raw, "%m/%d/%Y %H:%M")
    except ValueError:
        logger.warning("无法解析时间: %s", raw)
        return None

    sid = str(game.get("stadium_id", ""))
    tz = STADIUM_TZ.get(sid)
    if tz is None:
        logger.warning("未知 stadium_id=%s，跳过时区转换", sid)
        return dt_naive.replace(tzinfo=UTC_TZ)

    # 附上场馆本地时区，再转成 UTC 存储（方便后续统一比较）
    return dt_naive.replace(tzinfo=tz).astimezone(UTC_TZ)


def to_almaty(dt: datetime) -> datetime:
    """把任意带时区的 datetime 转换为阿拉木图时间（UTC+5）。"""
    return dt.astimezone(ALMATY_TZ)


def game_stage_cn(game: dict) -> str:
    """返回比赛阶段的中文描述，小组赛附带组别字母。"""
    t = game.get("type", "")
    g = game.get("group", "")
    cn = MATCH_TYPE_CN.get(t, t or "未知阶段")
    if t == "group" and g:
        return f"{cn} {g}组"
    return cn


# ── 消息格式 ─────────────────────────────────────────────────────────────────
def _format_day_block(date_label: str, games: list[dict]) -> list[str]:
    """生成单天赛程的文字块（供拼接用）。"""
    lines = []
    if not games:
        lines.append(f"📅 {date_label}\n休息日，没有比赛 😴\n")
        return lines
    lines.append(f"📅 {date_label}（共 {len(games)} 场）\n")
    for g in games:
        dt_almaty = to_almaty(parse_game_time(g))
        time_str = dt_almaty.strftime("%H:%M")
        home  = team_cn(g.get("home_team_name_en", "?"))
        away  = team_cn(g.get("away_team_name_en", "?"))
        stage = game_stage_cn(g)
        lines.append(f"⏰ {time_str}（阿拉木图时间）")
        lines.append(f"⚽ {home} vs {away}")
        lines.append(f"🏆 {stage}\n")
    return lines


def format_two_day_schedule(today_games: list[dict], tomorrow_games: list[dict]) -> str:
    """生成今天+明天赛程的合并推送消息。"""
    now_almaty = datetime.now(ALMATY_TZ)
    from datetime import timedelta
    today_str    = now_almaty.strftime("%m月%d日")
    tomorrow_str = (now_almaty + timedelta(days=1)).strftime("%m月%d日")

    lines = ["⚽ 2026世界杯赛程播报\n"]
    lines += _format_day_block(f"今天 {today_str}", today_games)
    lines += _format_day_block(f"明天 {tomorrow_str}", tomorrow_games)
    lines.append("祝观赛愉快！🎉")
    return "\n".join(lines)


def format_reminder(game: dict, minutes_left: int, index: int) -> str:
    """
    生成赛前提醒消息。
    index: 1=1小时前提醒，2=即将开赛提醒
    """
    home  = team_cn(game.get("home_team_name_en", "?"))
    away  = team_cn(game.get("away_team_name_en", "?"))
    dt_almaty = to_almaty(parse_game_time(game))
    time_str  = dt_almaty.strftime("%H:%M")
    stage     = game_stage_cn(game)
    if index == 1:
        header = "🔔 赛前1小时提醒"
        footer = "距开赛约 1 小时，准备好了吗？⚽"
    else:
        header = "🔔🔔 即将开赛！"
        footer = f"还有约 {minutes_left} 分钟，快起来！⏰"
    return (
        f"{header}\n\n"
        f"⚽ {home} vs {away}\n"
        f"🏆 {stage}\n"
        f"🕐 {time_str}（阿拉木图时间）开赛\n\n"
        f"{footer}"
    )


# ── 核心筛选逻辑 ──────────────────────────────────────────────────────────────
def get_games_by_date(games: list[dict], target_date) -> list[dict]:
    """筛选指定阿拉木图日期的比赛，按开赛时间排序。"""
    result = []
    for g in games:
        dt = parse_game_time(g)
        if dt is None:
            continue
        if to_almaty(dt).date() == target_date:
            result.append(g)
    result.sort(key=lambda g: parse_game_time(g))
    return result


def get_reminder_games(games: list[dict]) -> list[tuple]:
    """
    两个宽窗口提醒，抗 GitHub Actions 延迟（实际间隔可能长达 90 分钟）：
      窗口1：开赛前 45~90 分钟 → "1小时前提醒"
      窗口2：开赛前 10~45 分钟 → "即将开赛提醒"

    每个窗口宽 45 分钟，互不重叠，每场比赛最多各命中一次，不重复推送。

    返回：[(game, minutes_left, index), ...]
      index 1 = 还有约60分钟（窗口1）
      index 2 = 还有约15分钟（窗口2）
    """
    # 每个窗口：(展示分钟, 窗口下限, 窗口上限, 第几条)
    WINDOWS = [
        (60, 45, 90, 1),
        (15, 10, 45, 2),
    ]
    now = datetime.now(UTC_TZ)
    result = []
    for g in games:
        dt = parse_game_time(g)
        if dt is None:
            continue
        delta_min = (dt - now).total_seconds() / 60
        for display_min, lo, hi, idx in WINDOWS:
            if lo <= delta_min < hi:
                result.append((g, display_min, idx))
                break
    return result


# ── Telegram 推送 ─────────────────────────────────────────────────────────────
async def send_message(text: str) -> bool:
    """发送消息到 Telegram 频道，自动重试3次。"""
    bot = Bot(token=BOT_TOKEN)
    for attempt in range(3):
        try:
            await bot.send_message(chat_id=CHAT_ID, text=text)
            logger.info("消息推送成功 ✓")
            return True
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning("Telegram 频控，等待 %ds 后重试（第 %d 次）", wait, attempt + 1)
            await asyncio.sleep(wait)
        except TelegramError as e:
            logger.error("Telegram 发送失败: %s", e)
            return False
    return False


# ── 命令实现 ──────────────────────────────────────────────────────────────────
async def cmd_today(send: bool):
    """今日+明日赛程：打印并可选推送。"""
    from datetime import timedelta
    games = fetch_games()
    if not games:
        print("❌ 无法获取赛程数据，请检查网络或 API 状态")
        sys.exit(1)

    now_almaty   = datetime.now(ALMATY_TZ)
    today        = now_almaty.date()
    tomorrow     = (now_almaty + timedelta(days=1)).date()

    today_games    = get_games_by_date(games, today)
    tomorrow_games = get_games_by_date(games, tomorrow)
    msg = format_two_day_schedule(today_games, tomorrow_games)

    print("=" * 50)
    print(msg)
    print("=" * 50)
    logger.info("今日 %d 场 / 明日 %d 场", len(today_games), len(tomorrow_games))

    if send:
        success = await send_message(msg)
        if not success:
            sys.exit(1)
    else:
        print("\n（--print-today 模式，未实际推送到 Telegram）")


async def cmd_check_reminder():
    """
    检查赛前提醒（每 5 分钟定时运行）。
    有比赛在 14-19 分钟内开赛就推送，否则静默退出。
    """
    games = fetch_games()
    upcoming = get_reminder_games(games)

    if not upcoming:
        logger.info("当前无赛前提醒窗口命中")
        return

    logger.info("发现 %d 条赛前提醒需推送", len(upcoming))
    for g, minutes_left, index in upcoming:
        msg = format_reminder(g, minutes_left, index)
        print(msg)
        await send_message(msg)


async def cmd_test_reminder():
    """
    测试提醒：找最近一场未来的比赛，不管时间窗口，直接发提醒。
    用于验证消息格式和 Telegram 推送是否正常。
    """
    games = fetch_games()
    now = datetime.now(UTC_TZ)

    future = [
        (g, parse_game_time(g))
        for g in games
        if parse_game_time(g) and parse_game_time(g) > now
    ]

    if not future:
        print("⚠️  没有找到未来的比赛（可能 API 数据还未更新到下一届？）")
        return

    future.sort(key=lambda x: x[1])
    g, dt = future[0]
    delta_min = (dt - now).total_seconds() / 60

    home = team_cn(g.get("home_team_name_en", "?"))
    away = team_cn(g.get("away_team_name_en", "?"))
    print(f"找到最近一场比赛：{home} vs {away}")
    print(f"开赛时间（阿拉木图）：{to_almaty(dt).strftime('%Y-%m-%d %H:%M')}")
    print(f"距现在约 {delta_min:.0f} 分钟\n")

    # 测试模式：模拟第4条提醒（还有15分钟）
    msg = format_reminder(g, 15, 4)
    print("推送内容预览（模拟第4/5条，还有约15分钟）：")
    print("=" * 50)
    print(msg)
    print("=" * 50)

    await send_message(msg)


# ── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="2026 世界杯 Telegram 推送机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--print-today", action="store_true",
        help="打印今日赛程（不推送，用于调试）"
    )
    group.add_argument(
        "--today", action="store_true",
        help="推送今日赛程到 Telegram"
    )
    group.add_argument(
        "--check-reminder", action="store_true",
        help="检查赛前 15 分钟提醒（每 5 分钟定时运行）"
    )
    group.add_argument(
        "--test-reminder", action="store_true",
        help="测试：对最近一场未来比赛发提醒（验证格式用）"
    )
    args = parser.parse_args()

    if args.print_today:
        asyncio.run(cmd_today(send=False))
    elif args.today:
        asyncio.run(cmd_today(send=True))
    elif args.check_reminder:
        asyncio.run(cmd_check_reminder())
    elif args.test_reminder:
        asyncio.run(cmd_test_reminder())
