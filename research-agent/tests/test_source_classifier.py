"""
Unit tests for source_classifier.py.

Tests classify() for all category branches and SourceDiversityTracker
for recording, summary, and nudge behaviour.
"""
import pytest

from source_classifier import SourceDiversityTracker, classify


# ── classify() ────────────────────────────────────────────────────────────────


class TestClassifyGovernment:
    def test_dotgov_tld(self):
        assert classify("https://www.cdc.gov/health") == "[Government]"

    def test_dotmil_tld(self):
        assert classify("https://www.army.mil/news") == "[Government]"

    def test_subdomain_dotgov(self):
        assert classify("https://data.census.gov/") == "[Government]"


class TestClassifyAcademic:
    def test_dotedu_tld(self):
        assert classify("https://mit.edu/research") == "[Academic]"

    def test_arxiv(self):
        assert classify("https://arxiv.org/abs/1234.5678") == "[Academic]"

    def test_pubmed(self):
        # pubmed.ncbi.nlm.nih.gov is a .gov subdomain — Government takes precedence
        assert classify("https://pubmed.ncbi.nlm.nih.gov/12345") == "[Government]"

    def test_nature(self):
        assert classify("https://nature.com/articles/abc") == "[Academic]"

    def test_jstor(self):
        assert classify("https://www.jstor.org/stable/123") == "[Academic]"

    def test_subdomain_of_academic_domain(self):
        assert classify("https://journals.plos.org/plosone/article") == "[Academic]"


class TestClassifyNews:
    def test_reuters(self):
        assert classify("https://reuters.com/world/story") == "[News]"

    def test_bbc(self):
        assert classify("https://www.bbc.com/news/technology") == "[News]"

    def test_bbc_co_uk(self):
        assert classify("https://bbc.co.uk/news") == "[News]"

    def test_nytimes(self):
        assert classify("https://nytimes.com/2026/01/01/tech.html") == "[News]"

    def test_techcrunch(self):
        assert classify("https://techcrunch.com/2026/01/01/story") == "[News]"

    def test_theverge(self):
        assert classify("https://theverge.com/2026/1/1/article") == "[News]"


class TestClassifyReference:
    def test_wikipedia(self):
        assert classify("https://en.wikipedia.org/wiki/Python") == "[Reference]"

    def test_britannica(self):
        assert classify("https://britannica.com/topic/python") == "[Reference]"

    def test_investopedia(self):
        assert classify("https://investopedia.com/terms/r/roi.asp") == "[Reference]"


class TestClassifySocialUGC:
    def test_reddit(self):
        assert classify("https://reddit.com/r/python") == "[Social/UGC]"

    def test_twitter(self):
        assert classify("https://twitter.com/user/status/123") == "[Social/UGC]"

    def test_x_com(self):
        assert classify("https://x.com/user") == "[Social/UGC]"

    def test_youtube(self):
        assert classify("https://youtube.com/watch?v=abc") == "[Social/UGC]"

    def test_medium(self):
        assert classify("https://medium.com/@author/article") == "[Social/UGC]"


class TestClassifyProfessional:
    def test_linkedin(self):
        assert classify("https://linkedin.com/in/someone") == "[Professional]"

    def test_crunchbase(self):
        assert classify("https://crunchbase.com/organization/acme") == "[Professional]"

    def test_glassdoor(self):
        assert classify("https://glassdoor.com/Reviews/company-reviews.htm") == "[Professional]"


class TestClassifyNonProfit:
    def test_dotorg_tld(self):
        assert classify("https://example.org/about") == "[Non-profit/NGO]"

    def test_subdomain_dotorg(self):
        assert classify("https://blog.example.org/post") == "[Non-profit/NGO]"


class TestClassifyWeb:
    def test_generic_com(self):
        assert classify("https://example.com/page") == "[Web]"

    def test_generic_net(self):
        assert classify("https://example.net/page") == "[Web]"

    def test_www_stripped(self):
        # www is stripped before matching — should still classify correctly
        result = classify("https://www.example.com/page")
        assert result == "[Web]"

    def test_invalid_url_returns_web(self):
        assert classify("not-a-url") == "[Web]"

    def test_empty_string_returns_web(self):
        assert classify("") == "[Web]"


