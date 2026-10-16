"""Task definitions, account whitelist and fixed device configuration."""

from pathlib import Path

from share_settings import APP_NAME as APP_NAME
from share_settings import load_settings

SHARE_CONFIG = load_settings()
DEVICE = SHARE_CONFIG["device"]
MUMU_PATH = SHARE_CONFIG["mumu_path"]
MUMU_INDEX = SHARE_CONFIG["mumu_index"]
GALAXY_TARGET = SHARE_CONFIG["galaxy_target"]
GAME_PACKAGE = "com.sszg.tt.ffsdk01"
GAME_ACTIVITY = "com.sszg.tt.ffsdk01/com.game.sdk.GameSdkActivity"
ADB = Path(MUMU_PATH) / "shell" / "adb.exe"
PUBLIC_ENTRIES = {
    "RankingLikesDaily",
    "TournamentPredictionDaily",
    "GameBootstrap",
    "TaskAssistantDaily",
    "IdleTicketDaily",
    "IdleIncomeClaimDaily",
    "VoyageDaily",
    "FastBattleVoyageDaily",
    "EndlessTrialDaily",
    "DailyInitialClaim",
    "DailySummonDaily",
    "DailyArenaDaily",
    "ArenaDaily",
    "DailyShopRefreshDaily",
    "PlaneExpeditionDaily",
    "DailyFinalClaim",
    "MailClaimDaily",
    "CrossServerLadderDaily",
    "TeamArenaDaily",
    "SuperRankDaily",
    "WorldArenaDaily",
    "AbyssDungeonDaily",
    "ElementTempleDaily",
    "DomainSpiritDaily",
    "AccountSwitchOpenSdk",
    "AccountSwitchSdkCurrentScan",
    "AccountSwitchExpandAccountList",
    "AccountSwitchListReady",
    "AccountSwitchListScanAll",
    "AccountSwitchSelectAccount",
    "AccountSwitchEnterGame",
    "AccountSwitchHomeVisible",
    "AccountServerVerify",
}

ACCOUNT_PRESETS = SHARE_CONFIG["accounts"]
DAILY_ACCOUNT_KEYS = tuple(ACCOUNT_PRESETS)

TASK_COMPLETION_REVISIONS = {
    "ArenaDaily": 2,
    "DailyShopRefreshDaily": 2,
    "DailyQuestDaily": 3,
}

FIXED_SERVER = SHARE_CONFIG["server"]

TASKS = [
    (
        "排行榜点赞",
        "竞技场前10名；在线锦标赛、巅峰冠军赛、天梯英雄殿及世界竞技场前三名，按实际页面核验；世界榜未显示时下次重查",
        "RankingLikesDaily",
        True,
    ),
    (
        "主城提醒",
        "识别黄框提醒，处理两类已支持竞猜并记录未知事项；按账号和轮次核验竞猜，支持空闲常驻检查",
        "TournamentPredictionDaily",
        True,
    ),
    (
        "竞技场",
        "确保勾选跳过战斗后完成3场；每场优先选择战力低于我方的对手，刷新3次仍没有时选择任意对手",
        "ArenaDaily",
        True,
    ),
    ("任务助手", "打开助手并执行已配置的日常", "TaskAssistantDaily", True),
    (
        "领取抽奖券",
        "单独检查出击页的每日抽奖券；入口已消失或今日已领取时正常跳过",
        "IdleTicketDaily",
        True,
    ),
    (
        "快速战斗远航",
        "先将远航值消耗到2000以下，领取挂机收益后再清空远航值，用完快速战斗次数并再次清空远航值；不用钻石加速远航",
        "FastBattleVoyageDaily",
        True,
    ),
    (
        "领取挂机收益",
        "进入出击挂机页后，先将远航值消耗到2000以下，再确保快捷领取已勾选并领取收益",
        "IdleIncomeClaimDaily",
        True,
    ),
    ("无尽试炼", "右侧元素试炼挑战一次，选择第一项增益后返回历练", "EndlessTrialDaily", True),
    (
        "深渊幽城",
        "进入历练的深渊幽城，使用当前全部免费体力执行 Max 扫荡；不购买体力",
        "AbyssDungeonDaily",
        True,
    ),
    (
        "元素圣殿",
        "工作日首通今日圣殿；周六各圣殿推进两层；周日扫荡最高已通关层；失败后使用剩余免费次数扫荡，不购买次数",
        "ElementTempleDaily",
        True,
    ),
    (
        "域灵日常",
        "域灵试炼挑战3次并跳过战斗，领取阶段与荣院每日奖励；域灵柱升级至资源不足，返回域灵主页和主城",
        "DomainSpiritDaily",
        True,
    ),
    (
        "精灵商店",
        "购买基础召唤券、紫色石头和竞技场券三种金币商品，并完成每日刷新2次",
        "DailyShopRefreshDaily",
        True,
    ),
    (
        "位面征战",
        "逐格优先打怪，固定困难；每周期最多复活一次，复活后再败则结束",
        "PlaneExpeditionDaily",
        True,
    ),
    (
        "日常任务",
        "按13项日常实际分值核账；完成召唤、竞技场和商店刷新。最后只领已达标宝箱，活跃不足记待完成并返回；不追加购买快速作战次数",
        "DailyQuestDaily",
        True,
    ),
    ("邮件领取", "领取全部附件，阅读并删除剩余未读邮件，完成后回到主城", "MailClaimDaily", True),
    (
        "跨服天梯",
        "开放时间：周一00:05至周日23:00；使用全部免费挑战次数，从上到下选择战力低于自身的对手，不购买次数",
        "CrossServerLadderDaily",
        True,
    ),
    (
        "组队斗技",
        "组队期等待，挑战期使用免费次数并领取挑战宝箱；补齐隐藏敌人战力，比较六种对位争取两胜。支持已配置账号立即执行或攒到5次常驻清空，以页面关闭倒计时清尾。",
        "TeamArenaDaily",
        True,
    ),
    (
        "超凡段位",
        "开放时间：每日00:00至23:30；使用全部免费匹配次数，前三次不能跳过时等待战斗结束，不购买次数",
        "SuperRankDaily",
        True,
    ),
    (
        "世界竞技场",
        "开放时间：每周一、三、五、日全天；自动勾选挑战3次后跳过，用完全部免费匹配次数，不购买次数",
        "WorldArenaDaily",
        True,
    ),
]

# Resolve tournament reminders before the assistant opens its own panel.
# The assistant then precedes all ordinary daily tasks.
TASK_PRIORITY = {
    "TournamentPredictionDaily": -1,
    "TaskAssistantDaily": 0,
    "ArenaDaily": 1,
    # Activity points are produced by the other daily modules.  Keep the
    # whole daily-quest pass (including its final chest sweep) at the end so
    # an unearned chest is never treated as an early-run failure.
    "DailyQuestDaily": 99,
}
TASKS.sort(key=lambda task: TASK_PRIORITY.get(task[2], 2))
