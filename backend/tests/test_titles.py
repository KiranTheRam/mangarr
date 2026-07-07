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


def test_title_queries_rank_english_alternates_before_other_languages():
    # the live failure: the official English title ranked below Polish and
    # romaji variants and was never used to search sources
    alt_titles = [
        "Przeciwieństwa do szaleństwa (Polish)",
        "Seihantaina Kimi to Boku",
        "Tentang Kita yang Bertolak Belakang",
        "The Polar Opposite You And Me",
        "Trái Dấu Hút Nhau",
        "Tu y yo somos polos opuestos",
        "You and I Are Polar Opposites",
        "ลุ้นรักฉบับคู่ต่างขั้ว (SIC)",
        "正反対な君と僕",
        "相反的你和我",
        "정반대의 너와 나",
    ]
    queries = title_queries("Seihantai na Kimi to Boku", alt_titles)
    # link_sources only tries the first 4 — the official English title must
    # be among them
    assert "You and I Are Polar Opposites" in queries[:4]
