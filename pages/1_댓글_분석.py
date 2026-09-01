"""YouTube 댓글 분석 2단계: 키워드 자동 제안 + 연구자 수기 확정."""

import hashlib
import json
import re

import pandas as pd
import requests
import streamlit as st


st.set_page_config(page_title="댓글 분석", page_icon="🔎", layout="wide")

# 연구 코드북의 10개 다중분류 범주와, 자동 제안에 사용할 대표 단어입니다.
# 자동 태그는 최종 판정이 아니며 연구자가 표에서 반드시 수정할 수 있습니다.
CATEGORY_KEYWORDS = {
    "정보 질문": ["언제", "며칠", "몇일", "얼마나", "어떻게", "왜", "무엇", "궁금", "알려", "방법"],
    "개인 의사결정 질문": ["뽑아도", "발치해도", "해야 할", "해야하", "해도 될", "해도될", "가능할", "제 경우", "저는"],
    "수술 후 증상·회복": ["통증", "아프", "붓", "부종", "출혈", "피", "개구", "입이 안", "회복", "저림"],
    "합병증·위험 불안": ["신경손상", "신경 손상", "감각이상", "마비", "dry socket", "드라이소켓", "감염", "염증", "사망", "큰일"],
    "자가관리 질문": ["식사", "먹어", "음식", "양치", "칫솔", "가글", "운동", "흡연", "담배", "술", "음주", "약 복용", "진통제"],
    "비용·접근성": ["비용", "가격", "얼마", "보험", "병원", "수면마취", "수면 마취", "예약"],
    "경험 공유": ["저는", "저도", "제가", "뽑았", "발치했", "수술했", "후기", "경험"],
    "정서 표현": ["무서", "걱정", "불안", "공포", "안심", "감사", "고마", "신뢰", "불신", "울었"],
    "조언·오정보 가능성": ["하지 마", "하지마", "무조건", "절대", "추천", "이렇게 하", "저는 .* 하세요"],
    "단순 반응": ["좋은 영상", "감사합니다", "고맙습니다", "잘 봤", "유익", "최고", "👍", "😂", "ㅎㅎ"],
}

QUESTION_TYPES = ["", "정보 요청", "확인 요청", "의사결정 위임"]
UNCERTAINTY_WORDS = ["혹시", "괜찮을까요", "괜찮나", "무서워", "큰일", "걱정", "불안", "될까요"]
TRUST_WORDS = ["의사 선생님", "의사쌤", "전문의", "병원", "치과", "유튜브 보고"]

SHEET_HEADERS = [
    "원본 유튜브 링크", "댓글 원문", "좋아요 수", "작성일", "답글 수", "자동 제안 근거",
    "정보 질문", "개인 의사결정 질문", "수술 후 증상,회복", "합병증,위험 불안",
    "자가관리 질문", "비용,접근성", "경험 공유", "정서 표현", "조언,오정보 가능성",
    "단순 반응", "의문문유형", "불확실성 표현", "신뢰 표지", "비표준 표기,반복,이모티콘",
]

SHEET_BOOLEAN_MAP = {
    "정보 질문": "정보 질문",
    "개인 의사결정 질문": "개인 의사결정 질문",
    "수술 후 증상,회복": "수술 후 증상·회복",
    "합병증,위험 불안": "합병증·위험 불안",
    "자가관리 질문": "자가관리 질문",
    "비용,접근성": "비용·접근성",
    "경험 공유": "경험 공유",
    "정서 표현": "정서 표현",
    "조언,오정보 가능성": "조언·오정보 가능성",
    "단순 반응": "단순 반응",
    "불확실성 표현": "불확실성 표현",
    "신뢰 표지": "신뢰 표지",
    "비표준 표기,반복,이모티콘": "비표준 표기·반복·이모티콘",
}


def get_sheet_url():
    """Streamlit 비밀 금고에서 Google Apps Script 웹 앱 주소를 읽습니다."""
    try:
        return str(st.secrets["SHEET_URL"]).strip()
    except (KeyError, FileNotFoundError):
        return ""


def ox(value):
    """체크 여부를 Google Sheet에 기록할 o/x 값으로 바꿉니다."""
    if pd.isna(value):
        return "x"
    return "o" if bool(value) else "x"


