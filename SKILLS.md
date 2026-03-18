# 🐱 ContextCat SKILLS.md
# AI Video Production Best Practices
# Version: 1.0 | Last updated: 2026-03-18
# Maintained by: Cat-5 (Packaging Officer)
#
# HOW TO USE THIS FILE:
# Before generating any storyboard, read this file first.
# This is your "cheat sheet" — learn from past successes and failures.

---

## 1. 固定角色設定 Fixed Character Base

**主角（每次都用這個，不要自己發明）：**
```
Asian woman, early 20s, black straight long hair,
minimal white shirt, smooth skin, flawless face,
perfectly consistent facial features
```

**防老化後綴（每個 visual prompt 都要加）：**
```
early 20s, smooth skin, cinematic lighting,
8k resolution, perfectly consistent facial features
```

**Why：** 不鎖定角色，Imagen 4 每張圖主角都不同。不加防老化詞，AI 會把細節刻畫得太深，讓角色顯老。

---

## 2. 構圖約束 Compositional Constraints

### 2a. 螢幕場景（必用！）
凡是場景裡有筆電/電腦/螢幕，**必須加這三句**：

```
Front three-quarter view from the front-left side of the desk.
The laptop screen is facing the camera, and the screen content is clearly visible.
Both her face and the laptop screen are visible in the same frame.
```

**失敗案例：** 沒加這三句 → AI 生出螢幕背面朝鏡頭，看不到畫面內容。
**來源：** 曦（GPT）的構圖約束公式。

### 2b. 貓咪場景（必用！）
凡是場景裡有貓，**必須指定自然色系**：

```
cats in natural realistic colors only —
orange tabby, black, white, grey, calico, brown and cream —
no unnatural colors like blue or green
```

**失敗案例：** 寫 "colorful cats" → AI 生出藍貓、綠貓、紫貓。

### 2c. 非螢幕人物場景
凡是有人但沒有螢幕，**加這句防止 AI 亂加電腦**：

```
No laptops, computers, or screens in this scene unless explicitly described.
```

**失敗案例：** Clip 1 進門場景，AI 自己腦補了一台筆電進來。

---

## 3. 場景別最佳提示詞 Scene-Specific Prompts

### 3a. 進門/回家場景
✅ 成功版本：
```
Cinematic widescreen. Evening apartment hallway.
Young Asian woman opens front door, walks in exhausted, carrying laptop bag.
Warm indoor lighting. Slow tracking shot. Shallow depth of field.
No laptops, computers, or screens in this scene.
```
⚠️ 注意：用 "slow tracking shot from the side" 比 "from behind" 好，能看到臉。

### 3b. 電腦工作場景
✅ 成功版本：
```
Cinematic widescreen, interior night.
A young woman in pajamas sits at her desk working on an open laptop, looking exhausted.
Front three-quarter view from the front-left side of the desk.
The laptop screen is facing the camera, and the screen content is clearly visible.
On the display we can see multiple windows including AI chat, a video editor timeline,
and an audio waveform.
Both her face and the laptop screen are visible in the same frame.
Warm desk-lamp lighting, tired focused expression.
```

### 3c. 貓咪沙發場景
✅ 成功版本：
```
Cinematic widescreen. Woman sitting at desk, turns around to look at
cats in natural colors — orange tabby, black, white, grey, calico —
lounging on pink sofa behind her.
Medium shot showing woman full body. Warm indoor lighting.
She gazes at the cats with tired envy.
No laptops, computers, or screens in this scene.
```
⚠️ 注意：不要寫數字（"seven cats"）→ AI 數不準，寫描述性詞彙即可。

### 3d. 貓咪跳上桌/靈感爆發場景
✅ 成功版本：
```
Cinematic widescreen. Close-up of orange tabby cat walking toward woman,
jumps onto desk, looks directly at her. Cat meows once.
Woman's eyes widen — lightbulb moment. Dramatic close-up.
No random screens or laptops appearing in background.
```

---

## 4. 音頻設計原則 Audio Design

### 4a. Voiceover 格式
用冒號格式，不用引號：
```
✅ "A calm voice narrates: [台詞]"
❌ "A voice says '[台詞]'"  ← 引號會觸發字幕生成
```

