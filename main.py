import json
import os
import textwrap
import asyncio
import edge_tts
from PIL import Image, ImageDraw, ImageFont
from moviepy import ImageClip, AudioFileClip, VideoFileClip, concatenate_videoclips

# ================= 配置區 =================
# 路徑設定 (自動抓取當前目錄)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
TEMP_DIR = os.path.join(BASE_DIR, "temp")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DATA_FILE = os.path.join(BASE_DIR, "data.json")

# 確保輸出資料夾存在
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 素材路徑
FONT_EN = os.path.join(ASSETS_DIR, "font_en.ttf")
FONT_CN = os.path.join(ASSETS_DIR, "font_cn.ttf")
BG_IMAGE = os.path.join(ASSETS_DIR, "bg.jpg")
INTRO_VIDEO = os.path.join(ASSETS_DIR, "intro.mp4")
BREAK_VIDEO = os.path.join(ASSETS_DIR, "break.mp4")
OUTRO_VIDEO = os.path.join(ASSETS_DIR, "outro.mp4")

# 品牌名稱
BRAND_NAME = "Rayo: AI Flashcards"

# ================= 核心功能 =================

def check_assets():
    """檢查必要檔案是否存在，避免跑一半報錯"""
    missing = []
    if not os.path.exists(FONT_EN): missing.append("font_en.ttf")
    if not os.path.exists(FONT_CN): missing.append("font_cn.ttf")
    if not os.path.exists(BG_IMAGE): missing.append("bg.jpg")

    if missing:
        print(f"❌ 缺少必要素材 (請放入 assets 資料夾): {', '.join(missing)}")
        return False
    return True

def draw_text_wrapped(draw, text, font, max_width, start_x, start_y, color, line_spacing=15):
    """
    自動換行繪圖函數
    回傳: 繪製結束後的 Y 座標 (方便下一段文字接著畫)
    """
    lines = []
    # 簡單的單字分割 (針對英文)
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        # 如果是中文，簡單處理 (每20個字換行，可依需求調整)
        lines = textwrap.wrap(text, width=22)
    else:
        # 英文處理
        words = text.split(' ')
        current_line = []
        for word in words:
            test_line = ' '.join(current_line + [word])
            bbox = draw.textbbox((0, 0), test_line, font=font)
            w = bbox[2] - bbox[0]
            if w <= max_width:
                current_line.append(word)
            else:
                lines.append(' '.join(current_line))
                current_line = [word]
        if current_line:
            lines.append(' '.join(current_line))

    current_y = start_y
    for line in lines:
        draw.text((start_x, current_y), line, font=font, fill=color)
        bbox = draw.textbbox((0, 0), line, font=font)
        h = bbox[3] - bbox[1]
        current_y += h + line_spacing

    return current_y

def draw_text_with_highlight(draw, text, highlight_word, font, max_width, start_x, start_y, default_color, highlight_color, line_spacing=15):
    """
    自動換行並針對特定單字/片語標記顏色的繪圖函數
    """
    # 簡化處理：為了避免拆字太複雜，我們對句子進行拆解
    # 只針對英文句子做highlight
    words = text.split(' ')
    highlight_words = highlight_word.lower().split(' ')
    
    current_x = start_x
    current_y = start_y
    
    for word in words:
        # 去除標點符號進行比對
        clean_word = "".join(c for c in word if c.isalpha()).lower()
        is_highlight = clean_word in highlight_words

        color = highlight_color if is_highlight else default_color
        
        # 測量單個單字的寬度
        bbox = draw.textbbox((0, 0), word + ' ', font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]

        if current_x + w > start_x + max_width:
            # 換行
            current_x = start_x
            current_y += h + line_spacing

        draw.text((current_x, current_y), word + ' ', font=font, fill=color)
        current_x += w

    # 回傳最終的 Y
    return current_y + h + line_spacing


def create_base_image(bg_filename):
    bg_path = os.path.join(ASSETS_DIR, "bg", bg_filename)
    if not os.path.exists(bg_path):
        bg_path = BG_IMAGE # fallback to default if not found
    
    base = Image.open(bg_path).convert("RGBA").resize((1920, 1080))
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 210))
    return Image.alpha_composite(base, overlay)

