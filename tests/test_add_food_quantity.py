"""
Tests for mfp_add_food_to_diary's quantity handling and read-back check.

A fake v2 API stores entries the way MyFitnessPal does (energy = food energy
x nutrition_multiplier x servings), so each test checks what the diary would
really hold. The foods mirror real ones that were logged at 100x the amount
when quantity was taken as a count of "100 g" servings.
"""

import asyncio
import json
import re
from datetime import date

import pytest

from mfp_mcp import server

DAY = date(2026, 10, 21)


def serving(value, unit, mult):
    return {"value": value, "unit": unit, "nutrition_multiplier": mult}


FOODS = {
    # default serving 100 g, a 1 g serving and a container mentioning "gram"
    "muesli": {
        "description": "Müsli",
        "energy": 407.0,
        "serving_sizes": [
            serving(100.0, "gram", 1.0),
            serving(1.0, "gram", 0.01),
            serving(1.0, "container (425.0 gram)", 4.25),
        ],
    },
    # only a 1 g serving, German spelling
    "skyr": {
        "description": "Skyr",
        "energy": 79.0,
        "serving_sizes": [serving(1.0, "Gramm", 0.01)],
    },
    # only a 100 g serving
    "pear": {
        "description": "Birne",
        "energy": 101.46,
        "serving_sizes": [serving(100.0, "gramm", 0.5618)],
    },
    # 100 ml default plus 1 ml
    "milk": {
        "description": "Milch 1,5",
        "energy": 49.0,
        "serving_sizes": [serving(100.0, "ml", 1.0), serving(1.0, "ml", 0.01)],
    },
    # pieces only
    "tangerine": {
        "description": "Mandarine",
        "energy": 39.0,
        "serving_sizes": [serving(1.0, "Stück", 1.0)],
    },
}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Just enough of MFP's v2 food and diary API for add_food_to_diary."""

    def __init__(self, store_factor=1.0, lose_entries=False, delete_status=204):
        self.entries = {}
        self.posts = []
        self.deleted = []
        self.store_factor = store_factor
        self.lose_entries = lose_entries
        self.delete_status = delete_status

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/v2/foods"):
            food_id = params["ids"]
            food = FOODS[food_id]
            return FakeResponse(200, {"items": [{
                "id": food_id,
                "version": "1",
                "description": food["description"],
                "serving_sizes": food["serving_sizes"],
            }]})
        if url.endswith("/v2/diary"):
            assert params["entry_date"] == DAY.isoformat()
            return FakeResponse(200, {"items": list(self.entries.values())})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, data=None, headers=None, timeout=None):
        assert url.endswith("/v2/diary")
        entry = json.loads(data)["items"][0]
        self.posts.append(entry)
        entry_id = f"entry-{len(self.posts)}"
        food = FOODS[entry["food"]["id"]]
        servings = entry["servings"] * self.store_factor
        size = entry["serving_size"]
        if not self.lose_entries:
            self.entries[entry_id] = {
                "id": entry_id,
                "type": "food_entry",
                "servings": servings,
                "serving_size": size,
                "nutritional_contents": {"energy": {
                    "unit": "calories",
                    "value": food["energy"] * size["nutrition_multiplier"] * servings,
                }},
            }
        return FakeResponse(200, {"items": [{"id": entry_id}]})

    def delete(self, url, headers=None, timeout=None):
        entry_id = re.search(r"/v2/diary/([^/]+)$", url).group(1)
        self.deleted.append(entry_id)
        if self.delete_status in (200, 204):
            self.entries.pop(entry_id, None)
        return FakeResponse(self.delete_status)


class FakeClient:
    access_token = "token"
    user_id = "1"

    def __init__(self, **kwargs):
        self.session = FakeSession(**kwargs)


def add(client, food, quantity, unit=None):
    return server.add_food_to_diary(client, food, "Breakfast", DAY, quantity, unit)


