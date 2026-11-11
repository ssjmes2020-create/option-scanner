import streamlit as st
from SmartApi import SmartConnect
import pyotp
import pandas as pd
import requests
import time
from datetime import datetime

# ==========================================
# १. पेज सेटिंग्ज (डार्क प्रोफेशनल थीम)
# ==========================================
st.set_page_config(page_title="4-Pillar Option Buying Scanner", layout="wide")

st.markdown("""
    <style>
    .main { background-color: #0e1117; color: white; }
    .signal-buy { background-color: #133822; border: 2px solid #00ff6a; padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 20px; }
    .signal-put { background-color: #3d1418; border: 2px solid #ff4b4b; padding: 20px; border-radius: 10px; text-align: center; margin-bottom: 20px; }
    .signal-wait { background-color: #1e222d; border: 1px solid #444; padding: 15px; border-radius: 10px; text-align: center; margin-bottom: 20px; }
    </style>
""", unsafe_allow_html=True)

st.title("🎯 4-Pillar Option Buying Scanner - Angel One Live")

# ==========================================
# २. साईडबारमध्ये तुमची API माहिती टाकण्याची सोय
# ==========================================
with st.sidebar:
    st.header("🔐 Angel One & Telegram सेटिंग्ज")
    index_choice = st.selectbox("इंडेक्स निवडा (Index)", ["NIFTY", "BANKNIFTY"])
    api_key = st.text_input("Angel API Key", type="password")
    client_id = st.text_input("Angel Client ID")
    pin = st.text_input("MPIN", type="password")
    totp_secret = st.text_input("TOTP Secret Key", type="password")
    st.divider()
    tg_token = st.text_input("Telegram Bot Token (ऐच्छिक)", type="password")
    tg_chat_id = st.text_input("Telegram Chat ID (ऐच्छिक)")
    run_btn = st.checkbox("🟢 लाइव्ह स्कॅनर चालू करा (Auto-Refresh)")

def send_telegram(msg):
    if tg_token and tg_chat_id:
        url = f"https://telegram.org{tg_token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": tg_chat_id, "text": msg, "parse_mode": "Markdown"})
        except:
            pass

@st.cache_data(ttl=3600)
def load_scrip_master():
    url = "https://angelbroking.com"
    return pd.DataFrame(requests.get(url).json())

