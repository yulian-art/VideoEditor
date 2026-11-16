# 维护者发布 0.4.1

这是从最终验收版本整理的源码发行。核心实现由 `RUNTIME_IDENTITY.json` 固定；文档、合成示例和发布脚本单独维护。不要用含私人课程验收数据的旧 ZIP 作为 GitHub Release 资产。

## 目标仓库和权限

先执行 `gh auth status --active`，确认当前登录身份。明确目标 `owner/repo`、已有仓库 URL，或新建仓库名称及 `public/private`。缺少目标或公开性时应停在已准备好的本地提交，不猜仓库名、不创建公开仓库。

仓库公开性不等于代码开源许可。当前没有新增项目 LICENSE，由仓库所有者决定；第三方依赖和模型按各自许可使用。

## 可提交的内容

源码、静态界面、单元测试、通用验收脚本、文档、依赖清单、启动脚本、固定摘要和纯合成文本示例。`scripts/release_files.py` 是显式清单和隐私审计入口。

不得提交媒体、模型、虚拟环境、缓存数据库、转写/OCR、私有项目、个人路径、访问令牌、旧课程验收证据、导出日志、浏览器配置或工作平台元数据。新增文件时先更新发布清单并人工检查，不能只依赖 .gitignore。

## 本地验证和提交

在干净的发布目录运行：

```powershell
py -3.12 -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
py -3.12 scripts\release_files.py
if ($LASTEXITCODE -ne 0) { throw "Audit failed" }
py -3.12 scripts\acceptance_demo.py --ffmpeg "D:\Tools\ffmpeg\bin\ffmpeg.exe"
if ($LASTEXITCODE -ne 0) { throw "Export checks failed" }
py -3.12 scripts\package_release.py --with-demo
if ($LASTEXITCODE -ne 0) { throw "Packaging failed" }
```

检查 `git status`、`git diff --cached --stat`，再运行 `py -3.12 scripts\release_files.py --tracked`。保留现有 Git 身份，不全局覆盖署名。仓库内不需要保存 dist/；源码资产由脚本生成。

解压源码 ZIP 到新目录重复标准库回归与审计，并逐项验证 `MANIFEST.sha256`。对比两次打包的 SHA256SUMS。合成演示由 `make_demo.py` 生成，记录文件摘要；它不是课程语义准确率证据。

## 发布资产

- `board-align-studio-0.4.1-source.zip`：完整可运行源码和使用文档。
- `board-align-studio-0.4.1-demo.zip`：独立合成图案/音调及四个项目。
- `USER_GUIDE.zh-CN.md`：中文详细手册。
- `RELEASE_NOTES_0.4.1.md`：版本说明。
- `SHA256SUMS`：以上资产校验值。

模型和 FFmpeg 不重新分发。源码 ZIP 与 demo ZIP 内各有 `MANIFEST.sha256`。源文件 LF，Windows cmd 文件 CRLF，Git 属性固定换行，避免克隆后改变语义 worker 实现摘要。

## 推送与 Release

得到明确仓库后先核对仓库 owner、可见性、默认分支及现有内容。已有同名版本先检查，不覆盖 tag 或 Release 资产，也不强推用户历史。新远程、分支、tag 和上传必须指向已确认的同一仓库。

审核本地提交后推送源码，创建 `v0.4.1` 标签，使用 `docs/RELEASE_NOTES_0.4.1.md` 作为 Release 正文，将 dist 中上述资产一并上传。`gh release create --help` 可查看当前 CLI 的 `--verify-tag`、`--notes-file` 等参数。

此文档不预填远程仓库名、公开性、Release URL 或 CI 成功状态。最终发布后应记录仓库 URL、tag 对应提交、Release URL 和资产 SHA。GitHub 自动生成的源码压缩包与本项目自带 manifest 的源码 ZIP 是不同资产。
