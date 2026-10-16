"""Read only the role-name crop; full-screen OCR may merge the adjacent VIP badge."""

import re

from task_config import ACCOUNT_PRESETS, DAILY_ACCOUNT_KEYS


def identify_home(runtime, expected=None):
    from maa.pipeline import JOCR, JRecognitionType

    def normalize(text):
        return re.sub(r"[\W_丶]", "", text)

    unreadable = 0
    for _ in range(24):
        runtime._check_cancelled()
        shot = runtime.controller.post_screencap()
        runtime._wait_job(shot, 10)
        frame = shot.get()
        if frame is None:
            raise RuntimeError("主城角色截图失败")
        # Supply the actual crop: on the failure frame, ROI OCR misses the name
        # while the same pixels as a standalone image read it correctly.
        job = runtime.tasker.post_recognition(
            JRecognitionType.OCR, JOCR(), frame[0:55, 170:345].copy()
        )
        runtime._wait_job(job, 10)
        detail = job.get()
        names = (
            {
                normalize(row.text)
                for node in detail.nodes
                if node.recognition
                for row in node.recognition.all_results
            }
            if detail
            else set()
        )
        matches = [
            key
            for key in DAILY_ACCOUNT_KEYS
            if normalize(ACCOUNT_PRESETS[key]["role_name"]) in names
        ]
        if len(matches) == 1:
            if expected and matches[0] != expected:
                raise RuntimeError(f"当前角色与任务账号不一致：{matches[0]} / {expected}")
            return matches[0]
        if names & {"日常完成", "周常完成"}:
            # Assistant completions queue several banners over the role name.
            # Wait for that known foreground; never use it as an account ID.
            runtime._cancel_event.wait(1)
            continue
        unreadable += 1
        if unreadable >= 3:
            break
        runtime._cancel_event.wait(0.4)
    raise RuntimeError(f"主城角色裁剪识别失败，未操作或记账：{sorted(names)}")