class TestClassifyWwwStripping:
    def test_www_arxiv_is_academic(self):
        assert classify("https://www.arxiv.org/abs/123") == "[Academic]"

    def test_www_reddit_is_social(self):
        assert classify("https://www.reddit.com/r/python") == "[Social/UGC]"

    def test_www_wikipedia_is_reference(self):
        assert classify("https://www.wikipedia.org/wiki/Test") == "[Reference]"


# ── SourceDiversityTracker ───────────────────────────────────────────────────


class TestSourceDiversityTrackerRecord:
    def test_record_increments_count(self):
        tracker = SourceDiversityTracker()
        tracker.record("[News]")
        tracker.record("[News]")
        assert tracker._counts["[News]"] == 2
        assert tracker._total == 2

    def test_record_multiple_categories(self):
        tracker = SourceDiversityTracker()
        tracker.record("[Academic]")
        tracker.record("[News]")
        tracker.record("[Social/UGC]")
        assert tracker._total == 3
        assert tracker._counts["[Academic]"] == 1


class TestSourceDiversityTrackerSummary:
    def test_summary_empty(self):
        tracker = SourceDiversityTracker()
        assert tracker.summary() == ""

    def test_summary_single_category(self):
        tracker = SourceDiversityTracker()
        tracker.record("[News]")
        summary = tracker.summary()
        assert "[News]: 1" in summary
        assert "Sources fetched so far" in summary

    def test_summary_multiple_categories_sorted(self):
        tracker = SourceDiversityTracker()
        tracker.record("[Web]")
        tracker.record("[Academic]")
        tracker.record("[News]")
        summary = tracker.summary()
        # Categories should be sorted alphabetically
        academic_pos = summary.index("[Academic]")
        news_pos = summary.index("[News]")
        web_pos = summary.index("[Web]")
        assert academic_pos < news_pos < web_pos


class TestSourceDiversityTrackerNudge:
    def test_nudge_empty_no_suggestion(self):
        tracker = SourceDiversityTracker()
        assert tracker.nudge() == ""

    def test_nudge_single_source_no_suggestion(self):
        tracker = SourceDiversityTracker()
        tracker.record("[News]")
        assert tracker.nudge() == ""

    def test_nudge_no_high_quality_sources(self):
        tracker = SourceDiversityTracker()
        for _ in range(3):
            tracker.record("[News]")
        nudge = tracker.nudge()
        assert nudge != ""
        assert "academic" in nudge.lower() or "government" in nudge.lower()

    def test_nudge_too_many_social_ugc(self):
        tracker = SourceDiversityTracker()
        for _ in range(5):
            tracker.record("[Social/UGC]")
        tracker.record("[News]")
        nudge = tracker.nudge()
        assert nudge != ""
        assert "social" in nudge.lower() or "ugc" in nudge.lower() or "reddit" in nudge.lower() or "credibility" in nudge.lower()

    def test_nudge_all_news_suggests_primary_sources(self):
        tracker = SourceDiversityTracker()
        for _ in range(3):
            tracker.record("[News]")
        nudge = tracker.nudge()
        assert nudge != ""
        assert "primary" in nudge.lower() or "academic" in nudge.lower()

    def test_nudge_good_mix_returns_empty(self):
        tracker = SourceDiversityTracker()
        tracker.record("[Academic]")
        tracker.record("[Government]")
        tracker.record("[News]")
        tracker.record("[Reference]")
        tracker.record("[Web]")
        assert tracker.nudge() == ""

    def test_nudge_low_high_quality_ratio(self):
        tracker = SourceDiversityTracker()
        tracker.record("[Academic]")  # 1 high-quality
        for _ in range(9):
            tracker.record("[News]")  # 9 others
        nudge = tracker.nudge()
        assert nudge != ""  # < 20% high-quality should trigger nudge


class TestSourceDiversityTrackerThreadSafety:
    def test_concurrent_records_are_safe(self):
        import threading

        tracker = SourceDiversityTracker()
        threads = [
            threading.Thread(target=tracker.record, args=("[News]",))
            for _ in range(100)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert tracker._total == 100
        assert tracker._counts["[News]"] == 100
