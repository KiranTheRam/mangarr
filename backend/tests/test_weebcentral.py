import httpx
import pytest
import respx

from mangarr.sources.weebcentral import BASE_URL, WeebCentralSource

# Real WeebCentral chapter-list structure: each link wraps a leaf <span> with
# the label, a "Last Read" span, and a <time> whose date digits must be ignored.
CHAPTER_LIST = """
<html><body>
  <a class="flex-1" href="https://weebcentral.com/chapters/01KW7C7HP8RPGDEMRKQVMQ32ZD">
    <span class="me-2"><svg></svg></span>
    <span class="grow flex items-center gap-2">
      <span>Days 265</span>
      <span class="link-info"><span class="hidden md:inline">Last Read</span></span>
      <time datetime="2026-06-28T15:05:39Z">2026-06-28T15:05:39.272196Z</time>
    </span>
  </a>
  <a class="flex-1" href="https://weebcentral.com/chapters/01KVNBE65CF5HJASF0000000000">
    <span class="grow">
      <span>Days 264</span>
      <time>2026-06-21T15:05:28Z</time>
    </span>
  </a>
</body></html>
"""


@respx.mock
async def test_list_chapters_ignores_last_read_and_timestamp():
    src = WeebCentralSource(client=httpx.AsyncClient())
    respx.get(f"{BASE_URL}/series/ABC/full-chapter-list").respond(text=CHAPTER_LIST)
    chapters = await src.list_chapters("ABC")
    # both chapters parsed as 264/265 — NOT the 2026 year from the <time>
    assert [c.number for c in chapters] == [264.0, 265.0]
    assert chapters[1].external_id == "01KW7C7HP8RPGDEMRKQVMQ32ZD"
