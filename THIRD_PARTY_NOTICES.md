# 第三方组件与模型

本仓库没有捆绑第三方二进制、分析 Python 包或模型权重。安装和使用时应查看所选版本的许可、模型卡和分发要求；GitHub 可见性不替代许可授权。项目自身尚未指定开源许可证，由仓库所有者确定，本次未赋予新的使用授权。

主要上游入口：

| 组件 | 用途 | 官方来源及许可入口 |
| --- | --- | --- |
| Python | 本地服务与工具 | [Python](https://www.python.org/), [许可](https://docs.python.org/3/license.html) |
| FFmpeg | 解码、抽帧、视频编码、音频流复制 | [FFmpeg](https://ffmpeg.org/), [法律与许可](https://ffmpeg.org/legal.html) |
| faster-whisper / CTranslate2 | 本地旁白转写 | [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [CTranslate2](https://github.com/OpenNMT/CTranslate2) |
| RapidOCR | 板书 OCR | [RapidOCR](https://github.com/RapidAI/RapidOCR) |
| ONNX Runtime | 本地模型推理 | [ONNX Runtime](https://github.com/microsoft/onnxruntime) |
| NumPy | 向量运算 | [NumPy](https://github.com/numpy/numpy) |
| Pillow | 图像处理 | [Pillow](https://github.com/python-pillow/Pillow) |
| pypinyin | 中文词汇辅助 | [pypinyin](https://github.com/mozillazg/python-pinyin) |
| tokenizers | 语义分词 | [tokenizers](https://github.com/huggingface/tokenizers) |
| multilingual-e5-small | 语义嵌入 | [原始模型卡](https://huggingface.co/intfloat/multilingual-e5-small), [ONNX 转换](https://huggingface.co/Xenova/multilingual-e5-small) |
| Whisper 模型 | 语音识别权重 | [OpenAI Whisper](https://github.com/openai/whisper), [faster-whisper 模型入口](https://github.com/SYSTRAN/faster-whisper) |

依赖的传递组件以安装后的包元数据为准。E5 下载脚本固定模型提交和文件 SHA，不表示忽略模型许可。选择包含 libx264 的 FFmpeg 构建时尤其应查看该构建的具体许可。

`scripts/make_demo.py` 生成的演示是 FFmpeg 测试图案与音调，没有课程、人物语音或第三方授课素材。文档中的示例路径为虚构工作路径。