def integer_or_zero(value):
    """빈 숫자 셀도 시트에 안전하게 기록합니다."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def text_or_blank(value):
    """CSV의 빈 텍스트를 nan 같은 문자열로 저장하지 않습니다."""
    return "" if pd.isna(value) else str(value)


def make_sheet_rows(edited_rows):
    """현재 코딩 페이지를 사용자가 지정한 20개 Google Sheet 열로 변환합니다."""
    sheet_rows = []
    for _, row in edited_rows.iterrows():
        item = {
            "원본 유튜브 링크": text_or_blank(row.get("원본 유튜브 링크", "")),
            "댓글 원문": text_or_blank(row.get("댓글 원문", "")),
            "좋아요 수": integer_or_zero(row.get("좋아요 수", 0)),
            "작성일": text_or_blank(row.get("작성일", "")),
            "답글 수": integer_or_zero(row.get("답글 수", 0)),
            "자동 제안 근거": text_or_blank(row.get("자동 제안 근거", "")),
            "의문문유형": text_or_blank(row.get("의문문 유형", "")),
        }
        for sheet_column, coding_column in SHEET_BOOLEAN_MAP.items():
            item[sheet_column] = ox(row.get(coding_column, False))
        sheet_rows.append({header: item.get(header, "") for header in SHEET_HEADERS})
    return sheet_rows


def make_batch_id(sheet_rows):
    """같은 20개가 중복 저장되지 않도록 전송 내용 기반 식별자를 만듭니다."""
    canonical = json.dumps(sheet_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def send_batch_to_sheet(sheet_rows):
    """현재 페이지의 댓글들을 한 번의 POST 요청으로 Google Sheet에 저장합니다."""
    sheet_url = get_sheet_url()
    if not sheet_url:
        return False, "Streamlit Secrets에 SHEET_URL을 추가해 주세요."
    if not sheet_url.startswith("https://") or not sheet_url.rstrip("/").endswith("/exec"):
        return False, "SHEET_URL은 https:// 로 시작하고 /exec 로 끝나는 웹 앱 주소여야 합니다."

    payload = {
        "action": "append_coding_batch",
        "batch_id": make_batch_id(sheet_rows),
        "headers": SHEET_HEADERS,
        "rows": sheet_rows,
    }
    try:
        response = requests.post(sheet_url, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as error:
        return False, f"Google Sheet 접수창구에 연결하지 못했습니다: {error}"
    except ValueError:
        return False, "Google Sheet 접수창구가 JSON 형식으로 응답하지 않았습니다. Apps Script 배포를 확인해 주세요."

    if not result.get("ok"):
        return False, str(result.get("error", "Google Sheet 저장에 실패했습니다."))
    if result.get("duplicate"):
        return True, "이미 저장된 동일한 코딩 페이지입니다. 중복 행은 추가하지 않았습니다."
    return True, f"현재 코딩 페이지 {int(result.get('inserted', len(sheet_rows)))}개를 Google Sheet에 저장했습니다."


def find_matches(text, keywords):
    """문자열 또는 정규표현식 목록에서 실제로 나온 단어를 찾습니다."""
    return [keyword for keyword in keywords if re.search(keyword, text, flags=re.IGNORECASE)]


def suggest_question_type(text):
    """질문 댓글에만 의문문 유형을 제안합니다."""
    if "?" not in text and not re.search(r"(나요|까요|인가요|인가요|어떡하|어떻게 하)", text):
        return ""
    if re.search(r"(뽑아도|발치해도|해야|해도 될|가능할|어떤 게|어느 게)", text):
        return "의사결정 위임"
    if re.search(r"(맞나요|인가요|그런가요|정상인가요|괜찮나요)", text):
        return "확인 요청"
    return "정보 요청"


def make_coding_table(comments):
    """댓글마다 자동 제안 근거와 수정 가능한 최종 코딩 열을 만듭니다."""
    base = comments.copy().reset_index(drop=True)
    if "댓글 원문" not in base.columns:
        raise ValueError("CSV에 '댓글 원문' 열이 필요합니다.")
    base["comment_key"] = [f"comment_{number}" for number in base.index]

    suggestions = []
    for _, row in base.iterrows():
        text = str(row["댓글 원문"])
        matched_categories = {}
        for category, keywords in CATEGORY_KEYWORDS.items():
            matches = find_matches(text, keywords)
            matched_categories[category] = matches
            base.loc[row.name, category] = bool(matches)

        # 다른 범주가 제안된 댓글은 단순 반응으로 자동 제안하지 않습니다.
        if any(matched_categories[category] for category in CATEGORY_KEYWORDS if category != "단순 반응"):
            base.loc[row.name, "단순 반응"] = False

        evidence = [f"{category}: {', '.join(matches)}" for category, matches in matched_categories.items() if matches]
        suggestions.append(" / ".join(evidence) if evidence else "자동 제안 없음")
        base.loc[row.name, "의문문 유형"] = suggest_question_type(text)
        base.loc[row.name, "불확실성 표현"] = bool(find_matches(text, UNCERTAINTY_WORDS))
        base.loc[row.name, "신뢰 표지"] = bool(find_matches(text, TRUST_WORDS))
        base.loc[row.name, "비표준 표기·반복·이모티콘"] = bool(
            re.search(r"[ㅋㅎㅠㅜ]{2,}|[!?]{2,}|[😀-🙏]|\.{3,}", text)
        )

    base["자동 제안 근거"] = suggestions
    base["수기 확정"] = False
    for column in list(CATEGORY_KEYWORDS) + ["불확실성 표현", "신뢰 표지", "비표준 표기·반복·이모티콘", "수기 확정"]:
        base[column] = base[column].fillna(False).astype(bool)
    return base


def coding_source():
    """첫 페이지 세션의 댓글을 우선 사용하고, 없으면 CSV를 받습니다."""
    if "comments" in st.session_state:
        return st.session_state["comments"], "1단계에서 수집한 댓글"
    uploaded = st.file_uploader("댓글 CSV 업로드", type="csv", help="1단계에서 내려받은 CSV를 올려 주세요.")
    if uploaded is None:
        return None, None
    try:
        return pd.read_csv(uploaded), "업로드한 CSV"
    except UnicodeDecodeError:
        uploaded.seek(0)
        return pd.read_csv(uploaded, encoding="cp949"), "업로드한 CSV"


def save_batch(edited_batch):
    """현재 화면의 수정값을 전체 코딩표에 반영합니다."""
    coding = st.session_state["coding_table"]
    for _, row in edited_batch.iterrows():
        target = coding["comment_key"] == row["comment_key"]
        for column in list(CATEGORY_KEYWORDS) + [
            "의문문 유형", "불확실성 표현", "신뢰 표지", "비표준 표기·반복·이모티콘", "수기 확정"
        ]:
            coding.loc[target, column] = row[column]
    st.session_state["coding_table"] = coding


st.title("🔎 댓글 분석 · 키워드 자동 제안과 수기 확정")
st.caption("자동 태그는 연구자의 판단을 돕는 초벌 제안입니다. 최종 분석에는 수기 확정한 댓글만 사용하세요.")

comments, source_label = coding_source()
if comments is None:
    st.info("먼저 ‘영상·댓글 수집’ 페이지에서 댓글을 가져오거나, 내려받은 댓글 CSV를 업로드해 주세요.")
    st.stop()

# 새 수집 파일에는 원본 링크가 포함됩니다. 예전 CSV라면 사용자가 한 번만 입력합니다.
comments = comments.copy()
if "원본 유튜브 링크" not in comments.columns:
    comments["원본 유튜브 링크"] = ""
missing_source = comments["원본 유튜브 링크"].fillna("").astype(str).str.strip() == ""
if missing_source.any():
    current_video_id = st.session_state.get("current_video_id", "")
    default_source_url = f"https://www.youtube.com/watch?v={current_video_id}" if current_video_id else ""
    fallback_source_url = st.text_input(
        "원본 유튜브 링크",
        value=default_source_url,
        placeholder="https://www.youtube.com/watch?v=...",
        help="예전 CSV에 원본 링크 열이 없을 때, 이 CSV에 해당하는 영상 주소를 입력해 주세요.",
    ).strip()
    if not fallback_source_url:
        st.info("Google Sheet에 출처를 남기기 위해 원본 유튜브 링크를 입력해 주세요.")
        st.stop()
    comments.loc[missing_source, "원본 유튜브 링크"] = fallback_source_url

source_signature = (
    len(comments),
    tuple(comments.columns),
    tuple(comments["댓글 원문"].astype(str).head(5)),
    tuple(comments["원본 유튜브 링크"].astype(str).head(5)),
)
if st.session_state.get("coding_source_signature") != source_signature:
    st.session_state["coding_table"] = make_coding_table(comments)
    st.session_state["coding_source_signature"] = source_signature

coding = st.session_state["coding_table"]
confirmed = int(coding["수기 확정"].sum())
st.success(f"{source_label} · 전체 {len(coding):,}개 댓글 중 {confirmed:,}개 수기 확정")
if st.session_state.get("sheet_save_message"):
    st.success(st.session_state.pop("sheet_save_message"))

with st.expander("코딩 원칙과 자동 제안의 한계", expanded=False):
    st.markdown("""- 한 댓글은 여러 범주에 동시에 해당할 수 있으므로 **복수 선택**합니다.
