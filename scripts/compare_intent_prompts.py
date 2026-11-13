"""
对比旧提示词 (把 skills frontmatter 全部塞进去) 和新的紧凑提示词在同一组例句上的解析结果。

用法 (需要 OPENAI_API_KEY):
    python -m scripts.compare_intent_prompts --model gpt-6-luna

输出每句话两种提示词得到的命令序列、是否一致，以及各自的耗时和提示词长度。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

from pc import constants
from pc.llm import intent_mapper, intent_parser
from pc.llm.llm_client import LLMClient
from pc.utils.utils import normalize_text

SENTENCES = [
    # docs 第 1-3 级
    "向前走 30 厘米", "往后退 20 厘米", "向左转 90 度", "向右转 45 度",
    "把夹子抬起来", "把夹子放下来", "左夹子抬起来", "右夹子放下来", "停！",
    "向前走 50 厘米，然后向右转 90 度", "往后退 10 厘米，再把夹子抬起来",
    "向左转 180 度，然后向前走 20 厘米", "左夹子抬起来，右夹子放下来",
    "把夹子放下来，向前走 20 厘米，把夹子抬起来，再往后退 20 厘米",
    "左夹子抬起来，左夹子放下来，右夹子抬起来，右夹子放下来",
    "向左转 45 度，向右转 90 度，向左转 45 度，左夹子和右夹子都抬起来",
    "向前走 30 厘米再左转 90 度，一共做四次",
    # 快速通道不覆盖的复杂说法
    "向前走半米然后掉头",
    "原地转一圈",
    "往前开一点五米再向左拐个直角",
    "拍张照片然后后退二十厘米",
    "先抬起左边的爪子再把右边的放下",
]


def build_legacy_prompt(user_text: str) -> str:
    """改动前 LLMClient.build_intent_prompt 的原样复刻。"""
    blocks: list[str] = []
    for p in sorted((constants.project_root_path / "skills").glob("*.skill.md")):
        text = p.read_text(encoding="utf-8").lstrip()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        fm = parts[1].strip()
        if fm:
            blocks.append(f"[{p.name}]\n{fm}")
    skills_context = "\n\n".join(blocks)
    return (
        "你是机器人动作解析器。请严格遵循以下技能定义与约束，"
        "把用户输入转换为合法 JSON。\n"
        "只允许输出一个 JSON 对象，不要 markdown，不要解释。\n\n"
        "可用技能定义（来自 skills 目录 frontmatter）:\n"
        f"{skills_context}\n\n"
        f"用户输入: {user_text}"
    )


def to_commands(llm_text: str) -> list[str]:
    seq = intent_mapper.intent_to_sequence(intent_parser.parse_intent(llm_text))
    return [item["cmd"] for item in seq["sequence"]]


async def run(model: str, timeout: float) -> int:
    client = LLMClient(default_model=model, timeout=int(timeout))
    if not client.openai_api_key:
        print("需要 OPENAI_API_KEY")
        return 2

    legacy_len = len(build_legacy_prompt(""))
    new_len = len(client.intent_instructions)
    print(f"模型: {model}  提示词长度: 旧 {legacy_len} 字符 / 新 {new_len} 字符\n")

    same = 0
    legacy_times: list[float] = []
    new_times: list[float] = []
    for sentence in SENTENCES:
        text = normalize_text(sentence)

        t0 = time.perf_counter()
        legacy_out = await asyncio.wait_for(
            client._call_openai(
                [{"role": "user", "content": build_legacy_prompt(text)}], model
            ),
            timeout,
        )
        legacy_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        new_out = await client.generate(text, model=model, timeout=int(timeout))
        new_times.append(time.perf_counter() - t0)

        legacy_cmds, new_cmds = to_commands(legacy_out), to_commands(new_out)
        ok = legacy_cmds == new_cmds
        same += ok
        mark = "一致" if ok else "不同"
        print(f"[{mark}] {sentence}")
        print(f"    旧: {legacy_cmds}")
        if not ok:
            print(f"    新: {new_cmds}")

    print(f"\n一致: {same}/{len(SENTENCES)}")
    print(
        f"耗时中位数: 旧 {statistics.median(legacy_times) * 1000:.0f} ms / "
        f"新 {statistics.median(new_times) * 1000:.0f} ms"
    )
    await client.aclose()
    return 0 if same == len(SENTENCES) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.model, args.timeout)))


if __name__ == "__main__":
    if str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    main()
