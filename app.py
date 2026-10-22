"""
Real Exchange Rate (RER) Explorer
=================================

Streamlit app that pulls monthly CPI and USD exchange rates from the IMF
SDMX 3.0 API and computes the bilateral real exchange rate between any two
economies.

Definitions
-----------
E_i(t)  : IMF rate, local currency units of country i per 1 USD (period avg)
S(t)    : units of Country 2 currency per 1 unit of Country 1 currency
          S(t) = E2(t) / E1(t)
RER(t)  : S(t) * P1(t) / P2(t)
RER*    : mean of RER(t) over valid months in the selected window
Index(t): 100 * RER(t) / RER*

Run with:  streamlit run app.py
"""

from __future__ import annotations

import io
import re
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IMF_BASE = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
START_PERIOD = pd.Period("1976-01", freq="M")
HTTP_TIMEOUT = 60
USER_AGENT = "RER-Explorer/1.0 (Streamlit; educational use)"

CSV_ACCEPT = "application/vnd.sdmx.data+csv;version=2.0.0"
JSON_ACCEPT = "application/vnd.sdmx.data+json;version=2.0.0"
STRUCT_JSON_ACCEPT = "application/vnd.sdmx.structure+json;version=2.0.0"

EURO_AREA = "G163"

# Kellogg / Northwestern purple palette
KELLOGG = {
    "purple": "#4E2A84",      # Northwestern Purple (Purple 100)
    "purple_120": "#401F68",
    "purple_60": "#836EAA",
    "purple_30": "#B6ACD1",
    "purple_10": "#E4E0EE",
    "black_80": "#342F2E",
    "black_50": "#716C6B",
    "black_20": "#BBB8B8",
    "white": "#FFFFFF",
}

# Fallback list used only if the IMF codelist cannot be retrieved.
FALLBACK_COUNTRIES: dict[str, str] = {
    "G163": "Euro area",
    "ARG": "Argentina", "AUS": "Australia", "AUT": "Austria",
    "BEL": "Belgium", "BRA": "Brazil", "CAN": "Canada", "CHE": "Switzerland",
    "CHL": "Chile", "CHN": "China", "COL": "Colombia", "CZE": "Czech Republic",
    "DEU": "Germany", "DNK": "Denmark", "EGY": "Egypt", "ESP": "Spain",
    "FIN": "Finland", "FRA": "France", "GBR": "United Kingdom",
    "GRC": "Greece", "HKG": "Hong Kong SAR", "HUN": "Hungary",
    "IDN": "Indonesia", "IND": "India", "IRL": "Ireland", "ISL": "Iceland",
    "ISR": "Israel", "ITA": "Italy", "JPN": "Japan", "KOR": "Korea",
    "MEX": "Mexico", "MYS": "Malaysia", "NGA": "Nigeria", "NLD": "Netherlands",
    "NOR": "Norway", "NZL": "New Zealand", "PER": "Peru", "PHL": "Philippines",
    "POL": "Poland", "PRT": "Portugal", "ROU": "Romania", "SAU": "Saudi Arabia",
    "SGP": "Singapore", "SWE": "Sweden", "THA": "Thailand", "TUR": "Turkiye",
    "URY": "Uruguay", "USA": "United States", "VNM": "Vietnam",
    "ZAF": "South Africa",
}

# Currency labels for axis titles. Anything not listed falls back to
# "<ISO3> currency", so the label is never wrong, just less specific.
CURRENCY_CODES: dict[str, str] = {
    "G163": "EUR", "ARG": "ARS", "AUS": "AUD", "BRA": "BRL", "CAN": "CAD",
    "CHE": "CHF", "CHL": "CLP", "CHN": "CNY", "COL": "COP", "CZE": "CZK",
    "DNK": "DKK", "EGY": "EGP", "GBR": "GBP", "HKG": "HKD", "HUN": "HUF",
    "IDN": "IDR", "IND": "INR", "ISL": "ISK", "ISR": "ILS", "JPN": "JPY",
    "KOR": "KRW", "MEX": "MXN", "MYS": "MYR", "NGA": "NGN", "NOR": "NOK",
    "NZL": "NZD", "PER": "PEN", "PHL": "PHP", "POL": "PLN", "ROU": "RON",
    "SAU": "SAR", "SGP": "SGD", "SWE": "SEK", "THA": "THB", "TUR": "TRY",
    "URY": "UYU", "USA": "USD", "VNM": "VND", "ZAF": "ZAR",
}


