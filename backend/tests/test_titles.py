from mangarr.titles import english_title, title_queries


def test_english_title_prefers_query_match():
    assert (
        english_title(
            "Ijiranaide, Nagatoro-san",
            ["イジらないで、長瀞さん", "Don't Toy With Me, Miss Nagatoro"],
            query="dont toy with me miss nagatoro",
        )
        == "Don't Toy With Me, Miss Nagatoro"
    )


def test_english_title_falls_back_to_likely_alias():
    assert (
        english_title("Sousou no Frieren", ["Frieren", "Frieren: Beyond Journey's End"])
        == "Frieren: Beyond Journey's End"
    )


def test_short_english_title_can_be_selected():
    assert english_title("ブルーロック", ["Blue Lock"]) == "Blue Lock"


def test_title_queries_try_primary_then_english_alias():
    assert title_queries("Ijiranaide, Nagatoro-san", ["Don't Toy With Me, Miss Nagatoro"])[:2] == [
        "Ijiranaide, Nagatoro-san",
        "Don't Toy With Me, Miss Nagatoro",
    ]
