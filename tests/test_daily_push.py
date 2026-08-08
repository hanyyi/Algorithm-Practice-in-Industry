import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from paperBotV2.arxiv_daily.arxiv_feishu_msg import send_papers_to_feishu
from paperBotV2.arxiv_daily.daily_push import (
    _result_pages,
    build_markdown as build_arxiv_markdown,
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
from paperBotV2.industry_practice.daily_push import (
    build_markdown as build_industry_markdown,
    load_articles,
    select_articles,
)
from paperBotV2.llm_enrichment import generate_chinese_summaries
from paperBotV2.metrics import (
    canonical_url,
    fetch_hn_metrics,
    fetch_s2_metrics,
    search_s2_conference_papers,
)
from paperBotV2.relevance import is_recommendation_relevant


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
                "title": f"Recommendation System Ranking A{i}",
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

    def test_rejects_generic_engineering_and_outputs_english(self):
        articles = [
            {
                "title": "Turn one giant AI pull request into a reviewable stack",
                "summary": "A software engineering workflow for coding agents.",
                "link": "https://example.com/code",
                "tags": ["Engineering"],
                "date": "2026-08-04",
                "hn_points": 100,
            },
            {
                "title": "GenRec: LLM-Native Recommendation at Scale",
                "summary": "A recommender system for personalized content.",
                "title_zh": "不应显示的中文标题",
                "summary_zh": "面向个性化内容的推荐系统。",
                "link": "https://example.com/rec",
                "tags": ["Recommendation"],
                "date": "2026-08-04",
            },
        ]

        selected = select_articles(articles, date(2026, 8, 6), 5, lookback_days=7)

        self.assertEqual([item["link"] for item in selected], ["https://example.com/rec"])
        markdown = build_industry_markdown(selected)
        self.assertIn("GenRec: LLM-Native Recommendation at Scale", markdown)
        self.assertIn("中文解读：面向个性化内容的推荐系统。", markdown)
        self.assertNotIn("不应显示的中文标题", markdown)


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
                "title": "Personalized Search Ranking for Users and Items",
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

    def test_rejects_generic_retrieval_and_uses_english(self):
        papers = online_conference_candidates([
            {
                "title": "VideoRAG: Retrieval-Augmented Generation for Long Videos",
                "abstract": "A generic retrieval augmented generation system.",
                "year": 2026,
                "venue": "KDD",
                "url": "https://example.com/rag",
                "citationCount": 100,
            },
            {
                "title": "OneTrans for Industrial Recommender Systems",
                "abstract": "A recommendation system for personalized item ranking.",
                "year": 2026,
                "venue": "KDD",
                "url": "https://example.com/rec",
                "citationCount": 2,
            },
        ])
        papers[0]["title_zh"] = "不应显示的中文标题"
        papers[0]["summary_zh"] = "面向工业推荐系统的统一建模方法。"

        self.assertEqual([paper["paper_url"] for paper in papers], ["https://example.com/rec"])
        markdown = build_conf_markdown(papers)
        self.assertIn("OneTrans for Industrial Recommender Systems", markdown)
        self.assertIn("中文解读：面向工业推荐系统的统一建模方法。", markdown)
        self.assertNotIn("不应显示的中文标题", markdown)


class ArxivPushTests(unittest.TestCase):
    def test_arxiv_candidate_pool_is_split_into_api_safe_pages(self):
        self.assertEqual(_result_pages(300), [(0, 100), (100, 100), (200, 100)])
        self.assertEqual(_result_pages(250), [(0, 100), (100, 100), (200, 50)])

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

        self.assertEqual([paper["id"] for paper in selected], ["relevant"])
        self.assertGreater(arxiv_relevance_score(selected[0]), 0)

    def test_rejects_generic_llm_agent_papers_and_uses_english(self):
        now = datetime(2026, 8, 5, tzinfo=timezone.utc)
        papers = [
            {
                "id": "agent",
                "title": "EvolveNet: Agent Self-Improvement",
                "summary": "An LLM agent harness with retrieval augmented generation.",
                "url": "https://arxiv.org/abs/1",
                "published": now,
                "categories": ["cs.CL"],
                "citation_count": 100,
            },
            {
                "id": "rec",
                "title": "Dual Exploration for Generative Re-Ranking",
                "summary": "An industrial recommendation system for personalized item ranking.",
                "title_zh": "不应显示的中文标题",
                "summary_zh": "面向个性化物品排序的工业推荐系统。",
                "url": "https://arxiv.org/abs/2",
                "published": now,
                "categories": ["cs.IR"],
            },
        ]

        selected = select_arxiv_papers(papers, now, limit=10, lookback_days=7)

        self.assertEqual([paper["id"] for paper in selected], ["rec"])
        markdown = build_arxiv_markdown(selected)
        self.assertIn("Dual Exploration for Generative Re-Ranking", markdown)
        self.assertIn("中文解读：面向个性化物品排序的工业推荐系统。", markdown)
        self.assertNotIn("不应显示的中文标题", markdown)

    def test_relevance_avoids_generic_recommend_and_supports_ads_ranking(self):
        self.assertFalse(
            is_recommendation_relevant(
                "AI Security Leaderboard",
                "We recommend defense-in-depth for a secure product.",
            )
        )
        self.assertFalse(
            is_recommendation_relevant(
                "Chemical Reaction Mining",
                "Condition recommendation improves the reaction product.",
            )
        )
        self.assertTrue(
            is_recommendation_relevant(
                "Generative Ads Ranking",
                "A production ranking model for advertising.",
            )
        )

    def test_relevance_rejects_real_generic_false_positives(self):
        rejected = [
            (
                "An Emerging Retail Portfolio Management Application: Personalized, "
                "Tax-Aware Reinforcement Learning with Natural Language Goals",
                "Personalized portfolio management for retail investors.",
            ),
            (
                "Mapping Similarity Spaces across Embedding Models with Synthetic Query Probing",
                "Retrieval-Augmented Generation uses similarity scores to retrieve content.",
            ),
            (
                "Learning to Rank Tensor Network Contraction Plans for GPU-Accelerated "
                "Quantum Circuit Simulation",
                "Searches a large plan space and ranks contraction plans for quantum "
                "simulation workloads.",
            ),
            (
                "From Trajectories to Evidence: Auditable Experimental Records for "
                "Industrial Research Agents",
                "Agents run experiments in industrial recommendation settings.",
            ),
        ]
        for title, summary in rejected:
            with self.subTest(title=title):
                self.assertFalse(is_recommendation_relevant(title, summary))

    def test_relevance_keeps_direct_product_advice_and_rec_reranking(self):
        self.assertTrue(
            is_recommendation_relevant(
                "Cleo: A Transparent Chatbot for Conversational Commerce",
                "A controllable conversational product advisor for e-commerce.",
            )
        )
        self.assertTrue(
            is_recommendation_relevant(
                "DEGR: Dual Exploration-Driven Generative Re-Ranking",
                "The re-ranking stage in industrial recommendation systems.",
            )
        )


class LLMEnrichmentTests(unittest.TestCase):
    @patch("paperBotV2.llm_enrichment.requests.post")
    def test_batches_open_code_summaries_with_the_free_model(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "paper-1": "该方法改进推荐系统的候选生成与重排序。",
                                "paper-2": "该研究分析个性化搜索中的用户行为。",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        post.return_value = response

        summaries = generate_chinese_summaries(
            [
                {
                    "id": "paper-1",
                    "title": "Recommendation Ranking",
                    "summary": "A candidate generation and reranking method.",
                },
                {
                    "id": "paper-2",
                    "title": "Personalized Search",
                    "summary": "A study of user behavior in search.",
                },
            ],
            api_key="secret-value",
            attempts=1,
        )

        self.assertEqual(len(summaries), 2)
        self.assertEqual(post.call_count, 1)
        request = post.call_args
        self.assertEqual(
            request.args[0], "https://opencode.ai/zen/v1/chat/completions"
        )
        self.assertEqual(request.kwargs["json"]["model"], "deepseek-v4-flash-free")
        self.assertEqual(
            request.kwargs["headers"]["Authorization"], "Bearer secret-value"
        )

    @patch("paperBotV2.llm_enrichment.requests.post")
    def test_missing_key_and_missing_abstract_keep_english_without_api_call(self, post):
        self.assertEqual(
            generate_chinese_summaries(
                [{"id": "paper-1", "title": "Recommendation", "summary": ""}],
                api_key="",
            ),
            {},
        )
        post.assert_not_called()

    @patch("paperBotV2.llm_enrichment.requests.post")
    def test_invalid_response_falls_back_without_raising(self, post):
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "not valid json"}}]
        }
        post.return_value = response

        self.assertEqual(
            generate_chinese_summaries(
                [
                    {
                        "id": "paper-1",
                        "title": "Recommendation",
                        "summary": "A recommender-system abstract.",
                    }
                ],
                api_key="secret-value",
                attempts=1,
            ),
            {},
        )


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
    @patch("paperBotV2.github_models.generate_chinese_summaries")
    def test_legacy_helper_uses_llm_summary_without_translating_title(self, generate):
        generate.return_value = {"paper-1": "高质量中文论文摘要。"}

        result = enrich_chinese(
            [{"id": "paper-1", "title": "English title", "summary": "English abstract"}]
        )

        self.assertNotIn("title_zh", result["paper-1"])
        self.assertEqual(result["paper-1"]["summary_zh"], "高质量中文论文摘要。")
        generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
