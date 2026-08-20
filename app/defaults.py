DEFAULT_DISCOVERY_PROMPT = """Analyze the structure of this page and find the most recently
published item specifically from the main general stream of posts on this page.
Do not select:
categories;
subcategories;
tags;
popular items;
featured items;
recommended items;
advertisements;
author pages;
banners;
external websites;
archive links.
Determine semantically which block is the main general stream of posts.
Return only the post URL."""


DEFAULT_EXTRACTION_PROMPT = """This is the page of a specific article.
Extract only information that belongs to this article.
Do not include:
menus;
recommendations;
related articles;
popular articles;
advertisements;
comments;
the footer;
navigation;
other news items.
Return structured article data.
Schema:
{
  "title": "...",
  "text": "...",
  "published_at": "...",
  "updated_at": null,
  "author": null,
  "category": null,
  "image_url": null,
  "source_url": "...",
  "language": "...",
  "entities": []
}
The text field must contain the complete main article text without navigation or noise.
If a value cannot be determined, return null instead of inventing it."""


DEFAULT_PROCESSING_PROMPT = "Create a concise, natural text for a Telegram post."