# ---------------------------------------------------------------------------
# Pure helpers: parsing
# ---------------------------------------------------------------------------

def parse_period(value: object) -> Optional[pd.Period]:
    """Convert IMF period strings ('1976-M01', '1976-01', '1976M1') to a monthly Period."""
    if value is None:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d{4})\s*-?\s*M?\s*(\d{1,2})$", s, flags=re.IGNORECASE)
    if not m:
        try:
            return pd.Period(pd.Timestamp(s), freq="M")
        except (ValueError, TypeError):
            return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return pd.Period(year=year, month=month, freq="M")


def _to_series(periods: list, values: list) -> pd.Series:
    """Build a clean, sorted, monthly float Series from raw period/value lists."""
    idx, vals = [], []
    for p, v in zip(periods, values):
        per = parse_period(p)
        if per is None:
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        idx.append(per)
        vals.append(val)
    if not idx:
        return pd.Series(dtype="float64")
    ser = pd.Series(vals, index=pd.PeriodIndex(idx, freq="M"), dtype="float64")
    ser = ser[~ser.index.duplicated(keep="last")].sort_index()
    return ser


def parse_sdmx_csv(text: str) -> pd.Series:
    """Parse an SDMX-CSV response into a monthly Series (TIME_PERIOD -> OBS_VALUE)."""
    df = pd.read_csv(io.StringIO(text), dtype=str)
    cols = {c.upper(): c for c in df.columns}
    if "TIME_PERIOD" not in cols or "OBS_VALUE" not in cols:
        raise ValueError("SDMX-CSV without TIME_PERIOD/OBS_VALUE columns")
    return _to_series(df[cols["TIME_PERIOD"]].tolist(), df[cols["OBS_VALUE"]].tolist())


def parse_sdmx_json(payload: dict) -> pd.Series:
    """Parse an SDMX-JSON (2.0) data message into a monthly Series.

    Supports observations keyed by time index under each series, with the time
    dimension listed in structure(s).dimensions.observation.
    """
    data = payload.get("data", payload)
    datasets = data.get("dataSets") or data.get("datasets") or []
    structures = data.get("structures") or [data.get("structure") or {}]
    if not datasets:
        return pd.Series(dtype="float64")

    structure = structures[0] if structures else {}
    obs_dims = (structure.get("dimensions") or {}).get("observation") or []
    time_dim = next(
        (d for d in obs_dims if str(d.get("id", "")).upper() == "TIME_PERIOD"),
        obs_dims[0] if obs_dims else None,
    )
    time_values = [
        (v.get("id") or v.get("value") or v.get("name")) for v in (time_dim or {}).get("values", [])
    ]

    periods, values = [], []
    series_block = datasets[0].get("series") or {}
    if series_block:
        # Use the first series (our key fully identifies one series).
        first = next(iter(series_block.values()))
        observations = first.get("observations") or {}
    else:
        observations = datasets[0].get("observations") or {}

    for obs_key, obs_val in observations.items():
        t_idx = int(str(obs_key).split(":")[-1])
        if t_idx >= len(time_values):
            continue
        value = obs_val[0] if isinstance(obs_val, list) and obs_val else obs_val
        periods.append(time_values[t_idx])
        values.append(value)
    return _to_series(periods, values)


# ---------------------------------------------------------------------------
# Data access (cached)
# ---------------------------------------------------------------------------

