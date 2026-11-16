from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


class MP4Error(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Box:
    kind: str
    start: int
    size: int
    header_size: int

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def end(self) -> int:
        return self.start + self.size


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise MP4Error("MP4 文件意外结束")
    return data


def iter_boxes(handle: BinaryIO, start: int, end: int) -> Iterator[Box]:
    position = start
    while position + 8 <= end:
        handle.seek(position)
        size32, kind_bytes = struct.unpack(">I4s", _read_exact(handle, 8))
        kind = kind_bytes.decode("latin-1")
        header_size = 8
        if size32 == 1:
            size = struct.unpack(">Q", _read_exact(handle, 8))[0]
            header_size = 16
        elif size32 == 0:
            size = end - position
        else:
            size = size32
        if kind == "uuid":
            _read_exact(handle, 16)
            header_size += 16
        if size < header_size or position + size > end:
            raise MP4Error(f"无效的 {kind!r} box 大小")
        yield Box(kind=kind, start=position, size=size, header_size=header_size)
        position += size


def _children(handle: BinaryIO, parent: Box) -> list[Box]:
    return list(iter_boxes(handle, parent.payload_start, parent.end))


def _child(handle: BinaryIO, parent: Box, kind: str, required: bool = True) -> Box | None:
    for box in iter_boxes(handle, parent.payload_start, parent.end):
        if box.kind == kind:
            return box
    if required:
        raise MP4Error(f"缺少 {kind} box")
    return None


def _payload(handle: BinaryIO, box: Box) -> bytes:
    handle.seek(box.payload_start)
    return _read_exact(handle, box.end - box.payload_start)


def _fullbox(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < 4:
        raise MP4Error("full box 太短")
    return payload[0], payload[4:]


def _handler_type(handle: BinaryIO, mdia: Box) -> str:
    payload = _payload(handle, _child(handle, mdia, "hdlr"))
    _, body = _fullbox(payload)
    if len(body) < 8:
        raise MP4Error("hdlr box 太短")
    return body[4:8].decode("latin-1")


def _mdhd(handle: BinaryIO, mdia: Box) -> tuple[int, int]:
    payload = _payload(handle, _child(handle, mdia, "mdhd"))
    version, body = _fullbox(payload)
    if version == 0:
        if len(body) < 16:
            raise MP4Error("mdhd v0 box 太短")
        timescale, duration = struct.unpack_from(">II", body, 8)
    elif version == 1:
        if len(body) < 28:
            raise MP4Error("mdhd v1 box 太短")
        timescale, duration = struct.unpack_from(">IQ", body, 16)
    else:
        raise MP4Error(f"不支持 mdhd 版本 {version}")
    if timescale <= 0:
        raise MP4Error("无效音频 timescale")
    return timescale, duration


def _table_box(handle: BinaryIO, mdia: Box, kind: str, required: bool = True) -> Box | None:
    minf = _child(handle, mdia, "minf")
    stbl = _child(handle, minf, "stbl")
    return _child(handle, stbl, kind, required=required)


def _codec(handle: BinaryIO, mdia: Box) -> str:
    payload = _payload(handle, _table_box(handle, mdia, "stsd"))
    _, body = _fullbox(payload)
    if len(body) < 12:
        raise MP4Error("stsd box 太短")
    entry_count = struct.unpack_from(">I", body, 0)[0]
    if not entry_count:
        raise MP4Error("stsd 中没有 sample entry")
    size, codec = struct.unpack_from(">I4s", body, 4)
    if size < 8:
        raise MP4Error("无效 sample entry")
    code = codec.decode("latin-1")
    return {"mp4a": "aac"}.get(code, code)


def _stsz(handle: BinaryIO, mdia: Box) -> list[int]:
    payload = _payload(handle, _table_box(handle, mdia, "stsz"))
    _, body = _fullbox(payload)
    if len(body) < 8:
        raise MP4Error("stsz box 太短")
    sample_size, sample_count = struct.unpack_from(">II", body, 0)
    if sample_size:
        return [sample_size] * sample_count
    expected = 8 + sample_count * 4
    if len(body) < expected:
        raise MP4Error("stsz 样本大小表不完整")
    return list(struct.unpack_from(f">{sample_count}I", body, 8)) if sample_count else []


def _run_table(handle: BinaryIO, mdia: Box, kind: str, signed_second: bool = False) -> list[tuple[int, int]]:
    box = _table_box(handle, mdia, kind, required=kind != "ctts")
    if box is None:
        return []
    payload = _payload(handle, box)
    version, body = _fullbox(payload)
    if len(body) < 4:
        raise MP4Error(f"{kind} box 太短")
    count = struct.unpack_from(">I", body, 0)[0]
    if len(body) < 4 + count * 8:
        raise MP4Error(f"{kind} 表不完整")
    rows = []
    for index in range(count):
        offset = 4 + index * 8
        sample_count = struct.unpack_from(">I", body, offset)[0]
        if signed_second and version == 1:
            value = struct.unpack_from(">i", body, offset + 4)[0]
        else:
            value = struct.unpack_from(">I", body, offset + 4)[0]
        rows.append((sample_count, value))
    return rows


def _stsc(handle: BinaryIO, mdia: Box) -> list[tuple[int, int, int]]:
    payload = _payload(handle, _table_box(handle, mdia, "stsc"))
    _, body = _fullbox(payload)
    if len(body) < 4:
        raise MP4Error("stsc box 太短")
    count = struct.unpack_from(">I", body, 0)[0]
    if len(body) < 4 + count * 12:
        raise MP4Error("stsc 表不完整")
    return [struct.unpack_from(">III", body, 4 + index * 12) for index in range(count)]


def _chunk_offsets(handle: BinaryIO, mdia: Box) -> list[int]:
    box = _table_box(handle, mdia, "stco", required=False)
    width = 4
    if box is None:
        box = _table_box(handle, mdia, "co64")
        width = 8
    payload = _payload(handle, box)
    _, body = _fullbox(payload)
    if len(body) < 4:
        raise MP4Error(f"{box.kind} box 太短")
    count = struct.unpack_from(">I", body, 0)[0]
    if len(body) < 4 + count * width:
        raise MP4Error(f"{box.kind} 表不完整")
    format_code = "I" if width == 4 else "Q"
    return list(struct.unpack_from(f">{count}{format_code}", body, 4)) if count else []


def _expand_runs(runs: list[tuple[int, int]], sample_count: int, name: str) -> list[int]:
    values: list[int] = []
    for count, value in runs:
        values.extend([value] * count)
    if len(values) != sample_count:
        raise MP4Error(f"{name} 样本数 {len(values)} 与 stsz 的 {sample_count} 不一致")
    return values


def _sample_offsets(
    sample_sizes: list[int], chunk_offsets: list[int], stsc: list[tuple[int, int, int]]
) -> list[int]:
    if sample_sizes and (not chunk_offsets or not stsc):
        raise MP4Error("缺少音频 chunk 映射")
    offsets: list[int] = []
    sample_index = 0
    entry_index = 0
    for chunk_index, chunk_offset in enumerate(chunk_offsets, 1):
        while entry_index + 1 < len(stsc) and stsc[entry_index + 1][0] <= chunk_index:
            entry_index += 1
        first_chunk, samples_per_chunk, _description = stsc[entry_index]
        if first_chunk > chunk_index:
            raise MP4Error("stsc 的 first_chunk 无效")
        cursor = chunk_offset
        for _ in range(samples_per_chunk):
            if sample_index >= len(sample_sizes):
                break
            offsets.append(cursor)
            cursor += sample_sizes[sample_index]
            sample_index += 1
    if sample_index != len(sample_sizes):
        raise MP4Error(f"仅定位到 {sample_index}/{len(sample_sizes)} 个音频样本")
    return offsets


def audio_signature(path: str | Path) -> dict[str, object]:
    source = Path(path).expanduser().resolve()
    file_size = source.stat().st_size
    with source.open("rb") as handle:
        top = list(iter_boxes(handle, 0, file_size))
        moov = next((box for box in top if box.kind == "moov"), None)
        if moov is None:
            raise MP4Error("不是可识别的 MP4/M4A：缺少 moov")
        audio_mdia = None
        for trak in (box for box in _children(handle, moov) if box.kind == "trak"):
            mdia = _child(handle, trak, "mdia", required=False)
            if mdia is not None and _handler_type(handle, mdia) == "soun":
                audio_mdia = mdia
                break
        if audio_mdia is None:
            raise MP4Error("文件中没有音频轨")

        timescale, declared_duration = _mdhd(handle, audio_mdia)
        codec = _codec(handle, audio_mdia)
        sizes = _stsz(handle, audio_mdia)
        durations = _expand_runs(_run_table(handle, audio_mdia, "stts"), len(sizes), "stts")
        composition_runs = _run_table(handle, audio_mdia, "ctts", signed_second=True)
        composition = _expand_runs(composition_runs, len(sizes), "ctts") if composition_runs else [0] * len(sizes)
        offsets = _sample_offsets(sizes, _chunk_offsets(handle, audio_mdia), _stsc(handle, audio_mdia))

        payload_digest = hashlib.sha256()
        timing_digest = hashlib.sha256()
        decode_time = 0
        first_pts = None
        for offset, size, duration, composition_offset in zip(offsets, sizes, durations, composition):
            handle.seek(offset)
            payload_digest.update(_read_exact(handle, size))
            pts = decode_time + composition_offset
            if first_pts is None:
                first_pts = pts
            timing_digest.update(f"{pts},{decode_time},{duration}\n".encode("ascii"))
            decode_time += duration

    return {
        "codec": codec,
        "time_base": f"1/{timescale}",
        "start_time": first_pts or 0,
        "duration_ticks": decode_time,
        "declared_duration_ticks": declared_duration,
        "packet_count": len(sizes),
        "payload_sha256": payload_digest.hexdigest(),
        "packet_timing_sha256": timing_digest.hexdigest(),
    }


def compare_audio(source: str | Path, output: str | Path) -> dict[str, object]:
    before = audio_signature(source)
    after = audio_signature(output)
    compared_fields = [
        "codec",
        "time_base",
        "start_time",
        "duration_ticks",
        "packet_count",
        "payload_sha256",
        "packet_timing_sha256",
    ]
    mismatches = {
        field: {"source": before[field], "output": after[field]}
        for field in compared_fields
        if before[field] != after[field]
    }
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "source": before,
        "output": after,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="逐包比较 M4A/MP4 中的 AAC 内容与时间戳")
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args()
    result = compare_audio(args.source, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
