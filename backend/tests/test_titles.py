from mangarr.titles import english_title, plausible_title_match, title_queries


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


class TestPlausibleTitleMatch:
    def test_exact_and_punctuation_variants_match(self):
        assert plausible_title_match("Chainsaw Man", "Chainsaw Man!")
        assert plausible_title_match("SPY×FAMILY", "Spy x Family")

    def test_spacing_variants_match(self):
        # the case the fallback exists for: same series, different word split
        assert plausible_title_match("Kagura Bachi", "Kagurabachi")

    def test_shared_prefix_of_a_different_series_does_not_match(self):
        # the live failure mode: a bare prefix must not link the wrong series
        assert not plausible_title_match("Berserk of Gluttony", "Berserk")
        assert not plausible_title_match("Monster Musume no Iru Nichijou", "Monster")

    def test_small_suffix_still_matches(self):
        assert plausible_title_match("Dandadan!", "Dandadan")

    def test_short_or_empty_queries_never_match(self):
        assert not plausible_title_match("One", "One")  # < 4 normalized chars
        assert not plausible_title_match("Anything", "")
        assert not plausible_title_match("Anything", "×××")  # normalizes empty-ish
