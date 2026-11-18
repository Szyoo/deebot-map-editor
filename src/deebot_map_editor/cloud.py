"""科沃斯云端会话：登录、找设备、发送任意命令拿原始回包。

凭据只从环境变量读取（可以先用 load_env_file 从项目外的文件导入）：
  ECOVACS_USERNAME  账号
  ECOVACS_PASSWORD  密码（明文，发给云端前做 md5）
  ECOVACS_COUNTRY   国家/地区代码，国行填 CN
可选：
  DEEBOT_DID        设备 did，账号下有多台设备时用来指定
  DEEBOT_CLASS      设备型号 class（例如 sd3tgp），同上

本模块不打印、不记录任何凭据。
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path


class DeviceOffline(Exception):
    """设备没有回包（超时、云端报设备离线等）。"""


class CommandError(Exception):
    def __init__(self, name: str, body: dict) -> None:
        self.body = body
        super().__init__(f"{name} 返回 code={body.get('code')} msg={body.get('msg')!r}")


def load_env_file(path: str | os.PathLike | None) -> bool:
    """读取 KEY=VALUE 文件，只填充环境变量里还没有的键。文件不存在返回 False。"""
    if not path:
        return False
    p = Path(path).expanduser()
    if not p.is_file():
        return False
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)
    return True


def has_credentials() -> bool:
    return bool(os.environ.get("ECOVACS_USERNAME") and os.environ.get("ECOVACS_PASSWORD"))


class CloudSession:
    """异步上下文：async with CloudSession() as s: body = await s.send(name, args)"""

    # 型号不在 deebot-client 支持列表里时，借用这个型号的静态能力描述。
    # 只影响库内部的事件处理；本工具只发原始命令，不依赖具体能力。
    FALLBACK_CLASS = "9jc32i"

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self._session = None
        self.bot = None
        self.device_name = ""

    async def __aenter__(self) -> CloudSession:
        import aiohttp
        from deebot_client.api_client import ApiClient
        from deebot_client.authentication import Authenticator, create_rest_config
        from deebot_client.device import Device
        from deebot_client.hardware import get_static_device_info
        from deebot_client.models import DeviceInfo
        from deebot_client.util import md5

        if not has_credentials():
            raise RuntimeError("没有凭据：请设置 ECOVACS_USERNAME / ECOVACS_PASSWORD，或用 --env-file 指定凭据文件")
        country = os.environ.get("ECOVACS_COUNTRY", "CN")
        self._session = aiohttp.ClientSession()
        try:
            device_id = md5(str(time.time()))
            rest = create_rest_config(self._session, device_id=device_id, alpha_2_country=country)
            auth = Authenticator(rest, os.environ["ECOVACS_USERNAME"], md5(os.environ["ECOVACS_PASSWORD"]))
            devices = await ApiClient(auth).get_devices()

            want_did = os.environ.get("DEEBOT_DID")
            want_class = os.environ.get("DEEBOT_CLASS")

            def match(api: dict) -> bool:
                if want_did and api.get("did") != want_did:
                    return False
                if want_class and api.get("class") != want_class:
                    return False
                return True

            known = [d for d in devices.mqtt if match(d.api)]
            unknown = [d for d in devices.not_supported if match(d)]
            if len(known) + len(unknown) == 0:
                raise RuntimeError("账号下没有找到匹配的设备（可用 DEEBOT_DID / DEEBOT_CLASS 指定）")
            if len(known) + len(unknown) > 1:
                names = [d.api.get("class") for d in known] + [d.get("class") for d in unknown]
                raise RuntimeError(f"账号下有多台设备 {names}，请用 DEEBOT_DID 或 DEEBOT_CLASS 指定")
            if known:
                info = known[0]
            else:
                static = await get_static_device_info(self.FALLBACK_CLASS)
                if static is None:
                    raise RuntimeError("deebot-client 版本不兼容：找不到备用型号描述")
                info = DeviceInfo(unknown[0], static)
            self.device_name = info.api.get("nick") or info.api.get("deviceName") or info.api.get("class", "")
            self.bot = Device(info, auth)
        except BaseException:
            await self._session.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self.bot is not None:
            try:
                await self.bot.teardown()
            except Exception:
                pass
        if self._session is not None:
            await self._session.close()

    async def send(self, name: str, args: dict, timeout: float | None = None) -> dict:
        """发送命令，返回回包 body（含 code/msg/data）。设备没回包时抛 DeviceOffline。"""
        from deebot_client.commands.json.common import JsonCommandWithMessageHandling
        from deebot_client.const import DataType
        from deebot_client.message import HandlingResult, MessageBody

        class RawCommand(JsonCommandWithMessageHandling, MessageBody):
            DATA_TYPE = DataType.JSON
            NAME = name

            @classmethod
            def _handle_body(cls, _event_bus, _body) -> HandlingResult:
                return HandlingResult.success()

        assert self.bot is not None
        try:
            result = await asyncio.wait_for(
                self.bot._execute_command(RawCommand(args)),  # noqa: SLF001
                timeout or self.timeout,
            )
        except TimeoutError as exc:
            raise DeviceOffline(f"{name}：等待回包超时") from exc
        raw = result.raw_response or {}
        body = ((raw.get("resp") or {}).get("body")) or {}
        if raw.get("ret") != "ok" or not body:
            raise DeviceOffline(f"{name}：设备未回包（ret={raw.get('ret')} errno={raw.get('errno')}）")
        return body