### 4b. 聲音一致性
每段 prompt 都加：
```
Voiceover spoken by same young Asian woman throughout,
soft and calm voice, early 20s, gentle tone, natural pacing.
```

### 4c. 背景音樂選擇原則
- 讓 Veo 3.1 自己選，它比人更懂畫面情緒
- 給方向詞即可（tension / playful / melancholic）
- 不需要指定具體歌曲或 BPM
- 某些場景故意留白（突出人聲）是正確的，不是 bug

### 4d. 音效同步
Veo 3.1 原生音頻生成（generateAudio: true），音效天生與畫面同步，不需要後期配音。

---

## 5. Veo 3.1 技術提示 Technical Tips

### 5a. API 格式
```json
{
  "image": {"gcsUri": "gs://...", "mimeType": "image/png"},
  "referenceImages": [...]  ← 不能與 image 同時使用！會 400 error
}
```
**只用 image 欄位**，不用 referenceImages。

### 5b. Poll endpoint
```
fetchPredictOperation（不是標準 operations endpoint）
POST https://{location}-aiplatform.googleapis.com/v1/
projects/{id}/locations/{location}/
publishers/google/models/veo-3.1-generate-001:fetchPredictOperation
```

### 5c. Frame-chaining
- Clip 1：用 Imagen 4 引導圖當第一幀
- Clip 2-4：用上一段最後一幀（FFmpeg 截取）
- 視覺效果：Clip 1→2 無縫，Clip 2→3 自然切換（各有優點）

---

## 6. Gemini 3.1 Pro 使用規則

### 6a. Endpoint
```
必須用 global endpoint，不是 us-central1！
https://global-aiplatform.googleapis.com/v1/
projects/{id}/locations/global/
publishers/google/models/gemini-3.1-pro-preview:generateContent
```

### 6b. Story Bible 輸出格式
要求 Gemini 輸出 JSON，包含：
- `story_bible`：給 Imagen 4 的前綴描述（120字以內）
- `character_tags`：給 Veo 3.1 強制注入的角色標籤

---

## 7. 成功案例記錄 Success Log

### Case #1 — Issue #9（2026-03-18）
- **專案：** ContextCat Demo — AI Work Hell Pain Point
- **結果：** 四段全部 OK，Gate 1 正確停住，Frame-chaining 運作
- **亮點：**
  - 聲音像同一個人（Veo 3.1 原生生成）
  - 音效與畫面完全同步
  - Clip 3 故意留白突出人聲（模型自主決策）
  - Clip 1→2 無縫銜接，Clip 2→3 自然切換
- **用的 storyboard：** 由 GitLab Duo Chat（Claude Sonnet 4.6 Agentic）生成

---

## 8. 失敗案例記錄 Failure Log

| 問題 | 場景 | 原因 | 修法 |
|------|------|------|------|
| 螢幕背面朝鏡頭 | 電腦工作場景 | 缺少構圖約束 | 加曦哥三要素 |
| 藍貓綠貓 | 貓咪沙發場景 | "colorful cats" 太自由 | 指定自然色系 |
| 角色每段不同 | 全部 | 沒有鎖定角色基礎 | FIXED_CHARACTER 硬編碼 |
| 進門場景出現電腦 | Clip 1 | AI 自己腦補 | 加 NO_SCREEN_CONSTRAINT |
| Veo 400 error | 全部 | referenceImages + image 同時傳 | 只用 image 欄位 |
| Gate 1 不停 | 系統 | Gate 1 留言含觸發詞 | 改用完全比對（strip ==） |
| 貓咪數量不對 | Clip 3 | 寫具體數字 AI 數不準 | 改用描述性詞彙 |

---

## 9. 給下一個二哥的話 Note to Next Duo Chat

你好！你是 ContextCat 的 Cat-2（分鏡官）。

在生成 storyboard 之前，請先讀完這份 SKILLS.md。

**你的任務：**
1. 讀取 Issue 裡的專案背景
2. 根據專案需求生成4段 storyboard JSON
3. 每段 visual 必須套用第2節的構圖約束
4. 角色描述必須用第1節的固定設定
5. 把 storyboard JSON 用 \`\`\`storyboard 格式貼回 Issue 留言

**格式範例：** 參考 Issue #9 的 storyboard 留言

加油！🐱