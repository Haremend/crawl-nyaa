"""标题聚类回归测试：用虚构数据集验证「同作品合并 / 不同作品不合并」。

数据集设计：25 条条目应当聚成 12 部作品
（6 组多版本作品 + 6 个单版本作品），与真实抓取场景的结构一致。
"""
import unittest

from app import titles

# (标题, 期望所属作品编号)
DATASET = [
    # 作品 1：Vol.3 的 4 个语言版本
    ('910004 [SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 (Sample Series A) [English] [TLsGroup] [Digital]', 1),
    ('910002 [SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 | 示例中文副标题 Vol.3 (Sample Series A) [Chinese] [汉化组2] [Digital]', 1),
    ('910001 [SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 (Sample Series A) [Digital]', 1),
    ('910005 [SampleCircle (Various)] Sample Anthology "Alpha" Vol.3 (Sample Series A) [Chinese] [汉化组1] [Digital]', 1),
    # 作品 2：Vol.2 的 3 个语言版本（与作品 1 名似但是不同卷）
    ('910022 [SampleCircle (Various)] Sample Anthology "Alpha" Vol.2 (Sample Series A) [Chinese] [汉化组3] [Digital]', 2),
    ('910023 [SampleCircle (Various)] Sample Anthology "Alpha" Vol.2 | Sample Series A Side Story "Alpha" Vol.2 (Sample Series A) [English] [Digital]', 2),
    ('910026 [SampleCircle (Various)] Sample Anthology "Alpha" Vol.2 (Sample Series A) [Chinese] [汉化组4] [Digital]', 2),
    # 作品 3：Reverse Anthology 的 3 个版本（含展会编号前缀）
    ('910017 [SampleCircle (Various)] Sample Reverse Anthology "Beta" (Sample Series A) [English] [TLsGroup] [Digital]', 3),
    ('910018 (C107) [SampleCircle (Various)] Sample Reverse Anthology "Beta" (Sample Series A) [Chinese] [汉化组3]', 3),
    ('910020 [SampleCircle (Various)] Sample Reverse Anthology "Beta" (Sample Series A) [Digital]', 3),
    # 作品 4：Archive Anthology 的 4 个版本（含同语言重复投稿）
    ('910013 [SampleCircle (Various)] Sample Archive Anthology "Gamma" (Sample Series B) [Chinese] [汉化组3] [Digital]', 4),
    ('910014 [SampleCircle (Various)] Sample Archive Anthology "Gamma" (Sample Series B) [Digital][汉化组3]', 4),
    ('910015 [SampleCircle (Various)] Sample Archive Anthology "Gamma" (Sample Series B) [Digital][汉化组3]', 4),
    ('910019 [SampleCircle (Various)] Sample Archive Anthology "Gamma" (Sample Series B) [Digital]', 4),
    # 作品 5：Heroine Anthology 的 3 个版本（英文版带副标题）
    ('910009 [SampleCircle (Various)] Sample Heroine Anthology "Delta" | Side Story Anthology "That Outfit"  (Sample Series B) [Digital] [English] [Extra]', 5),
    ('910010 [SampleCircle (Various)] Sample Heroine Anthology "Delta" (Sample Series B) [Chinese] [汉化组1] [Digital]', 5),
    ('910012 [SampleCircle (Various)] Sample Heroine Anthology "Delta" (Sample Series B) [Digital]', 5),
    # 作品 6：Collab 的 2 个版本
    ('910016 [SampleCircle (Various)] Sample Collab ~Pure Love Singularity~ (Sample Series C) [English] [Digital] [Incomplete]', 6),
    ('910025 [SampleCircle (Various)] Sample Collab ~Pure Love Singularity~ (Sample Series C) [Chinese] [汉化组3] [Digital]', 6),
    # 6 个单版本作品
    ('910006 [SampleCircle (Various)] Sample Anthology "SAMPLE" (Sample Series D) [Digital]', 7),
    ('910007 [SampleAuthor] サンプル映像は今後100年間非公開とされる [書換え版] [SampleEditor]', 8),
    ('910008 [SampleCircle (SampleArtist)] Sample Heroine (Sample Archive -Sample Anthology-) (Sample Series E) [English] [MTLs] [Digital]', 9),
    ('910011 [SampleCircle (Various)] Sample Fair Anthology "Zuri" (Sample Series B) [Chinese] [汉化组4] [Digital]', 10),
    ('910024 [SampleCircle (Various)] Sample Festival Anthology "Omega" (Sample Series B) [English] [Digital]', 11),
    ('910003 [SampleCircle (Various)] Sample School Anthology "Mode" (Sample Series E) [English] [Digital]', 12),
]

