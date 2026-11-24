"""i18n.py 仅翻译界面消息；规则、提示词、模型输入与证据原文不经过本模块。"""

from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
import ctypes
import gettext
import json
import os
from pathlib import Path
import re
import subprocess
import sys


CATALOGS = Path(__file__).resolve().parent / "locales"


class LanguageStrategy(ABC):
    """LanguageStrategy 以 GoF Strategy 隔离平台语言探测，不把平台差异或语言清单混入业务流程。"""

    @abstractmethod
    def preferred(self) -> list[str]:
        """preferred() 返回操作系统按优先顺序给出的界面语言，不推测项目材料使用的语言。"""


class EnvironmentLanguage(LanguageStrategy):
    """EnvironmentLanguage 读取 POSIX 消息语言约定，缺少设置时交给英文回退。"""

    def preferred(self) -> list[str]:
        """preferred() 按 gettext 约定读取第一项非空设置，保留 LANGUAGE 的偏好顺序。"""
        for key in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            if os.environ.get(key):
                return os.environ[key].split(":")
        return []


class MacLanguage(LanguageStrategy):
    """MacLanguage 使用 macOS 的 AppleLanguages 用户偏好，而非把区域格式误作界面语言。"""

    def preferred(self) -> list[str]:
        """preferred() 只读取指定偏好；命令不可用或格式不足时回退到环境设置，不阻断安装。"""
        try:
            result = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                                    capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                names = re.findall(r'"([A-Za-z][A-Za-z0-9_-]*)"', result.stdout)
                if not names:
                    names = re.findall(r"(?m)^\s*([A-Za-z][A-Za-z0-9_-]*)\s*,?\s*$", result.stdout)
                if names:
                    return names
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass
        return EnvironmentLanguage().preferred()


class WindowsLanguage(LanguageStrategy):
    """WindowsLanguage 使用 Windows 用户的有序显示语言列表，避免从键盘或地区猜测语言。"""

    def preferred(self) -> list[str]:
        """preferred() 按 Win32 两次调用取得缓冲区尺寸及内容；不可调用时仍可使用环境设置。"""
        try:
            read = ctypes.windll.kernel32.GetUserPreferredUILanguages
            read.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                             ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
            read.restype = ctypes.c_bool
            count, size = ctypes.c_ulong(), ctypes.c_ulong()
            # MUI_LANGUAGE_NAME 返回语言标签，避免维护 LCID 数字到语言的另一张对照表。
            language_name = 0x8
            if read(language_name, ctypes.byref(count), None, ctypes.byref(size)) and size.value:
                buffer = ctypes.create_unicode_buffer(size.value)
                if read(language_name, ctypes.byref(count), buffer, ctypes.byref(size)):
                    return [name for name in buffer[:size.value].split("\0") if name]
        except (AttributeError, OSError, ValueError, TypeError, ctypes.ArgumentError):
            pass
        return EnvironmentLanguage().preferred()


def normalize(language: str) -> str:
    """normalize() 接受语言标签和常见 locale 写法，移除编码后规范大小写，拒绝目录路径或控制字符。"""
    value = language.strip().split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    if value.upper() in {"C", "POSIX"}:
        return "en"
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value):
        return ""
    parts = value.split("-")
    return "-".join([parts[0].lower()] + [part.title() if len(part) == 4 and part.isalpha()
                                        else part.upper() if len(part) == 2 and part.isalpha()
                                        else part.lower() for part in parts[1:]])


class JsonTranslations(gettext.NullTranslations):
    """JsonTranslations 将 JSON 语言包适配为 gettext 对象，通过 GoF 职责链处理缺失消息。

    每个实例只保存一种语言的消息，没有每种语言一套类；英文原始消息是链末端，新增语言只需数据。
    """

    def __init__(self, language: str, messages: dict[str, str]):
        super().__init__()
        self.language = language
        self.messages = messages

    def gettext(self, message: str) -> str:
        """gettext() 优先使用本语言完整消息，缺失时委派下一语言；链末端返回英文消息本身。"""
        value = self.messages.get(message)
        if isinstance(value, str):
            return value
        return self._fallback.gettext(message) if self._fallback else message


