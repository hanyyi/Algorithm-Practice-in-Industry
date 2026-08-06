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
    semantic_scholar_id,
)
from paperBotV2.conf_summary.daily_push import (
    build_markdown as build_conf_markdown,
    conference_candidates,
    load_results,
    online_conference_candidates,
    select_papers,
    select_yearly_papers,
)
from paperBotV2.feishu import send_card
from paperBotV2.github_models import enrich_chinese
from paperBotV2.industry_practice.daily_push import load_articles, select_articles
from paperBotV2.metrics import (
    canonical_url,
    fetch_hn_metrics,
    fetch_s2_metrics,
    search_s2_conference_papers,
)


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

    def test_selects_weekly_articles_by_public_metrics(self):
        articles = [
            {
                "title": f"A{i}",
                "link": f"https://example.com/{i}",
                "tags": ["推荐"],
                "date": f"2026-08-{i + 2:02d}",
                "hn_points": i,
                "hn_comments": i * 2,
            }
            for i in range(7)
        ] + [
            {
                "title": "Future",
                "link": "https://example.com/future",
                "tags": ["推荐"],
                "date": "2026-08-09",
                "hn_points": 999,
            },
            {
                "title": "Old",
                "link": "https://example.com/old",
                "tags": ["推荐"],
                "date": "2026-08-01",
                "hn_points": 999,
            },
        ]
        first = select_articles(articles, date(2026, 8, 8), 3, {"推荐"}, 7)
        second = select_articles(articles, date(2026, 8, 8), 3, {"推荐"}, 7)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertTrue(all("推荐" in item["tags"] for item in first))
        self.assertEqual([item["hn_points"] for item in first], [6, 5, 4])
        self.assertTrue(all("2026-08-02" <= item["date"] <= "2026-08-08" for item in first))


class ConferencePushTests(unittest.TestCase):
    def test_lfs_pointer_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "lfs: true"):
                load_results(path)

    def test_selects_relevant_recent_conference_papers(self):
        results = {
            "kdd2026": [
                {
                    "paper_name": "Large-Scale Recommendation Ranking",
                    "paper_url": "https://example.com/a",
                    "publication_date": "2026-08-02",
                    "citation_count": 3,
                },
                {
                    "paper_name": "Old Recommendation Ranking",
                    "paper_url": "https://example.com/old",
                    "publication_date": "2026-07-01",
                    "citation_count": 100,
                },
                {"paper_name": "Unrelated Geometry", "paper_url": "https://example.com/b"},
            ],
            "acl2026": [{"paper_name": "Search and Retrieval", "paper_url": "https://example.com/c"}],
            "sigir2020": [{"paper_name": "Old Search", "paper_url": "https://example.com/d"}],
        }
        candidates = conference_candidates(results, {"kdd", "sigir"}, 2021)
        selected = select_papers(candidates, date(2026, 8, 5), 3)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["conference"], "KDD")
        self.assertIn("KDD 2026", build_conf_markdown(selected))

    def test_fills_from_current_year_only_by_quality(self):
        papers = online_conference_candidates([
            {
                "title": "Recommendation Retrieval at Scale",
                "year": 2026,
                "venue": "KDD",
                "url": "https://example.com/top",
                "citationCount": 8,
                "influentialCitationCount": 2,
                "authors": [{"name": "A"}],
            },
            {
                "title": "Search Ranking System",
                "year": 2026,
                "venue": "SIGIR",
                "url": "https://example.com/second",
                "citationCount": 2,
            },
            {
                "title": "Old Recommendation System",
                "year": 2025,
                "venue": "RecSys",
                "url": "https://example.com/old",
                "citationCount": 100,
            },
        ])

        selected = select_yearly_papers(papers, 2026, 2)

        self.assertEqual([paper["year"] for paper in selected], [2026, 2026])
        self.assertEqual(selected[0]["citation_count"], 8)


