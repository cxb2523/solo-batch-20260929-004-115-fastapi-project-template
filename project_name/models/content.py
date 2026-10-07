import base64
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Extra, field_validator
from sqlalchemy import func
from sqlmodel import Field, Relationship, SQLModel, and_, or_, select

if TYPE_CHECKING:
    from project_name.security import User


class Content(SQLModel, table=True):
    """This is an example model for your application.

    Replace with the *things* you do in your application.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    slug: str = Field(default=None)
    text: str
    published: bool = False
    created_time: str = Field(
        default_factory=lambda: datetime.now().isoformat()
    )
    tags: str = Field(default="")
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")

    # It populates a `.contents` attribute to the `User` model.
    user: Optional["User"] = Relationship(back_populates="contents")


class ContentResponse(BaseModel):
    """This the serializer exposed on the API"""

    id: int
    title: str
    slug: str
    text: str
    published: bool
    created_time: str
    tags: List[str]
    user_id: int

    @field_validator("tags", mode="before")
    @classmethod
    def _split_tags(cls, tags):
        # tags to model representation
        if isinstance(tags, str):
            return [tag for tag in tags.split(",") if tag]
        return tags


class ContentIncoming(BaseModel):
    """This is the serializer used for POST/PATCH requests"""

    title: Optional[str]
    text: Optional[str]
    published: Optional[bool] = False
    tags: Optional[Union[List[str], str]]

    class Config:
        extra = Extra.allow
        arbitrary_types_allowed = True

    def __init__(self, *args, **kwargs):
        # tags to database representation
        tags = kwargs.pop("tags", None)
        if tags and isinstance(tags, list):
            kwargs["tags"] = ",".join(tags)
        super().__init__(*args, **kwargs)
        self.generate_slug()

    def generate_slug(self):
        """Generate a slug from the title."""
        if self.title:
            self.slug = self.title.lower().replace(" ", "-")


# ---------------------------------------------------------------------------
# Declarative query layer
#
# `build_content_filters` produces the single declarative description of
# the active filters. Every entry point (HTTP route, CLI, ...) and every
# backend compiler (SQLAlchemy, Mongo) consumes this same structure, so
# the filtering logic exists exactly once and both backends produce
# identical hit sets.
# ---------------------------------------------------------------------------

# Fields that `q` is matched against (literal substring, OR combined)
Q_SEARCH_FIELDS = ("title", "text")

# Sort whitelist: only these fields may be ordered on
SORTABLE_FIELDS = ("id", "title", "slug", "created_time", "published")
DEFAULT_SORT_FIELD = "id"


def normalize_tags(tags: Optional[Union[str, List[str]]]) -> List[str]:
    """Normalize the `tags` parameter.

    Accepts a comma separated string or a list of strings (each item may
    itself be comma separated). Values are stripped, emptied dropped and
    deduplicated preserving order. Multiple tags are AND combined.
    """
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [tags]
    normalized = []
    for item in tags:
        for tag in str(item).split(","):
            tag = tag.strip()
            if tag and tag not in normalized:
                normalized.append(tag)
    return normalized


def build_content_filters(
    q: Optional[str] = None,
    tags: Optional[Union[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """The single declarative description of the active filters."""
    filters: List[Dict[str, Any]] = []
    if q:
        filters.append(
            {
                "op": "icontains",
                "fields": list(Q_SEARCH_FIELDS),
                "value": q,
            }
        )
    for tag in normalize_tags(tags):
        filters.append({"op": "tag", "fields": ["tags"], "value": tag})
    return filters


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so the value matches literally."""
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def compile_sqlalchemy_filters(filters: List[Dict[str, Any]]) -> List[Any]:
    """Compile declarative filters into SQLAlchemy WHERE clauses."""
    clauses = []
    for filter_ in filters:
        if filter_["op"] == "icontains":
            pattern = f"%{_escape_like(filter_['value'])}%"
            clauses.append(
                or_(
                    *(
                        getattr(Content, field).like(pattern, escape="\\")
                        for field in filter_["fields"]
                    )
                )
            )
        elif filter_["op"] == "tag":
            # whole-token match inside the comma separated column
            pattern = f"%,{_escape_like(filter_['value'])},%"
            clauses.append(
                func.concat(",", Content.tags, ",").like(
                    pattern, escape="\\"
                )
            )
        else:  # pragma: no cover
            raise ValueError(f"Unknown filter op: {filter_['op']}")
    return clauses


