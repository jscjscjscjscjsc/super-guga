# 咕嘎专属声音

咕嘎的本机朗读和《晴天》《偏爱》等翻唱使用同一段合成音色样本 `resources/voice/guga-reference.wav`。这是用 Qwen3-TTS VoiceDesign 为咕嘎设计的普通话声线。以下路径以 `super-guga` 文件夹为起点。

- 在桌面管理台的「语音输出」选择「咕嘎专属声音（本机）」，可以保存并试听。第一次使用需要加载模型，之后重复使用已加载的模型。可选的长期实时语音使用其平台提供的音色。
- Windows 上可在 `runtime/voice/voice-studio/.venv-fast` 使用 [Faster Qwen3-TTS](https://github.com/andimarafioti/faster-qwen3-tts)。本机验证组合为 Faster Qwen3-TTS 0.5.4、Transformers 5.15.1 和 PyTorch 2.8.0 CUDA 12.8。它读同一份模型和声音样本，支持的扬声器可边生成边播放。加速环境缺失或启动失败时会用原本的本机合成。
- 点击录音可优先使用 `runtime/voice/sense-voice` 下的 [SenseVoice INT8](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)（`model.int8.onnx` 和 `tokens.txt`），通过桌宠环境的 `sherpa-onnx` 在 CPU 上离线识别。依赖或模型不可用时依次尝试已有的 Whisper 和 Vosk。
- 点播「唱晴天」会从歌曲库播放完成的咕嘎版。原始音频保留在 `runtime/music/local`。歌曲库记录了翻唱用的声音样本校验值。
- `runtime/voice/voice-studio` 存放声音样本、模型和独立 Python 环境。咕嘎的声音提示词和样本台词见 `tools/guga_voice_worker.py`。

## 点歌与本地制作

在对话里输入或说「咕嘎，我想听《歌名》」。如果有多个同名版本，可以说「点歌：歌名 - 歌手」。咕嘎先查成品，再查导入的音源、系统音乐文件夹和 `runtime/music/song_sources.json` 中配置的文件夹，最后查 [Openverse 音频曲库](https://docs.openverse.org/meta/media_properties/api.html)。在线查询只发送歌名，用支持改编的完整音源。没有可用音源的歌曲，可以通过右键菜单「外观与动作 → 音乐与动作 → 导入歌曲音频」添加。

首次制作时会显示查找音源、分轨、换声和混音进度。完成后自动播放完整翻唱，并将成品留在歌曲库。可以说「停止唱歌」取消查找或制作。伴奏会压低，轻声唱段会抬高。

普通右键菜单的入口在「更多操作 → 桌宠外观 → 音乐与动作」。用本地音频分别导入《晴天》和《偏爱》，再点对应的播放选项即可制作。原始音源和生成的歌曲存在本机。

## 可选的模型环境

- Windows 上把独立的 Python 环境放在 `runtime/voice/voice-studio/.venv`；朗读需要 `qwen-tts`、PyTorch、`soundfile` 和下方的 Qwen3-TTS Base 权重，按原目录放在 `models/speech`。
- 翻唱需要 NVIDIA CUDA、FFmpeg、`audio-separator` 和 Seed-VC。将 Seed-VC 源码放在 `runtime/voice/voice-studio/seed-vc`，按该项目的歌声模型下载说明准备 44.1 kHz F0 权重、对应配置和提取器，把本地模型放在 `models/sing`。预期文件名和目录见 `tools/guga_cover_worker.py`。
- 分轨器从 `runtime/music/models` 读取权重。安装了 Mel-Band RoFormer 时优先使用它，否则使用 UVR MDX。

```powershell
.\.venv\Scripts\python.exe tools\make_guga_cover.py --title 晴天 --octave 0 --steps 34
```

这个命令读取已经导入的原曲，分离伴奏和主唱，保留旋律换声，再混合两条音轨。成品时长与原曲匹配后才会写入歌曲库。分轨优先使用已安装的 Mel-Band RoFormer，换声使用 Seed-VC 的歌声模型和 CUDA。歌曲处理在本机完成。

- [DSH Singing Studio](https://github.com/Dhao007/dsh-singing-studio) 提供了分轨、音域检查和缓存成品的参考。
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) 用 VoiceDesign 设计声音，用 Base 给新的台词复用同一音色。
- [Seed-VC](https://github.com/Plachtaa/seed-vc) 提供保留音高走向的歌声换声。[Audio Separator](https://github.com/nomadkaraoke/python-audio-separator) 提供分轨。
- [Applio](https://github.com/IAHispano/Applio) 可作为已有或另行训练 RVC 音色时的参考。
