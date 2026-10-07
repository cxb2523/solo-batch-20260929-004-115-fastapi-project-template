from typing import List, Optional, Union

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse
from sqlmodel import Session, or_, select

from ..db import ActiveSession
from ..models.content import (
    Content,
    ContentIncoming,
    ContentResponse,
    build_content_query,
    encode_cursor,
    parse_sort,
)
from ..security import AuthenticatedUser, User, get_current_user

router = APIRouter()


def _serialize(content: Content) -> ContentResponse:
    return ContentResponse(**content.model_dump())


@router.get("/")
async def list_contents(
    *,
    session: Session = ActiveSession,
    q: Optional[str] = None,
    tags: Optional[List[str]] = Query(None),
    sort: Optional[str] = None,
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    """List contents.

    Without any of the new query parameters this keeps the legacy
    behaviour and returns a bare array with every content. As soon as
    one of q/tags/sort/offset/cursor is present it returns an envelope
    with items, next_cursor and (except in cursor mode) total.
    """
    new_style = (
        q is not None
        or tags is not None
        or sort is not None
        or cursor is not None
        or offset > 0
    )
    if not new_style:
        contents = session.exec(select(Content)).all()
        return [_serialize(content) for content in contents]

    cursor_mode = cursor is not None
    try:
        statement = build_content_query(
            q=q,
            tags=tags,
            sort=sort,
            offset=0 if cursor_mode else offset,
            cursor=cursor or None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    contents = session.exec(statement).all()
    items = [_serialize(content) for content in contents]

    sort_field, _ = parse_sort(sort)
    next_cursor = None
    if contents and len(contents) == limit:
        last = contents[-1]
        next_cursor = encode_cursor(getattr(last, sort_field), last.id)

    envelope = {"items": items, "next_cursor": next_cursor}
    if not cursor_mode:
        # cursor mode skips the full table count on purpose
        count_statement = build_content_query(q=q, tags=tags, count=True)
        envelope["total"] = session.exec(count_statement).one()
    return envelope


@router.get("/search", response_class=HTMLResponse)
async def search_contents_page():
    """A small demo page to exercise the search endpoint manually."""
    return SEARCH_PAGE


@router.get("/{id_or_slug}/", response_model=ContentResponse)
async def query_content(
    *, id_or_slug: Union[str, int], session: Session = ActiveSession
):
    content = session.query(Content).where(
        or_(
            Content.id == id_or_slug,
            Content.slug == id_or_slug,
        )
    )
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")
    return content.first()


@router.post(
    "/", response_model=ContentResponse, dependencies=[AuthenticatedUser]
)
async def create_content(
    *,
    session: Session = ActiveSession,
    request: Request,
    content: ContentIncoming,
):
    # set the ownsership of the content to the current user
    db_content = Content.from_orm(content)
    user: User = get_current_user(request=request)
    db_content.user_id = user.id
    session.add(db_content)
    session.commit()
    session.refresh(db_content)
    return db_content


@router.patch(
    "/{content_id}/",
    response_model=ContentResponse,
    dependencies=[AuthenticatedUser],
)
async def update_content(
    *,
    content_id: int,
    session: Session = ActiveSession,
    request: Request,
    patch: ContentIncoming,
):
    # Query the content
    content = session.get(Content, content_id)
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    # Check the user owns the content
    current_user: User = get_current_user(request=request)
    if content.user_id != current_user.id and not current_user.superuser:
        raise HTTPException(
            status_code=403, detail="You don't own this content"
        )

    # Update the content
    patch_data = patch.dict(exclude_unset=True)
    for key, value in patch_data.items():
        setattr(content, key, value)

    # Commit the session
    session.commit()
    session.refresh(content)
    return content


@router.delete("/{content_id}/", dependencies=[AuthenticatedUser])
def delete_content(
    *, session: Session = ActiveSession, request: Request, content_id: int
):

    content = session.get(Content, content_id)
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")
    # Check the user owns the content
    current_user = get_current_user(request=request)
    if content.user_id != current_user.id and not current_user.superuser:
        raise HTTPException(
            status_code=403, detail="You don't own this content"
        )
    session.delete(content)
    session.commit()
    return {"ok": True}


SEARCH_PAGE = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Content Search</title>
<style>
  body { font-family: sans-serif; margin: 2rem; color: #222; }
  fieldset { border: 1px solid #ccc; border-radius: 6px; margin-bottom: 1rem; }
  legend { font-weight: bold; }
  label { display: inline-block; margin: .3rem .8rem .3rem 0; }
  input, select { padding: .25rem; }
  button { padding: .3rem .8rem; margin-right: .5rem; cursor: pointer; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  th, td { border: 1px solid #ddd; padding: .3rem .5rem; text-align: left;
           font-size: .9rem; }
  th { background: #f3f3f3; }
  #summary { background: #eef6ff; padding: .6rem; border-radius: 6px; }
  #raw { white-space: pre-wrap; background: #fafafa; border: 1px solid #eee;
         padding: .6rem; font-size: .75rem; max-height: 220px;
         overflow: auto; }
  .mode { font-weight: bold; }
</style>
</head>
<body>
<h1>Content Search</h1>

<fieldset>
  <legend>过滤与排序</legend>
  <label>关键词 q <input id="q" placeholder="按字面匹配, 正则元字符无效"></label>
  <label>标签 tags <input id="tags" placeholder="逗号分隔, 多值 AND"></label>
  <label>排序 sort
    <select id="sort">
      <option value="id">id ↑</option>
      <option value="-id">id ↓</option>
      <option value="title">title ↑</option>
      <option value="-title">title ↓</option>
      <option value="slug">slug ↑</option>
      <option value="-slug">slug ↓</option>
      <option value="created_time">created_time ↑</option>
      <option value="-created_time">created_time ↓</option>
      <option value="published">published ↑</option>
      <option value="-published">published ↓</option>
    </select>
  </label>
  <label>每页 limit <input id="limit" type="number" value="5" min="1"
      max="100" style="width:4rem"></label>
  <button id="searchBtn">搜索</button>
</fieldset>

<fieldset>
  <legend>翻页 (当前: <span id="mode" class="mode">offset</span>)</legend>
  <button id="offsetPrev">← offset 上一页</button>
  <button id="offsetNext">offset 下一页 →</button>
  <span>offset = <b id="offsetVal">0</b></span>
  <hr>
  <button id="cursorStart">cursor 模式: 第一页</button>
  <button id="cursorNext">cursor 下一页 →</button>
  <button id="cursorOff">退出 cursor 模式</button>
  <div>next_cursor: <code id="cursorVal">(无)</code></div>
</fieldset>

<div id="summary">尚未查询</div>

<table>
  <thead><tr>
    <th>id</th><th>title</th><th>slug</th><th>tags</th>
    <th>published</th><th>created_time</th>
  </tr></thead>
  <tbody id="rows"></tbody>
</table>

<h3>原始响应</h3>
<div id="raw"></div>

<script>
var state = { offset: 0, cursorMode: false, cursor: null, nextCursor: null };

function el(id) { return document.getElementById(id); }

function buildParams() {
  var p = new URLSearchParams();
  var q = el('q').value.trim();
  if (q) { p.set('q', q); }
  el('tags').value.split(',').map(function(t) { return t.trim(); })
    .filter(Boolean).forEach(function(t) { p.append('tags', t); });
  p.set('sort', el('sort').value);
  p.set('limit', el('limit').value || '10');
  if (state.cursorMode) {
    p.set('cursor', state.cursor === null ? '' : state.cursor);
  } else {
    p.set('offset', String(state.offset));
  }
  return p;
}

function search() {
  fetch('/content/?' + buildParams().toString())
    .then(function(r) { return r.json(); })
    .then(render);
}

function render(data) {
  el('raw').textContent = JSON.stringify(data, null, 2);
  var items, total, nextCursor;
  if (Array.isArray(data)) {
    items = data; total = null; nextCursor = null;
    el('summary').textContent =
      '返回了裸数组 (legacy 模式), 共 ' + items.length + ' 条';
  } else {
    items = data.items || [];
    total = (data.total === undefined) ? null : data.total;
    nextCursor = data.next_cursor || null;
    var totalText = (total === null)
      ? 'total 未返回 (cursor 模式)' : 'total = ' + total;
    el('summary').textContent = totalText +
      ' | 本页 items = ' + items.length +
      ' | next_cursor = ' + (nextCursor || '(无)');
  }
  state.nextCursor = nextCursor;
  el('cursorVal').textContent = nextCursor || '(无)';
  el('offsetVal').textContent = state.offset;
  el('mode').textContent = state.cursorMode ? 'cursor' : 'offset';
  var rows = items.map(function(c) {
    return '<tr><td>' + c.id + '</td><td>' + c.title + '</td><td>' +
      c.slug + '</td><td>' + (c.tags || []).join(', ') + '</td><td>' +
      c.published + '</td><td>' + c.created_time + '</td></tr>';
  }).join('');
  el('rows').innerHTML = rows;
}

el('searchBtn').onclick = function() {
  state.offset = 0; state.cursor = null; search();
};
el('offsetPrev').onclick = function() {
  state.cursorMode = false;
  state.offset = Math.max(0, state.offset -
    parseInt(el('limit').value || '10', 10));
  search();
};
el('offsetNext').onclick = function() {
  state.cursorMode = false;
  state.offset += parseInt(el('limit').value || '10', 10);
  search();
};
el('cursorStart').onclick = function() {
  state.cursorMode = true; state.cursor = null; search();
};
el('cursorNext').onclick = function() {
  state.cursorMode = true;
  if (state.nextCursor) { state.cursor = state.nextCursor; }
  search();
};
el('cursorOff').onclick = function() {
  state.cursorMode = false; state.cursor = null; state.offset = 0;
  search();
};

search();
</script>
</body>
</html>
"""
