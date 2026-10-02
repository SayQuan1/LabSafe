# 生成更真实的测试图片用于 LabSafe Demo
# 运行: python generate_test_images.py

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import os
import math


def cv2_imread_unicode(path):
    import cv2
    import numpy as np
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def draw_realistic_bottle(draw, img, cx, cy, width, height, liquid_color, label_text, font, small_font):
    """绘制更真实的试剂瓶（圆柱体 + 渐变 + 阴影 + 标签 + 瓶盖）"""
    x = cx - width // 2
    top = cy - height // 2
    bottom = cy + height // 2

    # 1. 阴影（投射在货架上）
    shadow_offset = 8
    for i in range(width // 2):
        alpha = int(80 * (1 - i / (width / 2)))
        draw.ellipse(
            [x + i, bottom + shadow_offset, x + width - i, bottom + shadow_offset + 6],
            fill=(max(0, 139 - alpha), max(0, 90 - alpha), max(0, 43 - alpha))
        )

    # 2. 瓶身渐变（左暗右亮模拟圆柱光照）
    body_left = x + 4
    body_top = top + 25  # 瓶盖下方
    body_right = x + width - 4
    body_bottom = bottom - 5

    # 用多条竖线模拟渐变
    for px in range(body_left, body_right):
        t = (px - body_left) / (body_right - body_left)
        # 中间最亮，两侧暗（圆柱光照模型）
        brightness = 1.0 - abs(t - 0.4) * 1.2
        brightness = max(0.3, min(1.0, brightness))
        r = int(liquid_color[0] * brightness + 40)
        g = int(liquid_color[1] * brightness + 40)
        b = int(liquid_color[2] * brightness + 40)
        r, g, b = min(r, 255), min(g, 255), min(b, 255)
        draw.line([(px, body_top), (px, body_bottom)], fill=(r, g, b))

    # 瓶身边框
    draw.rectangle([body_left, body_top, body_right, body_bottom],
                    outline=(60, 60, 60), width=2)

    # 3. 瓶肩（圆弧过渡）
    shoulder_h = 15
    for i in range(shoulder_h):
        t = i / shoulder_h
        offset = int((1 - t) * width * 0.15)
        draw.line([(x + 4 + offset, body_top - shoulder_h + i),
                   (x + width - 4 - offset, body_top - shoulder_h + i)],
                  fill=(int(liquid_color[0] * 0.6 + 30), int(liquid_color[1] * 0.6 + 30), int(liquid_color[2] * 0.6 + 30)))

    # 4. 瓶颈
    neck_w = width // 3
    neck_x = x + (width - neck_w) // 2
    neck_top = top + 8
    draw.rectangle([neck_x, neck_top, neck_x + neck_w, body_top - shoulder_h + 2],
                    fill=(int(liquid_color[0] * 0.7), int(liquid_color[1] * 0.7), int(liquid_color[2] * 0.7)),
                    outline=(60, 60, 60), width=1)

    # 5. 瓶盖
    cap_w = neck_w + 6
    cap_x = x + (width - cap_w) // 2
    cap_top = top
    cap_bottom = top + 12
    # 瓶盖渐变
    for px in range(cap_x, cap_x + cap_w):
        t = (px - cap_x) / cap_w
        brightness = 1.0 - abs(t - 0.5) * 0.8
        r = int(200 * brightness)
        g = int(40 * brightness)
        b = int(40 * brightness)
        draw.line([(px, cap_top), (px, cap_bottom)], fill=(r, g, b))
    draw.rectangle([cap_x, cap_top, cap_x + cap_w, cap_bottom], outline=(120, 20, 20), width=2)

    # 6. 标签（白色矩形 + 文字 + 液位线）
    label_w = width - 12
    label_h = 55
    label_x = x + 6
    label_y = body_top + 10

    # 标签背景（微泛黄）
    for py in range(label_y, label_y + label_h):
        for px in range(label_x, label_x + label_w):
            pass  # 已用 rectangle 填充

    draw.rectangle([label_x, label_y, label_x + label_w, label_y + label_h],
                    fill=(252, 248, 235), outline=(100, 100, 100), width=1)

    # 标签文字
    draw.text((label_x + 8, label_y + 6), label_text, fill=(20, 20, 20), font=font)
    draw.text((label_x + 8, label_y + 32), "2025-01", fill=(80, 80, 80), font=small_font)

    # 7. 高光（左上角白色弧线模拟反光）
    highlight_x = body_left + 4
    for py in range(body_top + 5, body_bottom - 5, 2):
        for px in range(highlight_x, highlight_x + 6, 2):
            offset_y = (py - body_top) / (body_bottom - body_top)
            fade = 1.0 - abs(offset_y - 0.3) * 3
            if fade > 0:
                alpha = int(60 * fade)
                pixel = img.getpixel((px, py))
                new_r = min(255, pixel[0] + alpha)
                new_g = min(255, pixel[1] + alpha)
                new_b = min(255, pixel[2] + alpha)
                img.putpixel((px, py), (new_r, new_g, new_b))


def draw_shelf(draw, w, h):
    """绘制货架（多层木板 + 阴影）"""
    # 外框
    draw.rectangle([30, 30, w - 30, h - 30], fill=(100, 65, 30), outline=(60, 40, 15), width=3)
    # 内壁
    draw.rectangle([40, 40, w - 40, h - 40], fill=(130, 85, 45), outline=(80, 55, 25), width=2)
    # 隔板
    shelf_y = h // 2 + 20
    draw.rectangle([40, shelf_y, w - 40, shelf_y + 8], fill=(90, 60, 28), outline=(60, 40, 18), width=1)
    # 背景纹理（横纹）
    for y in range(42, h - 42, 3):
        c = 128 + (y % 6)
        draw.line([(42, y), (w - 42, y)], fill=(c, c - 40, c - 60))


def create_normal_scene():
    """场景1: 乙醇与丙酮相邻存放（更真实的试剂瓶）"""
    w, h = 640, 480
    img = Image.new('RGB', (w, h), (240, 235, 225))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 20)
        small_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 14)
    except:
        font = ImageFont.load_default()
        small_font = font

    draw_shelf(draw, w, h)

    # 两个瓶子（位置错开，模拟真实摆放）
    draw_realistic_bottle(draw, img, 200, 260, 90, 220, (180, 180, 185), "乙醇", font, small_font)
    draw_realistic_bottle(draw, img, 340, 260, 90, 220, (170, 170, 175), "丙酮", font, small_font)

    img = img.filter(ImageFilter.GaussianBlur(radius=0.3))
    return img


