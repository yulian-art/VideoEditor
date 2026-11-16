# 合成演示

本例只有自行生成的 FFmpeg 测试图案和 440 Hz 音调，没有课程、语音或真实用户素材。用于练习手工对齐与导出，不用于评估自动语义粗调。

## 两种使用方式

**Release demo ZIP：** 解压 `board-align-studio-0.4.1-demo.zip` 后，在工作台“打开项目”，选择解压目录中的 `demo.align.json`。媒体与项目同目录，可直接使用。

**源码自行生成：** 在源码根目录运行：

```powershell
py -3.12 scripts\make_demo.py --ffmpeg "D:\Tools\ffmpeg\bin\ffmpeg.exe"
```

打开 `examples/generated/demo.align.json`。生成目录默认被 Git 忽略；再次生成会校验已有文件，不覆盖被修改的示例，需另选空输出目录。此目录不是你自己的素材存放处。

本目录的 `demo.align.json` 引用 `generated/board.mp4`、`generated/narration.m4a`，需先运行生成器。`anchors.csv` 和 `captions.srt` 是纯文本格式示例；CSV 导入前选择对应合成媒体，完整恢复建议打开 JSON。

## 四个项目

| 项目 | 预期 |
| --- | --- |
| demo | target 16–20 秒保持 source 20，字幕仍在录音 4–7、12–15 秒 |
| ahead-5s | source 6 在 target 11 出现，板书延后 5 秒 |
| behind-5s | source 11 在 target 6 出现，板书提前 5 秒 |
| cut | source 6–10 被裁，target 6 跳到 source 10；恢复后还原暂存自动点和人工点 |

生成器按实际 AAC 时长微调终点，避免编码延迟造成一帧以上误差。打开前后检查源/目标标记，使用 `A/V/P` 练习，保存副本，再实际导出和独立运行 AAC 校验。

生成记录 `SYNTHETIC_DEMO.json` 保存生成器与所有媒体/项目的 SHA-256。FFmpeg 版本不同可能生成不同媒体字节，不承诺跨 FFmpeg 构建得到相同 demo ZIP；所下载的 ZIP 以该版 SHA256SUMS 和内部 MANIFEST 为准。
