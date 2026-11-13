"""
用 OpenAI TTS 合成测试录音 (合成语音, 不用真人或小朋友的声音)。

每个分句单独合成, 分句之间插入停顿, 模拟真人说话时分句之间的停顿;
输出 24 kHz 单声道 PCM16 WAV (实时转写接口只支持 24 kHz, 整句识别也能用)。

用法 (需要 OPENAI_API_KEY):
    python -m scripts.make_test_audio            # 生成到 tests/fixtures/audio/
    python -m scripts.make_test_audio --force    # 覆盖已有文件
"""

from __future__ import annotations

import argparse
import io
import re

import numpy as np
import soundfile as sf
import truststore
from dotenv import load_dotenv
from openai import OpenAI

from pc import constants

SAMPLE_RATE = 24000
PAUSE_S = 0.6
EDGE_SILENCE_S = 0.3
OUT_DIR = constants.project_root_path / "tests" / "fixtures" / "audio"

# 文件名 -> 句子 (用 "，" 分隔分句)
SENTENCES: dict[str, str] = {
    "l1_forward_30": "向前走三十厘米",
    "l1_backward_20": "往后退二十厘米",
    "l1_left_90": "向左转九十度",
    "l1_right_45": "向右转四十五度",
    "l1_gripper_up": "把夹子抬起来",
    "l1_gripper_down": "把夹子放下来",
    "l1_left_gripper_up": "左夹子抬起来",
    "l1_right_gripper_down": "右夹子放下来",
    "l1_stop": "停！",
    "l2_forward_right": "向前走五十厘米，然后向右转九十度",
    "l2_backward_gripper": "往后退十厘米，再把夹子抬起来",
    "l2_left_forward": "向左转一百八十度，然后向前走二十厘米",
    "l2_grippers": "左夹子抬起来，右夹子放下来",
    "l3_gift": "把夹子放下来，向前走二十厘米，把夹子抬起来，再往后退二十厘米",
    "l3_wave": "左夹子抬起来，左夹子放下来，右夹子抬起来，右夹子放下来",
    "l3_dance": "向左转四十五度，向右转九十度，向左转四十五度，左夹子和右夹子都抬起来",
    "l3_square": "向前走三十厘米再左转九十度，一共做四次",
    "mixed_dance": "前进三十厘米，然后跳个舞",
    # 任务层 (改动 task-layer): 目标级请求
    "l4_task2": "完成任务二",
    "l4_yellow_home": "把黄色小人送回家",
    "l4_red_to_d": "把红色的送到D区",
}

# 唤醒模式用的录音, 放在 wake/ 子目录 (replay_audio 默认只回放上一级目录的文件)
WAKE_DIR = OUT_DIR / "wake"
WAKE_SENTENCES: dict[str, str] = {
    "wake_only": "小蓝小蓝",
    "wake_hello": "你好小蓝",
    "wake_forward_30": "小蓝小蓝，前进三十厘米",
    "wake_forward_30_fast": "小蓝小蓝前进三十厘米",  # 不停顿, 一口气说完
    "wake_in_middle": "嗯那个，我想想，小蓝小蓝，向左转九十度",
    "wake_stop": "急停",
    "chat_lunch": "今天天气真不错，我们中午去吃面条吧",
    "chat_homework": "小明你好，你的作业写完了吗，快去写作业",
    "chat_blue": "把蓝色的积木放到桌子上，然后去洗手",
    "chat_long": (
        "昨天我们去公园玩了，那里有很多小朋友在放风筝，"
        "还有人在湖边钓鱼，我们一直玩到天黑才回家"
    ),
}


def synthesize(client: OpenAI, text: str) -> np.ndarray:
    resp = client.audio.speech.create(
        model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="wav"
    )
    data, sr = sf.read(io.BytesIO(resp.content), dtype="int16")
    if sr != SAMPLE_RATE:
        raise RuntimeError(f"unexpected TTS sample rate {sr}")
    return data


def make(client: OpenAI, sentence: str) -> np.ndarray:
    clauses = [c for c in re.split(r"[，,]", sentence) if c.strip()]
    pause = np.zeros(int(PAUSE_S * SAMPLE_RATE), dtype=np.int16)
    edge = np.zeros(int(EDGE_SILENCE_S * SAMPLE_RATE), dtype=np.int16)
    parts = [edge]
    for i, clause in enumerate(clauses):
        if i:
            parts.append(pause)
        parts.append(synthesize(client, clause))
    parts.append(edge)
    return np.concatenate(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    truststore.inject_into_ssl()  # 公司网络 TLS 拦截: 使用系统证书库
    load_dotenv(constants.project_root_path / ".env")
    client = OpenAI()
    for out_dir, sentences in ((OUT_DIR, SENTENCES), (WAKE_DIR, WAKE_SENTENCES)):
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, sentence in sentences.items():
            path = out_dir / f"{name}.wav"
            if path.exists() and not args.force:
                continue
            audio = make(client, sentence)
            sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
            print(f"{path.name}: {len(audio) / SAMPLE_RATE:.1f}s  {sentence}")

        # 句子文本一起保存, 回放测试用来核对
        (out_dir / "sentences.txt").write_text(
            "\n".join(f"{k}\t{v}" for k, v in sentences.items()) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
