"""随口记 · 纯标准库 AES-128-ECB。

为什么自己写：iLink 的媒体文件走 AES-128-ECB 加密的 CDN，
而本机没有 cryptography / pycryptodome，项目也不想引第三方依赖
（商家要在自己的机器上跑，装不上依赖就没法用）。

只用得到解密：我们要读商家发进来的表格和图片，不需要加密。
但为了能对着标准测试向量自证正确，加密也一并实现了。

正确性用 FIPS-197 的已知答案向量验证，见 tests/test_aes.py。
"""
from __future__ import annotations

# ---------------------------------------------------------------- GF(2^8)


def _xtime(a: int) -> int:
    """乘 2（模 AES 的不可约多项式 x^8+x^4+x^3+x+1）。"""
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法。"""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _inv(a: int) -> int:
    """GF(2^8) 乘法逆元：a^254（费马小定理，AES 里 a!=0 时 a^255=1）。"""
    if a == 0:
        return 0
    result, base, exp = 1, a, 254
    while exp:
        if exp & 1:
            result = _mul(result, base)
        base = _mul(base, base)
        exp >>= 1
    return result


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_sbox() -> list:
    """按定义构造 S 盒：先取逆元，再做仿射变换。

    比手抄 256 字节的表可靠 —— 抄错一个字节只有极少数输入会暴露。
    """
    box = []
    for a in range(256):
        s = _inv(a)
        box.append(s ^ _rotl8(s, 1) ^ _rotl8(s, 2) ^ _rotl8(s, 3) ^ _rotl8(s, 4) ^ 0x63)
    return box


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


# ---------------------------------------------------------------- 轮密钥

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def expand_key(key: bytes) -> list:
    """AES-128 密钥扩展，返回 11 个 16 字节轮密钥。"""
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须是 16 字节，收到 {len(key)}")
    words = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]                      # RotWord
            t = [SBOX[b] for b in t]               # SubWord
            t[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ t[j] for j in range(4)])
    return [bytes(b for w in words[r * 4:r * 4 + 4] for b in w) for r in range(11)]


# ---------------------------------------------------------------- 轮变换


def _add_round_key(state: list, round_key: bytes) -> None:
    for i in range(16):
        state[i] ^= round_key[i]


def _sub_bytes(state: list, box: list) -> None:
    for i in range(16):
        state[i] = box[state[i]]


def _shift_rows(state: list) -> None:
    """状态按列主序存放：state[c*4 + r] 是第 c 列第 r 行。"""
    for r in range(1, 4):
        row = [state[c * 4 + r] for c in range(4)]
        row = row[r:] + row[:r]
        for c in range(4):
            state[c * 4 + r] = row[c]


def _inv_shift_rows(state: list) -> None:
    for r in range(1, 4):
        row = [state[c * 4 + r] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            state[c * 4 + r] = row[c]


def _mix_columns(state: list) -> None:
    for c in range(4):
        i = c * 4
        a = state[i:i + 4]
        state[i + 0] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
        state[i + 1] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
        state[i + 2] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
        state[i + 3] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)


def _inv_mix_columns(state: list) -> None:
    for c in range(4):
        i = c * 4
        a = state[i:i + 4]
        state[i + 0] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
        state[i + 1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
        state[i + 2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
        state[i + 3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)


# ---------------------------------------------------------------- 分组


def encrypt_block(block: bytes, round_keys: list) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[0])
    for rnd in range(1, 10):
        _sub_bytes(state, SBOX)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[rnd])
    _sub_bytes(state, SBOX)
    _shift_rows(state)
    _add_round_key(state, round_keys[10])
    return bytes(state)


def decrypt_block(block: bytes, round_keys: list) -> bytes:
    state = list(block)
    _add_round_key(state, round_keys[10])
    for rnd in range(9, 0, -1):
        _inv_shift_rows(state)
        _sub_bytes(state, INV_SBOX)
        _add_round_key(state, round_keys[rnd])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _sub_bytes(state, INV_SBOX)
    _add_round_key(state, round_keys[0])
    return bytes(state)


# ---------------------------------------------------------------- 对外


def _check(data: bytes, key: bytes) -> None:
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须是 16 字节，收到 {len(key)}")
    if len(data) % 16:
        raise ValueError(f"密文长度必须是 16 的整数倍，收到 {len(data)}")


def ecb_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密。data 长度必须是 16 的整数倍。

    末尾的 PKCS#7 填充会去掉 —— iLink 下载下来的密文是按块补过位的，
    直接当明文用会在文件末尾多出几个字节。
    """
    _check(data, key)
    round_keys = expand_key(key)
    out = b"".join(decrypt_block(data[i:i + 16], round_keys)
                   for i in range(0, len(data), 16))
    return _strip_pkcs7(out)


def ecb_decrypt_raw(data: bytes, key: bytes) -> bytes:
    """同上，但**不去填充**。数据本身不是 PKCS#7 补位时用这个。"""
    _check(data, key)
    round_keys = expand_key(key)
    return b"".join(decrypt_block(data[i:i + 16], round_keys)
                    for i in range(0, len(data), 16))


def ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密，按 PKCS#7 补位。只用于自测，业务上不需要。"""
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须是 16 字节，收到 {len(key)}")
    pad = 16 - (len(data) % 16)
    padded = data + bytes([pad]) * pad
    return ecb_encrypt_raw(padded, key)


def ecb_encrypt_raw(data: bytes, key: bytes) -> bytes:
    """同上，但**不补位**。对已知答案向量时用这个，长度必须对齐。"""
    _check(data, key)
    round_keys = expand_key(key)
    return b"".join(encrypt_block(data[i:i + 16], round_keys)
                    for i in range(0, len(data), 16))


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def parse_aes_key(aes_key_b64: str) -> bytes:
    """解析 iLink 给的 aes_key。

    base64 解出来可能是 16 字节原始密钥，也可能是 32 个字符的 hex 文本
    （见 docs/ilink-protocol-notes.md 第 6 节），两种都要认。
    """
    import base64

    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"aes_key 是 32 字节但不是 hex 文本：{decoded!r}") from exc
    raise ValueError(f"认不出的 aes_key 长度：base64 解码后 {len(decoded)} 字节")


def key_from_hex(hex_key: str) -> bytes:
    """image_item.aeskey 是 hex 字符串，跟别的类型格式不一样。"""
    return bytes.fromhex(hex_key)