def create_word_card_image(data, output_filename):
    """生成單字教學卡片"""
    base = create_base_image(data.get('bg_image', 'bg.jpg'))
    draw = ImageDraw.Draw(base)

    font_header = ImageFont.truetype(FONT_EN, 40)
    font_en = ImageFont.truetype(FONT_EN, 100) # 字放大一點
    font_ipa = ImageFont.truetype(FONT_EN, 50)
    font_cn = ImageFont.truetype(FONT_CN, 70)
    font_tips = ImageFont.truetype(FONT_CN, 50)

    draw.text((100, 80), f"{data['id']}  |  {BRAND_NAME} - Vocabulary", font=font_header, fill="white")

    start_x = 150
    max_w = 1600
    cursor_y = 250

    cursor_y = draw_text_wrapped(draw, data['word_en'], font_en, max_w, start_x, cursor_y, "#ffdd00", 20)
    cursor_y += 20
    cursor_y = draw_text_wrapped(draw, data['word_ipa'], font_ipa, max_w, start_x, cursor_y, "#cccccc", 15)
    cursor_y += 60
    cursor_y = draw_text_wrapped(draw, data['word_cn'], font_cn, max_w, start_x, cursor_y, "white", 20)
    cursor_y += 100
    
    # Tips 有點像是註解
    draw.text((start_x, cursor_y), "Tip:", font=font_tips, fill="#00ffcc")
    cursor_y += 60
    draw_text_wrapped(draw, data['tips'], font_tips, max_w, start_x, cursor_y, "#eeeeee", 20)

    base.save(output_filename)

def create_sentence_card_image(data, output_filename):
    """生成例句教學卡片"""
    base = create_base_image(data.get('bg_image', 'bg.jpg'))
    draw = ImageDraw.Draw(base)

    font_header = ImageFont.truetype(FONT_EN, 40)
    font_en = ImageFont.truetype(FONT_EN, 80)
    font_ipa = ImageFont.truetype(FONT_EN, 50)
    font_cn = ImageFont.truetype(FONT_CN, 70)

    draw.text((100, 80), f"{data['id']}  |  {BRAND_NAME} - Example", font=font_header, fill="white")

    start_x = 150
    max_w = 1600
    cursor_y = 300

    cursor_y = draw_text_with_highlight(
        draw, data['sentence_en'], data['word_en'], 
        font=font_en, max_width=max_w, start_x=start_x, start_y=cursor_y, 
        default_color="white", highlight_color="#ffdd00", line_spacing=20
    )
    cursor_y += 40

    cursor_y = draw_text_wrapped(draw, data['sentence_ipa'], font_ipa, max_w, start_x, cursor_y, "#cccccc", 15)
    cursor_y += 80

    draw_text_wrapped(draw, data['sentence_cn'], font_cn, max_w, start_x, cursor_y, "white", 20)

    base.save(output_filename)

async def generate_audio(text, voice, output_filename, retries=3):
    """使用 Edge-TTS 生成語音，含重試機制"""
    for attempt in range(retries):
        try:
            # 加入語速調整，讓發音更清晰 (+0%)
            communicate = edge_tts.Communicate(text, voice, rate="+0%")
            await communicate.save(output_filename)
            return
        except Exception as e:
            if attempt == retries - 1:
                raise e
            print(f"      [警告] TTS 生成失敗 ({e})，正在進行第 {attempt + 1} 次重試...")
            await asyncio.sleep(2)

BATCH_SIZE = 5

