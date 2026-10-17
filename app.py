"""Mourinho AI: a half-time board over the team's football clips."""

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import streamlit as st

MATCHES = {
    "0b915801": {
        "title": "England vs Japan",
        "note": "Mitoma, 23'",
        "keep": True,
    },
    "cc3d288e": {
        "title": "PSG vs Inter",
        "note": "Hakimi, 12'",
        "keep": True,
    },
    "cff68f9c": {
        "title": "PSG vs Inter",
        "note": "Hakimi, 12' · earlier upload",
        "keep": False,
    },
}

STEMS = ["20261017_135124_0b915801", "20261017_135119_cc3d288e"]
ACTION_WORDS = ("dribbl", "pass", "goalkeeper", "penalty", "set piece", "tackle")
ANALYSIS_FILE = "FINAL_1_ENG-JPN_analysis.mp4"
RAW_STEM = "20261017_135124_0b915801"
SHAPE = {
    "weakness": "Rest-defence collapse after losing the ball",
    "fix": "Keep 2 CBs + holding midfielder behind the ball + counter-press 5s",
    "beat": "Starve transitions, attack patiently, keep full-back tucked",
    "phases": "Attack, turnover, break, line stretched, goal",
}


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _request(method: str, path: str, token: str | None = None, body: dict | None = None, timeout: int = 90):
    base = _env("VSS_URL").rstrip("/")
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400]
        raise RuntimeError(f"{method} {path} failed ({exc.code}): {detail.decode(errors='replace')}") from exc


@st.cache_resource(show_spinner=False)
def login() -> str:
    payload = _request(
        "POST",
        "/api/v1/auth/login",
        body={"username": _env("VSS_USERNAME"), "password": _env("VSS_PASSWORD")},
    )
    return payload["access_token"]


def _clip_label(filename: str) -> dict:
    for stem, meta in MATCHES.items():
        if stem in (filename or ""):
            return meta
    return {"title": "Match clip", "note": filename, "keep": True}


def _known_sources() -> list[str]:
    user = _env("VSS_USERNAME")
    bucket = f"{user}-vss-chunks-segments"
    sources = []
    for stem in STEMS:
        for number in range(1, 10):
            sources.append(f"s3://{bucket}/segments/{stem}_segment_{number:03d}_of_009.mp4")
    return sources


@st.cache_data(ttl=60, show_spinner=False)
def load_segments(_token: str) -> list[dict]:
    rows = []
    for source in _known_sources():
        query = urllib.parse.urlencode({"source": source})
        try:
            row = _request("GET", f"/api/v1/videos/metadata?{query}", token=_token)
        except RuntimeError:
            continue
        if row.get("reasoning_content"):
            rows.append(row)
    rows.sort(key=lambda row: (row.get("original_video") or "", row.get("segment_number") or 0))
    return rows


def search_moments(token: str, query: str, originals: set[str]) -> list[dict]:
    body = {
        "query": query,
        "top_k": 12,
        "llm_top_n": 1,
        "min_similarity": 0.05,
        "metadata_filters": {"capture_type": "sports"},
        "include_public": True,
    }
    try:
        payload = _request("POST", "/api/v1/search", token=token, body=body, timeout=120)
    except RuntimeError:
        body.pop("metadata_filters")
        payload = _request("POST", "/api/v1/search", token=token, body=body, timeout=120)
    hits = []
    for hit in payload.get("results") or []:
        if originals and hit.get("original_video") not in originals:
            continue
        hits.append(hit)
    return hits[:4]