class TestMetricQuantity:
    def test_default_100g_serving_logs_grams_via_1g_serving(self):
        client = FakeClient()
        result = add(client, "muesli", 50)
        posted = client.session.posts[0]
        assert posted["serving_size"] == serving(1.0, "gram", 0.01)
        assert posted["servings"] == 50
        assert result["logged"]["quantity"] == 50
        assert result["logged"]["unit"] == "g"
        assert result["logged"]["calories"] == 203.5

    def test_only_1g_serving(self):
        result = add(FakeClient(), "skyr", 120)
        assert result["logged"]["quantity"] == 120
        assert result["logged"]["unit"] == "g"
        assert result["logged"]["calories"] == 94.8

    def test_only_100g_serving_divides_quantity(self):
        client = FakeClient()
        result = add(client, "pear", 100)
        assert client.session.posts[0]["servings"] == 1.0
        assert result["logged"]["quantity"] == 100
        assert result["logged"]["calories"] == 57.0

    def test_only_100g_serving_partial_amount(self):
        client = FakeClient()
        result = add(client, "pear", 50)
        assert client.session.posts[0]["servings"] == 0.5
        assert result["logged"]["quantity"] == 50

    def test_millilitres(self):
        client = FakeClient()
        result = add(client, "milk", 50)
        assert client.session.posts[0]["serving_size"]["value"] == 1.0
        assert result["logged"]["quantity"] == 50
        assert result["logged"]["unit"] == "ml"
        assert result["logged"]["calories"] == 24.5

    @pytest.mark.parametrize("unit", ["g", "Gramm", "gram", "G."])
    def test_explicit_gram_spellings_match(self, unit):
        client = FakeClient()
        result = add(client, "muesli", 50, unit)
        assert client.session.posts[0]["serving_size"]["unit"] == "gram"
        assert client.session.posts[0]["servings"] == 50
        assert result["logged"]["calories"] == 203.5

    def test_explicit_milliliter(self):
        result = add(FakeClient(), "milk", 200, "milliliter")
        assert result["logged"]["quantity"] == 200
        assert result["logged"]["calories"] == 98.0

    def test_gram_word_inside_other_unit_is_not_metric(self):
        food = FOODS["muesli"]
        chosen = server.select_serving_size(food, "container")
        assert chosen["unit"] == "container (425.0 gram)"
        assert server.servings_for_quantity(chosen, 2) == 2


class TestPieceQuantity:
    def test_piece_unit_counts_servings(self):
        client = FakeClient()
        result = add(client, "tangerine", 1)
        assert client.session.posts[0]["servings"] == 1.0
        assert result["logged"]["quantity"] == 1.0
        assert result["logged"]["unit"] == "Stück"
        assert result["logged"]["calories"] == 39.0

    def test_two_pieces(self):
        result = add(FakeClient(), "tangerine", 2)
        assert result["logged"]["quantity"] == 2.0
        assert result["logged"]["calories"] == 78.0

    def test_gram_unit_on_piece_food_fails_before_writing(self):
        client = FakeClient()
        with pytest.raises(RuntimeError, match="no serving in g.*Stück"):
            add(client, "tangerine", 80, "g")
        assert client.session.posts == []


class TestReadBack:
    def test_wrong_stored_amount_is_removed(self):
        client = FakeClient(store_factor=100)
        with pytest.raises(RuntimeError, match="stored 5000 g instead of 50.*removed"):
            add(client, "muesli", 50)
        assert client.session.deleted == ["entry-1"]
        assert client.session.entries == {}

    def test_within_one_percent_is_accepted(self):
        client = FakeClient(store_factor=1.005)
        add(client, "muesli", 100)
        assert client.session.deleted == []

    def test_missing_entry_is_removed_and_reported(self):
        client = FakeClient(lose_entries=True)
        with pytest.raises(RuntimeError, match="not found"):
            add(client, "skyr", 120)
        assert client.session.deleted == ["entry-1"]

    def test_failed_removal_names_the_entry(self):
        client = FakeClient(store_factor=100, delete_status=500)
        with pytest.raises(RuntimeError, match="could not be removed.*entry-1"):
            add(client, "muesli", 50)


class TestTool:
    def run_tool(self, monkeypatch, client, **params):
        monkeypatch.setattr(server, "get_mfp_client", lambda: client)
        args = server.AddFoodToDiaryInput(date=DAY.isoformat(), **params)
        return asyncio.run(server.mfp_add_food_to_diary(args))

    def test_reports_logged_amount_and_kcal(self, monkeypatch):
        out = json.loads(self.run_tool(monkeypatch, FakeClient(), mfp_id="muesli", quantity=50))
        assert out["success"] is True
        assert out["logged"]["quantity"] == 50
        assert out["logged"]["unit"] == "g"
        assert out["logged"]["calories"] == 203.5
        assert out["message"] == "Added 50 g of Müsli to Breakfast (203.5 kcal)"

    def test_error_is_returned_as_text(self, monkeypatch):
        out = self.run_tool(monkeypatch, FakeClient(store_factor=100), mfp_id="muesli", quantity=50)
        assert out.startswith("Error adding food to diary:")
