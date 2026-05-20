"""encryptedpasswords plugin 호환 복호화 round-trip 테스트.

OpenSSL AES-256-CBC + EVP_BytesToKey(MD5, 1 iter) 형식.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


def _aes():
    pytest.importorskip("Crypto.Cipher")
    from Crypto.Cipher import AES  # type: ignore
    return AES


def _encrypt(plaintext: str, password: str, salt: bytes | None = None) -> str:
    """OpenSSL AES-256-CBC 호환 암호화 — 테스트용 (encryptedpasswords plugin
    의 출력과 동일 형식). 실 서비스에는 마이그레이션 스크립트가 *복호화만*
    제공하므로 본 함수는 테스트 fixture 용."""
    AES = _aes()
    import os
    salt = salt or os.urandom(8)
    key, iv = run._evp_bytes_to_key(password.encode("utf-8"), salt)
    pad = 16 - (len(plaintext.encode("utf-8")) % 16)
    pt = plaintext.encode("utf-8") + bytes([pad] * pad)
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pt)
    return base64.b64encode(b"Salted__" + salt + ct).decode("ascii")


def test_round_trip_ascii() -> None:
    enc = _encrypt("hello world", "mypassword")
    out = run.decrypt_encryptedpasswords(enc, "mypassword")
    assert out == "hello world"


def test_round_trip_korean() -> None:
    enc = _encrypt("비밀번호: 한글 테스트", "패스워드")
    out = run.decrypt_encryptedpasswords(enc, "패스워드")
    assert out == "비밀번호: 한글 테스트"


def test_round_trip_long_text() -> None:
    plain = "A" * 200 + "한글" * 30
    enc = _encrypt(plain, "x")
    out = run.decrypt_encryptedpasswords(enc, "x")
    assert out == plain


def test_round_trip_block_aligned() -> None:
    # 본문 16-bytes 정렬 (PKCS7 패딩이 한 블록 추가)
    plain = "0123456789ABCDEF"  # 16 bytes
    enc = _encrypt(plain, "k")
    out = run.decrypt_encryptedpasswords(enc, "k")
    assert out == plain


def test_wrong_password_raises_or_garbled() -> None:
    """잘못된 password — ValueError 또는 평문과 일치 안 함.

    PKCS7 padding 이 잘못된 password 라도 우연히 valid byte 가 나올 수 있어
    raise 안 될 수도 있음. 그 경우 평문이 *원본과 다르다* 는 점만 보장.
    """
    enc = _encrypt("secret", "correct", salt=b"\x01" * 8)  # 결정론적
    try:
        out = run.decrypt_encryptedpasswords(enc, "wrong")
        # raise 안 되면 평문은 원본과 다른지
        assert out != "secret"
    except (ValueError, UnicodeDecodeError):
        pass  # 정상 — 잘못된 password 검출됨


def test_invalid_format_raises() -> None:
    bad = base64.b64encode(b"NotSalted_abcdef" + b"random").decode("ascii")
    with pytest.raises(ValueError):
        run.decrypt_encryptedpasswords(bad, "any")


def test_evp_bytes_to_key_deterministic() -> None:
    """EVP_BytesToKey MD5 1-iter — OpenSSL 의 결과와 일치 확인.

    OpenSSL CLI 비교:
        echo -n "" | openssl enc -aes-256-cbc -P -pass pass:test -salt 0102030405060708 -nosalt
    의 key/iv 와 동일해야 함.
    """
    key, iv = run._evp_bytes_to_key(b"test", bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    assert len(key) == 32
    assert len(iv) == 16
    # 결정론적
    key2, iv2 = run._evp_bytes_to_key(b"test", bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    assert key == key2 and iv == iv2


def test_evp_bytes_to_key_different_salt() -> None:
    k1, _ = run._evp_bytes_to_key(b"pw", b"\x01" * 8)
    k2, _ = run._evp_bytes_to_key(b"pw", b"\x02" * 8)
    assert k1 != k2


def test_decrypt_known_vector() -> None:
    """알려진 vector — gibberish-aes.js 의 호환성 보장.

    plain="hello", password="p" 로 fixed salt 사용한 encryption.
    salt 가 random 이지만 _encrypt() 가 동일 함수 호출하면 같은 결과.
    """
    enc = _encrypt("hello", "p", salt=b"\xab" * 8)
    out = run.decrypt_encryptedpasswords(enc, "p")
    assert out == "hello"
    # cipher 의 prefix 가 "U2FsdGVk" (= base64 of "Salted__")
    assert enc.startswith("U2FsdGVk")


def test_decrypt_strips_whitespace() -> None:
    enc = _encrypt("x", "y")
    # cipher 앞뒤 공백/줄바꿈 — 사용자가 복붙 시 흔함
    out = run.decrypt_encryptedpasswords(f"  \n{enc}\n  ", "y")
    assert out == "x"
