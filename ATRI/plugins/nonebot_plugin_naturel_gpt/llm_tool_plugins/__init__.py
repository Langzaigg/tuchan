from .bocha_search import schema as bocha_search_schema, run as run_bocha_search
from .browse_url import schema as browse_url_schema, run as run_browse_url
from .fetch_url import schema as fetch_url_schema, run as run_fetch_url
from .pixiv_search import schema as pixiv_search_schema, run as run_pixiv_search

TOOL_REGISTRY = {
    "pixiv_search": (pixiv_search_schema, run_pixiv_search),
    "fetch_url": (fetch_url_schema, run_fetch_url),
    "browse_url": (browse_url_schema, run_browse_url),
    "bocha_search": (bocha_search_schema, run_bocha_search),
}
