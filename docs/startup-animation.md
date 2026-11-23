# 咕嘎开场动画

普通启动自动播放约 7.4 秒的晨光雪景开场。咕嘎从右上方飞进雪景，落地时扬起雪沫，带有轻风、落雪和铃音。画面淡出后，桌宠从屏幕右侧滑入平时的位置，再播放启动问候。

- 跳过：右上角「跳过开场」或 Esc、Enter、空格。
- 重播：右键咕嘎 → 更多操作 → 桌宠外观 → 开场动画 → 重播开场动画。
- 开场音效：可在开场左下角或开场动画菜单中关闭。
- 关闭自动播放：同一菜单取消「启动时播放开场动画」，或在开场左下角取消「下次也播放」。
- 独立预览：双击项目目录中的 `预览开场动画.cmd`，只预览开场。
- 已隐藏桌宠时，普通启动保持隐藏；录音、长语音、每日计划及战棋的专用启动参数直接进入对应功能。

开场由 Qt 实时渲染。图层 `guga-dawn-plate.png` 和 `guga-flight.png` 由内置 imagegen 基于原画 `guga-dawn-v2.png` 生成，提示词见 `resources/startup/flight-source.json`。同目录的 `dawn-flight.wav` 是本地合成的风声、落地声和铃音，MP4 预览也使用这条音轨。

## 开发与导出

```powershell
.\.venv\Scripts\python.exe -m unittest tools.test_startup_animation -v
.\.venv\Scripts\python.exe tools\preview_startup.py --snapshot 5.8 --output runtime\startup-preview.png
.\.venv\Scripts\python.exe tools\preview_startup.py --record --output runtime\startup-preview.mp4
```

命令行 `--no-intro` 或环境变量 `GUGA_STARTUP_ANIMATION=off` 可跳过自动播放。是否自动播放与是否启用音效分别保存在 `startup/animation` 和 `startup/sound`。

## 参考

根据用户给出的 [Dragon Codex Boot](https://github.com/yushua0808-cpu/dragon-codex-boot) 的播放与窗口交接思路，以及 [DSH Startup Screen](https://github.com/6shenhonghong9/dsh-startup-screen) 的分幕启动呈现，制作咕嘎自己的原画开场。运行代码由本项目实现，视觉使用咕嘎已有角色素材及实时绘制的几何图形。