def check_vwap_volume(smartApi, token):
    from_date = datetime.now().strftime("%Y-%m-%d 09:15")
    to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    params = {"exchange": "NFO", "symboltoken": token, "interval": "FIVE_MINUTE", "fromdate": from_date, "todate": to_date}
    hist = smartApi.getCandleData(params)
    if not hist['status'] or not hist['data'] or len(hist['data']) < 5:
        return False, 0, 0, 0
    df = pd.DataFrame(hist['data'], columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['vwap'] = (df['volume'] * (df['high'] + df['low'] + df['close']) / 3).cumsum() / df['volume'].cumsum()
    last = df.iloc[-1]
    avg_vol = df['volume'].tail(5).mean()
    vol_ratio = round(last['volume'] / avg_vol, 2) if avg_vol > 0 else 0
    # अट ४: किंमत VWAP च्या वर + अचानक व्हॉल्यूम वाढ (1.5x पेक्षा जास्त)
    ok = (last['close'] > last['vwap']) and (vol_ratio >= 1.5)
    return ok, last['close'], round(last['vwap'], 2), vol_ratio

# ==========================================
# ३. मुख्य डॅशबोर्ड आणि स्कॅनिंग लॉजिक
# ==========================================
if run_btn and api_key and client_id and pin and totp_secret:
    try:
        smartApi = SmartConnect(api_key=api_key)
        totp = pyotp.TOTP(totp_secret).now()
        session = smartApi.generateSession(client_id, pin, totp)
        
        if not session['status']:
            st.error(f"लॉगिन एरर: {session['message']}")
        else:
            scrip_df = load_scrip_master()
            idx_token = "99926000" if index_choice == "NIFTY" else "99926009"
            idx_name = "Nifty 50" if index_choice == "NIFTY" else "Nifty Bank"
            step = 50 if index_choice == "NIFTY" else 100

            spot_data = smartApi.ltpData("NSE", idx_name, idx_token)
            spot = spot_data['data']['ltp']
            atm_strike = round(spot / step) * step

            # जवळची एक्सपायरी शोधणे
            opt_df = scrip_df[(scrip_df['name'] == index_choice) & (scrip_df['instrumenttype'] == 'OPTIDX')].copy()
            opt_df['expiry_dt'] = pd.to_datetime(opt_df['expiry'], format='%d%b%Y')
            today = pd.to_datetime(datetime.now().strftime('%Y-%m-%d'))
            nearest_expiry = opt_df[opt_df['expiry_dt'] >= today].sort_values('expiry_dt').iloc['expiry']

            # Greeks डेटा
            greeks_res = smartApi.optionGreek({"name": index_choice, "expirydate": nearest_expiry})
            greeks_df = pd.DataFrame(greeks_res['data']) if greeks_res['status'] else pd.DataFrame()

            # जवळच्या स्ट्राइक्सचा OI आणि Volume डेटा
            exp_opts = opt_df[opt_df['expiry'] == nearest_expiry]
            nearby = exp_opts[exp_opts['strike'].astype(float).between((atm_strike - 4*step)*100, (atm_strike + 4*step)*100)]
            mkt_data = smartApi.getMarketData("FULL", {"NFO": nearby['token'].tolist()})
            fetched = {item['symbolToken']: item for item in mkt_data['data']['fetched']}

            total_ce_oi, total_pe_oi = 0, 0
            table_rows = []
            active_signal = None

            strikes = sorted(nearby['strike'].astype(float).unique() / 100)
            for s in strikes:
                ce_r = nearby[(nearby['strike'].astype(float) == s*100) & (nearby['symbol'].str.endswith('CE'))]
                pe_r = nearby[(nearby['strike'].astype(float) == s*100) & (nearby['symbol'].str.endswith('PE'))]
                
                ce_oi = fetched[ce_r.iloc['token']]['opnInterest'] if not ce_r.empty and ce_r.iloc['token'] in fetched else 0
                ce_chg = fetched[ce_r.iloc['token']]['netChangeOpnInterest'] if not ce_r.empty and ce_r.iloc['token'] in fetched else 0
                pe_oi = fetched[pe_r.iloc['token']]['opnInterest'] if not pe_r.empty and pe_r.iloc['token'] in fetched else 0
                pe_chg = fetched[pe_r.iloc['token']]['netChangeOpnInterest'] if not pe_r.empty and pe_r.iloc['token'] in fetched else 0
                
                total_ce_oi += ce_oi
                total_pe_oi += pe_oi

                # Greeks मिळवणे
                ce_g = greeks_df[(greeks_df['strikePrice'].astype(float) == s) & (greeks_df['optionType'] == 'CE')]
                pe_g = greeks_df[(greeks_df['strikePrice'].astype(float) == s) & (greeks_df['optionType'] == 'PE')]
                
                ce_delta = float(ce_g.iloc['delta']) if not ce_g.empty else 0
                ce_iv = float(ce_g.iloc['impliedVolatility']) if not ce_g.empty else 0
                ce_theta = float(ce_g.iloc['theta']) if not ce_g.empty else 0

                pe_delta = float(pe_g.iloc['delta']) if not pe_g.empty else 0
                pe_iv = float(pe_g.iloc['impliedVolatility']) if not pe_g.empty else 0

                table_rows.append({
                    "CE OI": ce_oi, "CE OI Chg": ce_chg, "CE Delta": ce_delta, "CE IV%": ce_iv,
                    "STRIKE": int(s),
                    "PE OI": pe_oi, "PE OI Chg": pe_chg, "PE Delta": pe_delta, "PE IV%": pe_iv, "Theta": ce_theta
                })

            pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0
            pcr_status = "Overbought (सावध)" if pcr > 1.55 else "Bullish" if pcr >= 1.15 else "Oversold (सावध)" if pcr < 0.50 else "Bearish" if pcr <= 0.85 else "Sideways"

            # टॉप ३ मेट्रिक कार्ड्स
            c1, c2, c3 = st.columns(3)
            c1.metric(f"{index_choice} Spot Price", f"₹{spot}", f"ATM: {atm_strike}")
            c2.metric("Live PCR (Put-Call Ratio)", f"{pcr}", pcr_status)
            c3.metric("Expiry Date", nearest_expiry, f"Updated: {datetime.now().strftime('%H:%M:%S')}")

            # ATM स्ट्राइकवर ४-फिल्टर सिग्नल तपासणे
            for opt_type in ['CE', 'PE']:
                row_check = nearby[(nearby['strike'].astype(float) == atm_strike*100) & (nearby['symbol'].str.endswith(opt_type))]
                if not row_check.empty:
                    tkn = row_check.iloc['token']
                    sym = row_check.iloc['symbol']
                    g_check = greeks_df[(greeks_df['strikePrice'].astype(float) == atm_strike) & (greeks_df['optionType'] == opt_type)]
                    if not g_check.empty and tkn in fetched:
                        d_val = abs(float(g_check.iloc['delta']))
                        iv_val = float(g_check.iloc['impliedVolatility'])
                        oi_chg_val = fetched[tkn]['netChangeOpnInterest']
                        pa_ok, ltp, vwap_val, v_ratio = check_vwap_volume(smartApi, tkn)
                        
                        pcr_cond = (1.15 <= pcr <= 1.55) if opt_type == 'CE' else (0.50 <= pcr <= 0.85)
                        if pcr_cond and (oi_chg_val < 0) and (0.45 <= d_val <= 0.65) and (iv_val < 25) and pa_ok:
                            active_signal = (opt_type, sym, ltp, vwap_val, d_val, iv_val, v_ratio)

            # सिग्नल बॉक्स दाखवणे
            if active_signal:
                otype, sym, ltp, vwap_v, dv, ivv, vr = active_signal
                css_class = "signal-buy" if otype == "CE" else "signal-put"
                st.markdown(f"""
                <div class="{css_class}">
                    <h2>🔥 STRONG {otype} BUY SIGNAL: {sym} @ ₹{ltp} 🔥</h2>
                    <p><b>SL (15%):</b> ₹{round(ltp*0.85, 1)} | <b>Target (25%):</b> ₹{round(ltp*1.25, 1)} | <b>VWAP:</b> ₹{vwap_v} | <b>Vol Spike:</b> {vr}x | <b>Delta:</b> {dv} | <b>IV:</b> {ivv}%</p>
                </div>
                """, unsafe_allow_html=True)
                send_telegram(f"🔥 *STRONG {otype} BUY:* `{sym}` @ ₹{ltp} | PCR: {pcr} | Vol: {vr}x")
            else:
                st.markdown("""
                <div class="signal-wait">
                    <h4>⏳ चारही अटी (OI + PCR + Greeks + VWAP Volume Spike) जुळण्याची वाट पाहत आहे...</h4>
                </div>
