import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from paperBotV2.arxiv_daily.arxiv_feishu_msg import send_papers_to_feishu
from paperBotV2.arxiv_daily.daily_push import (
    relevance_score as arxiv_relevance_score,
    select_papers as select_arxiv_papers,
)
from paperBotV2.conf_summary.daily_push import (
    build_markdown as build_conf_markdown,
    conference_candidates,
    load_results,
    select_papers,
)
from paperBotV2.feishu import send_card
from paperBotV2.industry_practice.daily_push import load_articles, select_articles


class IndustryPushTests(unittest.TestCase):
    def test_loads_old_chinese_and_new_english_schema(self):
        payload = [
            {"公司": "甲", "内容": "中文格式", "链接": "https://a.example", "标签": "推荐,搜索", "时间": "2025-01-01"},
            {"company": "乙", "title": "English schema", "link": "https://b.example", "tags": ["广告"], "date": "2025-01-02"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "articles.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            articles = load_articles(path)

        self.assertEqual(articles[0]["title"], "中文格式")
        self.assertEqual(articles[0]["tags"], ["推荐", "搜索"])
        self.assertEqual(articles[1]["company"], "乙")

    def test_daily_rotation_is_deterministic_and_filterable(self):
        articles = [
            {"title": f"A{i}", "link": f"https://example.com/{i}", "tags": ["推荐"]}
            for i in range(8)
        ] + [{"title": "Search", "link": "https://example.com/search", "tags": ["搜索"]}]
        first = select_articles(articles, date(2026, 8, 5), 3, {"推荐"})
        second = select_articles(articles, date(2026, 8, 5), 3, {"推荐"})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertTrue(all("推荐" in item["tags"] for item in first))


class ConferencePushTests(unittest.TestCase):
    def test_lfs_pointer_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "lfs: true"):
                load_results(path)

    def test_selects_relevant_recent_conference_papers(self):
        results = {
            "kdd2025": [
                {"paper_name": "Large-Scale Recommendation Ranking", "paper_url": "https://example.com/a"},
                {"paper_name": "Unrelated Geometry", "paper_url": "https://example.com/b"},
            ],
            "acl2025": [{"paper_name": "Search and Retrieval", "paper_url": "https://example.com/c"}],
            "sigir2020": [{"paper_name": "Old Search", "paper_url": "https://example.com/d"}],
        }
        candidates = conference_candidates(results, {"kdd", "sigir"}, 2021)
        selected = select_papers(candidates, date(2026, 8, 5), 3)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(selected[0]["conference"], "KDD")
        self.assertIn("KDD 2025", build_conf_markdown(selected))


class ArxivPushTests(unittest.TestCase):
    def test_prefers_recent_relevant_papers_and_deduplicates(self):
        now = datetime(2026, 8, 5, tzinfo=timezone.utc)
        papers = [
            {
                "id": "relevant",
                "title": "Neural Retrieval and Recommendation Ranking",
                "summary": "Personalized search systems",
                "url": "https://arxiv.org/abs/1",
                "published": now - timedelta(hours=3),
                "categories": ["cs.IR"],
            },
            {
                "id": "generic",
                "title": "A Generic Learning Method",
                "summary": "Learning",
                "url": "https://arxiv.org/abs/2",
                "published": now - timedelta(hours=1),
                "categories": ["cs.LG"],
            },
            {
                "id": "old",
                "title": "Old Recommendation Paper",
                "summary": "Recommendation",
                "url": "https://arxiv.org/abs/3",
                "published": now - timedelta(days=30),
                "categories": ["cs.IR"],
            },
        ]
        papers.append(dict(papers[0]))

        selected = select_arxiv_papers(papers, now, limit=5, lookback_days=7)

        self.assertEqual([paper["id"] for paper in selected], ["relevant", "generic"])
        self.assertGreater(arxiv_relevance_score(selected[0]), arxiv_relevance_score(selected[1]))


class FeishuClientTests(unittest.TestCase):
    @patch("paperBotV2.feishu.requests.post")
    def test_sends_generic_card_and_checks_response(self, post):
        response = Mock(ok=True)
        response.json.return_value = {"StatusCode": 0}
        post.return_value = response

        sent = send_card("Title", "Body", ["https://example.com/hook"])

        self.assertEqual(sent, 1)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["msg_type"], "interactive")
        self.assertIn("Title", body["card"])

    def test_dry_run_does_not_send_network_request(self):
        with patch("paperBotV2.feishu.requests.post") as post:
            self.assertEqual(send_card("Title", "Body", ["https://example.com/hook"], dry_run=True), 0)
            post.assert_not_called()

    @patch("paperBotV2.feishu.requests.post")
    def test_arxiv_sender_no_longer_requires_private_card_template(self, post):
        response = Mock(ok=True)
        response.json.return_value = {"code": 0}
        post.return_value = response
        papers = [{
            "title": "A useful retrieval paper",
            "url": "https://arxiv.org/abs/0000.00000",
            "translation": "一篇有用的检索论文",
            "summary": "Summary",
            "rerank_relevance_score": 5,
        }]

        send_papers_to_feishu(papers, ["https://example.com/hook"])

        card = post.call_args.kwargs["json"]["card"]
        self.assertIn("arXiv 论文日推", card)
        self.assertNotIn("template_id", card)


if __name__ == "__main__":
    unittest.main()
