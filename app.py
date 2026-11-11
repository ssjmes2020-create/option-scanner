import streamlit as st
from SmartApi import SmartConnect
import pyotp
import pandas as pd
import requests
import time
from datetime import datetime

st.set_page_config(page_title="Option Buying Scanner", layout="wide")
st.title("🎯 4-Pillar Option Buying Scanner - Angel One")

with st.sidebar:
    st.header("🔐 Angel One सेटिंग्ज")
    idx = st.selectbox("इंडेक्स निवडा", ["NIFTY", "BANKNIFTY"])
    api_key = st.text_input("API Key", type="password")
    client_id = st.text_input("Client ID")
    pin = st.text_input("MPIN", type="password")
    totp_key = st.text_input("TOTP Secret Key", type="password")
    tg_token = st.text_input("Telegram Bot Token (ऐच्छिक)", type="password")
    tg_chat = st.text_input("Telegram Chat ID (ऐच्छिक)")
    start_scan = st.checkbox("🟢 स्कॅनर चालू करा")

def send_tg(msg):
    if tg_token and tg_chat:
        try:
            requests.post(f"https://telegram.org{tg_token}/sendMessage", json={"chat_id": tg_chat, "text": msg}, timeout=5)
        except Exception:
            pass

@st.cache_data(ttl=3600)
def get_master():
    r = requests.get("https://angelone.in", timeout=20)
    return pd.DataFrame(r.json())

def check_vwap_vol(api, token):
    f_dt = datetime.now().strftime("%Y-%m-%d 09:15")
    t_dt = datetime.now().strftime("%Y-%m-%d %H:%M")
    res = api.getCandleData({"exchange": "NFO", "symboltoken": token, "interval": "FIVE_MINUTE", "fromdate": f_dt, "todate": t_dt})
    if not res or not res.get('status') or not res.get('data') or len(res['data']) < 3:
        return False, 0, 0, 0
    df = pd.DataFrame(res['data'], columns=['t', 'o', 'h', 'l', 'c', 'v'])
    df['vwap'] = (df['v'] * (df['h'] + df['l'] + df['c']) / 3).cumsum() / df['v'].cumsum()
    last = df.iloc[-1]
    avg_v = df['v'].tail(5).mean()
    v_rat = round(last['v'] / avg_v, 2) if avg_v > 0 else 0
    return (last['c'] > last['vwap'] and v_rat >= 1.5), last['c'], round(last['vwap'], 2), v_rat

