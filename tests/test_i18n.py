"""界面多语言：检测、覆盖与中英文案切换。"""

from __future__ import annotations

import unittest

from pickup import i18n
from pickup.i18n import t


class I18nDetectTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_lang("en")

    def test_default_is_english(self) -> None:
        self.assertEqual(i18n.detect_lang({}), "en")
        self.assertEqual(i18n.detect_lang({"LANG": "C"}), "en")
        self.assertEqual(i18n.detect_lang({"LANG": "en_US.UTF-8"}), "en")

    def test_chinese_locale_variants(self) -> None:
        for env in (
            {"LANG": "zh_CN.UTF-8"},
            {"LC_ALL": "zh_TW.UTF-8"},
            {"LC_MESSAGES": "zh-Hans"},
            {"LANGUAGE": "zh_CN:en_US:en"},
            {"LANG": "zh"},
        ):
            with self.subTest(env=env):
                self.assertEqual(i18n.detect_lang(env), "zh")

    def test_pickup_lang_overrides_system(self) -> None:
        self.assertEqual(
            i18n.detect_lang({"PICKUP_LANG": "en", "LANG": "zh_CN.UTF-8"}),
            "en",
        )
        self.assertEqual(
            i18n.detect_lang({"PICKUP_LANG": "zh", "LANG": "en_US.UTF-8"}),
            "zh",
        )


class I18nCatalogTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_lang("en")

    def test_english_and_chinese_strings(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(t("action.advanced"), "Advanced")
        self.assertEqual(t("list.new_session"), "+ New session")
        self.assertEqual(t("time.minutes_ago", n=2), "2m ago")

        i18n.set_lang("zh")
        self.assertEqual(t("action.advanced"), "高级操作")
        self.assertEqual(t("list.new_session"), "＋ 新建会话")
        self.assertEqual(t("time.minutes_ago", n=2), "2分钟前")

    def test_join_names_uses_locale_separator(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(i18n.join_names(["Claude", "Codex"]), "Claude, Codex")
        i18n.set_lang("zh")
        self.assertEqual(i18n.join_names(["Claude", "Codex"]), "Claude、Codex")

    def test_all_keys_have_both_languages(self) -> None:
        for key, catalog in i18n._MESSAGES.items():
            with self.subTest(key=key):
                self.assertIn("en", catalog)
                self.assertIn("zh", catalog)
                self.assertTrue(catalog["en"].strip())
                self.assertTrue(catalog["zh"].strip())


if __name__ == "__main__":
    unittest.main()