def _get(url: str, accept: str) -> Optional[requests.Response]:
    """GET with standard headers. Returns None for 404/204 (no data)."""
    resp = requests.get(
        url,
        headers={"Accept": accept, "User-Agent": USER_AGENT},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code in (204, 404):
        return None
    resp.raise_for_status()
    return resp


@st.cache_data(ttl=86_400, show_spinner=False)
def fetch_imf_series(dataflow: str, key: str) -> pd.Series:
    """Fetch one monthly IMF series from START_PERIOD onward.

    Tries SDMX-CSV first, then SDMX-JSON. Returns an empty Series when the IMF
    has no data for the key. Raises on network failure so the UI can report it.
    """
    url = f"{IMF_BASE}/data/dataflow/{AGENCY}/{dataflow}/+/{key}"

    series = pd.Series(dtype="float64")
    csv_error: Optional[Exception] = None
    try:
        resp = _get(url, CSV_ACCEPT)
        if resp is None:
            return series
        if resp.text.strip():
            series = parse_sdmx_csv(resp.text)
    except (ValueError, pd.errors.ParserError) as exc:
        csv_error = exc

    if series.empty:
        resp = _get(url, JSON_ACCEPT)
        if resp is None:
            return series
        try:
            series = parse_sdmx_json(resp.json())
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                f"Could not parse IMF response for {dataflow}/{key} "
                f"(CSV error: {csv_error}; JSON error: {exc})"
            ) from exc

    return series[series.index >= START_PERIOD]


@st.cache_data(ttl=86_400, show_spinner=False)
def fetch_country_list() -> dict[str, str]:
    """Return {code: name} for IMF economies (3-letter codes plus the Euro area).

    Falls back to a built-in list if the codelist request fails.
    """
    url = f"{IMF_BASE}/structure/codelist/IMF/CL_COUNTRY/+"
    try:
        resp = _get(url, STRUCT_JSON_ACCEPT)
        if resp is None:
            return dict(FALLBACK_COUNTRIES)
        payload = resp.json()
        data = payload.get("data", payload)
        codelists = data.get("codelists") or []
        codes = codelists[0].get("codes", []) if codelists else []
        out: dict[str, str] = {}
        for c in codes:
            code = str(c.get("id", "")).upper()
            name = c.get("name")
            if not name and isinstance(c.get("names"), dict):
                name = c["names"].get("en") or next(iter(c["names"].values()), None)
            if (re.fullmatch(r"[A-Z]{3}", code) or code == EURO_AREA) and name:
                out[code] = str(name)
        if len(out) < 20:
            return dict(FALLBACK_COUNTRIES)
        out[EURO_AREA] = "Euro area"
        return out
    except (requests.RequestException, ValueError, AttributeError, IndexError):
        return dict(FALLBACK_COUNTRIES)


def fetch_country_data(iso3: str) -> tuple[pd.Series, pd.Series]:
    """Return (exchange rate in LCU per USD, CPI) for one economy.

    The USA rate is set to exactly 1.0 for every month in which a CPI exists
    (and from START_PERIOD to the latest month otherwise).
    """
    cpi = fetch_imf_series("CPI", f"{iso3}.CPI._T.IX.M")
    if iso3 == "USA":
        end = pd.Period(pd.Timestamp.today(), freq="M")
        idx = pd.period_range(START_PERIOD, end, freq="M")
        fx = pd.Series(1.0, index=idx, dtype="float64")
    else:
        fx = fetch_imf_series("ER", f"{iso3}.XDC_USD.PA_RT.M")
    return fx, cpi


# ---------------------------------------------------------------------------
# Pure helpers: economics
# ---------------------------------------------------------------------------

def build_panel(e1: pd.Series, e2: pd.Series, p1: pd.Series, p2: pd.Series) -> pd.DataFrame:
    """Align the four inputs monthly and compute S(t), RER(t) and a validity flag.

    S(t)   = E2(t) / E1(t)          (Country 2 currency per 1 unit of Country 1)
    RER(t) = S(t) * P1(t) / P2(t)
    A month is valid when E1, E2, P1 and P2 are all present and > 0.
    """
    df = pd.concat({"E1": e1, "E2": e2, "P1": p1, "P2": p2}, axis=1).sort_index()
    df = df[df.index >= START_PERIOD]
    pos = lambda s: s.where(s > 0)  # noqa: E731  (non-positive values are invalid)
    e1p, e2p, p1p, p2p = pos(df["E1"]), pos(df["E2"]), pos(df["P1"]), pos(df["P2"])
    df["S"] = e2p / e1p
    df["RER"] = df["S"] * p1p / p2p
    df["valid"] = df[["E1", "E2", "P1", "P2"]].gt(0).all(axis=1)
    df.loc[~df["valid"], "RER"] = float("nan")
    return df