if start_scan and api_key and client_id and pin and totp_key:
    try:
        api = SmartConnect(api_key=api_key)
        api.root = "https://angelone.in"
        sess = api.generateSession(client_id, pin, pyotp.TOTP(totp_key).now())

        if not sess or not sess.get('status'):
            st.error(f"लॉगिन एरर: {sess.get('message', 'माहिती तपासा')}")
        else:
            master = get_master()
            tkn_idx = "99926000" if idx == "NIFTY" else "99926009"
            name_idx = "Nifty 50" if idx == "NIFTY" else "Nifty Bank"
            step = 50 if idx == "NIFTY" else 100

            spot = api.ltpData("NSE", name_idx, tkn_idx)['data']['ltp']
            atm = round(spot / step) * step

            opts = master[(master['name'] == idx) & (master['instrumenttype'] == 'OPTIDX')].copy()
            opts['dt'] = pd.to_datetime(opts['expiry'], format='%d%b%Y')
            today = pd.to_datetime(datetime.now().strftime('%Y-%m-%d'))
            exp = opts[opts['dt'] >= today].sort_values('dt').iloc[0]['expiry']

            g_res = api.optionGreek({"name": idx, "expirydate": exp})
            g_df = pd.DataFrame(g_res['data']) if g_res and g_res.get('status') else pd.DataFrame()

            exp_opts = opts[opts['expiry'] == exp]
            near = exp_opts[exp_opts['strike'].astype(float).between((atm - 3 * step) * 100, (atm + 3 * step) * 100)]
            mkt = api.getMarketData("FULL", {"NFO": near['token'].tolist()})
            f_map = {x['symbolToken']: x for x in mkt['data']['fetched']}

            ce_tot, pe_tot, rows = 0, 0, []
            for s in sorted(near['strike'].astype(float).unique() / 100):
                ce_r = near[(near['strike'].astype(float) == s * 100) & (near['symbol'].str.endswith('CE'))]
                pe_r = near[(near['strike'].astype(float) == s * 100) & (near['symbol'].str.endswith('PE'))]
                ct = ce_r.iloc[0]['token'] if not ce_r.empty else ""
                pt = pe_r.iloc[0]['token'] if not pe_r.empty else ""

                coi = f_map[ct]['opnInterest'] if ct in f_map else 0
                cchg = f_map[ct]['netChangeOpnInterest'] if ct in f_map else 0
                poi = f_map[pt]['opnInterest'] if pt in f_map else 0
                pchg = f_map[pt]['netChangeOpnInterest'] if pt in f_map else 0
                ce_tot += coi
                pe_tot += poi

                cg = g_df[(g_df['strikePrice'].astype(float) == s) & (g_df['optionType'] == 'CE')] if not g_df.empty else pd.DataFrame()
                pg = g_df[(g_df['strikePrice'].astype(float) == s) & (g_df['optionType'] == 'PE')] if not g_df.empty else pd.DataFrame()

                rows.append({
                    "CE OI": coi, "CE Chg": cchg,
                    "CE Delta": float(cg.iloc[0]['delta']) if not cg.empty else 0,
                    "CE IV": float(cg.iloc[0]['impliedVolatility']) if not cg.empty else 0,
                    "STRIKE": int(s),
                    "PE OI": poi, "PE Chg": pchg,
                    "PE Delta": float(pg.iloc[0]['delta']) if not pg.empty else 0,
                    "PE IV": float(pg.iloc[0]['impliedVolatility']) if not pg.empty else 0,
                    "Theta": float(cg.iloc[0]['theta']) if not cg.empty else 0
                })

            pcr = round(pe_tot / ce_tot, 2) if ce_tot > 0 else 0
            p_stat = "Overbought" if pcr > 1.55 else "Bullish" if pcr >= 1.15 else "Oversold" if pcr < 0.5 else "Bearish" if pcr <= 0.85 else "Sideways"

            c1, c2, c3 = st.columns(3)
            c1.metric(f"{idx} Spot", f"₹{spot}", f"ATM: {atm}")
            c2.metric("Live PCR", f"{pcr}", p_stat)
            c3.metric("Expiry", exp, datetime.now().strftime("%H:%M:%S"))

            sig_found = False
            for otype in ['CE', 'PE']:
                chk = near[(near['strike'].astype(float) == atm * 100) & (near['symbol'].str.endswith(otype))]
                if not chk.empty:
                    tkn, sym = chk.iloc[0]['token'], chk.iloc[0]['symbol']
                    gc = g_df[(g_df['strikePrice'].astype(float) == atm) & (g_df['optionType'] == otype)] if not g_df.empty else pd.DataFrame()
                    if not gc.empty and tkn in f_map:
                        dv = abs(float(gc.iloc[0]['delta']))
                        ivv = float(gc.iloc[0]['impliedVolatility'])
                        oic = f_map[tkn]['netChangeOpnInterest']
                        ok, ltp, vw, vr = check_vwap_vol(api, tkn)
                        p_ok = (1.15 <= pcr <= 1.55) if otype == 'CE' else (0.50 <= pcr <= 0.85)
                        if p_ok and oic < 0 and (0.45 <= dv <= 0.65) and ivv < 25 and ok:
                            st.success(f"🔥 STRONG {otype} BUY: {sym} @ ₹{ltp} | SL: ₹{round(ltp*0.85,1)} | Target: ₹{round(ltp*1.25,1)} | VWAP: ₹{vw} | Vol: {vr}x")
                            send_tg(f"🔥 BUY {sym} @ ₹{ltp} | PCR: {pcr}")
                            sig_found = True

            if not sig_found:
                st.info("⏳ चारही अटी (OI + PCR + Greeks + VWAP Volume Spike) जुळण्याची वाट पाहत आहे...")

            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            time.sleep(15)
            st.rerun()
    except Exception as e:
        st.error(f"Error: {e}")
else:
    st.warning("👈 डाव्या बाजूला तुमची Angel One माहिती भरा आणि 'स्कॅनर चालू करा' वर टिक करा.")
