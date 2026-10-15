"""Standalone (external) Streamlit app for public/no-Snowflake-account users.

Reads ONLY the PUBLIC_SAFE aggregated view via the SVC_PUBLIC_APP service account
(key-pair auth). Config comes from st.secrets - never hard-code credentials.
"""
import streamlit as st
import pandas as pd
import plotly.express as px
import pydeck as pdk
import snowflake.connector
from cryptography.hazmat.primitives import serialization

st.set_page_config(page_title="Frisco Traffic Analytics", page_icon="🚦", layout="wide")

st.markdown(
    """
    <style>
      #MainMenu, footer {visibility: hidden;}
      .block-container {padding-top: 2.2rem;}
      .kpi {background: linear-gradient(135deg,#0b2545,#13315c); color:#fff;
            border-radius:14px; padding:16px 20px; box-shadow:0 2px 10px rgba(0,0,0,.10);}
      .kpi .label {font-size:.72rem; text-transform:uppercase; letter-spacing:.07em; opacity:.80;}
      .kpi .value {font-size:1.8rem; font-weight:800; line-height:1.15; white-space:nowrap;}
      h1 {font-weight:800;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🚦 City of Frisco — Traffic Signal Analytics")
st.caption("Live from Snowflake · Jan–Apr 2026 · public, read-only aggregated data")

SOURCE = "FRISCO_DW.PUBLIC_SAFE.TRAFFIC_SUMMARY"
PALETTE = ["#1f6feb", "#2ea043", "#e3b341", "#db6d28", "#8957e5", "#da3633"]


def _private_key_der():
    # Community Cloud: PEM provided inline as a secret. Local: read from a file.
    pwd = st.secrets.get("SNOWFLAKE_PRIVATE_KEY_PWD") or None
    pem = st.secrets.get("SNOWFLAKE_PRIVATE_KEY")
    if pem:
        pem_bytes = pem.encode()
    else:
        with open(st.secrets["SNOWFLAKE_PRIVATE_KEY_PATH"], "rb") as f:
            pem_bytes = f.read()
    key = serialization.load_pem_private_key(
        pem_bytes, password=pwd.encode() if pwd else None
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@st.cache_resource
def get_connection():
    return snowflake.connector.connect(
        account=st.secrets["SNOWFLAKE_ACCOUNT"],
        user=st.secrets["SNOWFLAKE_USER"],
        private_key=_private_key_der(),
        role="PUBLIC_APP_ROLE",
        warehouse="TRAFFIC_PUBLIC_WH",
        database="FRISCO_DW",
        schema="PUBLIC_SAFE",
    )


@st.cache_data(ttl=1800)
def query(sql: str, params: tuple = ()):
    with get_connection().cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    # Snowflake returns Decimal for SUM/COUNT; coerce numeric-only columns to float.
    for c in df.columns:
        if df[c].dtype == object:
            conv = pd.to_numeric(df[c], errors="coerce")
            if len(df) and conv.notna().all():
                df[c] = conv
    return df


def kpi(col, label, value):
    col.markdown(
        f'<div class="kpi"><div class="label">{label}</div>'
        f'<div class="value">{value}</div></div>',
        unsafe_allow_html=True,
    )


def human(n):
    n = float(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return f"{n:,.0f}"


# ---------------- Filters ----------------
st.sidebar.header("Filters")
bounds = query(f"select min(EVENT_DATE) lo, max(EVENT_DATE) hi from {SOURCE}")
lo, hi = bounds.LO[0], bounds.HI[0]
dr = st.sidebar.date_input("Date range", value=(lo, hi), min_value=lo, max_value=hi)
start_date, end_date = dr if isinstance(dr, tuple) and len(dr) == 2 else (lo, hi)

all_types = query(f"select distinct EVENT_TYPE from {SOURCE} order by 1")["EVENT_TYPE"].tolist()
sel_types = st.sidebar.multiselect("Event type", all_types, default=all_types)
st.sidebar.markdown("---")
st.sidebar.caption("Served via a read-only Snowflake service account (PUBLIC_SAFE only). "
                   "No Snowflake login required to view.")

where = "EVENT_DATE between %s and %s"
params = [start_date, end_date]
if sel_types and len(sel_types) < len(all_types):
    where += " and EVENT_TYPE in (" + ",".join(["%s"] * len(sel_types)) + ")"
    params += sel_types
params = tuple(params)

# ---------------- KPIs ----------------
k = query(
    f"select sum(VEHICLE_COUNT) veh, sum(EVENT_COUNT) ev, "
    f"sum(WAIT_SEC_SUM)/nullif(sum(EVENT_COUNT),0) wait, "
    f"count(distinct SIGNAL_ID) sig from {SOURCE} where {where}",
    params,
)
peak = query(
    f"select HOUR_OF_DAY from {SOURCE} where {where} "
    f"group by 1 order by sum(VEHICLE_COUNT) desc limit 1",
    params,
)
veh, ev = int(k.VEH[0] or 0), int(k.EV[0] or 0)
wait, sig = float(k.WAIT[0] or 0), int(k.SIG[0] or 0)
peak_hr = int(peak.HOUR_OF_DAY[0]) if len(peak) else 0

cols = st.columns(5)
kpi(cols[0], "Total vehicles", human(veh))
kpi(cols[1], "Signal events", human(ev))
kpi(cols[2], "Avg wait / event", f"{wait:.1f}s")
kpi(cols[3], "Peak hour", f"{peak_hr:02d}:00")
kpi(cols[4], "Active signals", f"{sig}")
st.write("")


def style(fig, title, height=320):
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=44, b=10),
                      title=title, coloraxis_showscale=False)
    fig.update_xaxes(title=None)
    fig.update_yaxes(title=None)
    return fig


# ---------------- Trend + mix ----------------
a, b = st.columns([2, 1])
daily = query(f"select EVENT_DATE d, sum(VEHICLE_COUNT) veh from {SOURCE} "
              f"where {where} group by 1 order by 1", params)
a.plotly_chart(style(px.area(daily, x="D", y="VEH", template="plotly_white",
               color_discrete_sequence=["#1f6feb"]), "Daily vehicle volume"),
               use_container_width=True)

mix = query(f"select EVENT_TYPE t, sum(EVENT_COUNT) e from {SOURCE} "
            f"where {where} group by 1 order by 2 desc", params)
b.plotly_chart(style(px.pie(mix, names="T", values="E", hole=0.55, template="plotly_white",
               color_discrete_sequence=PALETTE), "Event mix"), use_container_width=True)

# ---------------- Hour bar + heatmap ----------------
a, b = st.columns(2)
hr = query(f"select HOUR_OF_DAY h, sum(VEHICLE_COUNT) veh from {SOURCE} "
           f"where {where} group by 1 order by 1", params)
fig = px.bar(hr, x="H", y="VEH", template="plotly_white", color_discrete_sequence=["#2ea043"])
fig.update_xaxes(dtick=2)
a.plotly_chart(style(fig, "Vehicles by hour of day"), use_container_width=True)

hm = query(f"select DOW_NAME d, HOUR_OF_DAY h, sum(VEHICLE_COUNT) veh from {SOURCE} "
           f"where {where} group by 1,2", params)
order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
pivot = hm.pivot(index="D", columns="H", values="VEH").reindex(order)
b.plotly_chart(style(px.imshow(pivot, aspect="auto", color_continuous_scale="Blues",
               template="plotly_white"), "Volume: day of week × hour"),
               use_container_width=True)

# ---------------- Top intersections + map ----------------
a, b = st.columns(2)
top = query(f"select INTERSECTION_NAME n, sum(VEHICLE_COUNT) veh from {SOURCE} "
            f"where {where} group by 1 order by 2 desc limit 15", params)
fig = px.bar(top.sort_values("VEH"), x="VEH", y="N", orientation="h",
             template="plotly_white", color_discrete_sequence=["#1f6feb"])
a.plotly_chart(style(fig, "Top 15 intersections by volume", height=440),
               use_container_width=True)

geo = query(f"select INTERSECTION_NAME, LATITUDE, LONGITUDE, sum(VEHICLE_COUNT) veh "
            f"from {SOURCE} where {where} group by 1,2,3", params)
geo["LATITUDE"] = geo["LATITUDE"].astype(float)
geo["LONGITUDE"] = geo["LONGITUDE"].astype(float)
geo["VEH"] = geo["VEH"].astype(float)
geo["R"] = 90 + geo["VEH"] / (geo["VEH"].max() or 1) * 300
b.markdown("**Signal map** — marker size = vehicle volume")
b.pydeck_chart(pdk.Deck(
    layers=[pdk.Layer("ScatterplotLayer", geo, get_position=["LONGITUDE", "LATITUDE"],
                      get_radius="R", get_fill_color=[31, 111, 235, 160], pickable=True)],
    initial_view_state=pdk.ViewState(latitude=33.15, longitude=-96.82, zoom=11.0),
    map_style="light",
    tooltip={"text": "{INTERSECTION_NAME}\n{VEH} vehicles"},
))

# ---------------- Detail table ----------------
with st.expander("View / download underlying summary"):
    detail = query(f"select INTERSECTION_NAME, EVENT_TYPE, sum(EVENT_COUNT) events, "
                   f"sum(VEHICLE_COUNT) vehicles from {SOURCE} where {where} "
                   f"group by 1,2 order by 4 desc", params)
    st.dataframe(detail, use_container_width=True, height=300)
    st.download_button("Download CSV", detail.to_csv(index=False),
                       "frisco_traffic_summary.csv", "text/csv")
