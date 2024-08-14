from datetime import datetime
import re
from typing import Optional, Tuple

import lxml
from newspaper import urls
from newspaper.configuration import Configuration
import newspaper.parsers as parsers
from dateutil.parser import parse as date_parser

from newspaper.extractors.defines import PUBLISH_DATE_META_INFO, PUBLISH_DATE_TAGS

# Separate lists for updated and published date metadata
UPDATED_DATE_META_INFO = [
    "updated_time",
    "og:updated_time",
    "datemodified",
    "last-modified",
    "Last-Modified",
    "DC.date.modified",
    "article:modified_time",
    "modified_time",
    "modifiedDateTime",
    "dc.dcterms.modified",
    "lastmod",
    "eomportal-lastUpdate",
]

PUBLISHED_DATE_META_INFO = [item for item in PUBLISH_DATE_META_INFO if item not in UPDATED_DATE_META_INFO]

class PubdateExtractor:
    def __init__(self, config: Configuration) -> None:
        self.config = config
        self.pubdate: Optional[datetime] = None
        self.updatedate: Optional[datetime] = None

    def parse(self, article_url: str, doc: lxml.html.Element) -> Tuple[Optional[datetime], Optional[datetime]]:
        """3 strategies for date extraction. The strategies
        are descending in accuracy and the next strategy is only
        attempted if a preferred one fails.

        1. Date from URL
        2. Date from metadata
        3. Raw regex searches in the HTML + added heuristics
        """

        def parse_date_str(date_str):
            if date_str:
                try:
                    return date_parser(date_str)
                except (ValueError, OverflowError, AttributeError, TypeError):
                    return None

        date_matches = []
        date_match = re.search(urls.STRICT_DATE_REGEX, article_url)
        if date_match:
            date_match_str = date_match.group(0)
            datetime_obj = parse_date_str(date_match_str)
            if datetime_obj:
                date_matches.append((datetime_obj, 10, 'published'))  # date, matchscore, and type

        # yoast seo structured data or json-ld
        json_ld_scripts = parsers.get_ld_json_object(doc)

        for script_tag in json_ld_scripts:
            if "@graph" in script_tag:
                g = script_tag.get("@graph", [])
                for item in g:
                    if not isinstance(item, dict):
                        continue
                    for date_key, date_type in [("dateModified", "updated"), ("datePublished", "published")]:
                        date_str = item.get(date_key)
                        if date_str is None:
                            continue
                        datetime_obj = parse_date_str(date_str)
                        if datetime_obj:
                            date_matches.append((datetime_obj, 10, date_type))
            else:
                for k in script_tag:
                    if k in ["dateModified", "datePublished", "dateCreated"]:
                        date_str = script_tag.get(k)
                        datetime_obj = parse_date_str(date_str)
                        if datetime_obj:
                            date_type = "updated" if k == "dateModified" else "published"
                            date_matches.append((datetime_obj, 9, date_type))

        # get <time> tags
        for item in parsers.get_tags(doc, tag="time"):
            if item.get("datetime"):
                date_str = item.get("datetime")
                datetime_obj = parse_date_str(date_str)
                if datetime_obj:
                    if item.text and re.search("updated|modified", item.text, re.I):
                        date_matches.append((datetime_obj, 8, "updated"))
                    elif item.text and re.search("published|\bon:", item.text, re.I):
                        date_matches.append((datetime_obj, 7, "published"))
                    else:
                        date_matches.append((datetime_obj, 5, "unknown"))

        candidates = []

        for known_meta_info in UPDATED_DATE_META_INFO:
            candidates.extend([(x, "content", "updated") for x in parsers.get_metatags(doc, value=known_meta_info)])

        for known_meta_info in PUBLISHED_DATE_META_INFO:
            candidates.extend([(x, "content", "published") for x in parsers.get_metatags(doc, value=known_meta_info)])

        for known_meta_tag in PUBLISH_DATE_TAGS:
            candidates.extend(
                [
                    (x, known_meta_tag["content"], "unknown")
                    for x in parsers.get_elements_by_attribs(
                        doc,
                        attribs={known_meta_tag["attribute"]: known_meta_tag["value"]},
                    )
                ]
            )

        for meta_tag, content_attr, date_type in candidates:
            date_str = parsers.get_attribute(meta_tag, content_attr)
            datetime_obj = parse_date_str(date_str)
            if datetime_obj:
                score = 6
                if meta_tag.tag.lower() == "meta":
                    score += 1  # Boost meta tags
                if date_type == "updated":
                    score += 2  # Boost updated dates
                days_diff = (datetime.now().date() - datetime_obj.date()).days
                if days_diff < 0:  # dates from the future
                    score -= 2
                elif days_diff > 25 * 365:  # very old dates
                    score -= 1
                date_matches.append((datetime_obj, score, date_type))

        date_matches.sort(key=lambda x: (x[1], x[2] == 'updated'), reverse=True)
        
        self.updatedate = next((date for date, _, type_ in date_matches if type_ == 'updated'), None)
        self.pubdate = next((date for date, _, type_ in date_matches if type_ == 'published'), None)
        
        return self.updatedate or self.pubdate
