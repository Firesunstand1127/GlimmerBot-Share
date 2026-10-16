"""Verified shop purchases and resumable free refreshes."""

import re


class SpiritShopRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.progress = runtime._progress()

    def free_count(self):
        rows = self.runtime._ocr_results("DailyShopFreeRefreshScan")
        for row in rows:
            text = row.text.replace(" ", "").replace("（", "(").replace("）", ")")
            match = re.fullmatch(r"(?:免费)?刷新\((\d+)/\d+\)", text)
            if match:
                return int(match[1])
            if re.fullmatch(r"\d+刷新", text) and self.matches(
                "DailyShopDiamondRefreshFeature", [500, 1000, 75, 75]
            ):
                return 0
        raise RuntimeError("无法读取商店免费刷新余额")

    def purchase_page(self):
        r = self.runtime
        rows = r._ocr_results("DailyShopPriceScan")
        slots = set()
        for row in rows:
            price = row.text.replace(",", "").replace(".", "").replace(" ", "")
            if price not in {"10000", "20000", "10万"}:
                continue
            x, y, w, h = row.box
            if 610 <= y <= 690 or 910 <= y <= 990:
                slots.add((int((x + w / 2) // 240), int(y > 800)))
        for col, row in sorted(slots):
            cx, cy = 145 + col * 214, 530 + row * 300
            roi = [cx - 75, cy - 75, 155, 150]
            price_roi = [cx - 85, cy + 75, 170, 90]
            # A claimed item retains its price. Never treat price alone as buyable.
            if self.matches("DailyShopPurchasedFeature", roi):
                continue
            if not self.matches("DailyShopGoldFeature", price_roi):
                continue
            r._post_checked(
                "DailyShopInspectItem", {"DailyShopInspectItem": {"target": [cx, cy, 1, 1]}}
            )
            names = r._ocr_results("DailyShopItemNameScan")
            wanted = any(
                re.fullmatch(
                    r"(?:基础召唤[券卷]|进阶石|竞技场(?:挑战)?[券卷]|竞技挑战[券卷])",
                    item.text.replace(" ", ""),
                )
                for item in names
            )
            r._post_checked("DailyShopCloseItem")
            if not wanted:
                continue
            r._post_checked(
                "DailyShopBuyGold",
                {
                    "DailyShopGoldFeature": {"roi": price_roi},
                    "DailyShopBuyGold": {"target": [cx, cy + 115, 1, 1]},
                    "DailyShopPurchaseVerified": {"roi": roi},
                },
            )
            r.emit("OK", "精灵商店：金币商品购买已核验")

    def matches(self, name, roi):
        from maa.pipeline import JRecognitionType, JTemplateMatch

        r = self.runtime
        frame = r.controller.post_screencap().get(wait=True)
        if frame is None:
            raise RuntimeError("商店截图失败")
        templates = {
            "DailyShopPurchasedFeature": [
                "shop_purchased.png",
                "shop_purchased_current.png",
                "shop_purchased_stone.png",
            ],
            "DailyShopGoldFeature": ["shop_gold.png"],
            "DailyShopDiamondRefreshFeature": ["shop_refresh_diamond.png"],
        }[name]
        detail = r.tasker.post_recognition(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(
                template=["daily_task/" + t for t in templates],
                threshold=[0.85] * len(templates),
                roi=roi,
            ),
            frame,
        ).get(wait=True)
        return bool(detail and any(n.recognition and n.recognition.hit for n in detail.nodes))

    def run(self):
        r = self.runtime
        r._post_checked("DailyShopRefreshDaily")
        state = self.progress.get("shop")
        completed = state.get("completed", 0)
        if state.get("pending"):
            free = self.free_count()
            if free != state["before_free"] - 1:
                raise RuntimeError("商店上次刷新结果未确认，停止重复刷新")
            completed += 1
            self.progress.put("shop", completed=completed, pending=False)
        while completed <= 2:
            self.purchase_page()
            if completed == 2:
                r._post_checked("DailyShopStoreCycleDone")
                return
            before = self.free_count()
            if before <= 0:
                self.progress.put("shop", completed=completed, pending=False, exhausted=True)
                r.emit("OK", "精灵商店：当前页已检查，免费刷新耗尽；不使用钻石刷新")
                r._post_checked("DailyShopStoreCycleDone")
                return
            self.progress.put("shop", completed=completed, pending=True, before_free=before)
            r._post_checked("DailyShopRefresh1")
            after = self.free_count()
            if after != before - 1:
                raise RuntimeError(f"商店免费刷新未生效：{before}→{after}")
            completed += 1
            self.progress.put("shop", completed=completed, pending=False)
        raise RuntimeError("商店步骤记录超出两次刷新")