class Localizer:
    """Localizer 按语言偏好建立很短的地区、基础语言、英文回退链，按需加载所用语言包。

    查找成本取决于本次标签的回退链，不扫描或加载所有语言；安装一万个语言包也不会创建一万个实例。
    """

    def __init__(self, directory: Path = CATALOGS, strategy: LanguageStrategy | None = None,
                 language: str | None = None):
        self.directory = Path(directory)
        self.loaded = []
        try:
            settings = json.loads((self.directory / "aliases.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        aliases, parents = settings.get("aliases", {}), settings.get("fallbacks", {})
        self.aliases = {key.casefold(): value for key, value in aliases.items()
                        if isinstance(key, str) and isinstance(value, str)} if isinstance(aliases, dict) else {}
        self.parents = {key.casefold(): value for key, value in parents.items()
                        if isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value)} if isinstance(parents, dict) else {}
        if strategy is None:
            strategy = MacLanguage() if sys.platform == "darwin" else WindowsLanguage() if sys.platform == "win32" else EnvironmentLanguage()
        explicit = language if language is not None else os.environ.get("CLARITY_LANG")
        preferences = explicit.split(":") if explicit else strategy.preferred()
        self.language = "en"
        self.translations = gettext.NullTranslations()
        for preference in preferences:
            chain = []
            for tag in self.candidates(preference):
                catalog = self.load(tag)
                if catalog is not None:
                    chain.append(catalog)
            if chain:
                self.language = chain[0].language
                for current, fallback in zip(chain, chain[1:]):
                    current.add_fallback(fallback)
                chain[-1].add_fallback(gettext.NullTranslations())
                self.translations = chain[0]
                break

    def candidates(self, language: str):
        """candidates() 从数据别名和父语言产生有限候选；显式脚本回退可避免繁体缺项意外变成简体。"""
        pending, seen = [normalize(language)], set()
        while pending:
            current = pending.pop(0)
            if not current:
                continue
            current = normalize(self.aliases.get(current.casefold(), current))
            if not current or current in seen:
                continue
            seen.add(current)
            yield current
            parents = self.parents.get(current.casefold())
            if parents is not None:
                pending.extend(normalize(parent) for parent in parents)
            elif "-" in current:
                pending.append(current.rsplit("-", 1)[0])

    def load(self, language: str) -> JsonTranslations | None:
        """load() 只打开当前候选的语言包；损坏、缺失或不完整的翻译不能让安装流程停止。"""
        if language == "en":
            return JsonTranslations("en", {})
        try:
            document = json.loads((self.directory / (language + ".json")).read_text(encoding="utf-8"))
            messages = document["messages"]
            if not isinstance(messages, dict):
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self.loaded.append(language)
        return JsonTranslations(language, messages)

    def text(self, message: str, **values) -> str:
        """text() 只格式化调用方指定的界面模板，变量作为原文插入，不再次翻译其中的规则、路径或证据。"""
        template = self.translations.gettext(message)
        try:
            return template.format(**values) if values else template
        except (KeyError, ValueError, IndexError):
            return message.format(**values) if values else message


_active = None
_override = object()


def localizer() -> Localizer:
    """localizer() 每个进程复用已选语言；显式环境覆盖改变时重建，不把界面语言写进审核配置或模型输入。"""
    global _active, _override
    requested = os.environ.get("CLARITY_LANG")
    if _active is None or requested != _override:
        _active, _override = Localizer(), requested
    return _active


def tr(message: str, **values) -> str:
    """tr() 仅作为固定界面消息的入口，不接收规则或材料正文进行自动翻译。"""
    return localizer().text(message, **values)


def main() -> int:
    """main() 为 shell 入口输出一条固定界面消息，参数原样插入，退出码与业务审核结果分离。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    print(tr(args.message, **{f"v{index}": value for index, value in enumerate(args.values)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
