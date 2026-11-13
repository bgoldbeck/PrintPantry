from test_library import by_name, client, library  # noqa: F401  (fixtures)

from app.tags import clean_tag, clean_tags


def test_clean_tag():
    assert clean_tag("  Gift  Idea! ") == "gift idea"
    assert clean_tag("***") == ""
    assert clean_tags(["Kitchen", "kitchen", " ", "PLA+"]) == ["kitchen", "pla+"]


def test_tags_round_trip_and_search(client):  # noqa: F811
    m = by_name(client, "Wrench Holder")
    r = client.put(f"/api/models/{m['id']}/tags", json={"tags": ["Garage", "wall mount", "garage"]})
    assert r.json()["tags"] == ["garage", "wall mount"]
    assert client.get(f"/api/models/{m['id']}").json()["tags"] == ["garage", "wall mount"]
    assert {"tag": "garage", "count": 1} in client.get("/api/tags").json()["tags"]

    # free text finds tags
    hits = client.get("/api/models", params={"q": "garage"}).json()["models"]
    assert [h["name"] for h in hits] == ["Wrench Holder"]
    assert hits[0]["tags"] == ["garage", "wall mount"]
    # tag: filter, with underscores for spaces
    hits = client.get("/api/models", params={"q": "tag:wall_mount"}).json()["models"]
    assert [h["name"] for h in hits] == ["Wrench Holder"]
    assert client.get("/api/models", params={"q": "tag:nope"}).json()["total"] == 0
    # category view carries tags too
    cat = client.get(f"/api/categories/{m['category']}").json()
    assert next(x for x in cat["models"] if x["id"] == m["id"])["tags"] == ["garage", "wall mount"]

    # clearing
    client.put(f"/api/models/{m['id']}/tags", json={"tags": []})
    assert client.get("/api/tags").json()["tags"] == []


def test_tags_unknown_model(client):  # noqa: F811
    assert client.put("/api/models/nope/tags", json={"tags": ["x"]}).status_code == 404


def test_search_ranks_name_hits_first_and_cat_filter(client):  # noqa: F811
    all_models = client.get("/api/models").json()["models"]
    some = all_models[0]
    res = client.get("/api/models", params={"q": f"cat:{some['category'].lower()}"}).json()
    assert res["total"] >= 1 and all(m["category"] == some["category"] for m in res["models"])
    word = some["name"].split()[0].lower()
    res = client.get("/api/models", params={"q": word}).json()["models"]
    assert res[0]["name"].lower().startswith(word) or word in res[0]["name"].lower()