async def main():
    print("🚀 啟動 VideoFactory 企業版...")

    if not check_assets():
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data_list = json.load(f)
    except Exception as e:
        print(f"❌ 讀取 data.json 失敗: {e}")
        return

    print(f"📋 共有 {len(data_list)} 個教學項目需要處理")
    
    chunk_files = []
    
    # 分批處理
    for batch_idx in range(0, len(data_list), BATCH_SIZE):
        batch_items = data_list[batch_idx:batch_idx + BATCH_SIZE]
        print(f"\n📦 正在處理 Batch {batch_idx//BATCH_SIZE + 1} (項目 {batch_idx+1} ~ {batch_idx+len(batch_items)})...")
        
        batch_clips = []
        
        for i, item in enumerate(batch_items):
            global_idx = batch_idx + i
            print(f"   正在處理 [{item['id']}] ...")

            # --- 檔案路徑準備 ---
            img_word_path = os.path.join(TEMP_DIR, f"card_word_{global_idx}.png")
            img_sent_path = os.path.join(TEMP_DIR, f"card_sent_{global_idx}.png")
            
            audio_word_en_path = os.path.join(TEMP_DIR, f"word_en_{global_idx}.mp3")
            audio_word_cn_path = os.path.join(TEMP_DIR, f"word_cn_{global_idx}.mp3")
            audio_tips_path = os.path.join(TEMP_DIR, f"tips_{global_idx}.mp3")
            
            audio_sent_en_path = os.path.join(TEMP_DIR, f"sent_en_{global_idx}.mp3")
            audio_sent_cn_path = os.path.join(TEMP_DIR, f"sent_cn_{global_idx}.mp3")

            # --- A. 生成圖片 ---
            create_word_card_image(item, img_word_path)
            create_sentence_card_image(item, img_sent_path)

            # --- B. 生成聲音 ---
            await generate_audio(item['word_en'], "en-US-GuyNeural", audio_word_en_path)
            await generate_audio(item['word_cn'], "zh-TW-HsiaoChenNeural", audio_word_cn_path)
            await generate_audio(item['tips'], "zh-TW-HsiaoChenNeural", audio_tips_path)
            
            await generate_audio(item['sentence_en'], "en-US-GuyNeural", audio_sent_en_path)
            await generate_audio(item['sentence_cn'], "zh-TW-HsiaoChenNeural", audio_sent_cn_path)

            # --- C. 組合片段 ---
            try:
                c_audio_w_en = AudioFileClip(audio_word_en_path)
                c_audio_w_cn = AudioFileClip(audio_word_cn_path)
                c_audio_tips = AudioFileClip(audio_tips_path)
                
                c_audio_s_en = AudioFileClip(audio_sent_en_path)
                c_audio_s_cn = AudioFileClip(audio_sent_cn_path)

                buffer_time = 0.5
                
                v_word_en = ImageClip(img_word_path).with_duration(c_audio_w_en.duration + buffer_time).with_audio(c_audio_w_en)
                v_word_cn = ImageClip(img_word_path).with_duration(c_audio_w_cn.duration + buffer_time).with_audio(c_audio_w_cn)
                v_tips = ImageClip(img_word_path).with_duration(c_audio_tips.duration + buffer_time).with_audio(c_audio_tips)
                
                v_sent_en = ImageClip(img_sent_path).with_duration(c_audio_s_en.duration + buffer_time).with_audio(c_audio_s_en)
                v_sent_cn = ImageClip(img_sent_path).with_duration(c_audio_s_cn.duration + buffer_time).with_audio(c_audio_s_cn)

                item_sequence = concatenate_videoclips([
                    v_word_en, v_word_cn, v_tips,
                    v_sent_en, v_sent_cn, v_sent_en, v_sent_cn
                ])
                batch_clips.append(item_sequence)

            except Exception as e:
                print(f"⚠️  合成教學項目 {global_idx} 失敗: {e}")
                
        # --- D. 寫入 Batch 影片並釋放記憶體 ---
        if batch_clips:
            chunk_filename = os.path.join(TEMP_DIR, f"chunk_{batch_idx//BATCH_SIZE}.mp4")
            print(f"   => 正在匯出此 Batch 為 {chunk_filename}...")
            batch_video = concatenate_videoclips(batch_clips, method="compose")
            batch_video.write_videofile(
                chunk_filename,
                fps=24,
                codec="libx264",
                audio_codec="aac",
                threads=4,
                logger=None # 關閉進度條避免洗版
            )
            chunk_files.append(chunk_filename)
            
            # 手動釋放資源，避免記憶體爆炸
            batch_video.close()
            for clip in batch_clips:
                clip.close()
            import gc
            gc.collect()

    # 3. 三明治結構組裝 (以 FFmpeg concat 快速合併 Chunk)
    print("\n🎬 正在使用 FFmpeg 高速組裝最終影片...")

    concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        if os.path.exists(INTRO_VIDEO):
            f.write(f"file '{INTRO_VIDEO}'\n")
            
        for chunk in chunk_files:
            f.write(f"file '{chunk}'\n")
            
        if os.path.exists(OUTRO_VIDEO):
            f.write(f"file '{OUTRO_VIDEO}'\n")

    output_file = os.path.join(OUTPUT_DIR, "final_lesson.mp4")
    
    # 執行 ffmpeg 複製命令 (無損合併，速度極快)
    concat_cmd = f'ffmpeg -y -f concat -safe 0 -i "{concat_list_path}" -c copy "{output_file}" -loglevel error'
    os.system(concat_cmd)

    print(f"✅ 企業版影片製作完成！檔案位於: {output_file}")

if __name__ == "__main__":
    asyncio.run(main())
