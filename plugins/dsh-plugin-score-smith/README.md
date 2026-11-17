# 制谱师 · dsh-plugin-score-smith

听一段音频，自动分轨、逐轨识别音高音符、排成可以随便改的曲谱（score.json / MusicXML / MIDI / PNG）。

## 用之前

需要两个 Python 环境（都已经装好了）：

| 用途 | 解释器 |
| --- | --- |
| 识音 / 排谱 / 渲染 | `C:\dsh-tools\score-venv\Scripts\python.exe` |
| 分轨（demucs） | `D:\dsh 大肥鱼\voice\tts\venv\Scripts\python.exe` |

曲谱环境必须放在**纯英文路径**下——verovio 在中文路径里读不到字体资源。

## 六个工具

- `score_doctor` — 自检：库在不在、浏览器在不在、verovio 能不能出图。
- `score_transcribe` — 主力。给一个音频路径（mp3/wav/mp4/flac），可以：
  - `stemsDir`：已经有分轨就直接用，跳过 demucs；
  - `only`：只识别某几轨，例如 `["vocals","guitar"]`；
  - `instrumentMap`：把某个 stem 当成别的乐器（例如 `{"other":"sax"}`）；
  - `tempo/meter/key/grid`：手动指定，不给我自己猜（我猜得还行，四四拍流行歌基本准）。
- `score_list` — 列项目，或者看某个项目的详情。
- `score_snippet` — 用音名读出第几小节是什么音（核对识别准不准用）。
- `score_edit` — 改谱：移调、删小节、改速度/拍号/调号/曲名/轨名、删轨、改和弦，还有逐音编辑 `addNote` / `setNote` / `deleteNote` / `clearNotes`（用「第几小节 + 第几拍」定位，音高可以写 `C#4` 或 MIDI 数字）。
- `score_render` — 重新排版出 PNG。

## 产物（每个项目一个目录，默认 `D:\dsh 大肥鱼\scores\<id>`）

```
score.json         ← 唯一的真相，所有音符/和弦/元信息都在这，随时可以改
score.musicxml     ← 拿去 MuseScore / Sibelius 继续编辑
score.mid          ← 直接听
preview.png        ← 整谱长图（一张到底，1600 px 宽）
preview-p1.png …   ← 每一页单独一张图，方便贴到聊天/文档里
preview.html       ← 可播放预览页：浏览器打开就能按谱听
manifest.json      ← 项目记录
```

`preview.html` 是自包含的单文件（内嵌 SVG + 音符 JSON，不联网、不依赖任何库）。页面工具栏：播放 / 暂停 / 停止、速度 0.5–1.5×、音量、跟随滚动、节拍器、每轨静音胶囊（显示轨名·音符数）；右上角实时显示「第 N / 总 M 小节、当前和弦、mm:ss / 总长」；进度条可点跳；空格键播放暂停。声音是 Web Audio 现场合成的（正弦/三角波做旋律、噪声+扫频做鼓），所以没装音源也能听。

少于 5 个音的整轨会自动略过（谱面上留一条空行只会碍事，时长 < 20 秒的短片段不套用这条规则）。

改谱两条路：用 `score_edit` 说人话改，或者直接动 `score.json` 再 `score_render`。改完会自动重排 + 重新出图和预览页。

## 原理一句话

demucs 六轨分离 → 复音（basic-pitch ONNX）/ 单音（pyin + 自适应门限）/ 鼓（onset + 多标签频段分类）→ 拍点映射到拍位并量化到网格 → 全曲合并音池估和弦 → music21 排成 MusicXML → verovio 渲染成谱。
