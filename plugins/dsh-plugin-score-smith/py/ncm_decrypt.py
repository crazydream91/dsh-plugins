"""把网易云音乐的 .ncm 加密音频解回 mp3/flac。

用法:
    python ncm_decrypt.py INPUT.ncm [OUTPUT]

原理（公开格式）:
  - 文件头 "CTENFDAM"，随后 2 字节保留、4 字节小端 key 长度
  - key 数据每字节 XOR 0x64，前面 17 字节是 "neteasecloudmusic" 标签
  - 剩下的用核心密钥 AES-128-ECB 解密，得到 RC4 的 key
  - 4 字节小端 meta 长度，meta 每字节 XOR 0x63，去掉 "163 key(Don't modify):" 前缀后 base64 + AES 解密得到 JSON
  - 4 字节图片长度，跳过封面
  - 之后整段音频用 keybox RC4 流解密，结尾 4 字节是 "163 key(Don't modify):" 的校验
"""
from __future__ import annotations

import base64
import json
import os
import sys

from Crypto.Cipher import AES

CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")  # hzHRAmso5kInbaxW
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")  # #14ljk_!\]&0U<'(

MAGIC = b"CTENFDAM"


def _unpad(data: bytes) -> bytes:
    pad = data[-1]
    if 0 < pad <= 16:
        return data[:-pad]
    return data


def _build_keybox(key: bytes) -> list[int]:
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) & 0xFF
        box[i], box[j] = box[j], box[i]
    return box


def decrypt_audio(payload: bytes, key: bytes) -> bytes:
    box = _build_keybox(key)
    out = bytearray(len(payload))
    i = j = 0
    for n, byte in enumerate(payload):
        i = (i + 1) & 0xFF
        j = (j + box[i]) & 0xFF
        box[i], box[j] = box[j], box[i]
        out[n] = byte ^ box[(box[i] + box[j]) & 0xFF]
    return bytes(out)


def parse(path: str) -> tuple[bytes, dict, str]:
    with open(path, "rb") as fh:
        raw = fh.read()

    if not raw.startswith(MAGIC):
        raise ValueError(f"{os.path.basename(path)} 不是 .ncm 文件")

    pos = len(MAGIC)
    pos += 2  # 保留位
    (key_len,) = int.from_bytes(raw[pos:pos + 4], "little"),
    pos += 4
    key_blob = raw[pos:pos + key_len]
    pos += key_len

    key_data = bytes(b ^ 0x64 for b in key_blob)
    if key_data[:17] != b"neteasecloudmusic":
        raise ValueError("密钥块格式不符")
    rc4_key = _unpad(AES.new(CORE_KEY, AES.MODE_ECB).decrypt(key_data[17:]))

    (meta_len,) = int.from_bytes(raw[pos:pos + 4], "little"),
    pos += 4
    meta: dict = {}
    if meta_len:
        meta_blob = bytes(b ^ 0x63 for b in raw[pos:pos + meta_len])
        pos += meta_len
        if meta_blob[:22] == b"163 key(Don't modify):":
            meta_blob = meta_blob[22:]
        try:
            plain = AES.new(META_KEY, AES.MODE_ECB).decrypt(base64.b64decode(meta_blob))
            meta = json.loads(_unpad(plain)[6:].decode("utf-8"))
        except Exception as exc:  # 元信息坏了不影响音频
            meta = {"_meta_error": str(exc)}

    (img_len,) = int.from_bytes(raw[pos:pos + 4], "little"),
    pos += 4
    if img_len:
        pos += img_len

    payload = raw[pos:]
    audio = decrypt_audio(payload, rc4_key)
    return audio, meta, os.path.splitext(path)[1].lower()


def _guess_ext(audio: bytes) -> str:
    if audio[:3] == b"ID3" or audio[:2] == b"\xff\xfb":
        return ".mp3"
    if audio[:4] == b"fLaC":
        return ".flac"
    return ".bin"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python ncm_decrypt.py INPUT.ncm [OUTPUT]")
        return 2
    src = argv[1]
    audio, meta, _ = parse(src)
    ext = _guess_ext(audio)
    if len(argv) > 2:
        dst = argv[2]
    else:
        base = meta.get("musicName") or os.path.splitext(os.path.basename(src))[0]
        artist = "、".join(a.get("name", "") for a in meta.get("artist") or [] if a.get("name"))
        name = f"{artist} - {base}" if artist else base
        dst = os.path.join(os.path.dirname(src), name + ext)
    with open(dst, "wb") as fh:
        fh.write(audio)
    print(json.dumps({
        "ok": True,
        "output": dst,
        "format": meta.get("format"),
        "bitrate": meta.get("bitrate"),
        "musicName": meta.get("musicName"),
        "album": meta.get("album"),
        "artist": [a.get("name") for a in meta.get("artist") or []],
        "durationMs": meta.get("duration"),
        "sizeMb": round(len(audio) / 1024 / 1024, 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
