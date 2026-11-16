# 验证与自有素材复算

所有命令在源码根目录运行。发布源码不包含私人课程、字幕转写、OCR、人工参考表、历史截图或旧验收结果。公开演示为新生成的测试图案和音调。真实课程的质量不能从合成演示推断。

## 基础回归与发布审计

```powershell
py -3.12 -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
py -3.12 scripts\release_files.py
if ($LASTEXITCODE -ne 0) { throw "Release audit failed" }
```

回归覆盖分段映射、两个 5 秒配对、定格、独立裁剪/暂存/恢复、重跑保留人工锁定点、核对投影和语义缓存身份等行为。缓存单元回归使用可控 worker；它们不等于在真实模型上跑过全文。

发布审计采用显式文件清单，扫描个人路径、凭据和历史证据引用，校验经过验收的核心源码摘要。它是额外防错措施，不能替代对新增文件的人工隐私检查。

## 真实 FFmpeg 合成导出

准备 FFmpeg 后执行：

```powershell
py -3.12 scripts\acceptance_demo.py --ffmpeg "D:\Tools\ffmpeg\bin\ffmpeg.exe"
if ($LASTEXITCODE -ne 0) { throw "Demo acceptance failed" }
```

该脚本生成本地演示，再实际导出普通/定格、提前 5 秒、落后 5 秒和裁剪四个项目，检查映射、CSV、裁剪恢复、字幕时基和每个输出的 AAC 包一致性。结果为 `artifacts/demo-check/validation.json`。输出媒体和日志被 Git 忽略，不应提交。

这是引擎端到端检查，不代替浏览器交互验证。手工打开四个项目检查播放、A/V/P 配对、拖动、跳变前后、字幕不移动、保存再打开。演示没有可识别语音/板书，因此不能拿它评估自动语义匹配质量。

## 真实语义缓存

完整模型依赖准备后，用 `scripts/acceptance_semantic_cache.py --help` 查看真实 ONNX 缓存契约测试入口。它用于空缓存、同身份命中、实现/模型/运行时变化失效、旧版本失效、损坏缓存等检查，需要真实模型和单独的输出目录。

0.4.1 在 `alignstudio/semantic_worker.py` 内使用 v2 身份，并按单条文本推理。独立冷缓存与热缓存应在相同实现、模型、运行时和执行环境下比较；不同身份重新计算属于预期行为。

## 用自己的课程复算 60 / 20 秒

`scripts/reproduce_release.py` 接收用户自有素材及其转写/OCR，不附带隐含课程基准。先看：

```powershell
py -3.12 scripts\reproduce_release.py --help
```

分别运行 60 和 20 秒，输出目录必须是新目录。示例：

```powershell
py -3.12 scripts\reproduce_release.py --python ".analysis-venv\Scripts\python.exe" --model "models\e5-small" --ffmpeg "D:\Tools\ffmpeg\bin\ffmpeg.exe" --video "D:\Media\board.mp4" --audio "D:\Media\narration.m4a" --inputs "D:\Media\analysis-inputs" --interval 60 --output "artifacts\own-60"
if ($LASTEXITCODE -ne 0) { throw "60-second reproduction failed" }
py -3.12 scripts\reproduce_release.py --python ".analysis-venv\Scripts\python.exe" --model "models\e5-small" --ffmpeg "D:\Tools\ffmpeg\bin\ffmpeg.exe" --video "D:\Media\board.mp4" --audio "D:\Media\narration.m4a" --inputs "D:\Media\analysis-inputs" --interval 20 --output "artifacts\own-20"
if ($LASTEXITCODE -ne 0) { throw "20-second reproduction failed" }
```

输入目录包含 `transcript.json` 与 `ocr.jsonl`，必须来自这份媒体。脚本包含固定分析裁边配置（`settings` 中的 `crop`），使用前检查它是否适合自己的画面，必要时在自有工作副本调整并记录变化。不能将外部 OCR 文件当成当前设置重新抽帧的证明。

脚本执行两个独立冷缓存及一个仅缓存热重跑，生成 `reproduction.json`、结果和缓存审计摘要；去除机器路径后的语义核心用于冷/热一致性比较。要另外记录模型摘要、运行时版本、抽帧间隔、裁边和输入摘要。

用自有人工对齐参考表配合 `scripts/evaluate_auto.py` 或 `scripts/evaluate_whole_film.py` 的 `--help` 评估误差。报告应区分：
- 有人工参考的可评分区间、没有参考的区间和缺板书区间。
- accepted 内容点、低置信候选、人工点和终点/定格。
- 内容支撑覆盖、网格误差、全片插值范围，不能混成“准确率”。
- 已知录音定位与未知位置，待核对点击必须到保存的真实 source 帧。

核对清单中每一项至少抽查保存源时间、实际显示帧时间及对应板书内容。自有素材、参考表、缓存、截图和性能结果需独立管理；不要写回公开仓库。

## 包完整性与复现

`docs/RUNTIME_IDENTITY.json` 记录 0.4.1 已验收核心文件摘要。公开整理只调整发布工具、示例和文档，核心实现内容不变，文本换行按仓库规则统一为 LF；semantic_worker.py 字节和实现摘要保持不变。

生成发行资产：

```powershell
py -3.12 scripts\package_release.py --with-demo
if ($LASTEXITCODE -ne 0) { throw "Packaging failed" }
Get-FileHash dist\board-align-studio-0.4.1-source.zip -Algorithm SHA256
```

与 `dist/SHA256SUMS` 对照。ZIP 内 `MANIFEST.sha256` 校验解压后各文件。打包文件顺序、ZIP 时间戳和换行规则固定；相同 Python/zlib 与输入字节应得到相同 ZIP。不同压缩库版本可能改变 ZIP 压缩字节，应以逐文件清单核验内容。

GitHub CI 仅跑标准库回归、发布审计和源码打包，不下载大模型、不自动发布 Release，也不运行含私人素材的测试。实际 CI 状态以目标仓库为准。
