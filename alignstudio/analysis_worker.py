"""Optional ASR/OCR process. The editor itself only needs the standard library."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    sys.path[:0] = request.get("module_paths", [])
    task = request["task"]
    output = Path(request["output"])
    if task == "asr":
        from faster_whisper import WhisperModel
        model = WhisperModel(
            request["model"], device="cpu", compute_type="int8",
            cpu_threads=request.get("threads", 4),
        )
        segments, info = model.transcribe(
            request["audio"], language=request.get("language") or None,
            beam_size=3, vad_filter=True, word_timestamps=True,
            condition_on_previous_text=False,
        )
        rows = []
        for segment in segments:
            rows.append({
                "start": segment.start, "end": segment.end, "text": segment.text,
                "words": [[word.start, word.end, word.word, round(word.probability, 3)]
                          for word in segment.words or []],
            })
            print(json.dumps({"stage": "asr", "time": segment.end}), flush=True)
        write_json(output, rows)
    elif task == "ocr":
        import numpy as np
        from PIL import Image
        from rapidocr import RapidOCR
        ocr = RapidOCR(params={
            "Global.use_cls": False, "Global.text_score": 0.40,
            "Det.box_thresh": 0.38, "Det.limit_side_len": 1180,
            "EngineConfig.onnxruntime.intra_op_num_threads": request.get("threads", 4),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
        left, top, right, bottom = request.get("crop", [0, 0, 0, 0])
        for index, item in enumerate(request["frames"]):
            destination = Path(item["output"])
            if destination.is_file():
                continue
            with Image.open(item["path"]) as image:
                if left + right >= image.width or top + bottom >= image.height:
                    raise ValueError("OCR crop exceeds frame bounds")
                pixels = np.asarray(image.crop((
                    left, top, image.width - right, image.height - bottom,
                )).convert("RGB"))[:, :, ::-1].copy()
            result = ocr(pixels, use_cls=False)
            findings = []
            if result.txts is not None:
                for box, text, score in zip(result.boxes, result.txts, result.scores):
                    findings.append({
                        "text": str(text), "score": round(float(score), 4),
                        "box": box.astype(int).tolist(),
                    })
            write_json(destination, {"board_time": item["time"], "findings": findings})
            print(json.dumps({"stage": "ocr", "done": index + 1,
                              "total": len(request["frames"])}), flush=True)
        write_json(output, {"done": True})
    else:
        raise ValueError("Unknown analysis task")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Analysis worker failed: {exc}\n"
              "Use a compatible Python with faster-whisper, rapidocr, onnxruntime, Pillow installed, "
              "or explicitly import existing JSONL caches.", file=sys.stderr, flush=True)
        raise