- 자동 제안은 키워드가 실제로 나타난 경우에만 표시합니다. 문맥·부정 표현·비꼼은 사람이 수정합니다.
- ‘단순 반응’은 다른 임상·정서 범주가 없을 때에만 선택하는 것을 권장합니다.
- 최종 빈도는 **수기 확정 댓글 중 해당 범주가 포함된 비율**로 계산합니다.""")

filter_unconfirmed = st.toggle("미확정 댓글만 보기", value=True)
visible = coding[~coding["수기 확정"]].copy() if filter_unconfirmed else coding.copy()
if visible.empty:
    st.info("표시할 댓글이 없습니다. ‘미확정 댓글만 보기’를 끄거나 새로운 댓글을 불러오세요.")
    visible = coding.copy()

page_size = 20
page_count = max(1, (len(visible) + page_size - 1) // page_size)
page = st.number_input("코딩 페이지", min_value=1, max_value=page_count, value=1, step=1)
start = (page - 1) * page_size
batch = visible.iloc[start : start + page_size].copy()

st.subheader(f"수기 코딩 · {start + 1}–{min(start + page_size, len(visible))} / {len(visible):,}개")
st.caption("각 체크박스를 수정한 뒤 아래 저장 버튼을 누르세요. ‘자동 제안 근거’는 수정하지 않습니다.")

editable_columns = [
    "comment_key", "원본 유튜브 링크", "댓글 원문", "좋아요 수", "작성일", "답글 수", "자동 제안 근거",
    *CATEGORY_KEYWORDS.keys(), "의문문 유형", "불확실성 표현", "신뢰 표지", "비표준 표기·반복·이모티콘", "수기 확정",
]
available_columns = [column for column in editable_columns if column in batch.columns]
# 댓글 원문을 Data Editor의 왼쪽 고정 인덱스로 사용합니다.
# 따라서 오른쪽 분류 열을 가로로 움직여도 원문을 계속 볼 수 있습니다.
editor_batch = batch[available_columns].set_index("댓글 원문", drop=True)
edited_batch = st.data_editor(
    editor_batch,
    use_container_width=True,
    hide_index=False,
    height=700,
    disabled=["comment_key", "원본 유튜브 링크", "좋아요 수", "작성일", "답글 수", "자동 제안 근거"],
    column_config={
        "comment_key": None,
        "원본 유튜브 링크": None,
        "_index": st.column_config.TextColumn("댓글 원문", width="large"),
        "자동 제안 근거": st.column_config.TextColumn(width="large"),
        "의문문 유형": st.column_config.SelectboxColumn(options=QUESTION_TYPES),
    },
    key=f"coding_editor_{filter_unconfirmed}_{page}",
)
if st.button(f"현재 페이지 {len(batch)}개를 Google Sheet에 저장", type="primary"):
    edited_rows = edited_batch.reset_index()
    source_by_key = batch.set_index("comment_key")["원본 유튜브 링크"]
    edited_rows["원본 유튜브 링크"] = edited_rows["comment_key"].map(source_by_key).fillna("")
    unconfirmed_count = int((~edited_rows["수기 확정"].fillna(False).astype(bool)).sum())
    if unconfirmed_count:
        st.warning(
            f"아직 ‘수기 확정’하지 않은 댓글이 {unconfirmed_count}개 있습니다. "
            "현재 페이지의 모든 댓글을 확정한 뒤 다시 저장해 주세요."
        )
    else:
        sheet_rows = make_sheet_rows(edited_rows)
        with st.spinner(f"현재 페이지 {len(sheet_rows)}개를 Google Sheet에 저장하는 중입니다..."):
            saved, message = send_batch_to_sheet(sheet_rows)
        if saved:
            save_batch(edited_rows)
            st.session_state["sheet_save_message"] = message
            st.rerun()
        else:
            st.error(message)

st.divider()
st.subheader("확정된 댓글의 현재 요약")
finalized = st.session_state["coding_table"].query("수기 확정 == True")
if finalized.empty:
    st.info("수기 확정한 댓글이 아직 없습니다.")
else:
    summary = pd.DataFrame(
        {
            "범주": list(CATEGORY_KEYWORDS),
            "댓글 수": [int(finalized[category].sum()) for category in CATEGORY_KEYWORDS],
        }
    )
    summary["비율(%)"] = (summary["댓글 수"] / len(finalized) * 100).round(1)
    st.bar_chart(summary.set_index("범주")["댓글 수"])
    st.dataframe(summary, use_container_width=True, hide_index=True)
    st.download_button(
        "수기 확정 코딩표 CSV 내려받기",
        finalized.drop(columns=["comment_key"]).to_csv(index=False).encode("utf-8-sig"),
        file_name="youtube_comment_coding_final.csv",
        mime="text/csv",
    )
