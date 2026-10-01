"""P0-3 登录态落盘：权限收紧 + AES-GCM 加密 + 一键清除。

规格书 3.2：
- runtime/user/ 下存的是能直接下单的登录 cookie
- 文件权限 0600
- 用 AES 加密，key 从环境变量读
- 提供一键清除登录态的命令

说明：本模块是「加密落盘」的工具层，不改动 py12306 现有的登录流程。
接入方式见 README（把原来直接 json.dump 的地方换成 state_store.save()）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .redaction import redact

logger = logging.getLogger(__name__)

MAGIC = b"P12306E1"
SALT_LEN = 16
NONCE_LEN = 12
_TAG_LEN = 16
_SCRYPT_N = 2 ** 15


class StateEncryptionUnavailable(RuntimeError):
    """需要加密但缺少 RUNTIME_ENC_KEY 或 cryptography。"""


def _load_aesgcm() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

        return AESGCM
    except Exception:  # pragma: no cover - 取决于环境
        return None


#: scrypt 的代价参数候选，按顺序尝试。
#: 说明：OpenSSL 的 scrypt 实现有内存上限（Windows 上尤其明显，约 32MB），
#: n=2^15/r=8 会直接报 "memory limit exceeded"，所以必须能降级。
_SCRYPT_CANDIDATES = (2 ** 14, 2 ** 13)
_SCRYPT_R = 8
_SCRYPT_P = 1


def derive_key(secret: str, salt: bytes) -> bytes:
    """从口令派生 32 字节密钥。

    优先 scrypt（内存硬），在当前 OpenSSL 不允许时降级为 PBKDF2-HMAC-SHA256。
    两种方式都用高迭代/内存代价，目的是防「拿到文件后离线爆破」。
    """
    import hashlib

    raw = secret.encode("utf-8")
    for n in _SCRYPT_CANDIDATES:
        try:
            return hashlib.scrypt(raw, salt=salt, n=n, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
        except (ValueError, OSError):
            continue
    # 最后的兜底：PBKDF2 不是内存硬，但 600k 次迭代下爆破成本仍然很高
    return hashlib.pbkdf2_hmac("sha256", raw, salt, 600_000, dklen=32)


def encrypt_bytes(plaintext: bytes, secret: str) -> bytes:
    aesgcm = _load_aesgcm()
    if aesgcm is None:
        raise StateEncryptionUnavailable(
            "缺少 cryptography，无法加密登录态。请 pip install cryptography，或显式设置 ALLOW_PLAINTEXT_STATE=1"
        )
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = derive_key(secret, salt)
    blob = aesgcm(key).encrypt(nonce, plaintext, MAGIC)
    return MAGIC + salt + nonce + blob


def decrypt_bytes(blob: bytes, secret: str) -> bytes:
    aesgcm = _load_aesgcm()
    if aesgcm is None:
        raise StateEncryptionUnavailable("缺少 cryptography，无法解密登录态")
    if not blob.startswith(MAGIC):
        raise ValueError("不是本模块写出的加密登录态（魔数不匹配）")
    body = blob[len(MAGIC) :]
    salt = body[:SALT_LEN]
    nonce = body[SALT_LEN : SALT_LEN + NONCE_LEN]
    ciphertext = body[SALT_LEN + NONCE_LEN :]
    key = derive_key(secret, salt)
    return aesgcm(key).decrypt(nonce, ciphertext, MAGIC)


def is_encrypted(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


# --- 文件权限 -------------------------------------------------------------


def _restrict_posix(path: Path, mode: int) -> bool:
    try:
        if path.is_dir():
            os.chmod(path, 0o700)
        else:
            os.chmod(path, mode)
        return True
    except OSError:
        return False


def _restrict_windows(path: Path) -> bool:
    """Windows 没有 0600；用 icacls 去掉继承并只保留当前用户。"""
    user = os.environ.get("USERNAME")
    if not user:
        return False
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
            check=False,
            capture_output=True,
            timeout=15,
        )
        return True
    except Exception:  # pragma: no cover - 非 Windows 或 icacls 缺失
        return False


def secure_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """原子写 + 收紧权限。临时文件与目标文件权限一致，避免中间态窗口。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    if os.name == "posix":
        _restrict_posix(path, mode)
    else:
        _restrict_windows(path)


def harden_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        _restrict_posix(path, 0o700)
    else:
        _restrict_windows(path)


def permission_report(path: Path) -> Dict[str, Any]:
    """给自检用：报告文件当前的可读权限是否收敛。"""
    out: Dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return out
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        out["mode"] = oct(mode)
        out["secure"] = mode & 0o077 == 0
    else:
        out["mode"] = "windows-acl"
        try:
            result = subprocess.run(
                ["icacls", str(path)], check=False, capture_output=True, text=True, timeout=15
            )
            text = result.stdout or ""
            out["acl_summary"] = " ".join(text.split())[:200]
            out["secure"] = "BUILTIN\\Users" not in text and "Everyone" not in text
        except Exception:
            out["secure"] = None
    return out


# --- 登录态仓库 -----------------------------------------------------------


@dataclass
class StoredState:
    name: str
    path: Path
    encrypted: bool
    size: int
    mtime: float