@st.cache_data(ttl=300, show_spinner=False)
def clip_bytes(_token: str, source: str) -> bytes:
    base = _env("VSS_URL").rstrip("/")
    query = urllib.parse.urlencode({"source": source, "token": _token})
    request = urllib.request.Request(base + "/api/v1/videos/stream?" + query)
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _evidence_text(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        meta = _clip_label(row.get("filename") or "")
        start = float(row.get("segment_start_sec") or 0)
        end = float(row.get("segment_end_sec") or start)
        lines.append(
            f"- Upload title (not a sighting): {meta['title']} — {meta['note']}\n"
            f"  Clock: {start:.0f}s–{end:.0f}s\n"
            f"  Caption: {row.get('reasoning_content')}"
        )
    return "\n".join(lines)


def write_board(rows: list[dict]) -> dict:
    system = (
        "You are the assistant manager writing a half-time board from video captions. "
        "The upload title is only a label for the clip. It is not evidence that a named player is visible. "
        "Use a player name only when that name is written in the caption. "
        "Use a jersey number only when that exact number is written in the caption. "
        "Otherwise label the person by the shirt colour the caption gives, such as 'White shirt on the ball'. "
        "Rank best from the clearest positive action to the next. Rank struggling from the least decisive action. "
        "The why field must paraphrase one sentence from that caption. Do not add skill, pace, mistakes, or chances the caption does not describe. "
        "If a caption does not show a mistake, say the wide shot does not isolate an error. "
        "The team talk must name two or three clocks exactly as written, such as England vs Japan 25s-30s. "
        "The summary must use the shape findings below and at least one Cosmos caption clock. "
        "Do not invent distances, speeds, or pass-completion numbers. "
        "Reply with JSON only, no markdown."
    )
    user = f"""Shape findings from the analysis overlay (use these, do not replace them):
- Weakness: {SHAPE['weakness']}
- Fix: {SHAPE['fix']}
- How to beat Japan: {SHAPE['beat']}
- Phases marked on the tape: {SHAPE['phases']}

Cosmos captions from the clips:

{_evidence_text(rows)}

Return JSON with this shape:
{{
  "best": [{{"player": "", "team": "", "rating": "excellent", "why": "", "when": "England vs Japan 25s-30s"}}],
  "struggling": [{{"player": "", "team": "", "rating": "poor", "why": "", "when": "PSG vs Inter 30s-35s"}}],
  "team_talk": "90 to 130 words the manager can say out loud. Cite the rest-defence fix and two caption clocks.",
  "summary": "70 to 110 words. Connect the rest-defence collapse and the 5-second counter-press to one or two Cosmos clocks."
}}
Put 3 entries in best and 3 in struggling, best first in each list. Ratings are excellent, good, average, or poor.
"""
    body = json.dumps(
        {
            "model": "meta-llama/Llama-3.3-70B-Instruct",
            "temperature": 0.2,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode()
    request = urllib.request.Request(
        "https://api.inference.wandb.ai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {_env('WANDB_API_KEY')}",
            "Content-Type": "application/json",
            "OpenAI-Project": f"{_env('WANDB_TEAM')}/{_env('WANDB_PROJECT')}",
        },
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.loads(response.read())
    text = payload["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;700&family=Source+Serif+4:opsz,wght@8..60,460;8..60,620&display=swap');
        html, body, [class*="css"] { font-family: "Source Serif 4", Georgia, serif; }
        .stApp { background: #101510; color: #f4f0e6; }
        .block-container { max-width: 1180px; padding-top: 1.2rem; padding-bottom: 3rem; }
        header[data-testid="stHeader"] { background: transparent; }
        #MainMenu, footer, [data-testid="stToolbar"] { visibility: hidden; }
        h1, h2, h3, .eyebrow, .section, .player, .meta, .rank { font-family: "Barlow Condensed", sans-serif; }
        .mast { border-bottom: 1px solid #314033; padding-bottom: 0.7rem; margin-bottom: 0.8rem; }
        .mast h1 { font-size: 3.6rem; line-height: 0.85; margin: 0; letter-spacing: 0.03em; color: #f7f3ea; }
        .eyebrow { color: #d7b56a; font-size: 0.92rem; text-transform: uppercase; letter-spacing: 0.12em; margin: 0 0 0.15rem; }
        .sub { color: #c9c2b2; margin: 0.4rem 0 0; font-size: 1.05rem; }
        .section { color: #f7f3ea; font-size: 1.7rem; letter-spacing: 0.04em; margin: 1.15rem 0 0.45rem; }
        .pitch-card, .talk, .clip-card {
          background: #182018; border: 1px solid #314033; border-radius: 14px;
          padding: 0.85rem 1rem 0.75rem; margin-bottom: 0.55rem;
        }
        .pitch-card.best { border-top: 3px solid #d7b56a; }
        .pitch-card.weak { border-top: 3px solid #c47a6a; }
        .rankline { display: flex; gap: 0.7rem; align-items: baseline; }
        .rank {
          color: #d7b56a; font-size: 1.35rem; margin: 0; min-width: 1.2rem;
        }
        .player { font-size: 1.55rem; margin: 0; color: #f7f3ea; letter-spacing: 0.02em; }
        .meta { color: #d7b56a; text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.84rem; margin: 0.15rem 0; }
        .why, .caption { color: #e7e1d4; margin: 0.35rem 0 0.15rem; line-height: 1.45; }
        .report p { margin: 0.15rem 0 0.45rem; }
        .talk h2 { margin: 0 0 0.35rem; font-size: 1.7rem; letter-spacing: 0.03em; }
        .talk p { font-size: 1.14rem; line-height: 1.5; margin: 0; }
        div[data-testid="stTextInput"] input {
          background: #101510; color: #f7f3ea; border: 1px solid #3c4a3a; border-radius: 10px;
          font-size: 1.05rem;
        }
        div[data-testid="stFormSubmitButton"] button {
          background: #d7b56a; color: #101510; border: 0; border-radius: 10px;
          font-family: "Barlow Condensed", sans-serif; letter-spacing: 0.08em;
          text-transform: uppercase; font-size: 1.05rem; padding: 0.35rem 1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _clock(row: dict) -> str:
    start = float(row.get("segment_start_sec") or 0)
    end = float(row.get("segment_end_sec") or start)
    return f"{start:.0f}s–{end:.0f}s"


def _find_row(rows: list[dict], when: str) -> dict | None:
    text = (when or "").lower()
    start_match = re.search(r"(\d+)\s*s", text)
    start = float(start_match.group(1)) if start_match else None
    titled = []
    for row in rows:
        title = _clip_label(row.get("filename") or "")["title"].lower()
        if title not in text:
            continue
        titled.append(row)
        if start is None:
            continue
        seg_start = float(row.get("segment_start_sec") or 0)
        seg_end = float(row.get("segment_end_sec") or seg_start)
        if seg_start <= start < seg_end:
            return row
    if titled and start is not None:
        last = titled[-1]
        if start >= float(last.get("segment_start_sec") or 0):
            return last
    return titled[0] if titled and start is None else None


def _proof_rows(rows: list[dict], board: dict) -> list[tuple[dict, str]]:
    chosen: list[tuple[dict, str]] = []
    seen = set()
    people = list(board.get("best") or []) + list(board.get("struggling") or [])
    for person in people:
        row = _find_row(rows, person.get("when") or "")
        source = (row or {}).get("source")
        if row and source and source not in seen:
            seen.add(source)
            chosen.append((row, person.get("player") or ""))
    if len(chosen) < 3:
        for row in rows:
            source = row.get("source")
            caption = (row.get("reasoning_content") or "").lower()
            if source in seen or not any(word in caption for word in ACTION_WORDS):
                continue
            seen.add(source)
            chosen.append((row, ""))
            if len(chosen) >= 4:
                break
    return chosen[:4]


def _render_people(people: list[dict], tone: str) -> None:
    if not people:
        st.caption("Nothing ranked yet.")
        return
    for index, person in enumerate(people, start=1):
        player = html.escape(person.get("player") or "Unnamed")
        team = html.escape(person.get("team") or "Team not stated")
        rating = html.escape(person.get("rating") or "")
        why = html.escape(person.get("why") or "")
        when = html.escape(person.get("when") or "")
        st.markdown(
            f"<div class='pitch-card {tone}'>"
            "<div class='rankline'>"
            f"<p class='rank'>{index}</p>"
            f"<p class='player'>{player}</p>"
            "</div>"
            f"<p class='meta'>{team} · {rating}</p>"
            f"<p class='why'>{why}</p>"
            f"<p class='meta'>{when}</p>"
            "</div>",
            unsafe_allow_html=True,
        )


def _render_clip(row: dict, token: str, cited: str = "") -> None:
    meta = _clip_label(row.get("filename") or "")
    cited_html = f" · cited for {html.escape(cited)}" if cited else ""
    caption = html.escape(row.get("reasoning_content") or "")
    st.markdown(
        "<div class='clip-card'>"
        f"<p class='meta'>{html.escape(meta['title'])} · {_clock(row)}{cited_html}</p>"
        "<p class='meta'>Cosmos caption</p>"
        f"<p class='caption'>{caption}</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    source = row.get("source")
    if not source:
        return
    try:
        st.video(clip_bytes(token, source))
    except Exception as exc:
        st.caption(f"Clip could not be loaded ({exc}).")


def _analysis_path() -> Path | None:
    here = Path(__file__).resolve().parent
    for path in (
        here / "media" / ANALYSIS_FILE,
        here / ANALYSIS_FILE,
        Path("/code/media") / ANALYSIS_FILE,
        Path("/data") / ANALYSIS_FILE,
    ):
        if path.is_file():
            return path
    return None


def _raw_source() -> str:
    user = _env("VSS_USERNAME")
    return f"s3://{user}-vss-chunks/{user}/{RAW_STEM}.mp4"


def _render_report() -> None:
    st.markdown(
        "<div class='pitch-card weak report'>"
        "<p class='meta'>Weakness</p>"
        f"<p class='player'>{html.escape(SHAPE['weakness'])}</p>"
        "<p class='meta'>Fix</p>"
        f"<p class='why'>{html.escape(SHAPE['fix'])}</p>"
        "<p class='meta'>How to beat Japan</p>"
        f"<p class='why'>{html.escape(SHAPE['beat'])}</p>"
        "<p class='meta'>Phases on the tape</p>"
        f"<p class='why'>{html.escape(SHAPE['phases'])}</p>"
        "</div>",
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="Mourinho AI", page_icon="⚽", layout="wide")
    _css()
    st.markdown(
        """
        <div class="mast">
          <p class="eyebrow">Half-time board</p>
          <h1>MOURINHO AI</h1>
          <p class="sub">England vs Japan. The shape, the weakness, and the clips that prove it.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        token = login()
        rows = load_segments(token)
    except Exception as exc:
        st.error(f"The archive is not reachable yet. {exc}")
        return

    kept = [row for row in rows if _clip_label(row.get("filename") or "").get("keep", True)]
    if not kept:
        st.info("The two match clips are still being written into the index. Refresh in a moment.")
        if st.button("Check again"):
            load_segments.clear()
            st.rerun()
        return

    originals = {row.get("original_video") for row in kept if row.get("original_video")}
    by_match: dict[str, list[dict]] = {}
    for row in kept:
        by_match.setdefault(_clip_label(row.get("filename") or "")["title"], []).append(row)

    if "board" not in st.session_state or "summary" not in (st.session_state.get("board") or {}):
        with st.spinner("Reading the shape notes and the captions…"):
            try:
                st.session_state.board = write_board(kept)
                st.session_state.board_error = ""
            except Exception as exc:
                st.session_state.board_error = str(exc)
                st.session_state.board = st.session_state.get("board") or {}

    board = st.session_state.get("board") or {}
    if st.session_state.get("board_error") and not board.get("summary"):
        st.warning(f"The summary could not be written. {st.session_state.board_error}")

    footage, report = st.columns([3, 2])
    with footage:
        mode = st.radio(
            "Footage",
            ["Analysis overlay", "Raw clip"],
            horizontal=True,
            label_visibility="collapsed",
        )
        if mode == "Raw clip":
            st.caption("England vs Japan, the upload without the tracking overlay.")
            try:
                st.video(clip_bytes(token, _raw_source()))
            except Exception as exc:
                st.caption(f"The raw clip could not be loaded ({exc}).")
        else:
            uploaded = st.file_uploader(
                "Analysis video",
                type=["mp4"],
                label_visibility="collapsed",
                help=ANALYSIS_FILE,
            )
            if uploaded is not None:
                st.session_state.analysis_upload = uploaded.getvalue()
            video = st.session_state.get("analysis_upload")
            path = _analysis_path()
            if video is None and path is not None:
                video = path.read_bytes()
            if video:
                st.caption("Player tracks, team shape, ball arrows, and phase captions.")
                st.video(video)
            else:
                st.markdown(
                    "<div class='clip-card'>"
                    "<p class='meta'>Analysis overlay</p>"
                    f"<p class='player'>{html.escape(ANALYSIS_FILE)}</p>"
                    "<p class='why'>Drop that file above. It is not on this machine yet, "
                    "so the report beside this box is what the tape concludes.</p>"
                    "</div>",
                    unsafe_allow_html=True,
                )
    with report:
        st.markdown("<p class='section'>England vs Japan</p>", unsafe_allow_html=True)
        _render_report()

    talk = board.get("team_talk") or board.get("summary")
    st.markdown("<p class='section'>Half-time team talk</p>", unsafe_allow_html=True)
    if talk:
        st.markdown(f"<div class='talk'><p>{html.escape(talk)}</p></div>", unsafe_allow_html=True)
    else:
        st.caption("The talk will appear once the captions have been read.")

    chips = st.columns(len(by_match) or 1)
    for column, (title, clips) in zip(chips, by_match.items()):
        note = _clip_label(clips[0].get("filename") or "")["note"]
        with column:
            st.markdown(
                "<div class='pitch-card'>"
                f"<p class='meta'>{html.escape(note)}</p>"
                f"<p class='player'>{html.escape(title)}</p>"
                f"<p class='why'>{len(clips)} captioned clips in the index</p>"
                "</div>",
                unsafe_allow_html=True,
            )

    with st.form("manager_search", clear_on_submit=False):
        question = st.text_input(
            "Ask the footage",
            placeholder="Who is dribbling? Where is the goalkeeper?",
        )
        submitted = st.form_submit_button("Search clips")
    if submitted and question.strip():
        st.session_state.search_query = question.strip()

    st.markdown("<p class='section'>Proof clips</p>", unsafe_allow_html=True)
    st.caption("The moments behind the rankings. Each clip plays with its Cosmos caption and clock.")
    proof = _proof_rows(kept, board)
    for pair_start in range(0, len(proof), 2):
        pair = proof[pair_start : pair_start + 2]
        columns = st.columns(len(pair))
        for column, (row, cited) in zip(columns, pair):
            with column:
                _render_clip(row, token, cited)

    query = st.session_state.get("search_query")
    if query:
        st.markdown(
            f"<p class='section'>Clips for “{html.escape(query)}”</p>",
            unsafe_allow_html=True,
        )
        with st.spinner("Searching the two matches…"):
            hits = search_moments(token, query, originals)
        if not hits:
            st.caption("No caption in these two uploads matched that question.")
        else:
            for hit in hits:
                _render_clip(hit, token)

    left, right = st.columns(2)
    with left:
        st.markdown("<p class='section'>Best players</p>", unsafe_allow_html=True)
        _render_people(board.get("best") or [], "best")
    with right:
        st.markdown("<p class='section'>Struggling players</p>", unsafe_allow_html=True)
        _render_people(board.get("struggling") or [], "weak")


if __name__ == "__main__":
    main()
