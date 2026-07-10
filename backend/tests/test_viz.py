from mangarr.sources.viz import parse_archive


def test_parse_archive_uses_official_volume_containers_and_decimal_chapters():
    html = """
    <div class="o_chapter-vol-container">
      <a class="o_manga-buy-now" aria-label="Buy Chainsaw Man, Vol. 4"></a>
      <table><tr class="o_chapter"><td><a name="26" href="/chapters/csm-26">26</a></td></tr>
      <tr class="o_chapter"><td><a name="35.5" href="/chapters/csm-35-5">35.5</a></td></tr></table>
    </div>
    <div class="o_chapter-vol-container">
      <a aria-label="Buy Chainsaw Man, Vol. 5"></a>
      <a name="36" href="/chapters/csm-36">36</a>
    </div>
    """
    rows = parse_archive(html, "https://www.viz.com/shonenjump/chapters/chainsaw-man")
    assert [(row.number, row.volume) for row in rows] == [
        (26.0, 4), (35.5, 4), (36.0, 5),
    ]