# 详情 API 原始标题（title.japanese）分组：同组内 key 必须一致
ORIGINAL_GROUPS = [
    ['[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.3 (サンプルシリーズA) [DL版]',
     '[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.3 (サンプルシリーズA) [英訳] [DL版]',
     '[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.3 (サンプルシリーズA) [中国翻訳] [DL版]'],
    ['[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.2 (サンプルシリーズA) [DL版]',
     '[サンプルサークル (よろず)] サンプル合同「アルファ」Vol.2 (サンプルシリーズA) [中国翻訳] [DL版]'],
    ['[サンプルサークル (よろず)] サンプル逆合同「ベータ」 (サンプルシリーズA) [DL版]',
     '[サンプルサークル (よろず)] サンプル逆合同「ベータ」 (サンプルシリーズA) [英訳] [DL版]'],
]


def strip_id(raw: str) -> str:
    return raw.split(" ", 1)[1] if raw[:1].isdigit() else raw


class DatasetClusteringTests(unittest.TestCase):
    """列表标题指纹：应当把 25 条聚成 12 组，且分组结果与设计一致。"""

    def _clusters(self):
        clusters = []
        index = {}
        for raw, _expected in DATASET:
            title = strip_id(raw)
            meta = titles.title_meta(title)
            key = meta["core"] or title
            if key in index:
                clusters[index[key]]["rows"].append(raw)
                continue
            index[key] = len(clusters)
            clusters.append({"key": key, "rows": [raw], **meta})

        parent = list(range(len(clusters)))

        def find(item):
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if find(i) != find(j) and titles.similar_meta(clusters[i], clusters[j]):
                    parent[find(j)] = find(i)

        grouped = {}
        for index_, cluster in enumerate(clusters):
            grouped.setdefault(find(index_), []).extend(cluster["rows"])
        return list(grouped.values())

    def test_cluster_count(self):
        self.assertEqual(len(self._clusters()), 12)

    def test_cluster_membership_matches_design(self):
        by_id = {}
        for raw, expected in DATASET:
            by_id[raw.split(" ", 1)[0]] = expected
        for cluster in self._clusters():
            works = {by_id[row.split(" ", 1)[0]] for row in cluster}
            self.assertEqual(len(works), 1, f"同一簇里混入了不同作品：{cluster}")
        covered = set()
        for cluster in self._clusters():
            for row in cluster:
                covered.add(by_id[row.split(" ", 1)[0]])
        self.assertEqual(len(covered), 12)

    def test_vol2_and_vol3_not_merged(self):
        vol2 = titles.title_meta(strip_id(next(r for r in DATASET if "Vol.2" in r[0])[0]))
        vol3 = titles.title_meta(strip_id(next(r for r in DATASET if "Vol.3" in r[0])[0]))
        self.assertFalse(titles.similar_meta(vol2, vol3))


class OriginalKeyTests(unittest.TestCase):
    def test_group_keys_are_identical(self):
        for group in ORIGINAL_GROUPS:
            keys = {titles.title_meta(value)["core"] for value in group}
            self.assertEqual(len(keys), 1, f"同组原始标题 key 不一致：{group}")

    def test_groups_are_mutually_distinct(self):
        keys = [next(iter({titles.title_meta(v)["core"] for v in group})) for group in ORIGINAL_GROUPS]
        self.assertEqual(len(set(keys)), len(keys))


if __name__ == "__main__":
    unittest.main()