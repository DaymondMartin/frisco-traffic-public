"""Standalone (external) Streamlit app for public/no-Snowflake-account users.

Reads ONLY the PUBLIC_SAFE aggregated view via the SVC_PUBLIC_APP service account
(key-pair auth). Config comes from st.secrets - never hard-code credentials.
"""
import streamlit as st
import pandas as pd
import snowflake.connector
from cryptography.hazmat.primitives import serialization

st.set_page_config(page_title="Frisco Traffic - Public", layout="wide")
st.title("Frisco Traffic Signal Insights")

SOURCE = "FRISCO_DW.PUBLIC_SAFE.TRAFFIC_DAILY_SUMMARY"


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
        return pd.DataFrame(cur.fetchall(), columns=cols)


bounds = query(f"select min(EVENT_DATE) lo, max(EVENT_DATE) hi from {SOURCE}")
lo, hi = bounds.LO[0], bounds.HI[0]

c1, c2 = st.columns(2)
start_date = c1.date_input("Start date", value=lo, min_value=lo, max_value=hi)
end_date = c2.date_input("End date", value=hi, min_value=lo, max_value=hi)

by_type = query(
    f"select EVENT_TYPE, sum(EVENT_COUNT) events from {SOURCE} "
    f"where EVENT_DATE between %s and %s group by 1 order by 2 desc",
    (start_date, end_date),
)
st.subheader("Events by type")
st.bar_chart(by_type, x="EVENT_TYPE", y="EVENTS")

top = query(
    f"select INTERSECTION_NAME, sum(EVENT_COUNT) events from {SOURCE} "
    f"where EVENT_DATE between %s and %s group by 1 order by 2 desc limit 20",
    (start_date, end_date),
)
st.subheader("Top 20 intersections")
st.dataframe(top, use_container_width=True)
