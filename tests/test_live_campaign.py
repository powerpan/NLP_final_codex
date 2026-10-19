import unittest

from scripts.live_collaboration_check import cited_urls


class LiveCampaignAuditTests(unittest.TestCase):
    def test_chinese_adjacent_citations_keep_complete_query_and_separate_sources(self):
        text = "來源：甲（https://example.com/news?id=1）；乙亦報導（https://example.org/page?a=2&b=3）。"
        self.assertEqual(cited_urls(text), {"https://example.com/news?id=1", "https://example.org/page?a=2&b=3"})
        self.assertEqual(cited_urls("[Source](https://example.com/page)."), {"https://example.com/page"})
