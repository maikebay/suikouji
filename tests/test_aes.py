"""纯标准库 AES-128-ECB 的测试。

用标准已知答案向量（KAT）自证正确 —— 自己写密码学，唯一的验收标准
就是能不能复现权威向量，不能靠「看着像对」。
"""
from __future__ import annotations

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel import aes  # noqa: E402


class TestSbox(unittest.TestCase):
    def test_known_entries(self):
        # FIPS-197 图 7 的前几项，抄错一个字节这里就会红
        self.assertEqual(aes.SBOX[0x00], 0x63)
        self.assertEqual(aes.SBOX[0x01], 0x7C)
        self.assertEqual(aes.SBOX[0x02], 0x77)
        self.assertEqual(aes.SBOX[0x03], 0x7B)
        self.assertEqual(aes.SBOX[0x53], 0xED)

    def test_is_a_bijection(self):
        self.assertEqual(sorted(aes.SBOX), list(range(256)))
        for i in range(256):
            self.assertEqual(aes.INV_SBOX[aes.SBOX[i]], i)


class TestFieldArithmetic(unittest.TestCase):
    def test_multiplicative_inverse(self):
        for a in range(1, 256):
            self.assertEqual(aes._mul(a, aes._inv(a)), 1, f"a={a}")

    def test_inv_of_zero(self):
        self.assertEqual(aes._inv(0), 0)


class TestKnownAnswerVectors(unittest.TestCase):
    """FIPS-197 附录 B / C.1，以及 NIST SP 800-38A 的 ECB-AES128 向量。"""

    def test_fips197_c1(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(aes.ecb_decrypt_raw(cipher, key), plain)
        self.assertEqual(aes.ecb_encrypt_raw(plain, key), cipher)

    def test_fips197_appendix_b(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        plain = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
        cipher = bytes.fromhex("3925841d02dc09fbdc118597196a0b32")
        self.assertEqual(aes.ecb_decrypt_raw(cipher, key), plain)

    def test_sp800_38a_ecb_multiblock(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        plain = bytes.fromhex(
            "6bc1bee22e409f96e93d7e117393172a"
            "ae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52ef"
            "f69f2445df4f9b17ad2b417be66c3710")
        cipher = bytes.fromhex(
            "3ad77bb40d7a3660a89ecaf32466ef97"
            "f5d3d58503b9699de785895a96fdbaaf"
            "43b1cd7f598ece23881b00e3ed030688"
            "7b0c785e27e8ad3f8223207104725dd4")
        self.assertEqual(aes.ecb_decrypt_raw(cipher, key), plain)
        self.assertEqual(aes.ecb_encrypt_raw(plain, key), cipher)

    def test_ecb_is_blockwise(self):
        """ECB 的特点：相同明文块出相同密文块。iLink 用它，我们也照做。"""
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        block = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        out = aes.ecb_decrypt_raw(bytes.fromhex(
            "3ad77bb40d7a3660a89ecaf32466ef97" * 3), key)
        self.assertEqual(out, block * 3)


class TestPadding(unittest.TestCase):
    def test_round_trip(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        for size in (0, 1, 15, 16, 17, 1000):
            data = bytes(range(256))[:size] if size <= 256 else os.urandom(size)
            self.assertEqual(aes.ecb_decrypt(aes.ecb_encrypt(data, key), key), data,
                             f"size={size}")

    def test_strip_is_conservative(self):
        # 末尾不构成合法补位时不能乱切
        self.assertEqual(aes._strip_pkcs7(b"hello world!!!!!"), b"hello world!!!!!")
        self.assertEqual(aes._strip_pkcs7(b""), b"")
        # 合法补位要切掉
        self.assertEqual(aes._strip_pkcs7(b"abc" + bytes([13]) * 13), b"abc")


class TestKeyParsing(unittest.TestCase):
    def test_raw_16_bytes(self):
        key = bytes(range(16))
        self.assertEqual(aes.parse_aes_key(base64.b64encode(key).decode()), key)

    def test_hex_text_32_bytes(self):
        # 有些字段给的 base64 解出来是 32 个字符的 hex 文本
        hex_text = "00112233445566778899aabbccddeeff"
        b64 = base64.b64encode(hex_text.encode()).decode()
        self.assertEqual(aes.parse_aes_key(b64), bytes.fromhex(hex_text))

    def test_hex_key_helper(self):
        # image_item.aeskey 直接就是 hex 字符串
        self.assertEqual(aes.key_from_hex("000102030405060708090a0b0c0d0e0f"),
                         bytes(range(16)))

    def test_bad_length(self):
        with self.assertRaises(ValueError):
            aes.parse_aes_key(base64.b64encode(b"short").decode())


class TestGuards(unittest.TestCase):
    def test_bad_key_length(self):
        with self.assertRaises(ValueError) as ctx:
            aes.ecb_decrypt_raw(b"\x00" * 16, b"tooshort")
        self.assertIn("16 字节", str(ctx.exception))

    def test_unaligned_data(self):
        with self.assertRaises(ValueError) as ctx:
            aes.ecb_decrypt_raw(b"\x00" * 17, bytes(16))
        self.assertIn("16 的整数倍", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
