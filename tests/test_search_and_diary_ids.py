"""Unit tests for food search error handling, diary food ids and recent foods.

Pure — no live MFP session required. Fakes stand in for the web search form
and the v2 diary API, using shapes captured live on 2026-10-21. The live check
against real search results is scripts/smoke_search.py.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import myfitnesspal
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mfp_mcp import server  # noqa: E402


# ----------------------------------------------------------------------------
# Food search
# ----------------------------------------------------------------------------

SEARCH_FORM = b"""<html><body><form>
<input name="authenticity_token" value="tok123"></form></body></html>"""

SEARCH_RESULTS = """<html><body><h2>Matching Foods:</h2><ul>
<li class="matched-food"><div class="search-title-container">
<a data-external-id="111">Banane</a></div>
<p class="search-nutritional-info">Generic, 1 Stück, 105 calories</p></li>
<li class="matched-food"><div class="search-title-container">
<a data-external-id="222">Banane getrocknet</a></div>
<p class="search-nutritional-info">Seeberger, 100 g, 340 calories</p></li>
</ul></body></html>""".encode()

# The login page MFP redirects a rejected search to also says "Matching Foods:".
LOGIN_PAGE = b"<html><body>Matching Foods: sign in</body></html>"


class _WebResponse:
    def __init__(self, content, status_code=200, url=server.MFP_FOOD_SEARCH_URL):
        self.content = content
        self.status_code = status_code
        self.url = url


class _SearchSession:
    def __init__(self, page, result):
        self.page = page
        self.result = result
        self.posts = []

    def get(self, url, timeout=None):
        return self.page

    def post(self, url, data=None, timeout=None):
        self.posts.append(data)
        return self.result


class _SearchClient:
    _get_food_search_results = myfitnesspal.Client._get_food_search_results

    def __init__(self, page, result):
        self.session = _SearchSession(page, result)


def _search_client(result, page=None):
    return _SearchClient(page or _WebResponse(SEARCH_FORM), result)


class TestCookieDomain:
    def test_cookies_are_scoped_to_www(self):
        jar = server.dict_to_cookiejar({"_mfp_session": "x", "remember_me": "y"})
        assert {c.domain for c in jar} == {"www.myfitnesspal.com"}


class TestSearchFoods:
    def test_returns_parsed_results(self):
        client = _search_client(_WebResponse(SEARCH_RESULTS))
        results = server.search_foods(client, "Banane")
        assert [(r.mfp_id, r.name, r.calories) for r in results] == [
            (111, "Banane", 105.0),
            (222, "Banane getrocknet", 340.0),
        ]
        assert client.session.posts[0]["authenticity_token"] == "tok123"
        assert client.session.posts[0]["search"] == "Banane"

    def test_genuine_no_match_is_empty(self):
        client = _search_client(_WebResponse(b"<html>Matching Foods: none</html>"))
        assert server.search_foods(client, "xyzzy") == []

    def test_login_redirect_raises(self):
        client = _search_client(_WebResponse(
            LOGIN_PAGE,
            url="https://www.myfitnesspal.com/account/login?callbackUrl=%2Ffood%2Fsearch",
        ))
        with pytest.raises(RuntimeError, match="login page"):
            server.search_foods(client, "Banane")

    def test_http_error_raises(self):
        client = _search_client(_WebResponse(b"", status_code=503))
        with pytest.raises(RuntimeError, match="HTTP 503"):
            server.search_foods(client, "Banane")

    def test_search_page_error_raises(self):
        client = _search_client(
            _WebResponse(SEARCH_RESULTS), page=_WebResponse(b"", status_code=403)
        )
        with pytest.raises(RuntimeError, match="HTTP 403"):
            server.search_foods(client, "Banane")

    def test_unexpected_page_raises(self):
        client = _search_client(_WebResponse(b"<html>maintenance</html>"))
        with pytest.raises(RuntimeError, match="unexpected page"):
            server.search_foods(client, "Banane")

    def test_missing_token_raises(self):
        client = _search_client(
            _WebResponse(SEARCH_RESULTS), page=_WebResponse(b"<html></html>")
        )
        with pytest.raises(RuntimeError, match="authenticity token"):
            server.search_foods(client, "Banane")


class TestSearchTool:
    def _run(self, monkeypatch, client):
        monkeypatch.setattr(server, "get_mfp_client", lambda: client)
        params = server.SearchFoodInput(query="Banane", limit=1, response_format="json")
        return asyncio.run(server.mfp_search_food(params))

    def test_login_redirect_is_an_error_not_zero_hits(self, monkeypatch):
        client = _search_client(_WebResponse(
            LOGIN_PAGE, url="https://www.myfitnesspal.com/account/login",
        ))
        result = self._run(monkeypatch, client)
        assert result.startswith("Error searching foods")
        assert '"count": 0' not in result


# ----------------------------------------------------------------------------
# v2 diary food entries
# ----------------------------------------------------------------------------


def _food_entry(
    description, meal_position, servings, value, unit, *,
    food_id="f1", meal_name="Breakfast", day="2026-10-21",
    serving_sizes=None, deleted=False,
):
    serving = {"value": value, "unit": unit, "nutrition_multiplier": 1.0, "id": "s1"}
    return {
        "id": f"entry-{food_id}-{day}",
        "type": "food_entry",
        "date": day,
        "meal_name": meal_name,
        "meal_position": meal_position,
        "servings": servings,
        "serving_size": serving,
        "deleted": deleted,
        "food": {
            "id": food_id,
            "version": "v1",
            "description": description,
            "brand_name": "Generic",
            "serving_sizes": serving_sizes if serving_sizes is not None else [serving],
        },
    }


class _DiaryResponse:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _DiarySession:
    def __init__(self, items_by_date, status_code=200, body=None):
        self.items_by_date = items_by_date
        self.status_code = status_code
        self.body = body
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        body = self.body if self.body is not None else {
            "items": self.items_by_date.get(params["entry_date"], [])
        }
        return _DiaryResponse(body, self.status_code)


class _DiaryClient:
    access_token = "token"
    user_id = "123"

    def __init__(self, items_by_date=None, status_code=200, body=None):
        self.session = _DiarySession(items_by_date or {}, status_code, body)


class TestGetFoodEntriesV2:
    def test_requests_food_entries_and_drops_deleted(self):
        client = _DiaryClient({"2026-10-21": [
            _food_entry("Birne", 0, 1.0, 100.0, "gramm"),
            _food_entry("Skyr", 0, 120.0, 1.0, "Gramm", food_id="f2", deleted=True),
        ]})
        items = server.get_food_entries_v2(client, date(2026, 10, 21))
        assert [i["food"]["description"] for i in items] == ["Birne"]
        assert client.session.calls[0][1] == {
            "entry_date": "2026-10-21", "types": "food_entry",
        }

    def test_http_error_raises(self):
        with pytest.raises(RuntimeError, match="HTTP 401"):
            server.get_food_entries_v2(_DiaryClient(status_code=401), date(2026, 10, 21))

    def test_malformed_body_raises(self):
        with pytest.raises(RuntimeError, match="no 'items' list"):
            server.get_food_entries_v2(_DiaryClient(body={"oops": 1}), date(2026, 10, 21))


class TestRelogArgs:
    def test_rescales_to_unit_serving(self):
        # Logged as 1 x "100 gramm"; the food also has a 1-gramm serving, which
        # mfp_add_food_to_diary picks for unit "gramm" -> quantity 100.
        item = _food_entry(
            "Birne", 0, 1.0, 100.0, "gramm",
            serving_sizes=[
                {"value": 100.0, "unit": "gramm", "nutrition_multiplier": 1.0},
                {"value": 1.0, "unit": "gramm", "nutrition_multiplier": 0.01},
            ],
        )
        assert server._relog_args(item) == {
            "mfp_id": "f1", "meal": "Breakfast", "quantity": 100.0, "unit": "gramm",
        }

    def test_keeps_servings_for_per_unit_serving(self):
        item = _food_entry("Skyr", 0, 120.0, 1.0, "Gramm")
        assert server._relog_args(item)["quantity"] == 120.0

    def test_metric_without_unit_serving_uses_weight(self):
        # Only a "100 gramm" serving: mfp_add_food_to_diary divides grams by 100.
        item = _food_entry(
            "Birne", 0, 0.5, 100.0, "gramm",
            serving_sizes=[{"value": 100.0, "unit": "gramm", "nutrition_multiplier": 1.0}],
        )
        assert server._relog_args(item)["quantity"] == 50.0

    def test_counts_servings_of_larger_portion(self):
        item = _food_entry("Brot", 0, 3.0, 50.0, "Scheibe")
        assert server._relog_args(item)["quantity"] == 3.0


class TestMatchFoodEntries:
    def test_pairs_entries_per_meal_by_name(self):
        items = [
            _food_entry("Back Mühle - Hofmischung", 0, 50.0, 1.0, "gram", food_id="a"),
            _food_entry("Skyr\xa0ja - Skyr", 0, 120.0, 1.0, "Gramm", food_id="b"),
            _food_entry("Butter", 2, 10.0, 1.0, "g", food_id="c", meal_name="Dinner"),
        ]
        refs = server.match_food_entries(
            [
                ["Skyr\xa0ja - Skyr, 120 Gramm", "Back Mühle - Hofmischung, 50 gram"],
                [],
                ["Butter, 10 g", "Butterkeks, 2 Stück"],
            ],
            items,
        )
        assert [r and r["food_id"] for r in refs[0]] == ["b", "a"]
        assert refs[1] == []
        assert [r and r["food_id"] for r in refs[2]] == ["c", None]
        assert refs[0][0]["serving_size"] == {
            "value": 1.0, "unit": "Gramm", "nutrition_multiplier": 1.0,
        }

    def test_repeated_food_pairs_in_order(self):
        first = _food_entry("Milch", 0, 100.0, 1.0, "ml", food_id="m")
        second = _food_entry("Milch", 0, 200.0, 1.0, "ml", food_id="m")
        second["id"] = "entry-second"
        refs = server.match_food_entries([["Milch, 100 ml", "Milch, 200 ml"]], [first, second])
        assert [r["entry_id"] for r in refs[0]] == [first["id"], "entry-second"]


class TestRecentFoods:
    def test_aggregates_by_food_most_frequent_first(self):
        client = _DiaryClient({
            "2026-10-19": [_food_entry("Skyr", 0, 150.0, 1.0, "g", food_id="skyr", day="2026-10-19")],
            "2026-10-20": [
                _food_entry("Birne", 0, 1.0, 100.0, "g", food_id="birne", day="2026-10-20"),
                _food_entry("Skyr", 3, 120.0, 1.0, "g", food_id="skyr", day="2026-10-20",
                            meal_name="Snacks"),
            ],
            "2026-10-21": [_food_entry("Mandarine", 0, 1.0, 1.0, "Stück", food_id="mand",
                                       day="2026-10-21")],
        })
        foods = server.get_recent_foods(client, date(2026, 10, 19), date(2026, 10, 21))
        assert [f["food_id"] for f in foods] == ["skyr", "mand", "birne"]
        skyr = foods[0]
        assert skyr["times_logged"] == 2
        assert skyr["last_used"] == "2026-10-20"
        assert skyr["meals"] == ["Breakfast", "Snacks"]
        assert skyr["relog"] == {
            "mfp_id": "skyr", "meal": "Snacks", "quantity": 120.0, "unit": "g",
        }
        assert len(client.session.calls) == 3

    def test_tool_limits_and_reports_errors(self, monkeypatch):
        today = date.today().strftime("%Y-%m-%d")
        client = _DiaryClient({today: [
            _food_entry("A", 0, 1.0, 1.0, "g", food_id="a", day=today),
            _food_entry("B", 0, 1.0, 1.0, "g", food_id="b", day=today),
        ]})
        monkeypatch.setattr(server, "get_mfp_client", lambda: client)
        params = server.GetRecentFoodsInput(days=2, limit=1, response_format="json")
        result = json.loads(asyncio.run(server.mfp_get_recent_foods(params)))
        assert result["count"] == 1
        assert result["start_date"] == str(date.today() - timedelta(days=1))
        assert len(result["foods"]) == 1

        monkeypatch.setattr(server, "get_mfp_client", lambda: _DiaryClient(status_code=500))
        result = asyncio.run(server.mfp_get_recent_foods(params))
        assert result.startswith("Error getting recent foods")
        assert "HTTP 500" in result
