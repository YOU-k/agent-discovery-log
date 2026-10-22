#!/usr/bin/env python3
"""小红书封面夜班自动生成。

读 publish/{date}-xhs.md 末尾的「> 首图建议：」那行，拼上固定的深色蓝图风
基底 prompt，调火山方舟 ARK 的 Seedream 生图，再用 PIL 把标题和日期叠上去
（文字绝不交给生图模型——它写汉字必乱码）。产出两份：
    publish/{date}-cover.png        —— 跟文案放一起，飞书里给链接
    docs/covers/{date}-cover.png    —— GitHub Pages 源，公开 URL

设计原则跟流水线其它部分一致：任何一步失败只 WARN 并以 0 退出，绝不炸掉
每晚的 discovery。封面已存在且不带 --force 时直接跳过（幂等）。

Usage:
    python3 scripts/gen_cover.py [--date YYYY-MM-DD] [--force]

Env:
    ARK_API_KEY / LLM_API_KEY    — 生图 key（CI 里就是 LLM_API_KEY）
    ARK_BASE_URL / LLM_BASE_URL  — 默认 https://ark.cn-beijing.volces.com/api/v3
    IMAGE_MODEL                  — 默认 doubao-seedream-5-0-flash-260915
    IMAGE_SIZE                   — 默认 1728x2304（3:4 竖版）
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "publish"
DOCS_COVERS = ROOT / "docs" / "covers"

BASE_URL = (os.environ.get("ARK_BASE_URL") or os.environ.get("LLM_BASE_URL")
            or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
API_KEY = os.environ.get("ARK_API_KEY") or os.environ.get("LLM_API_KEY") or ""
IMAGE_MODEL = os.environ.get("IMAGE_MODEL") or "doubao-seedream-5-0-flash-260915"

# 3:4 竖版。Seedream 4.x 的显式尺寸下限在 1280 边长量级，864x1152 是
# Seedream 3.0 的合法档位，放后面兜底；都失败再试 "2K" 令牌写法。
SIZE_CANDIDATES = [os.environ.get("IMAGE_SIZE") or "1728x2304", "864x1152", "2K"]

# 固定风格基底：单一清晰主体的等距 3D 物件隐喻。
# 教训（2026-10-22 首版被用户否了）：不要"线框图/终端/界面截图"类主题——
# 禁了文字之后界面图只剩一堆没人认识的抽象框框；必须让模型画"一个具体的东西"。
STYLE_BASE = (
    "竖版构图，深海军蓝到近黑渐变背景，画面中央只有一个清晰、巨大、"
    "一眼可辨的立体主体，等距 3D 渲染风格，柔和体积光，青色与暖橙色边缘光，"
    "细节精致有质感，电影级打光，周围大量留白。"
    "绝对不要画电脑屏幕、终端、代码、窗口或任何软件界面，"
    "把主题转化成一个具体的、现实中的物件隐喻。"
    "no text, no watermark, no letters, no words. 主题："
)

MAX_BYTES = 5 * 1024 * 1024  # 超过就降尺寸重存，Pages 上别挂太大的图


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"[INFO] {msg}", file=sys.stderr)


def parse_xhs_md(path: Path) -> tuple[str, str, str] | None:
    """从 xhs 草稿里抽 (H1 标题, 首图建议, 开篇段落)。抽不到首图建议返回 None。"""
    if not path.exists():
        warn(f"草稿不存在：{path}")
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^>\s*首图建议[：:]\s*(.+)$", text, re.MULTILINE)
    if not m:
        warn(f"{path.name} 里没找到「> 首图建议：」行")
        return None
    idea = m.group(1).strip()
    h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = h1.group(1).strip() if h1 else ""
    intro = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if para and not para.startswith(("#", ">", "---")):
            intro = para[:400]
            break
    return title, idea, intro


def scene_idea(title: str, idea: str, intro: str) -> str:
    """把「首图建议」改写成真实使用场景（一次廉价 LLM 调用），失败则用原句。

    教训（2026-10-22）：直接拿 cover_idea 画出来的是抽象概念（玻璃舱插件），
    和工具实际用途无关。封面应该画「这个工具被人真实使用的画面」。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import discover  # llm_chat；未配置 key 时返回 ""
        out = discover.llm_chat(
            "你在给一篇工具深扒文章设计封面图。请把下面的项目介绍转化成一句"
            "「图像生成用的场景描述」（中文，≤60字）：画出这个工具被人真实使用的"
            "画面，主体是一个具体的人或物在动手用它解决它专治的问题；"
            "不要屏幕/终端/窗口等界面，不要抽象符号和隐喻堆砌。"
            f"\n\n文章标题：{title}\n项目介绍：{intro}\n原首图建议：{idea}"
            "\n\n只输出场景描述一句，不要其他内容。",
            max_tokens=150, json_mode=False,
        )
        if out and len(out.strip()) >= 8:
            return out.strip().strip('"')
    except Exception as e:  # 场景改写是增强，任何失败都落回原句
        warn(f"场景改写失败，用原首图建议：{e}")
    return idea


