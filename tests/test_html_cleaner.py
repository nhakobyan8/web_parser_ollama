from app.html_cleaner import compact_article_page, compact_discovery_page

HTML = """
<html>
  <head>
    <title>News</title>
    <meta property="og:title" content="Main article">
    <script>window.bad = true;</script>
  </head>
  <body>
    <nav><a href="/category">Category</a></nav>
    <main>
      <h2>Latest news</h2>
      <article class="news-card">
        <a href="/posts/latest">New publication</a>
        <p>A short description of the new publication.</p>
      </article>
    </main>
    <footer>Footer</footer>
  </body>
</html>
"""


def test_discovery_keeps_article_link_and_removes_navigation():
    page = compact_discovery_page(HTML, "https://example.com/news", 20_000)
    assert "https://example.com/posts/latest" in page.content
    assert "Category" not in page.content
    assert "window.bad" not in page.content
    assert page.anchors_count == 1


def test_article_page_contains_metadata_and_body():
    page = compact_article_page(HTML, "https://example.com/posts/latest", 20_000)
    assert "Main article" in page.content
    assert "A short description" in page.content
    assert "Footer" not in page.content
