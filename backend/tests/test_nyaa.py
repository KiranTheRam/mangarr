from mangarr.sources.nyaa import parse_rss, parse_size

RSS_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0">
 <channel>
  <title>Nyaa - "ashita no joe" - Torrent File RSS</title>
  <item>
   <title>Ashita no Joe (Tomorrow's Joe) v01-v13 (Digital)</title>
   <link>https://nyaa.si/download/1000001.torrent</link>
   <guid isPermaLink="true">https://nyaa.si/view/1000001</guid>
   <pubDate>Mon, 01 Jun 2026 12:00:00 -0000</pubDate>
   <nyaa:seeders>42</nyaa:seeders>
   <nyaa:leechers>3</nyaa:leechers>
   <nyaa:downloads>900</nyaa:downloads>
   <nyaa:infoHash>0123456789abcdef0123456789abcdef01234567</nyaa:infoHash>
   <nyaa:categoryId>3_1</nyaa:categoryId>
   <nyaa:category>Literature - English-translated</nyaa:category>
   <nyaa:size>2.5 GiB</nyaa:size>
  </item>
  <item>
   <title>Ashita no Joe c001-c050</title>
   <link>https://nyaa.si/download/1000002.torrent</link>
   <guid isPermaLink="true">https://nyaa.si/view/1000002</guid>
   <pubDate>Tue, 02 Jun 2026 12:00:00 -0000</pubDate>
   <nyaa:seeders>7</nyaa:seeders>
   <nyaa:leechers>0</nyaa:leechers>
   <nyaa:downloads>120</nyaa:downloads>
   <nyaa:infoHash>fedcba9876543210fedcba9876543210fedcba98</nyaa:infoHash>
   <nyaa:categoryId>3_1</nyaa:categoryId>
   <nyaa:category>Literature - English-translated</nyaa:category>
   <nyaa:size>350.0 MiB</nyaa:size>
  </item>
  <item>
   <title>Broken item without hash</title>
   <guid>https://nyaa.si/view/1000003</guid>
  </item>
 </channel>
</rss>
"""


class TestParseSize:
    def test_gib(self):
        assert parse_size("2.5 GiB") == int(2.5 * 1024**3)

    def test_mib(self):
        assert parse_size("350.0 MiB") == 350 * 1024**2

    def test_garbage(self):
        assert parse_size("n/a") == 0
        assert parse_size("") == 0


class TestParseRss:
    def test_items_parsed(self):
        releases = parse_rss(RSS_FIXTURE)
        assert len(releases) == 2  # broken item skipped

        first = releases[0]
        assert first.source_name == "nyaa"
        assert first.title == "Ashita no Joe (Tomorrow's Joe) v01-v13 (Digital)"
        assert first.url == "https://nyaa.si/view/1000001"
        assert first.torrent_url == "https://nyaa.si/download/1000001.torrent"
        assert first.seeders == 42
        assert first.leechers == 3
        assert first.size_bytes == int(2.5 * 1024**3)

    def test_magnet_built_from_infohash(self):
        magnet = parse_rss(RSS_FIXTURE)[0].magnet
        assert magnet.startswith(
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn="
        )
        assert " " not in magnet