class LoginStateStore:
    """runtime/user/ 下登录态的读写门面。

    payload 结构对上层保持原样（py12306 的 cookie 字典），本层只负责
    「序列化 -> 加密 -> 0600 落盘」和反向过程。
    """

    def __init__(
        self,
        root: Path,
        secret: Optional[str] = None,
        *,
        allow_plaintext: bool = False,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.root = Path(root)
        self.secret = secret or None
        self.allow_plaintext = allow_plaintext
        self._log = logger_ or logger

    @classmethod
    def from_config(cls, config: Any) -> "LoginStateStore":
        env = config.raw_env or {}
        allow = (env.get("ALLOW_PLAINTEXT_STATE", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
        secret = config.enc_key.reveal() if config.enc_key else None
        return cls(config.paths.state_dir / "user", secret, allow_plaintext=allow)

    # -- 路径 ----------------------------------------------------------

    @staticmethod
    def _safe_name(name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in "-_.") or "default"
        # 去掉调用方可能带上的扩展名，保证 name <-> path 是双射
        for suffix in (".json.enc", ".json.enc.tmp", ".json"):
            if safe.endswith(suffix):
                safe = safe[: -len(suffix)]
                break
        return safe or "default"

    def path_for(self, name: str) -> Path:
        return self.root / f"{self._safe_name(name)}.json.enc"

    def legacy_path_for(self, name: str) -> Path:
        return self.root / f"{self._safe_name(name)}.json"

    # -- 写 ------------------------------------------------------------

    def save(self, name: str, payload: Dict[str, Any]) -> Path:
        harden_dir(self.root)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        target = self.path_for(name)

        if self.secret:
            secure_write(target, encrypt_bytes(raw, self.secret))
            self._scrub_legacy(name)
            return target

        if not self.allow_plaintext:
            raise StateEncryptionUnavailable(
                "未设置 RUNTIME_ENC_KEY，拒绝明文落盘登录态（能直接下单的 cookie）。"
                "请设置 RUNTIME_ENC_KEY，或显式设置 ALLOW_PLAINTEXT_STATE=1 表示你接受风险"
            )
        # 明确放行的明文路径：仍然收紧权限
        legacy = self.legacy_path_for(name)
        secure_write(legacy, raw)
        self._log.warning("登录态以明文写入 %s（ALLOW_PLAINTEXT_STATE=1）", legacy)
        return legacy

    def load(self, name: str) -> Optional[Dict[str, Any]]:
        enc = self.path_for(name)
        if enc.is_file():
            if not self.secret:
                raise StateEncryptionUnavailable(f"{enc} 是加密文件，但未提供 RUNTIME_ENC_KEY")
            return json.loads(decrypt_bytes(enc.read_bytes(), self.secret).decode("utf-8"))
        plain = self.legacy_path_for(name)
        if plain.is_file():
            self._log.warning("读到明文登录态 %s，建议重新登录以迁移到加密存储", plain)
            return json.loads(plain.read_text(encoding="utf-8"))
        return None

    # -- 清理 ----------------------------------------------------------

    def _scrub_legacy(self, name: str) -> None:
        legacy = self.legacy_path_for(name)
        if legacy.is_file():
            legacy.unlink(missing_ok=True)
            self._log.info("已删除迁移后的明文登录态 %s", legacy)

    def list_states(self) -> List[StoredState]:
        if not self.root.is_dir():
            return []
        out: List[StoredState] = []
        for path in sorted(self.root.iterdir()):
            if not self._is_state_file(path):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(
                StoredState(
                    name=self._safe_name(path.name),
                    path=path,
                    encrypted=is_encrypted(path),
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
        return out

    def _is_state_file(self, path: Path) -> bool:
        """只认本模块写出的登录态文件。

        必须收得很紧：runtime/user 下通常还有一个 .gitignore（上游靠它保住目录），
        无差别删除会把目录里的占位文件也删掉，导致目录不再被 git 跟踪。
        """
        if not path.is_file() or path.name.startswith("."):
            return False
        if path.name.endswith(".tmp") or ".tmp" in path.name:
            return False
        return path.name.endswith(".json.enc") or path.name.endswith(".json")

    def purge(self, names: Optional[Iterable[str]] = None) -> List[Path]:
        """一键清除登录态。返回被删除的文件列表。只动登录态文件，不动其它文件。"""
        removed: List[Path] = []
        targets: List[Path] = []
        if names is None:
            if self.root.is_dir():
                targets = [p for p in self.root.iterdir() if self._is_state_file(p)]
        else:
            for name in names:
                for candidate in (self.path_for(name), self.legacy_path_for(name)):
                    if self._is_state_file(candidate):
                        targets.append(candidate)
        for path in targets:
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                self._log.warning("删除 %s 失败：%s", path, exc)
        if names is None and self.root.is_dir():
            try:
                if not any(self.root.iterdir()):
                    shutil.rmtree(self.root, ignore_errors=True)
            except OSError:
                pass
        return removed

    def audit(self) -> List[Dict[str, Any]]:
        """逐个报告权限与加密状态。"""
        return [
            {
                "name": item.name,
                "path": str(item.path),
                "encrypted": item.encrypted,
                **{k: v for k, v in permission_report(item.path).items() if k != "path"},
            }
            for item in self.list_states()
        ]
