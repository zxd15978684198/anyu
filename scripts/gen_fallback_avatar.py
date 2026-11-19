"""生成 3D 加载失败时的降级静态头像 fallback.png（1920×1080）。

用途：前端 three-vrm 加载失败或超 5 秒时展示（PRD FR-3D-02）。旧图印有旧品牌字样，
本脚本用 Pillow 重新生成纯本地、可复现的「安语」版本——不依赖任何外部素材。

用法：
    uv run python scripts/gen_fallback_avatar.py           # 写入两个默认输出路径
    uv run python scripts/gen_fallback_avatar.py --out x.png [--out y.png]

输出是确定性的（不含时间戳），同版本重复生成 sha256 一致，便于 README 里登记校验值。
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]

WIDTH, HEIGHT = 1920, 1080

# 配色：浅色暖灰蓝渐变 + 品牌蓝，长者在平板上看应清晰柔和
GRAD_TOP = (243, 247, 251)
GRAD_BOTTOM = (212, 227, 240)
SILHOUETTE = (143, 176, 210)
TITLE = (48, 76, 106)
SUBTITLE = (91, 122, 148)

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",  # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",  # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",  # 黑体兜底
)

DEFAULT_OUTPUTS = (
    REPO_ROOT / "frontend" / "public" / "avatars" / "fallback.png",
    REPO_ROOT / "deploy" / "assets" / "avatars" / "fallback.png",
)


def _pick_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise SystemExit("未找到可用中文字体，请检查 FONT_CANDIDATES")


def _gradient() -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), GRAD_TOP)
    draw = ImageDraw.Draw(img)
    span = HEIGHT - 1
    for y in range(HEIGHT):
        t = y / span
        color = tuple(round(a + (b - a) * t) for a, b in zip(GRAD_TOP, GRAD_BOTTOM, strict=True))
        draw.line([(0, y), (WIDTH, y)], fill=color)
    return img


def render() -> Image.Image:
    img = _gradient()
    draw = ImageDraw.Draw(img)

    cx = WIDTH // 2
    head_cy = 300
    head_r = 120
    # 头部
    draw.ellipse(
        (cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r),
        fill=SILHOUETTE,
    )
    # 肩身（圆角胶囊）
    body_top = head_cy + head_r + 42
    body_bottom = 620
    body_half_w = 226
    draw.rounded_rectangle(
        (cx - body_half_w, body_top, cx + body_half_w, body_bottom),
        radius=body_half_w,
        fill=SILHOUETTE,
    )

    # 标题「安语」（置于人形下方，留足间距，不与图形压叠）
    title_font = _pick_font(140)
    title = "安语"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    title_cy = 800
    draw.text(
        (cx - tw / 2 - bbox[0], title_cy - th / 2 - bbox[1]),
        title,
        font=title_font,
        fill=TITLE,
    )

    # 副标题
    sub_font = _pick_font(48)
    subtitle = "Anyu · 数字人加载中"
    bbox = draw.textbbox((0, 0), subtitle, font=sub_font)
    sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (cx - sw / 2 - bbox[0], 940 - sh / 2 - bbox[1]),
        subtitle,
        font=sub_font,
        fill=SUBTITLE,
    )

    return img


def main() -> None:
    parser = argparse.ArgumentParser(description="生成安语降级静态头像 fallback.png")
    parser.add_argument(
        "--out", action="append", default=[], help="输出路径（可多次；缺省写入两个默认位置）"
    )
    args = parser.parse_args()

    outs = [Path(p) for p in args.out] or list(DEFAULT_OUTPUTS)
    img = render()
    for out in outs:
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "PNG")
        data = out.read_bytes()
        print(f"{out}  bytes={len(data)}  sha256={hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