def compute_window(panel: pd.DataFrame, start: pd.Period, end: pd.Period) -> tuple[pd.DataFrame, float]:
    """Slice [start, end], compute RER* over valid months and the Real Index.

    Returns (window DataFrame, RER*). RER* is NaN if no valid month exists.
    """
    win = panel[(panel.index >= start) & (panel.index <= end)].copy()
    valid_rer = win.loc[win["valid"], "RER"]
    rer_star = float(valid_rer.mean()) if not valid_rer.empty else float("nan")
    win["RER_star"] = rer_star
    win["Real_Index"] = 100.0 * win["RER"] / rer_star
    return win, rer_star


def window_to_csv(win: pd.DataFrame, c1: str, c2: str) -> bytes:
    """Format the window for download, keeping full numeric precision."""
    out = win.copy()
    out.insert(0, "month", out.index.astype(str))
    out = out.rename(columns={
        "E1": f"E1_{c1}_per_USD",
        "E2": f"E2_{c2}_per_USD",
        "S": f"S_{c2}_per_1_{c1}",
        "P1": f"P1_CPI_{c1}",
        "P2": f"P2_CPI_{c2}",
        "RER": "RER",
        "RER_star": "RER_star",
        "Real_Index": "Real_Index",
        "valid": "valid_month",
    })
    return out.to_csv(index=False, float_format="%.12g").encode("utf-8")


