#!/usr/bin/env python3
"""每日流水线的最小回归测试。纯标准库，`python3 scripts/test_pipeline.py`。

守的是「跑挂了要第二天才发现」的那几处：workflow 每天 21:37 UTC 自动跑，
出错的表现是没收到飞书卡片，等发现已经晚了一天。

默认全离线（只测纯函数）。加 --net 才跑真实的 gh 查询检查 —— 那条守的是
「查询词写坏了返回 0 条」这个已经犯过一次的错（3 个以上生僻词做 AND 匹配
会静默返回 0，"agent skills bioinformatics" 就是这么废掉的）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover  # noqa: E402
import notify  # noqa: E402
import render_viz  # noqa: E402


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {name}{'  — ' + detail if detail else ''}")
    return ok


def synth_seen() -> dict:
    """一份最小但字段齐全的 seen.json，够把渲染链路走通。"""
    today = dt.date.today()
    d = lambda n: (today - dt.timedelta(days=n)).isoformat()  # noqa: E731
    return {
        "acme/rocket": {  # 有完整分析 + 长历史，且够热 → 应进自动关注
            "first_seen": d(30), "stars_at_first_seen": 100,
            "matched_query": "scientific agent skills", "score": 9, "fit": 9,
            "category": "科研 skill 库", "one_liner": "一句话说明",
            "use_for": "能做什么", "usage": "怎么用", "example": "例子",
            "compare": "跟谁比", "overlap": "无重复", "verdict": "装：对口",
            "stars_history": [[d(30), 100], [d(20), 900], [d(10), 2000], [d(0), 3200]],
        },
        "acme/flat": {  # 走平，历史够长
            "first_seen": d(30), "stars_at_first_seen": 500, "matched_query": "x",
            "score": 4, "one_liner": "走平的项目",
            "stars_history": [[d(30), 500], [d(10), 505], [d(0), 506]],
        },
        "acme/bare": {  # 从没打过分，字段最少 —— 渲染不能因此崩
            "first_seen": d(1), "stars_at_first_seen": 80, "matched_query": "y",
            "score": None, "one_liner": None, "stars_history": [[d(1), 80]],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--net", action="store_true",
                    help="额外跑真实 gh 查询（每条查询一次网络往返，慢）")
    args = ap.parse_args()
    ok = True

    # --- 1. 打分解析：LLM 的输出永远可能缺字段或类型不对 ---
    print("打分解析:")
    good = ('[{"full_name":"a/b","score":8,"fit":9,"category":"c","what":"w",'
            '"use_for":"u","usage":"g","example":"e","compare":"p",'
            '"overlap":"o","verdict":"装：理由"}]')
    r = discover._parse_scores(good)
    ok &= check("完整 JSON 能解析", set(r) == {"a/b"})
    if r:
        e = r["a/b"]
        ok &= check("fit/overlap/verdict 都落地",
                    e["fit"] == 9 and e["overlap"] == "o" and e["verdict"].startswith("装"))
    r = discover._parse_scores('```json\n[{"full_name":"a/b","score":"7"}]\n```')
    ok &= check("带代码围栏 + score 是字符串", r.get("a/b", {}).get("score") == 7)
    r = discover._parse_scores('[{"full_name":"a/b"},{"no_name":1},{"full_name":"c/d","fit":null}]')
    ok &= check("缺字段/脏条目不炸", set(r) == {"a/b", "c/d"} and r["c/d"]["fit"] == 0,
                str(sorted(r)))

    # --- 2. 过滤与降噪 ---
    print("\n过滤降噪:")
    fresh = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat().replace("+00:00", "Z")
    ok &= check("star 不够的滤掉",
                not discover.filter_repo({"stargazersCount": 1, "updatedAt": fresh, "description": "x"}))
    ok &= check("太久没更新的滤掉",
                not discover.filter_repo({"stargazersCount": 999, "updatedAt": old, "description": "x"}))
    ok &= check("没描述的滤掉",
                not discover.filter_repo({"stargazersCount": 999, "updatedAt": fresh, "description": ""}))
    ok &= check("正常的留下",
                discover.filter_repo({"stargazersCount": 999, "updatedAt": fresh, "description": "x"}))
    ok &= check("awesome 合集判为噪音", discover.is_noise("a/awesome-things", "x"))
    ok &= check("specialist 不误伤（词边界）", not discover.is_noise("a/specialist", "x"))

    # --- 3. 星速与自动关注 ---
    print("\n星速/自动关注:")
    seen = synth_seen()
    rate = discover.daily_rate(seen["acme/rocket"])
    ok &= check("涨速算得出且为正", bool(rate and rate > 0), f"{rate:.0f}/天" if rate else "None")
    ok &= check("热门进自动关注", discover.is_watched(seen["acme/rocket"]))
    ok &= check("走平的不进", not discover.is_watched(seen["acme/flat"]))
    ok &= check("只有一个历史点时不算涨速", discover.daily_rate(seen["acme/bare"]) in (None, 0))

    # --- 4. 渲染：整条链路能出东西，且新字段真的出现在产物里 ---
    print("\n渲染:")
    html = render_viz.render(seen)
    ok &= check("HTML 渲得出来", len(html) > 2000, f"{len(html)} 字符")
    for frag in ("<!doctype html>", "Agent Discovery", "acme/rocket", "tiles", "全部追踪"):
        ok &= check(f"含「{frag}」", frag in html)
    ok &= check("fit 徽章出现在表里", "fit 9" in html)
    ok &= check("sparkline 渲出 svg", html.count("<svg") >= 2, f"{html.count('<svg')} 个")
    ok &= check("没打过分的 repo 不让渲染崩", "acme/bare" in html)
    sp = render_viz.sparkline([["2026-01-01", 1], ["2026-01-02", 5]])
    ok &= check("两点就能画 sparkline", sp.startswith("<svg"))
    ok &= check("一个点不画", render_viz.sparkline([["2026-01-01", 1]]) == "")
    ok &= check("空历史不画", render_viz.sparkline([]) == "")

    print("\n飞书卡片:")
    block = notify.repo_block(1, "acme/rocket", seen["acme/rocket"])
    ok &= check("卡片块生成", "acme/rocket" in block)
    ok &= check("fit 上了卡片标题", "fit 9" in block, block.split("\n")[0][-40:])
    ok &= check("verdict 上了卡片", "装不装" in block)
    ok &= check("字段缺失时不炸", "acme/bare" in notify.repo_block(2, "acme/bare", seen["acme/bare"]))

    # --- 5. 查询词（要网络）：3 个以上生僻词做 AND 会静默返回 0 ---
    if args.net:
        print("\n查询词实际能返回结果（--net）:")
        cs = (dt.date.today() - dt.timedelta(days=discover.CREATED_WITHIN_DAYS)).isoformat()
        for q, _w in discover.RESEARCH_QUERIES:
            q = q.format(created_since=cs)
            n = len(discover.gh_search(q, limit=30))
            ok &= check(f"{q!r}", n > 0, f"{n} 条" if n else "返回 0 条 —— 查询词废了")
    else:
        print("\n（跳过查询词检查；加 --net 跑，改了 RESEARCH_QUERIES 后必须跑一次）")

    print(f"\n{'全部通过' if ok else '有失败项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