class ArxivPushTests(unittest.TestCase):
    def test_builds_clean_semantic_scholar_id(self):
        paper = {
            "url": "https://arxiv.org/abs/2608.04807v1",
            "id": "http://arxiv.org/abs/2608.04807v1",
        }
        self.assertEqual(semantic_scholar_id(paper), "ARXIV:2608.04807")

    def test_requires_weekly_dates_and_prioritizes_public_metrics(self):
        now = datetime(2026, 8, 5, tzinfo=timezone.utc)
        papers = [
            {
                "id": "relevant",
                "title": "Neural Retrieval and Recommendation Ranking",
                "summary": "Personalized search systems",
                "url": "https://arxiv.org/abs/1",
                "published": now - timedelta(hours=3),
                "categories": ["cs.IR"],
                "citation_count": 0,
            },
            {
                "id": "cited",
                "title": "A Cited Learning Method",
                "summary": "Learning method",
                "url": "https://arxiv.org/abs/2",
                "published": now - timedelta(days=2),
                "categories": ["cs.LG"],
                "citation_count": 2,
            },
            {
                "id": "old",
                "title": "Old Recommendation Paper",
                "summary": "Recommendation",
                "url": "https://arxiv.org/abs/3",
                "published": now - timedelta(days=30),
                "categories": ["cs.IR"],
                "citation_count": 100,
            },
            {
                "id": "future",
                "title": "Future Recommendation Paper",
                "summary": "Recommendation",
                "url": "https://arxiv.org/abs/4",
                "published": now + timedelta(hours=1),
                "categories": ["cs.IR"],
                "citation_count": 100,
            },
            {
                "id": "undated",
                "title": "Undated Recommendation Paper",
                "summary": "Recommendation",
                "url": "https://arxiv.org/abs/5",
                "published": None,
                "categories": ["cs.IR"],
                "citation_count": 100,
            },
        ]
        papers.append(dict(papers[0]))

        selected = select_arxiv_papers(papers, now, limit=5, lookback_days=7)

        self.assertEqual([paper["id"] for paper in selected], ["cited", "relevant"])
        self.assertGreater(arxiv_relevance_score(selected[1]), arxiv_relevance_score(selected[0]))


class PublicMetricsTests(unittest.TestCase):
    def test_canonical_url_removes_tracking(self):
        self.assertEqual(
            canonical_url("https://www.example.com/post/?utm_source=rss&x=1#section"),
            "https://example.com/post?x=1",
        )
        self.assertEqual(canonical_url(""), "")

    @patch("paperBotV2.metrics.requests.request")
    def test_fetches_hacker_news_metrics(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {
            "hits": [{
                "url": "https://example.com/post?utm_source=hn",
                "points": 42,
                "num_comments": 7,
                "objectID": "123",
            }]
        }
        request.return_value = response

        metrics = fetch_hn_metrics(datetime(2026, 8, 1, tzinfo=timezone.utc))

        self.assertEqual(metrics["https://example.com/post"]["hn_points"], 42)

    @patch("paperBotV2.metrics.requests.request")
    def test_fetches_semantic_scholar_citations(self, request):
        response = Mock(status_code=200)
        response.json.return_value = [{
            "citationCount": 5,
            "influentialCitationCount": 2,
            "publicationDate": "2026-08-02",
        }]
        request.return_value = response

        metrics = fetch_s2_metrics(["ARXIV:2608.00001"])

        self.assertEqual(metrics["ARXIV:2608.00001"]["citation_count"], 5)
        self.assertEqual(
            metrics["ARXIV:2608.00001"]["influential_citation_count"], 2
        )

    @patch("paperBotV2.metrics.requests.request")
    def test_semantic_scholar_splits_a_rejected_batch(self, request):
        rejected = Mock(status_code=400, text="invalid paper id")
        first = Mock(status_code=200)
        first.json.return_value = [{"citationCount": 1}]
        second = Mock(status_code=200)
        second.json.return_value = [{"citationCount": 2}]
        request.side_effect = [rejected, first, second]

        metrics = fetch_s2_metrics(["ARXIV:2608.00001", "ARXIV:2608.00002"])

        self.assertEqual(metrics["ARXIV:2608.00001"]["citation_count"], 1)
        self.assertEqual(metrics["ARXIV:2608.00002"]["citation_count"], 2)

    @patch("paperBotV2.metrics.requests.request")
    def test_searches_current_year_conference_metrics(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [{"paperId": "p1", "title": "Recommendation Retrieval"}]
        }
        request.return_value = response

        papers = search_s2_conference_papers(year=2026, venues=["KDD", "SIGIR"])

        self.assertEqual(papers[0]["paperId"], "p1")
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["publicationDateOrYear"], "2026")
        self.assertEqual(params["venue"], "KDD,SIGIR")


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


class ChineseEnrichmentTests(unittest.TestCase):
    @patch("paperBotV2.github_models.requests.get")
    def test_translates_title_and_summary_to_chinese(self, get):
        title_response = Mock()
        title_response.json.return_value = [[['中文论文标题', 'English title']]]
        summary_response = Mock()
        summary_response.json.return_value = [[['中文论文摘要。', 'English abstract']]]
        get.side_effect = [title_response, summary_response]

        result = enrich_chinese(
            [{"id": "paper-1", "title": "English title", "summary": "English abstract"}]
        )

        self.assertEqual(result["paper-1"]["title_zh"], "中文论文标题")
        self.assertEqual(result["paper-1"]["summary_zh"], "中文论文摘要。")
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
