import re
import json
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components


st.set_page_config(page_title="사랑니 YouTube 댓글 수집", page_icon="🦷", layout="wide")


def get_api_key():
    """Streamlit Cloud의 비밀 금고에서 API 키를 안전하게 읽습니다."""
    try:
        return st.secrets["YOUTUBE_API_KEY"]
    except (KeyError, FileNotFoundError):
        return None


def extract_video_id(url):
    """일반 YouTube 주소와 youtu.be 짧은 주소에서 영상 ID만 꺼냅니다."""
    try:
        parsed = urlparse(url.strip())
        hostname = parsed.netloc.lower().replace("www.", "")

        if hostname == "youtu.be":
            return parsed.path.strip("/").split("/")[0] or None
        if hostname in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                return parse_qs(parsed.query).get("v", [None])[0]
            if parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
                return parsed.path.strip("/").split("/")[1]
    except (IndexError, ValueError):
        pass
    return None


def duration_to_seconds(value):
    """API가 주는 PT1M30S 형식의 길이를 초 단위로 바꿉니다."""
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(number or 0) for number in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_text(seconds):
    """표에서 읽기 쉬운 영상 길이 표기입니다."""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def copy_for_excel_button(dataframe, button_id, label):
    """탭으로 나눈 표를 클립보드에 넣어 Excel에 바로 붙여넣게 합니다."""
    tab_separated = dataframe.to_csv(sep="\t", index=False)
    # 댓글에 HTML처럼 보이는 문자가 있어도 버튼의 스크립트가 깨지지 않게 처리합니다.
    payload = json.dumps(tab_separated, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    components.html(
        f"""
        <button id="{button_id}" style="padding:0.4rem 0.75rem; border:1px solid #d0d7de;
        background:white; border-radius:0.4rem; cursor:pointer; font-size:0.9rem;">{label}</button>
        <span id="{button_id}-status" style="margin-left:0.5rem; color:#16803c;"></span>
        <script>
        const button = document.getElementById({json.dumps(button_id)});
        button.addEventListener('click', async () => {{
          try {{
            await navigator.clipboard.writeText({payload});
            document.getElementById({json.dumps(button_id + '-status')}).textContent = '복사했습니다. Excel에 붙여넣으세요.';
          }} catch (error) {{
            document.getElementById({json.dumps(button_id + '-status')}).textContent = '복사에 실패했습니다. 표를 직접 선택해 복사해 주세요.';
          }}
        }});
        </script>
        """,
        height=48,
    )


def set_selected_video_url():
    """검색 결과 선택 상자의 URL을 댓글 수집 입력칸으로 옮깁니다."""
    selected = st.session_state.get("video_picker")
    if selected:
        st.session_state["video_url_input"] = selected[1]


def api_get(endpoint, params):
    """YouTube Data API에 요청하고, 실패 이유를 한국어로 돌려줍니다."""
    try:
        response = requests.get(endpoint, params=params, timeout=20)
        payload = response.json()
    except requests.RequestException:
        return None, "YouTube 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
    except ValueError:
        return None, "YouTube 서버에서 예상하지 못한 응답을 받았습니다."

    if response.ok:
        return payload, None

    reason = payload.get("error", {}).get("errors", [{}])[0].get("reason", "")
    if reason == "commentsDisabled":
        return None, "이 영상은 댓글이 비공개이거나 댓글 기능이 꺼져 있어 가져올 수 없습니다. 다른 영상을 선택해 주세요."
    if reason in {"keyInvalid", "forbidden"}:
        return None, "YouTube API 키를 확인해 주세요. Streamlit secrets의 YOUTUBE_API_KEY 설정 또는 API 사용 설정이 필요합니다."
    if reason == "videoNotFound":
        return None, "영상을 찾을 수 없습니다. 링크가 올바른지 확인해 주세요."
    return None, "댓글을 가져오지 못했습니다. 비공개 영상·연령 제한·API 할당량 등의 사유일 수 있습니다."


def search_videos(query, api_key, max_candidates=250):
    """여러 검색 페이지를 자동 수집한 뒤 현재 조회수 기준으로 전체 정렬합니다."""
    ids = []
    seen_ids = set()
    next_page_token = None
    pages_loaded = 0
    max_pages = max(1, (max_candidates + 49) // 50)
    partial_warning = None

    # search.list는 한 번에 최대 50개만 반환하므로 페이지를 내부에서 자동 순회합니다.
    while pages_loaded < max_pages:
        search_params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "viewCount",
            "maxResults": min(50, max_candidates - pages_loaded * 50),
            "relevanceLanguage": "ko",
            "regionCode": "KR",
            "key": api_key,
        }
        if next_page_token:
            search_params["pageToken"] = next_page_token

        search_data, error = api_get(
            "https://www.googleapis.com/youtube/v3/search",
            search_params,
        )
        if error:
            if ids:
                partial_warning = f"추가 페이지를 불러오지 못해 {pages_loaded}페이지까지만 수집했습니다: {error}"
                break
            return [], error, pages_loaded, 0, None

        pages_loaded += 1
        for item in search_data.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            if video_id and video_id not in seen_ids:
                seen_ids.add(video_id)
                ids.append(video_id)

        next_page_token = search_data.get("nextPageToken")
        if not next_page_token:
            break

    if not ids:
        return [], "검색 결과가 없습니다. 검색어를 조금 바꾸어 보세요.", pages_loaded, 0, None

    # videos.list 역시 ID를 최대 50개씩 조회하고, 여기서 최신 조회수를 가져옵니다.
    detail_items = []
    for start in range(0, len(ids), 50):
        details, error = api_get(
            "https://www.googleapis.com/youtube/v3/videos",
            {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(ids[start : start + 50]),
                "key": api_key,
            },
        )
        if error:
            return [], error, pages_loaded, len(ids), None
        detail_items.extend(details.get("items", []))

    rows = []
    for item in detail_items:
        snippet = item["snippet"]
        statistics = item.get("statistics", {})
        title = snippet.get("title", "")
        duration_seconds = duration_to_seconds(item.get("contentDetails", {}).get("duration"))
        rows.append(
            {
                "video_id": item["id"],
                "영상 제목": title,
                "채널명": snippet.get("channelTitle", ""),
                "게시일": snippet.get("publishedAt", "")[:10],
                "영상 길이": seconds_to_text(duration_seconds),
                "duration_seconds": duration_seconds,
                "조회수": int(statistics.get("viewCount", 0)),
                "좋아요 수": int(statistics.get("likeCount", 0)),
                "댓글 수": int(statistics.get("commentCount", 0)),
                "링크": f"https://www.youtube.com/watch?v={item['id']}",
            }
        )
    # 연구자가 요청한 두 가지 명확한 기준만 자동 적용합니다.
    # 그 외 포함·제외 기준은 연구자가 직접 영상 내용을 보고 판단합니다.
    filtered_rows = [
        row
        for row in rows
        if row["duration_seconds"] >= 60
        and "#shorts" not in row["영상 제목"].lower()
    ]
    # 검색 페이지별 순서를 신뢰하지 않고, 모든 페이지를 합친 뒤 최신 조회수로 다시 정렬합니다.
    return (
        sorted(filtered_rows, key=lambda row: row["조회수"], reverse=True),
        None,
        pages_loaded,
        len(ids),
        partial_warning,
    )


def fetch_comments(video_id, api_key):
    """최상위 댓글을 관련성(대체로 좋아요)순으로 최대 100개 수집합니다."""
    data, error = api_get(
        "https://www.googleapis.com/youtube/v3/commentThreads",
        {
            "part": "snippet",
            "videoId": video_id,
            "order": "relevance",
            "maxResults": 100,
            "textFormat": "plainText",
            "key": api_key,
        },
    )
    if error:
        return None, error

    rows = []
    for item in data.get("items", []):
        thread = item["snippet"]
        comment = thread["topLevelComment"]["snippet"]
        rows.append(
            {
                "댓글 원문": comment.get("textOriginal", ""),
                "좋아요 수": int(comment.get("likeCount", 0)),
                "작성일": comment.get("publishedAt", "")[:10],
                "답글 수": int(thread.get("totalReplyCount", 0)),
            }
        )
    return pd.DataFrame(rows).sort_values("좋아요 수", ascending=False, ignore_index=True), None


st.title("🦷 사랑니 YouTube 댓글 수집 · 1단계")
st.caption("조회수순으로 영상을 고르고, 인기순 최상위 댓글 최대 100개를 수집합니다.")

api_key = get_api_key()
if not api_key:
    st.error("YouTube API 키가 설정되지 않았습니다. Streamlit Cloud의 Secrets에 YOUTUBE_API_KEY를 추가해 주세요.")
    st.stop()

with st.expander("연구 설계: 포함·제외 기준과 수집 변수", expanded=False):
    left, right = st.columns(2)
    with left:
        st.markdown("""**포함 기준**

- 한국어 제목 또는 설명을 가진 영상
- 사랑니 발치 또는 매복 사랑니 발치가 주제인 영상
- 일반 환자·보호자 대상의 설명 또는 경험 영상
- 길이 1분 이상, 댓글이 공개된 영상""")
    with right:
        st.markdown("""**제외 기준**

- Shorts
- 술기 중심의 전문가 대상 영상
- 환자교육 내용이 거의 없는 수술 장면 영상
- 동일 영상의 재업로드 또는 사랑니가 주제가 아닌 영상""")
    st.markdown("""**수집 변수**

- 영상: 제목, 채널명, 게시일, 길이, 조회수, 좋아요 수, 댓글 수, 제작자 유형(수기 분류)
- 댓글: 댓글 원문, 작성일, 좋아요 수, 답글 수
- 본 단계의 댓글 표본: 영상당 **최대 100개 최상위 댓글**, `relevance` 요청값으로 수집 후 좋아요 수 기준 재정렬""")

st.subheader("1. 검색어로 영상 고르기")
query = st.text_input("YouTube 검색어", value="사랑니 발치", placeholder="예: 매복 사랑니 발치")
max_candidates = st.select_slider(
    "한 번에 확인할 최대 검색 결과",
    options=[50, 100, 250, 500],
    value=250,
    help="YouTube API가 한 요청당 최대 50개를 반환하므로 앱이 여러 페이지를 자동으로 불러옵니다.",
)
st.caption("한 번만 클릭하면 선택한 수만큼 자동 수집한 뒤, 현재 조회수 기준으로 전체 목록을 다시 정렬합니다.")
if st.button("조회수순 영상 검색", type="primary"):
    if not query.strip():
        st.warning("검색어를 입력해 주세요.")
    else:
        with st.spinner("여러 검색 페이지를 자동으로 불러와 전체 조회수순으로 정렬하는 중입니다..."):
            videos, error, pages_loaded, raw_count, partial_warning = search_videos(
                query.strip(), api_key, max_candidates=max_candidates
            )
        if error:
            st.error(error)
        else:
            st.session_state["video_candidates"] = videos
            st.session_state["search_query"] = query.strip()
            st.session_state["search_pages_loaded"] = pages_loaded
            st.session_state["search_raw_count"] = raw_count
            st.session_state["search_partial_warning"] = partial_warning
            st.session_state.pop("video_url_input", None)
            st.session_state.pop("video_picker", None)
            st.session_state.pop("comments", None)

candidates = st.session_state.get("video_candidates", [])
if candidates:
    st.success(
        f"검색 {st.session_state.get('search_pages_loaded', 0)}페이지에서 "
        f"중복 제거 후 {st.session_state.get('search_raw_count', len(candidates))}개를 확인했고, "
        f"포함 기준을 자동 적용한 후보 {len(candidates)}개를 전체 조회수순으로 정렬했습니다."
    )
    if st.session_state.get("search_partial_warning"):
        st.warning(st.session_state["search_partial_warning"])
    st.caption("포함·제외 기준은 표와 영상을 직접 확인하여 연구자가 최종 판단합니다.")
    candidate_table = pd.DataFrame(candidates).drop(columns=["video_id", "duration_seconds"])
    st.dataframe(
        candidate_table,
        use_container_width=True,
        hide_index=True,
    )
    copy_for_excel_button(candidate_table, "copy-videos", "영상 목록을 Excel용으로 복사")

st.subheader("2. 댓글 가져오기")
if candidates:
    picker_options = [("", "", "직접 링크 붙여넣기")] + [
        (row["video_id"], row["링크"], f"{row['영상 제목']}  |  조회수 {row['조회수']:,}")
        for row in candidates
    ]
    st.selectbox(
        "검색 결과에서 영상 선택",
        options=picker_options,
        format_func=lambda option: option[2],
        key="video_picker",
        on_change=set_selected_video_url,
        help="여기서 고르면 아래 링크 입력칸에 자동으로 채워집니다.",
    )
video_url = st.text_input(
    "선택한 영상 링크",
    key="video_url_input",
    placeholder="https://www.youtube.com/watch?v=... 또는 https://youtu.be/...",
    help="검색 결과 대신 직접 붙여넣을 수도 있습니다. si= 등의 뒤쪽 값은 자동으로 무시됩니다.",
)

if st.button("인기순 댓글 최대 100개 가져오기"):
    video_id = extract_video_id(video_url)
    if not video_id:
        st.warning("YouTube 영상 링크를 확인해 주세요. youtu.be 또는 youtube.com/watch 주소를 넣을 수 있습니다.")
    else:
        with st.spinner("댓글을 가져오는 중입니다..."):
            comments, error = fetch_comments(video_id, api_key)
        if error:
            st.error(error)
        elif comments.empty:
            st.info("공개된 최상위 댓글이 없습니다. 다른 영상을 선택해 주세요.")
        else:
            st.session_state["comments"] = comments
            st.session_state["current_video_id"] = video_id

comments = st.session_state.get("comments")
if comments is not None:
    st.subheader("3. 수집 결과")
    st.metric("가져온 댓글 수", f"{len(comments):,}개")
    st.dataframe(comments, use_container_width=True, hide_index=True, height=520)
    copy_for_excel_button(comments, "copy-comments", "댓글 표를 Excel용으로 복사")
    st.download_button(
        "댓글 CSV 내려받기",
        comments.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"youtube_comments_{st.session_state['current_video_id']}.csv",
        mime="text/csv",
    )
    st.info("다음 단계에서는 이 CSV를 pages의 ‘댓글 분석’ 화면으로 넘겨 5개 임상 범주와 불안 표현을 분류합니다.")
