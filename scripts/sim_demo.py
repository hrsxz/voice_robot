"""
在模拟器里跑一整局: 把物体依次送到各自的默认目标, 最后回起终点区 (改动 navigation-grasp)。
不需要摄像头和小车。

用法:
    python -m scripts.sim_demo --show                       # Starter 2022 布局, 实时显示俯视图
    python -m scripts.sim_demo --layout random --seed 3 --show
    python -m scripts.sim_demo --objects red_robot yellow_person --save logs/sim.png
    python -m scripts.sim_demo --stats 10                   # 统计: Starter 2022 和 10 个随机布局
    python -m scripts.sim_demo --no-noise                   # 没有运动误差和测量噪声

默认带误差: 直行和转向 ±5%, 位姿噪声 0.3 cm / 1°, 物体位置噪声 0.3 cm (每个种子不同)。
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

from pc.field.layout import load_layout, random_layout
from pc.field.model import load_field
from pc.sim.mission import DEFAULT_ORDER, MissionResult, run_mission
from pc.sim.simulator import SimConfig

NOISY = dict(distance_error=0.05, turn_error=0.05, pose_noise_cm=0.3, heading_noise_deg=1.0,
             object_noise_cm=0.3)


def config(seed: int, noise: bool, time_scale: float = 0.0) -> SimConfig:
    return SimConfig(seed=seed, time_scale=time_scale, **(NOISY if noise else {}))


def print_result(r: MissionResult) -> None:
    for o in r.outcomes:
        mark = "OK " if o.id in r.delivered else "失败"
        extra = "" if o.status == "ok" else f"  {o.reason}"
        print(f"  [{mark}] {o.id:14s} → {o.target:14s} {o.sim_time_s:6.1f} s{extra}")
    parked = "是" if r.parked else "否"
    print(f"  送达 {len(r.delivered)}/{len(r.outcomes)}，回到起终点区：{parked}，"
          f"总时间 {r.total_s:.0f} s（模拟），命令 {r.commands} 条，"
          f"碰到的树 / 障碍杆：{', '.join(r.knocked) or '无'}")


async def run_one(args) -> int:
    field = load_field()
    layout = (load_layout(args.layout, field) if args.layout != "random"
              else random_layout(field, args.seed))
    order = tuple(args.objects) if args.objects else DEFAULT_ORDER
    if args.objects:
        layout_objects = {k: v for k, v in layout.objects.items()}
        missing = [o for o in args.objects if o not in layout_objects]
        if missing:
            print(f"布局里没有这些物体：{missing}")
            return 2

    view = None
    on_step = None
    if args.show:
        view_holder: dict = {}

        def on_step(sim):
            view_holder["view"].update(force=True)

        cfg = config(args.seed, not args.no_noise, time_scale=args.speed)
    else:
        cfg = config(args.seed, not args.no_noise)

    # 实时显示: 在模拟器每条命令之后刷新
    import pc.sim.mission as mission

    orig_build = mission.build

    def build(*a, **k):
        sim, world, nav, manip = orig_build(*a, **k)
        if args.show:
            from pc.sim.viewer import LiveView
            nonlocal view
            view = LiveView(sim)
            view_holder["view"] = view
            orig = sim._move_robot

            def moved(new, direction):
                orig(new, direction)
                view.update()
            sim._move_robot = moved
        return sim, world, nav, manip

    mission.build = build
    try:
        result = await run_mission(field, layout, cfg, order=order,
                                   log=print if args.verbose else (lambda _m: None),
                                   on_step=on_step)
    finally:
        mission.build = orig_build
    print(f"布局：{layout.name}，种子：{args.seed}，误差：{'无' if args.no_noise else '有'}")
    print_result(result)
    if args.save and result.sim is not None:
        from pc.sim.viewer import save
        print(f"俯视图：{save(result.sim, args.save)}")
    if view is not None:
        import cv2
        print("按任意键关闭窗口")
        view.update(force=True)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    return 0


async def run_stats(args) -> int:
    field = load_field()
    noise = not args.no_noise
    rows = []
    seeds = list(range(1, args.stats + 1))
    for seed in seeds[: max(3, min(len(seeds), 5))]:
        rows.append(("starter2022", seed, await run_mission(
            field, load_layout("starter2022", field), config(seed, noise))))
        print(f"starter2022 种子 {seed}: {len(rows[-1][2].delivered)}/10", flush=True)
    for seed in seeds:
        r = await run_mission(field, random_layout(field, seed), config(seed, noise))
        rows.append((f"random-{seed}", seed, r))
        print(f"random-{seed}: {len(r.delivered)}/10", flush=True)

    def summary(name, items):
        delivered = sum(len(r.delivered) for _, _, r in items)
        total = sum(len(r.outcomes) for _, _, r in items)
        times = [r.total_s for _, _, r in items]
        knocked = sum(len(r.knocked) for _, _, r in items)
        parked = sum(r.parked for _, _, r in items)
        print(f"| {name} | {len(items)} | {delivered}/{total}（{100 * delivered / total:.0f}%） | "
              f"{parked}/{len(items)} | {statistics.median(times):.0f} s | {knocked} |")

    print("\n| 布局 | 局数 | 送达 | 回到起终点区 | 每局总时间中位数（模拟） "
          "| 碰到树/障碍杆（次） |")
    print("|---|---|---|---|---|---|")
    summary("Starter 2022", [x for x in rows if x[0] == "starter2022"])
    summary("随机布局", [x for x in rows if x[0] != "starter2022"])
    reasons: dict[str, int] = {}
    for _, _, r in rows:
        for o in r.outcomes:
            if o.id not in r.delivered:
                key = o.reason.split("（")[0] if o.reason else "放下后没有接触目标"
                reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("\n失败原因：")
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"- {k}：{v} 次")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--layout", default="starter2022", help="布局名称，或 random")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--objects", nargs="*", help="只送这些物体（按顺序）")
    parser.add_argument("--no-noise", action="store_true")
    parser.add_argument("--show", action="store_true", help="实时显示俯视图（OpenCV 窗口）")
    parser.add_argument("--speed", type=float, default=0.1,
                        help="显示时的速度：真实时间 = 模拟时间 × speed（默认 0.1，十倍速）")
    parser.add_argument("--save", default=None, help="结束后把俯视图存成 PNG")
    parser.add_argument("--stats", type=int, default=0,
                        help="统计模式：Starter 2022 布局跑几局 + N 个随机布局")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
    runner = run_stats(args) if args.stats else run_one(args)
    sys.exit(asyncio.run(runner))


if __name__ == "__main__":
    main()
