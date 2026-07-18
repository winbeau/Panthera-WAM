"""armd 命令行入口。"""

from __future__ import annotations

import argparse
import json
import time

from .backend import SimBackend
from .hardware_loop import HardwareLoop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Panthera-HT armd 守护服务")
    parser.add_argument("--sim", action="store_true", help="使用无需真机的仿真后端")
    parser.add_argument("--control-hz", type=float, default=200.0, help="控制循环频率（默认 200Hz）")
    parser.add_argument("--check", action="store_true", help="启动后做一次仿真自检并退出")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.sim:
        raise SystemExit("真机后端尚未落地；当前请使用 armd --sim")

    loop = HardwareLoop(SimBackend, control_hz=args.control_hz)
    loop.start()
    try:
        if args.check:
            if not loop.wait_for_cycles(3):
                raise SystemExit("仿真控制循环未能按期推进")
            state = loop.latest_state()
            stats = loop.stats()
            print(
                json.dumps(
                    {
                        "sim": True,
                        "motors": len(state.motors) if state is not None else 0,
                        "cycles": stats.cycles,
                        "actual_hz": round(stats.actual_hz, 2),
                        "overruns": stats.overruns,
                    },
                    ensure_ascii=False,
                )
            )
            return

        print(f"armd 仿真 HardwareLoop 已启动：{args.control_hz:g}Hz（Ctrl+C 停止）")
        while loop.is_running:
            time.sleep(0.5)
            if loop.failure is not None:
                raise RuntimeError("HardwareLoop 异常退出") from loop.failure
    except KeyboardInterrupt:
        pass
    finally:
        loop.stop()


if __name__ == "__main__":
    main()