def compile_mongo_filters(filters: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compile declarative filters into a MongoDB query document.

    Assumes Mongo documents store `tags` as a real array. Regex
    metacharacters in `q` are escaped so matching is literal, exactly
    like the SQLAlchemy compiler.
    """
    and_clauses = []
    for filter_ in filters:
        if filter_["op"] == "icontains":
            regex = {"$regex": re.escape(filter_["value"])}
            and_clauses.append(
                {"$or": [{field: regex} for field in filter_["fields"]]}
            )
        elif filter_["op"] == "tag":
            and_clauses.append({"tags": filter_["value"]})
        else:  # pragma: no cover
            raise ValueError(f"Unknown filter op: {filter_['op']}")
    if not and_clauses:
        return {}
    if len(and_clauses) == 1:
        return and_clauses[0]
    return {"$and": and_clauses}


def parse_sort(sort: Optional[str] = None) -> Tuple[str, bool]:
    """Parse the `sort` parameter into (field, descending).

    Accepts `field` or `-field`. Raises ValueError for fields outside
    the SORTABLE_FIELDS whitelist.
    """
    if not sort:
        return DEFAULT_SORT_FIELD, False
    descending = sort.startswith("-")
    field = sort[1:] if descending else sort
    if field not in SORTABLE_FIELDS:
        raise ValueError(
            f"Invalid sort field {field!r}, "
            f"allowed: {', '.join(SORTABLE_FIELDS)}"
        )
    return field, descending


def encode_cursor(value: Any, content_id: int) -> str:
    """Encode a keyset cursor as (sort value, id)."""
    payload = json.dumps([value, content_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> Tuple[Any, int]:
    """Decode a keyset cursor back into (sort value, id)."""
    try:
        value, content_id = json.loads(
            base64.urlsafe_b64decode(cursor.encode())
        )
        return value, int(content_id)
    except Exception:
        raise ValueError("Invalid cursor")


def _keyset_sqlalchemy(
    field: str, descending: bool, value: Any, content_id: int
) -> Any:
    """Keyset condition; ties on the sort field fall back to `id`."""
    column = getattr(Content, field)
    tiebreak = and_(column == value, Content.id > content_id)
    if descending:
        return or_(column < value, tiebreak)
    return or_(column > value, tiebreak)


def _keyset_mongo(
    field: str, descending: bool, value: Any, content_id: int
) -> Dict[str, Any]:
    """Same keyset condition as a MongoDB query fragment."""
    operator = "$lt" if descending else "$gt"
    return {
        "$or": [
            {field: {operator: value}},
            {field: value, "id": {"$gt": content_id}},
        ]
    }


def build_content_query(
    *,
    q: Optional[str] = None,
    tags: Optional[Union[str, List[str]]] = None,
    sort: Optional[str] = None,
    offset: int = 0,
    cursor: Optional[str] = None,
    limit: Optional[int] = None,
    count: bool = False,
    backend: str = "sqlalchemy",
) -> Any:
    """The single query constructor every entry point must call.

    With ``backend="sqlalchemy"`` returns a SQLModel select statement
    (a count statement when ``count=True``). With ``backend="mongo"``
    returns a dict with ``filter``/``sort``/``skip``/``limit`` keys.
    """
    filters = build_content_filters(q=q, tags=tags)
    field, descending = parse_sort(sort)
    cursor_value: Tuple[Any, int] = (None, 0)
    if cursor:
        cursor_value = decode_cursor(cursor)

    if backend == "mongo":
        return _build_mongo_query(
            filters, field, descending, cursor_value, offset, limit
        )
    return _build_sqlalchemy_query(
        filters, field, descending, cursor_value, offset, limit, count
    )


def _build_sqlalchemy_query(
    filters, field, descending, cursor_value, offset, limit, count
):
    clauses = compile_sqlalchemy_filters(filters)
    if count:
        statement = select(func.count(Content.id))
    else:
        statement = select(Content)
    if clauses:
        statement = statement.where(*clauses)
    if count:
        return statement
    if cursor_value != (None, 0):
        statement = statement.where(
            _keyset_sqlalchemy(field, descending, *cursor_value)
        )
    column = getattr(Content, field)
    statement = statement.order_by(
        column.desc() if descending else column.asc(), Content.id.asc()
    )
    if offset:
        statement = statement.offset(offset)
    if limit:
        statement = statement.limit(limit)
    return statement


def _build_mongo_query(filters, field, descending, cursor_value, offset, limit):
    query: Dict[str, Any] = {
        "filter": compile_mongo_filters(filters),
        "sort": [(field, -1 if descending else 1), ("id", 1)],
    }
    if cursor_value != (None, 0):
        keyset = _keyset_mongo(field, descending, *cursor_value)
        if query["filter"]:
            query["filter"] = {"$and": [query["filter"], keyset]}
        else:
            query["filter"] = keyset
    if offset:
        query["skip"] = offset
    if limit:
        query["limit"] = limit
    return query
