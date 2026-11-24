"""test_i18n.py 核对界面语言策略、回退链、材料边界以及大量语言包的惰性读取。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import string
import tempfile
import unittest
from unittest.mock import patch

import i18n


class FixedLanguage(i18n.LanguageStrategy):
    def __init__(self, *languages):
        self.languages = list(languages)

    def preferred(self):
        """preferred() 提供确定的偏好，不依赖测试机器当前使用的语言。"""
        return self.languages


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clarity-languages-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def catalog(self, language, messages):
        """catalog() 只写测试界面数据；材料正文不参与这些消息查找。"""
        (self.directory / (language + ".json")).write_text(
            json.dumps({"language": language, "messages": messages}, ensure_ascii=False), encoding="utf-8")

    def test_unknown_language_or_missing_system_preference_falls_back_to_english(self):
        for preference in ("zz-ZZ", "C.UTF-8", "", "../fr", "fr\nforged"):
            ui = i18n.Localizer(self.directory, language=preference)
            self.assertEqual(ui.language, "en")
            self.assertEqual(ui.text("Installed at: {path}", path="项目/{原文}"), "Installed at: 项目/{原文}")

    def test_region_and_language_chain_fall_back_per_message_without_translating_values(self):
        self.catalog("fr-CA", {"Status": "État (Canada)"})
        self.catalog("fr", {"Done": "Terminé", "Evidence: {text}": "Preuve : {text}"})
        ui = i18n.Localizer(self.directory, language="fr_CA.UTF-8")
        self.assertEqual(ui.language, "fr-CA")
        self.assertEqual(ui.text("Status"), "État (Canada)")
        self.assertEqual(ui.text("Done"), "Terminé")
        self.assertEqual(ui.text("Missing message"), "Missing message")
        evidence = "规则：不能把 FAUX 改成 VRAI。{unchanged}\n// 原注释"
        self.assertIn(evidence, ui.text("Evidence: {text}", text=evidence))
        self.assertEqual(ui.loaded, ["fr-CA", "fr"])

    def test_explicit_override_has_priority_over_system_strategy(self):
        self.catalog("es", {"yes": "sí"})
        with patch.dict(os.environ, {"CLARITY_LANG": "es-MX"}):
            ui = i18n.Localizer(self.directory, strategy=FixedLanguage("de"))
        self.assertEqual(ui.language, "es")
        self.assertEqual(ui.text("yes"), "sí")

    def test_preferred_languages_are_strategies_and_unsupported_preferences_can_be_skipped(self):
        self.catalog("it", {"Ready": "Pronto"})
        with patch.dict(os.environ, {"CLARITY_LANG": ""}):
            ui = i18n.Localizer(self.directory, strategy=FixedLanguage("zz-ZZ", "it-IT"))
        self.assertEqual(ui.language, "it")
        self.assertEqual(ui.text("Ready"), "Pronto")

    def test_macos_user_interface_preference_is_read_separately_from_environment_locale(self):
        result = type("Result", (), {"returncode": 0, "stdout": '(\n "zh-Hant-TW",\n "en-US"\n)'})()
        with patch.object(i18n.subprocess, "run", return_value=result) as read:
            self.assertEqual(i18n.MacLanguage().preferred(), ["zh-Hant-TW", "en-US"])
        self.assertEqual(read.call_args.args[0], ["/usr/bin/defaults", "read", "-g", "AppleLanguages"])

    def test_windows_display_languages_use_the_win32_ordered_buffer(self):
        class Reader:
            def __call__(self, flags, count, buffer, size):
                self.assert_flags = flags
                value = "ja-JP\0en-US\0\0"
                size._obj.value = len(value)
                count._obj.value = 2
                if buffer is not None:
                    buffer.value = value
                return True

        read = Reader()
        library = type("Library", (), {"GetUserPreferredUILanguages": read})()
        windll = type("WinDLL", (), {"kernel32": library})()
        with patch.object(i18n.ctypes, "windll", windll, create=True):
            self.assertEqual(i18n.WindowsLanguage().preferred(), ["ja-JP", "en-US"])
        self.assertEqual(read.assert_flags, 8)

    def test_damaged_catalog_or_bad_format_falls_back_without_stopping_the_program(self):
        (self.directory / "de.json").write_text("not JSON")
        ui = i18n.Localizer(self.directory, language="de")
        self.assertEqual(ui.text("Ready"), "Ready")
        self.catalog("de", {"Path: {path}": "Pfad: {missing}"})
        self.assertEqual(i18n.Localizer(self.directory, language="de").text("Path: {path}", path="abc"), "Path: abc")
        (self.directory / "aliases.json").write_text('["invalid registry"]')
        self.assertEqual(i18n.Localizer(self.directory, language="de").language, "de")

    def test_ten_thousand_catalogs_do_not_require_registration_or_loading_all_messages(self):
        for number in range(10000):
            self.catalog(f"qaa-x-l{number:04d}", {"Ready": f"language-{number}"})
        with patch.object(Path, "glob", side_effect=AssertionError("UI loading must not scan every language")):
            ui = i18n.Localizer(self.directory, language="qaa-x-l9999")
        self.assertEqual(ui.text("Ready"), "language-9999")
        self.assertEqual(ui.loaded, ["qaa-x-l9999"])

    def test_shipped_catalogs_preserve_all_message_placeholders(self):
        reference = json.loads((i18n.CATALOGS / "zh-Hans.json").read_text())["messages"]
        def fields(text):
            return sorted((name, spec, conversion) for _, name, spec, conversion in string.Formatter().parse(text) if name is not None)
        for language in ("zh-Hans", "zh-Hant", "es", "fr", "de", "it", "ja"):
            document = json.loads((i18n.CATALOGS / (language + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(set(document["messages"]), set(reference), language)
            for original, translated in document["messages"].items():
                self.assertEqual(fields(original), fields(translated), (language, original))

    def test_chinese_region_aliases_preserve_script_and_english_fallback(self):
        for language, expected in (("zh_CN.UTF-8", "zh-Hans"), ("zh_TW", "zh-Hant"),
                                   ("zh-HK", "zh-Hant"), ("zh-Hans-US", "zh-Hans")):
            self.assertEqual(i18n.Localizer(language=language).language, expected)
        self.assertEqual(i18n.Localizer(language="en_US.UTF-8").language, "en")

    def test_english_report_preserves_chinese_rule_quotes_and_model_text(self):
        import review

        quotation = "必须保留业务边界。{原文不格式化}"
        finding = {"category": "boundary", "title": "模型原始问题", "origin": "uncertain",
                   "path": "业务.py", "line": 3, "question": "原始提问？", "rule_path": "规则.md",
                   "rule_line": 2, "rule_quote": quotation, "reason": "原始理由",
                   "counterevidence": "原始反证"}
        report = {"status": "issues", "summary": "模型原始总结", "snapshot_id": "frozen-snapshot",
                  "findings": [finding], "current_category": "boundary"}
        with patch.dict(os.environ, {"CLARITY_LANG": "en"}), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as output:
            review.render(report, self.directory)
        rendered = (self.directory / "report.md").read_text()
        self.assertIn("# BLOCK: code-review findings need a response", rendered)
        self.assertIn("  Rule: `规则.md:2` — " + quotation, rendered)
        for text in ("模型原始总结", "模型原始问题", "原始提问？", "原始理由", "原始反证"):
            self.assertIn(text, rendered)
        self.assertEqual(output.getvalue().strip(), "CLARITY_REPORT_DIR=" + str(self.directory))

    def test_interface_language_does_not_change_model_prompt_or_rule_inputs(self):
        import review

        texts = {"README.md": "中文引擎关系", "principles.md": "中文编写原则", "workflow.md": "中文审核流程",
                 "prompts/review.md": "中文审核职责"}
        packet = {"adopted_rules": {"规则.md": "1: 中文正式规则"}, "current_files": {"业务.py": "1: value = 1"}}
        prompts = []
        for language in ("en", "zh-Hans", "zh-Hant", "es", "fr", "de", "it", "ja"):
            with patch.dict(os.environ, {"CLARITY_LANG": language}):
                prompts.append(review.make_prompt("review", packet, "原始阶段指令", texts=texts))
        self.assertEqual(len(set(prompts)), 1)
        self.assertIn("中文正式规则", prompts[0])
        self.assertIn("原始阶段指令", prompts[0])


if __name__ == "__main__":
    unittest.main()
