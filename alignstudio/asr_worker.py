"""PCM ASR adapter, avoiding PyAV/faster-whisper decoder version mismatches."""
from __future__ import annotations
import json
import sys
import wave
from pathlib import Path

def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.path[:0] = request.get("module_paths", [])
    import numpy as np
    from faster_whisper import WhisperModel
    with wave.open(request["audio"], "rb") as stream:
        if stream.getframerate() != 16000 or stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError("Expected mono 16kHz PCM16 WAV")
        audio = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    model = WhisperModel(request["model"], device="cpu", compute_type="int8",
                         cpu_threads=request.get("threads", 4))
    segments, _ = model.transcribe(
        audio, language=request.get("language") or None, beam_size=3,
        vad_filter=True, word_timestamps=True, condition_on_previous_text=False,
    )
    rows = []
    for segment in segments:
        rows.append({
            "start": segment.start, "end": segment.end, "text": segment.text,
            "words": [[word.start, word.end, word.word, round(word.probability, 3)]
                      for word in segment.words or []],
        })
        print(json.dumps({"stage": "asr", "time": segment.end}), flush=True)
    output = Path(request["output"])
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output)

if __name__ == "__main__":
    main()
