# 生成测试图片用于 LabSafe Demo
# 运行: python generate_test_images.py

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import os

def draw_label(draw, x, y, width, height, text, color, font):
    """绘制模拟试剂瓶标签"""
    # 瓶身
    draw.rectangle([x, y, x+width, y+height], fill=(220, 220, 220), outline=(150, 150, 150), width=2)
    # 标签区域
    label_y = y + 20
    label_height = 80
    draw.rectangle([x+5, label_y, x+width-5, label_y+label_height], fill=(255, 255, 240), outline=(100, 100, 100), width=1)
    # 文字
    draw.text((x+10, label_y+10), text, fill=(0, 0, 0), font=font)
    # 日期
    draw.text((x+10, label_y+40), "2025-01-15", fill=(80, 80, 80), font=font)
    # 瓶盖
    draw.rectangle([x+20, y-10, x+width-20, y], fill=color, outline=(100, 100, 100), width=1)

def create_normal_scene():
    """场景1: 乙醇与丙酮相邻存放"""
    img = Image.new('RGB', (640, 480), (245, 245, 240))
    draw = ImageDraw.Draw(img)
    
    # 货架背景
    draw.rectangle([50, 50, 590, 430], fill=(139, 90, 43), outline=(100, 70, 30), width=3)
    draw.rectangle([60, 60, 580, 420], fill=(160, 110, 60), outline=(120, 80, 40), width=2)
    
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 24)
        small_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 16)
    except:
        font = ImageFont.load_default()
        small_font = ImageFont.load_default()
    
    # 乙醇瓶
    draw_label(draw, 120, 150, 80, 160, "乙醇", (200, 50, 50), font)
    # 丙酮瓶
    draw_label(draw, 250, 150, 80, 160, "丙酮", (200, 50, 50), font)
    
    # 说明文字
    draw.text((180, 360), "乙醇 + 丙酮 同柜存放", fill=(200, 0, 0), font=font)
    
    return img

def create_blurry_image():
    """场景2: 模糊图片（触发质量拦截）"""
    img = Image.new('RGB', (640, 480), (245, 245, 240))
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 24)
    except:
        font = ImageFont.load_default()
    
    # 模糊的内容
    draw.text((200, 200), "此图片故意模糊", fill=(100, 100, 100), font=font)
    draw.text((180, 240), "测试质量检测拦截", fill=(100, 100, 100), font=font)
    
    # 应用高斯模糊
    img = img.filter(ImageFilter.GaussianBlur(radius=8))
    return img

def create_single_bottle():
    """场景3: 单独存放的硫酸"""
    img = Image.new('RGB', (640, 480), (245, 245, 240))
    draw = ImageDraw.Draw(img)
    
    # 货架背景
    draw.rectangle([50, 50, 590, 430], fill=(139, 90, 43), outline=(100, 70, 30), width=3)
    draw.rectangle([60, 60, 580, 420], fill=(160, 110, 60), outline=(120, 80, 40), width=2)
    
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 24)
    except:
        font = ImageFont.load_default()
    
    # 单个硫酸瓶
    draw_label(draw, 250, 150, 80, 160, "硫酸", (200, 100, 50), font)
    
    draw.text((200, 360), "硫酸 单独存放", fill=(0, 100, 0), font=font)
    
    return img

def create_glare_image():
    """场景4: 反光图片（触发质量拦截）"""
    img = Image.new('RGB', (640, 480), (245, 245, 240))
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 24)
    except:
        font = ImageFont.load_default()
    
    # 标签区域有强光反射
    draw.text((200, 180), "强光反射区域", fill=(255, 255, 255), font=font)
    
    # 添加白色高光区域模拟反光
    for i in range(100):
        alpha = int(255 * (1 - i/100))
        x = 150 + i * 3
        y = 100 + i * 2
        draw.ellipse([x, y, x+200-i, y+150-i], fill=(255, 255, 255))
    
    return img

def create_dark_image():
    """场景5: 光线不足（触发质量拦截）"""
    img = Image.new('RGB', (640, 480), (30, 30, 35))
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 24)
    except:
        font = ImageFont.load_default()
    
    draw.text((200, 220), "光线不足场景", fill=(80, 80, 80), font=font)
    
    return img

if __name__ == "__main__":
    output_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 生成各场景图片
    images = {
        "shelf_normal.jpg": create_normal_scene(),
        "shelf_blurry.jpg": create_blurry_image(),
        "shelf_single.jpg": create_single_bottle(),
        "shelf_glare.jpg": create_glare_image(),
        "shelf_dark.jpg": create_dark_image(),
    }
    
    for filename, img in images.items():
        filepath = os.path.join(output_dir, filename)
        img.save(filepath, "JPEG", quality=90)
        print(f"生成: {filepath}")
    
    print(f"\n共生成 {len(images)} 张测试图片")
    print("使用说明:")
    print("  shelf_normal.jpg  - 乙醇+丙酮相邻（高风险场景）")
    print("  shelf_blurry.jpg  - 模糊图片（质量拦截）")
    print("  shelf_single.jpg  - 单独硫酸（低风险场景）")
    print("  shelf_glare.jpg   - 反光图片（质量拦截）")
    print("  shelf_dark.jpg    - 光线不足（质量拦截）")