def create_blurry_image():
    """场景2: 模糊图片"""
    w, h = 640, 480
    img = Image.new('RGB', (w, h), (240, 235, 225))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 20)
        small_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 14)
    except:
        font = ImageFont.load_default()
        small_font = font

    draw_shelf(draw, w, h)
    draw_realistic_bottle(draw, img, 200, 260, 90, 220, (180, 180, 185), "乙醇", font, small_font)
    draw_realistic_bottle(draw, img, 340, 260, 90, 220, (170, 170, 175), "丙酮", font, small_font)

    img = img.filter(ImageFilter.GaussianBlur(radius=6))
    return img


def create_single_bottle():
    """场景3: 单独存放的硫酸"""
    w, h = 640, 480
    img = Image.new('RGB', (w, h), (240, 235, 225))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 20)
        small_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 14)
    except:
        font = ImageFont.load_default()
        small_font = font

    draw_shelf(draw, w, h)
    draw_realistic_bottle(draw, img, 320, 260, 95, 230, (200, 180, 100), "硫酸", font, small_font)

    img = img.filter(ImageFilter.GaussianBlur(radius=0.3))
    return img


def create_glare_image():
    """场景4: 反光图片"""
    w, h = 640, 480
    img = Image.new('RGB', (w, h), (240, 235, 225))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 20)
        small_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 14)
    except:
        font = ImageFont.load_default()
        small_font = font

    draw_shelf(draw, w, h)
    draw_realistic_bottle(draw, img, 200, 260, 90, 220, (180, 180, 185), "乙醇", font, small_font)

    # 大面积白色高光
    for i in range(200):
        x = 150 + i
        y = 100 + i // 2
        r = 255
        g = 255
        b = 255
        draw.ellipse([x - 60, y - 40, x + 60, y + 40], fill=(r, g, b, 180))

    return img


def create_dark_image():
    """场景5: 光线不足"""
    w, h = 640, 480
    img = Image.new('RGB', (w, h), (240, 235, 225))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 20)
        small_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 14)
    except:
        font = ImageFont.load_default()
        small_font = font

    draw_shelf(draw, w, h)
    draw_realistic_bottle(draw, img, 200, 260, 90, 220, (180, 180, 185), "乙醇", font, small_font)
    draw_realistic_bottle(draw, img, 340, 260, 90, 220, (170, 170, 175), "丙酮", font, small_font)

    # 整体变暗
    import numpy as np
    arr = np.array(img)
    arr = (arr * 0.25).astype(np.uint8)
    img = Image.fromarray(arr)
    return img


if __name__ == "__main__":
    output_dir = os.path.dirname(os.path.abspath(__file__))

    images = {
        "shelf_normal.jpg": create_normal_scene(),
        "shelf_blurry.jpg": create_blurry_image(),
        "shelf_single.jpg": create_single_bottle(),
        "shelf_glare.jpg": create_glare_image(),
        "shelf_dark.jpg": create_dark_image(),
    }

    for filename, img in images.items():
        filepath = os.path.join(output_dir, filename)
        img.save(filepath, "JPEG", quality=92)
        print(f"生成: {filepath}")

    print(f"\n共生成 {len(images)} 张测试图片")
