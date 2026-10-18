"""标题指纹与同作品判定测试（全部使用虚构样本数据）。"""
import unittest

from app import titles

# 同一作品的 4 个语言版本
SAMPLE_EN = '[SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 (Sample Series A) [English] [TLsGroup] [Digital]'
SAMPLE_CN = '[SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 (Sample Series A) [Chinese] [汉化组1] [Digital]'
SAMPLE_CN2 = ('[SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 | 示例中文副标题 Vol.3 '
              '(Sample Series A) [Chinese] [汉化组2] [Digital]')
SAMPLE_JP = '[SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 (Sample Series A) [Digital]'
# 同系列不同卷：绝不能合并
SAMPLE_VOL2 = '[SampleCircle (Various)] Sample Anthology "Alpha" Vol.2 (Sample Series A) [Chinese] [汉化组3] [Digital]'
# 同社团的另一个作品
OTHER_WORK = '[SampleCircle (Various)] Sample Reverse Anthology "Beta" (Sample Series A) [English] [TLsGroup] [Digital]'

# 原始标题（详情 API 的 title.japanese）：去掉 [英訳]/[中国翻訳]/[DL版] 后应完全一致
ORIGINAL_JP = '[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.3 (サンプルシリーズA) [DL版]'
ORIGINAL_EN = '[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.3 (サンプルシリーズA) [英訳] [DL版]'
ORIGINAL_CN = '[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.3 (サンプルシリーズA) [中国翻訳] [DL版]'


class TitleMetaTests(unittest.TestCase):
    def test_language_brackets_are_noise(self):
        meta = titles.title_meta(SAMPLE_EN)
        for text in ("english", "tlgroup", "digital"):
            self.assertNotIn(text, meta["core"])
        self.assertIn("sampleanthology", meta["core"])
        self.assertIn("vol3", meta["core"])

    def test_volume_signature(self):
        self.assertEqual(titles.title_meta(SAMPLE_EN)["volumes"], frozenset({"3"}))
        self.assertEqual(titles.title_meta(SAMPLE_VOL2)["volumes"], frozenset({"2"}))

    def test_original_key_identical_across_languages(self):
        keys = {titles.title_meta(value)["core"] for value in (ORIGINAL_JP, ORIGINAL_EN, ORIGINAL_CN)}
        self.assertEqual(len(keys), 1, "原始标题的核心 key 应当一致")

    def test_original_key_distinguishes_volumes(self):
        vol2 = ORIGINAL_JP.replace("Vol.3", "Vol.2")
        self.assertNotEqual(
            titles.title_meta(ORIGINAL_JP)["core"],
            titles.title_meta(vol2)["core"],
        )


class SimilarityTests(unittest.TestCase):
    def pair(self, left, right):
        return titles.similar_meta(titles.title_meta(left), titles.title_meta(right))

    def test_language_variants_merge(self):
        for other in (SAMPLE_CN, SAMPLE_JP, SAMPLE_CN2):
            self.assertTrue(self.pair(SAMPLE_EN, other), f"应合并：{other[:60]}")

    def test_different_volume_is_not_merged(self):
        self.assertFalse(self.pair(SAMPLE_EN, SAMPLE_VOL2))
        self.assertFalse(self.pair(SAMPLE_JP, SAMPLE_VOL2))

    def test_different_work_is_not_merged(self):
        self.assertFalse(self.pair(SAMPLE_EN, OTHER_WORK))

    def test_short_titles_are_not_fuzzy_merged(self):
        self.assertFalse(self.pair("[Group] A", "[Group] B"))


if __name__ == "__main__":
    unittest.main()