def currency_label(iso3: str) -> str:
    """Short currency label for axis titles."""
    return CURRENCY_CODES.get(iso3, f"{iso3} currency")


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def inject_css() -> None:
    """Apply the Kellogg palette to the Streamlit page."""
    k = KELLOGG
    st.markdown(
        f"""
        <style>
        .stApp {{ background-color: {k['white']}; color: {k['black_80']}; }}
        h1, h2, h3 {{ color: {k['purple']}; }}
        [data-testid="stSidebar"] {{ background-color: {k['purple_120']}; }}
        [data-testid="stSidebar"] * {{ color: {k['white']} !important; }}
        [data-testid="stSidebar"] [data-baseweb="select"] *,
        [data-testid="stSidebar"] input {{ color: {k['black_80']} !important; }}
        .kellogg-band {{
            background: linear-gradient(90deg, {k['purple']} 0%, {k['purple_120']} 100%);
            color: {k['white']}; padding: 1.1rem 1.4rem; border-radius: 6px;
            margin-bottom: 1.2rem;
        }}
        .kellogg-title {{
            color: {k['white']}; margin: 0; font-size: 2rem; font-weight: 700;
            line-height: 1.2;
        }}
        .kellogg-sub {{ color: {k['purple_10']}; margin: .3rem 0 0 0; font-size: 1.05rem; }}
        [data-testid="stMetric"] {{
            background-color: {k['purple_10']}; border-left: 4px solid {k['purple']};
            padding: .6rem .8rem; border-radius: 4px;
        }}
        [data-testid="stMetricValue"] {{ color: {k['purple']}; font-size: 1.7rem; }}
        .stButton > button, .stDownloadButton > button {{
            background-color: {k['purple']}; color: {k['white']};
            border: 1px solid {k['purple']}; border-radius: 4px;
        }}
        .stButton > button:hover, .stDownloadButton > button:hover {{
            background-color: {k['purple_120']}; color: {k['white']};
            border-color: {k['purple_120']};
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def make_chart(win: pd.DataFrame, mode: str, c1: str, c2: str,
               name1: str, name2: str, log_nominal: bool) -> go.Figure:
    """Build the Plotly chart for Real, Nominal or Both (dual y-axes)."""
    k = KELLOGG
    x = win.index.to_timestamp()
    nominal_title = f"S(t): {currency_label(c2)} per 1 {currency_label(c1)}"

    real_trace = go.Scatter(
        x=x, y=win["Real_Index"], name="Real Index (RER*=100)",
        mode="lines", line=dict(color=k["purple"], width=2.4),
        hovertemplate="%{x|%b %Y}<br>Real Index: %{y:.2f}<extra></extra>",
        connectgaps=False,
    )
    nominal_trace = go.Scatter(
        x=x, y=win["S"], name="Nominal S(t)",
        mode="lines", line=dict(color=k["black_50"], width=2, dash="dot"),
        hovertemplate="%{x|%b %Y}<br>S(t): %{y:,.6g}<extra></extra>",
        connectgaps=False,
    )

    if mode == "Both":
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(real_trace, secondary_y=False)
        fig.add_trace(nominal_trace, secondary_y=True)
        fig.update_yaxes(title_text="Real Index (RER* = 100)", secondary_y=False,
                         title_font=dict(color=k["purple"]), tickfont=dict(color=k["purple"]))
        fig.update_yaxes(title_text=nominal_title, secondary_y=True, showgrid=False,
                         type="log" if log_nominal else "linear", exponentformat="power",
                         title_font=dict(color=k["black_50"]), tickfont=dict(color=k["black_50"]))
        fig.add_hline(y=100, line=dict(color=k["purple_30"], dash="dash", width=1))
    elif mode == "Real":
        fig = go.Figure(real_trace)
        fig.update_yaxes(title_text="Real Index (RER* = 100)")
        fig.add_hline(y=100, line=dict(color=k["purple_30"], dash="dash", width=1))
    else:
        fig = go.Figure(nominal_trace)
        fig.update_yaxes(title_text=nominal_title, type="log" if log_nominal else "linear",
                         exponentformat="power")

    fig.update_layout(
        title=dict(text=f"{name1} vs {name2}", font=dict(color=k["purple"], size=18)),
        plot_bgcolor=k["white"], paper_bgcolor=k["white"],
        font=dict(color=k["black_80"]),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=60, r=60, t=80, b=40),
        height=520,
    )
    fig.update_xaxes(showgrid=True, gridcolor=k["purple_10"], linecolor=k["black_20"],
                     rangeslider=dict(visible=True, thickness=0.06))
    fig.update_yaxes(gridcolor=k["purple_10"], linecolor=k["black_20"], zeroline=False)
    return fig


def pick_country(label: str, countries: dict[str, str], default: str, key: str) -> str:
    """Sidebar selector with an optional manual ISO3 override."""
    codes = sorted(countries, key=lambda c: (c != EURO_AREA, countries[c]))
    idx = codes.index(default) if default in codes else 0
    choice = st.sidebar.selectbox(
        label, codes, index=idx, key=key,
        format_func=lambda c: f"{countries[c]} ({c})",
    )
    manual = st.sidebar.text_input(
        f"{label}: other IMF code (optional)", value="", key=f"{key}_manual",
        max_chars=4, placeholder="e.g. KEN",
    ).strip().upper()
    return manual if manual else choice


def main() -> None:
    """Streamlit entry point."""
    st.set_page_config(page_title="Real Exchange Rate Explorer", page_icon="📈", layout="wide")
    inject_css()
    st.markdown(
        """
        <div class="kellogg-band">
          <div class="kellogg-title">Real Exchange Rate Explorer</div>
          <div class="kellogg-sub">Bilateral real exchange rates from IMF monthly CPI and exchange-rate data</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    countries = fetch_country_list()

    st.sidebar.header("Countries")
    c1 = pick_country("Country 1", countries, "USA", "c1")
    c2 = pick_country("Country 2", countries, "BRA", "c2")
    name1, name2 = countries.get(c1, c1), countries.get(c2, c2)

    if c1 == c2:
        st.warning("Choose two different economies.")
        st.stop()

    try:
        with st.spinner(f"Fetching IMF data for {name1} and {name2}..."):
            e1, p1 = fetch_country_data(c1)
            e2, p2 = fetch_country_data(c2)
    except requests.RequestException as exc:
        st.error(f"Could not reach the IMF API: {exc}")
        st.stop()
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()

    missing = [lbl for lbl, s in [
        (f"exchange rate for {name1}", e1), (f"CPI for {name1}", p1),
        (f"exchange rate for {name2}", e2), (f"CPI for {name2}", p2),
    ] if s.empty]
    if missing:
        st.error("The IMF returned no monthly data for: " + "; ".join(missing) + ".")
        st.stop()

    panel = build_panel(e1, e2, p1, p2)
    valid_months = panel.index[panel["valid"]]
    if len(valid_months) == 0:
        st.error("These two economies have no month in which both CPIs and both exchange rates exist.")
        st.stop()

    months = list(panel.loc[valid_months.min():valid_months.max()].index)
    st.sidebar.header("Sample period")
    start, end = st.sidebar.select_slider(
        "Start and end month",
        options=months,
        value=(months[0], months[-1]),
        format_func=lambda p: p.strftime("%b %Y"),
    )

    win, rer_star = compute_window(panel, start, end)

    # Nominal range: if S(t) spans more than two orders of magnitude (hyperinflation
    # or currency redenominations), default the nominal axis to log scale.
    s_pos = win["S"].dropna()
    s_pos = s_pos[s_pos > 0]
    s_span = float(s_pos.max() / s_pos.min()) if not s_pos.empty else 1.0
    wide_nominal = s_span > 100

    st.sidebar.header("Chart")
    mode = st.sidebar.radio("Show", ["Real", "Nominal", "Both"], index=2, horizontal=True)
    log_nominal = st.sidebar.checkbox(
        "Log scale for nominal S(t)", value=wide_nominal,
        key=f"log_{c1}_{c2}_{start}_{end}", disabled=(mode == "Real"),
    )
    n_valid = int(win["valid"].sum())
    n_dropped = int((~win["valid"]).sum())

    if n_valid == 0:
        st.warning("No valid months in the selected window. Widen the sample.")
        st.stop()

    first_valid = win.index[win["valid"]].min()
    last_valid = win.index[win["valid"]].max()

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("RER*", f"{rer_star:,.6g}",
              help="Level depends on CPI base years, so only the index is comparable over time.")
    m2.metric("Valid months", f"{n_valid:,}")
    m3.metric("First valid month", first_valid.strftime("%b %Y"))
    m4.metric("Last valid month", last_valid.strftime("%b %Y"))
    m5.metric("Months dropped (gaps)", f"{n_dropped:,}")

    if s_span > 1_000:
        st.info(
            f"Nominal S(t) moves by a factor of about {s_span:,.3g} over this window. "
            "The IMF expresses old exchange rates in today's currency units, so for "
            "countries that redenominated (for example Brazil, which cut zeros from its "
            "currency several times before the real in 1994) early values look extreme. "
            "That is consistent with the data, and the Real Index is unaffected because "
            "the CPI is chained the same way. The log scale makes the nominal path readable."
        )

    st.plotly_chart(make_chart(win, mode, c1, c2, name1, name2, log_nominal),
                    width="stretch")

    st.caption(
        f"S(t) = {currency_label(c2)} per 1 {currency_label(c1)}. "
        f"Real Index above 100: {name1} is more expensive relative to {name2} "
        f"than its average over the sample (real appreciation of Country 1)."
    )

    with st.expander("Formulas and data notes"):
        st.markdown(
            f"""
- **E1(t), E2(t)**: IMF period-average rates, local currency per USD (`{{ISO3}}.XDC_USD.PA_RT.M`, dataflow `ER`). USA is fixed at 1.0.
- **P1(t), P2(t)**: IMF headline CPI index (`{{ISO3}}.CPI._T.IX.M`, dataflow `CPI`).
- **S(t) = E2(t) / E1(t)**: {currency_label(c2)} per 1 {currency_label(c1)}.
- **RER(t) = S(t) × P1(t) / P2(t)**
- **RER\\*** = mean of RER(t) over the {n_valid} valid months between {start.strftime('%b %Y')} and {end.strftime('%b %Y')} = {rer_star:,.10g}
- **Real Index(t) = 100 × RER(t) / RER\\***
- A month is valid only when all four inputs exist and are positive. Invalid months are kept in the CSV with blank RER and Real Index.
            """
        )

    st.subheader("Data")
    display = win.copy()
    display.index = display.index.astype(str)
    st.dataframe(display, width="stretch", height=320)

    st.download_button(
        "Download CSV for selected period",
        data=window_to_csv(win, c1, c2),
        file_name=f"rer_{c1}_{c2}_{start}_{end}.csv",
        mime="text/csv",
    )

    st.caption("Source: International Monetary Fund, SDMX 3.0 API (dataflows CPI and ER).")


if __name__ == "__main__":
    main()