def call_images_api(prompt: str) -> bytes | None:
    """调 ARK images/generations，按候选尺寸依次重试。返回 PNG bytes。"""
    if not API_KEY:
        warn("ARK_API_KEY / LLM_API_KEY 都没配，跳过生图")
        return None
    last_err = ""
    for size in SIZE_CANDIDATES:
        payload = {
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
            "watermark": False,
        }
        req = urllib.request.Request(
            BASE_URL + "/images/generations",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {API_KEY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            last_err = f"HTTP {e.code}: {body}"
            # 400 多半是 size 不合法，换下一个候选；404 是模型没权限，重试无用
            if e.code != 400:
                break
            info(f"size {size} 被拒（{body[:80]}），换下一个候选")
            continue
        except OSError as e:
            last_err = str(e)
            break
        data = (resp.get("data") or [{}])[0]
        if data.get("b64_json"):
            info(f"生图成功（model={IMAGE_MODEL} size={size} b64_json）")
            return base64.b64decode(data["b64_json"])
        if data.get("url"):
            try:
                with urllib.request.urlopen(data["url"], timeout=120) as r:
                    info(f"生图成功（model={IMAGE_MODEL} size={size} url）")
                    return r.read()
            except OSError as e:
                last_err = f"下载图片失败：{e}"
                break
        last_err = f"响应里既没有 b64_json 也没有 url：{str(resp)[:200]}"
        break
    warn(f"ARK 生图失败：{last_err}")
    return None


def find_cjk_font() -> str | None:
    """通过 fontconfig 找任意可用 CJK 字体；优先 Noto Sans CJK。"""
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK SC"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        # fc-match 找不到时会回退到非 CJK 字体，得验一下
        if out and Path(out).exists() and "cjk" in out.lower():
            return out
        out = subprocess.run(
            ["fc-list", ":lang=zh", "file"], capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            p = line.split(":")[0].strip()
            if p and Path(p).exists():
                return p
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def find_latin_font() -> str | None:
    """ASCII 用的西文字体。有些 CJK 字体（如 Droid Sans Fallback）不含
    Latin 字形，直接渲染英文/数字全是豆腐块，得混排。"""
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", "DejaVu Sans"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if out and Path(out).exists():
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def overlay_text(png: bytes, title: str, date: str) -> bytes:
    """PIL 叠字：标题左上角、日期左下角，垫半透明色块保证可读。
    中英文混排：ASCII 走西文字体，其余走 CJK 字体（有的 CJK 字体不含
    Latin 字形，单字体渲染会出豆腐块）。找不到 CJK 字体就只出底图。"""
    from PIL import Image, ImageDraw, ImageFont  # 延迟导入：没装 PIL 时底图还能出

    import io
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    font_path = find_cjk_font()
    if not font_path:
        warn("没找到任何 CJK 字体（fc-list :lang=zh 为空），跳过叠字只出底图")
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()
    latin_path = find_latin_font()
    title_size = max(28, w // 16)
    date_size = max(20, w // 30)
    f_cjk = ImageFont.truetype(font_path, title_size)
    f_lat = (ImageFont.truetype(latin_path, title_size)
             if latin_path else f_cjk)
    f_cjk_d = ImageFont.truetype(font_path, date_size)
    f_lat_d = (ImageFont.truetype(latin_path, date_size)
               if latin_path else f_cjk_d)

    def pick(ch: str, lat, cjk):
        return lat if ord(ch) < 0x2E80 else cjk  # 0x2E80 以下基本是 ASCII/拉丁

    pad = w // 24
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    def textlen(text: str, lat, cjk) -> float:
        return sum(d.textlength(ch, font=pick(ch, lat, cjk)) for ch in text)

    def draw_mixed(x: float, y: float, text: str, lat, cjk, fill) -> None:
        for ch in text:
            f = pick(ch, lat, cjk)
            d.text((x, y), ch, font=f, fill=fill)
            x += d.textlength(ch, font=f)

    line_h = f_cjk.getbbox("国Ag")[3] + 8
    # 标题超长就折成两行：优先在空格/标点处断，别从单词中间劈开
    lines = [title]
    if textlen(title, f_lat, f_cjk) > w - 2 * pad and len(title) > 8:
        mid = len(title) // 2
        cuts = [i + 1 for i, ch in enumerate(title[:-1]) if ch in " 　：:，,、，；;—"]
        cut = min(cuts, key=lambda i: abs(i - mid)) if cuts else mid
        lines = [title[:cut].strip(), title[cut:].strip()]
    y = pad
    for line in lines:
        lw = textlen(line, f_lat, f_cjk)
        d.rectangle([pad - 12, y - 8, pad + lw + 12, y + line_h + 8],
                    fill=(6, 12, 28, 160))
        draw_mixed(pad, y, line, f_lat, f_cjk, (235, 244, 255, 255))
        y += line_h + 20
    dw = textlen(date, f_lat_d, f_cjk_d)
    dh = f_cjk_d.getbbox("0Ag")[3] + 6
    dy = h - pad - dh
    d.rectangle([pad - 12, dy - 8, pad + dw + 12, dy + dh + 8],
                fill=(6, 12, 28, 160))
    draw_mixed(pad, dy, date, f_lat_d, f_cjk_d, (140, 200, 255, 255))
    out_img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    # 控大小：超 5MB 就缩到 864 宽重存
    out = io.BytesIO()
    out_img.save(out, "PNG")
    if out.tell() > MAX_BYTES and w > 864:
        out_img = out_img.resize((864, int(h * 864 / w)), Image.LANCZOS)
        out = io.BytesIO()
        out_img.save(out, "PNG")
        info(f"超过 {MAX_BYTES // 1024 // 1024}MB，缩到 864 宽重存")
    return out.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=dt.date.today().isoformat(),
                    help="草稿日期，默认今天")
    ap.add_argument("--force", action="store_true", help="封面已存在也重新生成")
    args = ap.parse_args()

    out_publish = PUBLISH / f"{args.date}-cover.png"
    out_docs = DOCS_COVERS / f"{args.date}-cover.png"
    if out_publish.exists() and out_docs.exists() and not args.force:
        info(f"封面已存在，跳过（--force 可重生成）：{out_publish.relative_to(ROOT)}")
        return 0

    parsed = parse_xhs_md(PUBLISH / f"{args.date}-xhs.md")
    if not parsed:
        return 0  # 抽不到首图建议不是错误，WARN 过了直接退
    title, idea, intro = parsed
    scene = scene_idea(title, idea, intro)
    if scene != idea:
        info(f"场景改写：{scene}")

    png = call_images_api(STYLE_BASE + scene)
    if not png:
        return 0  # 生图失败只 WARN，不出文件也不炸流水线

    try:
        png = overlay_text(png, title, args.date)
    except ImportError:
        warn("没装 PIL（pip install pillow），跳过叠字只出底图")
    except Exception as e:  # 叠字是锦上添花，任何异常都不该炸流水线
        warn(f"叠字失败，只出底图：{e}")

    out_publish.parent.mkdir(parents=True, exist_ok=True)
    out_docs.parent.mkdir(parents=True, exist_ok=True)
    out_publish.write_bytes(png)
    shutil.copyfile(out_publish, out_docs)
    info(f"封面已生成：{out_publish.relative_to(ROOT)} + {out_docs.relative_to(ROOT)}"
         f"（{len(png) // 1024}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
