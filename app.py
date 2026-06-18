import streamlit as st
import pandas as pd
import numpy as np
import os
import uuid
import hashlib
import base64
import plotly.express as px
from streamlit_paste_button import paste_image_button
import yfinance as yf
from lightweight_charts.widgets import StreamlitChart
import datetime
import requests
import io
from sqlalchemy import text
from supabase import create_client, Client

# 👉 [UI優化] 1. 全域環境設定與自訂 CSS
st.set_page_config(page_title="Pro Trading Journal", layout="wide", page_icon="📈")
st.markdown("""
<style>
    .block-container { padding-top: 1.5rem !important; padding-bottom: 2rem !important; }
    div[data-testid="stMetric"] { background-color: #ffffff; border: 1px solid #f0f2f6; padding: 15px 20px; border-radius: 10px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); transition: transform 0.2s ease-in-out; }
    div[data-testid="stMetric"]:hover { transform: translateY(-2px); }
    .stButton > button { border-radius: 8px !important; font-weight: 600 !important; transition: all 0.3s ease !important; }
    .stButton > button:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.1) !important; border-color: #2196F3 !important; color: #2196F3 !important; }
    [data-testid="stSidebar"] { background-color: #f8f9fa; }
    hr { margin: 1.5em 0px; opacity: 0.5; }
</style>
""", unsafe_allow_html=True)

# --- 雲端資料庫與環境初始化 ---
# --- 雲端資料庫與環境初始化 ---
# 💡 直接從環境變數強制抓取連線字串，直接餵給 st.connection，繞過 Streamlit 的解析盲區
db_url = os.environ.get("CONNECTIONS__POSTGRESQL__URL") or os.environ.get("connections__postgresql__url")

if db_url:
    conn = st.connection("postgresql", type="sql", ttl=0, url=db_url)
else:
    conn = st.connection("postgresql", type="sql", ttl="1m")

# 同步確保 Supabase 網址與金鑰也能完美讀取
url_supa: str = os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
key_supa: str = os.environ.get("SUPABASE_KEY") or st.secrets.get("SUPABASE_KEY", "")
supabase: Client = create_client(url_supa, key_supa)
STORAGE_BUCKET = "trade_images"

@st.cache_resource
def init_db():
    with conn.session as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS notes (
                trade_id TEXT PRIMARY KEY, note TEXT, pre_plan TEXT, market_cond TEXT,
                initial_sl REAL DEFAULT 0.0, trailing_sl REAL DEFAULT 0.0,
                max_risk_pct REAL DEFAULT 1.0, discipline TEXT DEFAULT '未評估'
            )
        """))
        s.execute(text("CREATE TABLE IF NOT EXISTS market_notes (date TEXT PRIMARY KEY, note TEXT)"))
        s.execute(text("CREATE TABLE IF NOT EXISTS cash_flows (flow_id TEXT PRIMARY KEY, date TEXT, amount REAL, note TEXT)"))
        s.execute(text("CREATE TABLE IF NOT EXISTS trade_images (trade_id TEXT, image_path TEXT, category TEXT DEFAULT 'general')"))
        s.execute(text("CREATE TABLE IF NOT EXISTS monthly_reviews (month_id TEXT PRIMARY KEY, note TEXT)"))
        s.execute(text("CREATE TABLE IF NOT EXISTS strategy_tags (name TEXT PRIMARY KEY)"))
        s.execute(text("CREATE TABLE IF NOT EXISTS trade_strategy_map (trade_id TEXT PRIMARY KEY, strategy_name TEXT)"))
        s.execute(text("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"))
        
        cursor = s.execute(text("SELECT COUNT(*) FROM strategy_tags"))
        if cursor.fetchone()[0] == 0:
            for strat in ["EP", "突破", "M.E.T.A", "情緒性交易"]:
                s.execute(text("INSERT INTO strategy_tags (name) VALUES (:n) ON CONFLICT DO NOTHING"), {"n": strat})
        else:
             s.execute(text("INSERT INTO strategy_tags (name) VALUES ('情緒性交易') ON CONFLICT DO NOTHING"))
        s.commit()

# --- 極速快取設計 ---
@st.cache_data(ttl="1d") 
def get_stock_data(symbol):
    return yf.download(symbol, period="5y", interval='1d', progress=False)

@st.cache_data(ttl="1d")
def get_spx_data():
    return yf.download('^GSPC', period="5y", interval='1d', progress=False)

@st.cache_data(ttl="1h")
def fetch_github_csv(pat, fetch_url):
    headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github.v3.raw"}
    try:
        res = requests.get(fetch_url, headers=headers, timeout=10)
        if res.status_code == 200: return res.text
        return None
    except: return None
@st.cache_data(ttl="5m")
def get_latest_price(symbol):
    try:
        hist = yf.Ticker(symbol).history(period="1d")
        return hist['Close'].iloc[-1] if not hist.empty else 0
    except Exception:
        return 0

# --- 雲端自動存檔轉換邏輯 ---
def auto_save_risk_params():
    if 'current_trade' in st.session_state:
        tid = st.session_state.current_trade['trade_id']
        sl_val = st.session_state.get(f"sl_{tid}", 0.0)
        trail_val = st.session_state.get(f"trail_{tid}", 0.0)
        risk_val = st.session_state.get(f"risk_{tid}", 1.0)
        with conn.session as s:
            if s.execute(text("SELECT 1 FROM notes WHERE trade_id=:tid"), {"tid": tid}).fetchone():
                s.execute(text("UPDATE notes SET initial_sl=:s, trailing_sl=:t, max_risk_pct=:r WHERE trade_id=:tid"), {"s": sl_val, "t": trail_val, "r": risk_val, "tid": tid})
            else:
                s.execute(text("INSERT INTO notes (trade_id, initial_sl, trailing_sl, max_risk_pct) VALUES (:tid, :s, :t, :r)"), {"tid": tid, "s": sl_val, "t": trail_val, "r": risk_val})
            s.commit()

def auto_save_discipline():
    if 'current_trade' in st.session_state:
        # ✅ 先拆出 tid
        tid = st.session_state.current_trade['trade_id']
        new_val = st.session_state[f"disc_{tid}"]
        with conn.session as s:
            if s.execute(text("SELECT 1 FROM notes WHERE trade_id=:tid"), {"tid": tid}).fetchone(): 
                s.execute(text("UPDATE notes SET discipline=:v WHERE trade_id=:tid"), {"v": new_val, "tid": tid})
            else: 
                s.execute(text("INSERT INTO notes (trade_id, discipline) VALUES (:tid, :v)"), {"tid": tid, "v": new_val})
            s.commit()

def auto_save_note():
    if 'current_trade' in st.session_state:
        # ✅ 先拆出 tid
        tid = st.session_state.current_trade['trade_id']
        new_note = st.session_state[f"note_{tid}"]
        with conn.session as s:
            if s.execute(text("SELECT 1 FROM notes WHERE trade_id=:tid"), {"tid": tid}).fetchone(): 
                s.execute(text("UPDATE notes SET note=:n WHERE trade_id=:tid"), {"n": n_note, "tid": tid}) # 注意原本程式碼此處變數為 new_note
                # 為了保險，修正為正確的變數名稱：
                s.execute(text("UPDATE notes SET note=:n WHERE trade_id=:tid"), {"n": new_note, "tid": tid})
            else: 
                s.execute(text("INSERT INTO notes (trade_id, note) VALUES (:tid, :n)"), {"tid": tid, "n": new_note})
            s.commit()

def auto_save_pre_plan():
    if 'current_trade' in st.session_state:
        # ✅ 先拆出 tid
        tid = st.session_state.current_trade['trade_id']
        new_plan = st.session_state[f"pre_plan_{tid}"]
        with conn.session as s:
            if s.execute(text("SELECT 1 FROM notes WHERE trade_id=:tid"), {"tid": tid}).fetchone(): 
                s.execute(text("UPDATE notes SET pre_plan=:p WHERE trade_id=:tid"), {"p": new_plan, "tid": tid})
            else: 
                s.execute(text("INSERT INTO notes (trade_id, pre_plan) VALUES (:tid, :p)"), {"tid": tid, "p": new_plan})
            s.commit()

def auto_save_market_note():
    if st.session_state.get('view_mode') == 'market' and 'current_market_date' in st.session_state:
        m_date, new_note = st.session_state.current_market_date, st.session_state[f"mkt_note_{st.session_state.current_market_date}"]
        with conn.session as s:
            s.execute(text("INSERT INTO market_notes (date, note) VALUES (:d, :n) ON CONFLICT (date) DO UPDATE SET note = EXCLUDED.note"), {"d": m_date, "n": new_note})
            s.commit()



def save_trade_strategy():
    if 'current_trade' in st.session_state:
        tid, strat = st.session_state.current_trade['trade_id'], st.session_state[f"strat_select_{st.session_state.current_trade['trade_id']}"]
        with conn.session as s:
            s.execute(text("INSERT INTO trade_strategy_map (trade_id, strategy_name) VALUES (:tid, :strat) ON CONFLICT (trade_id) DO UPDATE SET strategy_name = EXCLUDED.strategy_name"), {"tid": tid, "strat": strat})
            s.commit()

def migrate_open_trade_data(symbol, new_closed_tid):
    old_tid = f"OPEN_{symbol}"
    with conn.session as s:
        if not s.execute(text("SELECT 1 FROM notes WHERE trade_id=:tid"), {"tid": new_closed_tid}).fetchone():
            s.execute(text("UPDATE notes SET trade_id=:n WHERE trade_id=:o"), {"n": new_closed_tid, "o": old_tid})
        if not s.execute(text("SELECT 1 FROM trade_strategy_map WHERE trade_id=:tid"), {"tid": new_closed_tid}).fetchone():
            s.execute(text("UPDATE trade_strategy_map SET trade_id=:n WHERE trade_id=:o"), {"n": new_closed_tid, "o": old_tid})
        if not s.execute(text("SELECT 1 FROM trade_images WHERE trade_id=:tid"), {"tid": new_closed_tid}).fetchone():
            s.execute(text("UPDATE trade_images SET trade_id=:n WHERE trade_id=:o"), {"n": new_closed_tid, "o": old_tid})
        s.commit()

# ==========================================
# 👉 全新升級：修正 TV 圖表比例失真與假日缺失問題
# ==========================================
def draw_tv_chart(symbol, transactions, initial_sl=0.0):
    try:
        df = get_stock_data(symbol).copy()
        spx_df = get_spx_data().copy()
        if df.empty: return None

        for d in [df, spx_df]:
            if isinstance(d.columns, pd.MultiIndex): 
                d.columns = d.columns.get_level_values(0)
        
        df = df.reset_index().rename(columns={
            'Date': 'time', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'
        })
        df['time'] = df['time'].dt.strftime('%Y-%m-%d')
        
        spx_df = spx_df.reset_index().rename(columns={'Date': 'time', 'Close': 'SPX'}) 
        spx_df['time'] = spx_df['time'].dt.strftime('%Y-%m-%d')
        
        df = pd.merge(df, spx_df[['time', 'SPX']], on='time', how='left').ffill()
        
        df['10 MA'] = df['close'].rolling(window=10).mean()
        df['20 MA'] = df['close'].rolling(window=20).mean()
        df['50 MA'] = df['close'].rolling(window=50).mean()
        df['200 MA'] = df['close'].rolling(window=200).mean()

        df['prev_close'] = df['close'].shift(1).fillna(df['open'])
        colors, border_colors, wick_colors, vol_data = [], [], [], []
        for _, row in df.iterrows():
            is_up = row['close'] >= row['prev_close']
            main_color = '#089981' if is_up else '#f23645'
            border_colors.append(main_color)
            wick_colors.append(main_color)
            colors.append('rgba(0, 0, 0, 0)' if row['close'] > row['open'] else main_color)
            vol_data.append({
                'time': row['time'], 
                'volume': row['volume'], 
                'color': 'rgba(8, 153, 129, 0.5)' if is_up else 'rgba(242, 54, 69, 0.5)'
            })

        df['color'] = colors
        df['borderColor'] = border_colors
        df['wickColor'] = wick_colors

        chart = StreamlitChart(height=1200)
        chart.layout(background_color='#FFFFFF', text_color='#333333')
        chart.grid(color='#e0e3eb')

        chart.price_scale(auto_scale=True, scale_margin_top=0.05, scale_margin_bottom=0.05)
        chart.time_scale(right_offset=15, min_bar_spacing=5)

        chart.candle_style(
            up_color='rgba(0, 0, 0, 0)', down_color='#f23645', 
            border_up_color='#089981', border_down_color='#f23645',
            wick_up_color='#089981', wick_down_color='#f23645'
        )
        chart.set(df[['time', 'open', 'high', 'low', 'close', 'color', 'borderColor', 'wickColor']])

        m10 = chart.create_line(name='10 MA', color='#787b86', width=1, price_line=False, price_label=False)
        m20 = chart.create_line(name='20 MA', color="#00FF22", width=1, price_line=False, price_label=False)
        m50 = chart.create_line(name='50 MA', color='#2196F3', width=1, price_line=False, price_label=False)
        m200 = chart.create_line(name='200 MA', color='#f23645', width=1, price_line=False, price_label=False)
        
        m10.set(df[['time', '10 MA']].dropna())
        m20.set(df[['time', '20 MA']].dropna())
        m50.set(df[['time', '50 MA']].dropna())
        m200.set(df[['time', '200 MA']].dropna())

        if initial_sl and initial_sl > 0:
            sl_line = chart.create_line(name='初始停損點', color='rgba(255, 82, 82, 1)', style='dashed', width=2, price_label=True)
            sl_df = df[['time']].copy()
            sl_df['初始停損點'] = initial_sl
            sl_line.set(sl_df[['time', '初始停損點']])

        vol_series = chart.create_histogram('volume', price_line=False, scale_margin_top=0.8, scale_margin_bottom=0)
        vol_series.set(pd.DataFrame(vol_data))

        if transactions:
            buy_records = []
            sell_records = []
            fallback_records = []
            
            valid_times = set(df['time'].values)
            sorted_times = sorted(list(valid_times))

            for trx in transactions:
                trx_date_str = pd.to_datetime(trx['date']).strftime('%Y-%m-%d')
                
                # 解決假日下單找不到日期的問題 (自動貼齊最近的交易日)
                if trx_date_str not in valid_times:
                    prior_dates = [d for d in sorted_times if d <= trx_date_str]
                    if prior_dates: trx_date_str = prior_dates[-1]
                    else: continue
                    
                price = trx.get('price', 0)
                if price > 0:
                    if trx['type'] == 'Buy': buy_records.append({'time': trx_date_str, 'BuyPrice': price})
                    else: sell_records.append({'time': trx_date_str, 'SellPrice': price})
                else:
                    fallback_records.append(trx)

            # 解決 Y 軸比例失真：改用 how='inner' 取代 ffill/bfill，避免買賣點強行貫穿 5 年歷史
            if buy_records:
                buy_series = chart.create_line(name='BuyPrice', color='rgba(0,0,0,0)', price_line=False, price_label=False)
                buy_grouped = pd.DataFrame(buy_records).groupby('time').mean().reset_index()
                buy_df = df[['time']].merge(buy_grouped, on='time', how='inner')
                buy_series.set(buy_df)
                for _, row in buy_grouped.iterrows():
                    buy_series.marker(time=row['time'], position='inside', shape='arrow_up', color="#000000")

            if sell_records:
                sell_series = chart.create_line(name='SellPrice', color='rgba(0,0,0,0)', price_line=False, price_label=False)
                sell_grouped = pd.DataFrame(sell_records).groupby('time').mean().reset_index()
                sell_df = df[['time']].merge(sell_grouped, on='time', how='inner')
                sell_series.set(sell_df)
                for _, row in sell_grouped.iterrows():
                    sell_series.marker(time=row['time'], position='inside', shape='arrow_down', color='#000000')

            for trx in fallback_records:
                d_str = pd.to_datetime(trx['date']).strftime('%Y-%m-%d')
                if d_str not in valid_times:
                    prior_dates = [d for d in sorted_times if d <= d_str]
                    if prior_dates: d_str = prior_dates[-1]
                    else: continue
                if trx['type'] == 'Buy':
                    chart.marker(time=d_str, position='belowBar', shape='arrow_up', color="#000000")
                else:
                    chart.marker(time=d_str, position='aboveBar', shape='arrow_down', color="#000000")

        spx_sub = chart.create_subchart(height=0.3)
        spx_line = spx_sub.create_line(name='SPX', color='rgba(255, 152, 0, 1)', width=2)
        spx_line.set(df[['time', 'SPX']].dropna())

        return chart
    except Exception as e:
        return f"圖表發生錯誤: {str(e)}"

# ==========================================
# 👉 全新升級：採用平均成本 (Average Cost FIFO) 精準解離未平倉部位，確保 All-Time 統計完全正確
# ==========================================
@st.cache_data
def calculate_perfect_chartlog_stats(df):
    df.columns = df.columns.str.strip()
    try:
        type_col = next(col for col in df.columns if df[col].astype(str).str.contains('Buy|Sell', case=False, na=False).any())
    except StopIteration: 
        return None, None

    price_col = None
    for col in df.columns:
        if col.strip().lower() in ['price', 'trade price', 'avg price', '價格', '成交價','price']:
            price_col = col
            break
    
    df['Date'] = pd.to_datetime(df['Date'])
    df['Net Cash'] = pd.to_numeric(df['Net Cash'].astype(str).str.replace(',', ''), errors='coerce').abs().fillna(0)
    df['Quantity'] = pd.to_numeric(df['Quantity'].astype(str).str.replace(',', ''), errors='coerce').abs().fillna(0)
    
    if len(df) > 1 and df['Date'].iloc[0] > df['Date'].iloc[-1]:
        df = df.iloc[::-1].reset_index(drop=True)

    df = df.sort_values('Date').reset_index(drop=True)
    
    closed_trades = []
    tracker = {} 
    
    for _, row in df.iterrows():
        symbol = str(row['Symbol']).strip()
        if symbol.lower() == 'nan' or symbol == '' or symbol == '-': continue
            
        t_type = str(row[type_col]).strip().capitalize()
        qty = row['Quantity']
        amt = row['Net Cash']
        date = row['Date']

        is_buy = 'Buy' in t_type
        is_sell = 'Sell' in t_type
        if not is_buy and not is_sell: continue
        if qty == 0: continue

        cost_per_share = abs(amt) / qty if qty > 0 else 0
        trade_price = float(str(row[price_col]).replace(',', '').strip()) if price_col and pd.notnull(row[price_col]) else cost_per_share
        
        if symbol not in tracker: 
            tracker[symbol] = {'qty': 0, 'cost_basis': 0, 'start_date': date, 'transactions': []}
        t = tracker[symbol]

        t['transactions'].append({'date': date, 'type': t_type, 'qty': qty, 'price': trade_price})

        if t['qty'] == 0:
            t['qty'] = qty if is_buy else -qty
            t['cost_basis'] = abs(amt)
            t['start_date'] = date
        elif (t['qty'] > 0 and is_buy) or (t['qty'] < 0 and is_sell):
            t['qty'] += qty if is_buy else -qty
            t['cost_basis'] += abs(amt)
        else:
            # 進行減倉操作，立即釋放該部分的已實現損益
            close_qty = min(qty, abs(t['qty']))
            avg_cost_per_share = t['cost_basis'] / abs(t['qty'])
            cost_of_closed = avg_cost_per_share * close_qty
            
            if t['qty'] > 0: # 多單平倉
                realized = abs(amt) * (close_qty / qty) - cost_of_closed
            else: # 空單回補
                realized = cost_of_closed - abs(amt) * (close_qty / qty)
            
            t['qty'] -= close_qty if t['qty'] > 0 else -close_qty
            t['cost_basis'] -= cost_of_closed

            tid = f"{date.strftime('%Y%m%d')}_{symbol}_{round(realized, 2)}"
            closed_trades.append({
                'trade_id': tid, 
                'Exit_Date': date.strftime('%Y-%m-%d'), 
                'Symbol': symbol, 
                'pnl': realized, 
                'hold_time': date - t['start_date'], 
                'raw_date': date,
                'entry_date': t['start_date'],
                'transactions': t['transactions'].copy()
            })
            migrate_open_trade_data(symbol, tid)

            remain_qty = qty - close_qty
            if remain_qty > 1e-5:
                t['qty'] = remain_qty if is_buy else -remain_qty
                t['cost_basis'] = abs(amt) * (remain_qty / qty)
                t['start_date'] = date
                
            if abs(t['qty']) < 1e-5:
                del tracker[symbol]
                
    open_trades = []
    for symbol, t in tracker.items():
        if abs(t['qty']) >= 1e-5:
            open_trades.append({
                'trade_id': f"OPEN_{symbol}",
                'Exit_Date': '未平倉',
                'Symbol': symbol,
                'pnl': 0, 
                'qty': t['qty'],
                'hold_time': pd.Timestamp.now() - t['start_date'], 
                'raw_date': t['start_date'],
                'transactions': t['transactions'] 
            })

    t_df = pd.DataFrame(closed_trades) if closed_trades else pd.DataFrame()
    o_df = pd.DataFrame(open_trades) if open_trades else pd.DataFrame()

    return t_df, o_df

def get_stats(filtered_df):
    if filtered_df is None or filtered_df.empty: return None
    wins = filtered_df[filtered_df['pnl'] > 0]; losses = filtered_df[filtered_df['pnl'] <= 0]
    total_net = filtered_df['pnl'].sum(); num_trades = len(filtered_df)
    def format_td(td):
        if pd.isna(td) or td is None: return "0d 0h 0m"
        days, (hours, remainder) = td.days, divmod(td.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    return {
        "Total Net Profit": total_net, "Avg Net Profit": total_net / num_trades if num_trades > 0 else 0,
        "Avg Win": wins['pnl'].mean() if not wins.empty else 0, "Avg Loss": abs(losses['pnl'].mean()) if not losses.empty else 0,
        "Win Rate %": (len(wins)/num_trades*100) if num_trades > 0 else 0, "Trade Count": num_trades,
        "Win Count": len(wins), "Loss Count": len(losses),
        "Profit Factor": (wins['pnl'].sum() / abs(losses['pnl'].sum())) if not losses.empty and losses['pnl'].sum() != 0 else 0,
        "Largest Win": filtered_df['pnl'].max(), "Largest Loss": abs(filtered_df['pnl'].min()),
        "Avg Hold (Win)": format_td(wins['hold_time'].mean()), "Avg Hold (Loss)": format_td(losses['hold_time'].mean())
    }

@st.cache_data
def load_data(file):
    return pd.read_csv(file)

init_db()

# ==========================================
# 👉 側邊欄與管理員雙模式解鎖
# ==========================================
with st.sidebar.expander("🔐 管理員專屬解鎖", expanded=not st.session_state.get('is_admin', False)):
    if st.session_state.get('is_admin', False):
        st.success("✅ 已解鎖！顯示所有歷史交易。")
        if st.button("鎖定 (返回公開模式)"):
            st.session_state['is_admin'] = False
            st.rerun()
    else:
        st.caption("公開模式：僅顯示 2026/04/01 之後資料。")
        pwd = st.text_input("輸入管理員密碼", type="password")
        if st.button("解鎖歷史紀錄"):
            if pwd == "0000":
                st.session_state['is_admin'] = True
                st.rerun()
            else:
                st.error("密碼錯誤")

with st.sidebar.expander("⚙️ 系統設定與匯入", expanded=False):
    st.subheader("🏷️ 策略管理")
    new_strat = st.text_input("新增策略標籤")
    if st.button("確認新增"):
        if new_strat:
            with conn.session as s:
                try: s.execute(text("INSERT INTO strategy_tags (name) VALUES (:n) ON CONFLICT DO NOTHING"), {"n": new_strat}); s.commit()
                except Exception: pass
            st.rerun()

    available_strats = [row[0] for row in conn.query("SELECT name FROM strategy_tags", ttl=0).itertuples(index=False)]

    if st.checkbox("管理/刪除標籤"):
        for s_tag in available_strats:
            col_s1, col_s2 = st.columns([3, 1])
            col_s1.write(s_tag)
            if col_s2.button("🗑️", key=f"del_strat_{s_tag}"):
                with conn.session as s:
                    s.execute(text("DELETE FROM strategy_tags WHERE name=:n"), {"n": s_tag}); s.commit()
                st.rerun()

    st.divider()
    st.subheader("📥 記錄歷史入出金流水帳")
    
    flow_date = st.date_input("變動日期")
    flow_amount = st.number_input("金額 (美金, 出金請輸入負數)", step=1000.0)
    flow_note = st.text_input("備註說明", placeholder="例如：初始本金")
    
    if st.button("確認提交資金紀錄"):
        flow_id = f"FLOW_{uuid.uuid4().hex[:8]}"
        with conn.session as s:
            s.execute(text("INSERT INTO cash_flows (flow_id, date, amount, note) VALUES (:id, :d, :amt, :n)"), 
                      {"id": flow_id, "d": flow_date.strftime('%Y-%m-%d'), "amt": flow_amount, "n": flow_note}); s.commit()
        st.toast("資金紀錄已存檔！", icon="💰")
        st.rerun()

    if st.checkbox("管理/刪除歷史資金紀錄"):
        flows_df = conn.query("SELECT flow_id, date, amount, note FROM cash_flows ORDER BY date DESC", ttl=0)
        if not flows_df.empty:
            for _, row in flows_df.iterrows():
                col1, col2 = st.columns([4, 1])
                with col1:
                    st.markdown(f"**{row['date']}** | `${row['amount']:,.0f}`")
                    if row['note']: st.caption(f"📝 {row['note']}")
                with col2:
                    if st.button("🗑️", key=f"del_flow_{row['flow_id']}"):
                        with conn.session as s:
                            s.execute(text("DELETE FROM cash_flows WHERE flow_id=:id"), {"id": row['flow_id']}); s.commit()
                        st.toast("✅ 紀錄已刪除！", icon="🗑️"); st.rerun() 
                st.divider() 
        else: st.info("目前尚無任何資金變動紀錄。")

    st.divider()
    st.subheader("📥 資料匯入")
    uploaded_file = st.file_uploader("上傳您的交易 CSV", type="csv")

input_df = None

if uploaded_file is not None:
    input_df = load_data(uploaded_file)
    st.toast("✅ 已載入手動上傳的資料", icon="✅")
else:
    GITHUB_USERNAME = "Hua2628" 
    REPO_NAME = "ib-daily-report"
    FILE_PATH = "Daily_Report.csv"
    GITHUB_PAT = os.environ.get("GITHUB_PAT") or os.environ.get("github_pat", "")

    if GITHUB_PAT:
        url_csv = f"https://raw.githubusercontent.com/{GITHUB_USERNAME}/{REPO_NAME}/main/{FILE_PATH}"
        with st.sidebar.status("🔄 正在從 GitHub 取得最新紀錄...", expanded=True) as status:
            csv_text = fetch_github_csv(GITHUB_PAT, url_csv)
            if csv_text:
                input_df = pd.read_csv(io.StringIO(csv_text))
                status.update(label=f"✅ 成功讀取雲端資料！總筆數: {len(input_df)}", state="complete", expanded=False)
            else:
                status.update(label=f"❌ 取資料失敗", state="error")
                
    if input_df is None and os.path.exists("Daily_Report.csv"):
        input_df = load_data("Daily_Report.csv")
        st.sidebar.warning("⚠️ 已退回使用本地預設資料 (Daily_Report.csv)")

if input_df is not None:
    input_df['Date'] = pd.to_datetime(input_df['Date'])
    
    # ✅ 1. 先用「全部完整資料」去算，確保歷史總庫存與總損益完全精準
    raw_t_df, raw_o_df = calculate_perfect_chartlog_stats(input_df)
    
    # ✅ 2. 結算「全時段」的已實現總損益 (給側邊欄資金池用的)
    global_realized_pnl = raw_t_df['pnl'].sum() if raw_t_df is not None and not raw_t_df.empty else 0.0

    # ✅ 3. 接著才做「雙模式過濾」：把舊的平倉紀錄藏起來，不讓訪客看到明細
    if not st.session_state.get('is_admin', False):
        cutoff_date = pd.to_datetime('2026-04-01')
        if raw_t_df is not None and not raw_t_df.empty:
            raw_t_df['raw_date'] = pd.to_datetime(raw_t_df['raw_date'])
            raw_t_df = raw_t_df[raw_t_df['raw_date'] >= cutoff_date]

    strat_map = conn.query("SELECT * FROM trade_strategy_map", ttl=0)
    
    trades_df = pd.DataFrame()
    open_df = pd.DataFrame()

    if raw_t_df is not None and not raw_t_df.empty:
        trades_df = raw_t_df[raw_t_df['pnl'].abs() > 0.1].copy() 
        if not strat_map.empty: trades_df = trades_df.merge(strat_map, on='trade_id', how='left')
        else: trades_df['strategy_name'] = "未分類"
        trades_df['strategy_name'] = trades_df.get('strategy_name', "未分類").fillna("未分類")
        
    if raw_o_df is not None and not raw_o_df.empty:
        open_df = raw_o_df.copy()
        if not strat_map.empty: open_df = open_df.merge(strat_map, on='trade_id', how='left')
        else: open_df['strategy_name'] = "未分類"
        open_df['strategy_name'] = open_df.get('strategy_name', "未分類").fillna("未分類")
        
    # --- 計算資本與儀表板 ---
    try:
        cash_df = conn.query("SELECT amount, date FROM cash_flows", ttl=0)
        # ✅ 4. 取消入金日期的過濾！永遠把「所有歷史入金」全部加總
        net_deposits = cash_df['amount'].sum() if not cash_df.empty else 0.0
    except Exception as e:
        net_deposits = 0.0

    # ✅ 5. 把這行原本用局部 trades_df 算的損益，強制換成一開始算好的「全時段總損益」
    realized_pnl = global_realized_pnl
    
    unrealized_pnl = 0.0; projected_unrealized_pnl = 0.0; portfolio_data = []
    
    risk_df = conn.query("SELECT trade_id, initial_sl, trailing_sl FROM notes", ttl=0)
    risk_map = risk_df.set_index('trade_id').to_dict('index') if not risk_df.empty else {}

    if open_df is not None and not open_df.empty:
        for _, row in open_df.iterrows():
            try:
                # ✅ 替換成這行 (0 延遲讀取快取)
                latest_price = get_latest_price(row['Symbol'])
                
                if latest_price > 0:
                    txs = row['transactions']
                    is_long = row['qty'] > 0
                    
                    cost_sum = sum(t['qty'] * t['price'] for t in txs if (t['type'] == 'Buy' if is_long else t['type'] == 'Sell'))
                    qty_sum = sum(t['qty'] for t in txs if (t['type'] == 'Buy' if is_long else t['type'] == 'Sell'))
                    avg_price = cost_sum / qty_sum if qty_sum > 0 else 0
                    
                    if is_long: u_pnl = (latest_price - avg_price) * row['qty']
                    else: u_pnl = (avg_price - latest_price) * abs(row['qty'])
                        
                    unrealized_pnl += u_pnl
                    
                    tid = row['trade_id']
                    i_sl = risk_map.get(tid, {}).get('initial_sl', 0.0)
                    t_sl = risk_map.get(tid, {}).get('trailing_sl', 0.0)
                    
                    active_stop = t_sl if t_sl > 0 else i_sl
                    if active_stop == 0: active_stop = latest_price 
                    
                    if is_long: proj_u_pnl = (active_stop - avg_price) * row['qty']
                    else: proj_u_pnl = (avg_price - active_stop) * abs(row['qty'])
                        
                    projected_unrealized_pnl += proj_u_pnl
                    cost_basis = avg_price * abs(row['qty'])
                    market_value = latest_price * abs(row['qty'])
                    u_pnl_pct = (u_pnl / cost_basis * 100) if cost_basis > 0 else 0
                    
                    portfolio_data.append({
                        "Symbol": row['Symbol'], "Avg Price": avg_price, "Latest Price": latest_price,
                        "Active Stop": active_stop, "Unrealized P&L": u_pnl, "Unrealized P&L %": u_pnl_pct,
                        "Cost Basis": cost_basis, "Position": row['qty'], "Market Value": market_value,
                        "Projected P&L": proj_u_pnl, "strategy_name": row.get('strategy_name', '未分類'), "trade_id": tid
                    })
            except Exception: pass 

    current_capital = net_deposits + realized_pnl + unrealized_pnl
    projected_capital = net_deposits + realized_pnl + projected_unrealized_pnl 
    st.session_state['current_capital'] = current_capital

    if portfolio_data and current_capital > 0:
        for item in portfolio_data: item['% of Net Liq'] = (item['Market Value'] / current_capital) * 100

    portfolio_df = pd.DataFrame(portfolio_data)

    st.sidebar.title("💰 帳戶資金總覽")
    st.sidebar.metric(
        "動態帳戶總餘額 (含浮動)", f"${current_capital:,.2f}", 
        f"淨入金: {net_deposits:,.0f} | 浮動: {unrealized_pnl:+,.0f} | 已實現: {realized_pnl:+,.0f}"
    )
    st.session_state['projected_capital'] = projected_capital

    if (not trades_df.empty) or (not open_df.empty):
        st.title(f"📊 交易數據中心")
        
        display_df = pd.DataFrame()
        if trades_df is not None and not trades_df.empty:
            trades_df['Year'] = trades_df['raw_date'].dt.strftime('%Y')
            trades_df['Month'] = trades_df['raw_date'].dt.strftime('%Y-%m')
            display_df = trades_df.copy()

        tab_stats, tab_equity, tab_daily = st.tabs(["🏆 績效總覽", "📈 資金與淨值曲線", "📊 每日損益"])

        with tab_stats:
            if not display_df.empty:
                filter_col1, filter_col2, filter_col3 = st.columns(3)
                with filter_col1: filter_year = st.selectbox("📅 篩選年度", ["全部"] + sorted(display_df['Year'].unique().tolist(), reverse=True))
                month_options_ui = sorted(display_df['Month'].unique().tolist(), reverse=True)
                with filter_col2: filter_months = st.multiselect("📅 篩選月份 (可複選)", options=month_options_ui, key="filter_month_select")
                with filter_col3: filter_strat = st.selectbox("🎯 篩選策略", ["全部"] + available_strats + ["未分類"])
                
                if filter_year != "全部": display_df = display_df[display_df['Year'] == filter_year]
                if filter_months : display_df = display_df[display_df['Month'].isin(filter_months)]
                if filter_strat != "全部": display_df = display_df[display_df['strategy_name'] == filter_strat]
                    
                stats = get_stats(display_df)
                if stats:
                    with st.container(border=True):
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("Total Net Profit", f"${stats['Total Net Profit']:,.2f}")
                        m2.metric("Win Rate %", f"{stats['Win Rate %']:.2f}% ({stats['Win Count']}W / {stats['Loss Count']}L)")
                        m3.metric("Profit Factor", f"{stats['Profit Factor']:.2f}")
                        m4.metric("# of Trades", stats['Trade Count'])
                        m5, m6, m7, m8 = st.columns(4)
                        m5.metric("Avg. Win", f"${stats['Avg Win']:,.2f}"); m6.metric("Avg. Loss", f"-${stats['Avg Loss']:,.2f}")
                        m7.metric("Largest Win", f"${stats['Largest Win']:,.2f}"); m8.metric("Largest Loss", f"-${stats['Largest Loss']:,.2f}")
                        m9, m10 = st.columns([1, 3])
                        m9.metric("Avg. Hold (Win)", stats['Avg Hold (Win)'])
                        m10.metric("Avg. Hold (Loss)", stats['Avg Hold (Loss)'])

                notes_df = conn.query("SELECT trade_id, discipline FROM notes", ttl=0)
                if not notes_df.empty: stat_df = display_df.merge(notes_df, on='trade_id', how='left')
                else: stat_df = display_df.copy(); stat_df['discipline'] = "未評估"
                stat_df['discipline'] = stat_df.get('discipline', "未評估").fillna("未評估")
                
                st.divider()
                st.subheader("⚖️ 交易紀律與情緒分析 (四象限)")
                
                good_win = stat_df[(stat_df['discipline'] == '✅ 符合紀律') & (stat_df['pnl'] > 0)]
                good_loss = stat_df[(stat_df['discipline'] == '✅ 符合紀律') & (stat_df['pnl'] <= 0)]
                bad_win = stat_df[(stat_df['discipline'] == '❌ 不符合紀律') & (stat_df['pnl'] > 0)]
                bad_loss = stat_df[(stat_df['discipline'] == '❌ 不符合紀律') & (stat_df['pnl'] <= 0)]
                
                good_win_pnl, good_loss_pnl = good_win['pnl'].sum(), good_loss['pnl'].sum()
                bad_win_pnl, bad_loss_pnl = bad_win['pnl'].sum(), bad_loss['pnl'].sum()

                q1, q2, q3, q4 = st.columns(4)
                with q1: st.metric("✅ 符合紀律 (獲利)", f"${good_win_pnl:,.2f}", f"{len(good_win)} 筆 (應得的報酬)", delta_color="normal")
                with q2: st.metric("✅ 符合紀律 (虧損)", f"${good_loss_pnl:,.2f}", f"{len(good_loss)} 筆 (正確的試錯成本)", delta_color="off")
                with q3: st.metric("❌ 不守紀律 (獲利)", f"${bad_win_pnl:,.2f}", f"{len(bad_win)} 筆 (賽到的，強化壞習慣)", delta_color="off")
                with q4: st.metric("❌ 不守紀律 (虧損)", f"${bad_loss_pnl:,.2f}", f"{len(bad_loss)} 筆 (多賠的冤枉錢)", delta_color="inverse")
                
                st.caption("💡 註：系統會自動抓取你標註的「紀律」與實際平倉的「損益」進行交叉計算。盡量減少第三、第四象限的交易。")
                
                ideal_pnl = good_win_pnl + good_loss_pnl
                actual_pnl = ideal_pnl + bad_win_pnl + bad_loss_pnl
                discipline_diff = ideal_pnl - actual_pnl 
                bad_trade_count = len(bad_win) + len(bad_loss)

                if bad_trade_count > 0:
                    if discipline_diff > 0:
                        st.success(f"⚖️ **紀律覺醒試算：**\n\n目前包含所有交易的實際總損益為 **${actual_pnl:,.2f}**。\n\n如果您能管住手（不做右邊那 {bad_trade_count} 筆不守紀律的交易），您的總損益其實會是 **${ideal_pnl:,.2f}**。\n\n💡 **結論：** 不守紀律讓您白白損失了 **${discipline_diff:,.2f}**！", icon="🚀")
                    else:
                        st.warning(f"⚖️ **紀律覺醒試算：**\n\n目前包含所有交易的實際總損益為 **${actual_pnl:,.2f}**。\n\n如果您完全遵守紀律，總損益會是 **${ideal_pnl:,.2f}**。\n\n💡 **結論：** 雖然目前不守紀律的交易剛好讓您多賺了 **${abs(discipline_diff):,.2f}**，但這多半是「運氣成分」，長期下來極易釀成大錯，請務必當心！", icon="⚠️")

                st.markdown("##### 📈 交易紀律維持率趨勢 (越高越好)")
                if not stat_df.empty:
                    trend_df = stat_df.sort_values('Month')
                    trend_data = trend_df.groupby('Month').apply(lambda x: pd.Series({'總交易次數': len(x), '已評估次數': (x['discipline'] != '未評估').sum(), '符合紀律次數': (x['discipline'] == '✅ 符合紀律').sum()})).reset_index()
                    trend_data['紀律維持率 (%)'] = np.where(trend_data['已評估次數'] > 0, (trend_data['符合紀律次數'] / trend_data['已評估次數'] * 100).round(2), 0.0)
                    
                    fig_trend = px.line(trend_data, x='Month', y='紀律維持率 (%)', text='紀律維持率 (%)', markers=True, color_discrete_sequence=['#089981'])
                    fig_trend.update_traces(textposition="top center", texttemplate='%{text}%', marker=dict(size=10, line=dict(width=2, color='white')), hovertemplate='月份: %{x}<br>紀律維持率: %{y}%<extra></extra>')
                    fig_trend.update_layout(plot_bgcolor='rgba(0,0,0,0)', xaxis_title="月份", yaxis_title="紀律維持率 (%)", yaxis=dict(range=[-5, 115], showgrid=True, gridcolor='#e0e3eb'), xaxis=dict(type='category', showgrid=False), height=350, margin=dict(l=0, r=0, t=30, b=0))
                    st.plotly_chart(fig_trend, use_container_width=True)
                else: st.info("尚無數據可繪製趨勢圖。")
            else: st.info("目前尚無已平倉的交易可供統計。")

        with tab_equity:
            trade_history = display_df.groupby('Exit_Date')['pnl'].sum().reset_index() if not display_df.empty else pd.DataFrame(columns=['Exit_Date', 'pnl'])
            trade_history.rename(columns={'Exit_Date': 'Date', 'pnl': 'Amount'}, inplace=True)
            trade_history['Type'] = 'TradePnL'

            try:
                cash_history = cash_df.copy()
                cash_history.rename(columns={'date': 'Date', 'amount': 'Amount'}, inplace=True)
            except Exception:
                cash_history = pd.DataFrame(columns=['Date', 'Amount'])
                
            cash_history['Type'] = 'CashFlow'

            merged_timeline = pd.concat([trade_history, cash_history], ignore_index=True)
            if not merged_timeline.empty:
                merged_timeline = merged_timeline.dropna(subset=['Date'])
                merged_timeline['Date'] = pd.to_datetime(merged_timeline['Date'])
                merged_timeline = merged_timeline.sort_values('Date').reset_index(drop=True)

                current_balance, current_shares, nav = 0.0, 0.0, 1.0
                history_timeline = []

                for _, row in merged_timeline.iterrows():
                    val = float(row['Amount']) if pd.notnull(row['Amount']) else 0.0
                    if row['Type'] == 'CashFlow':
                        if current_balance == 0: current_shares = val; current_balance = val
                        else:
                            if nav > 0: current_shares += (val / nav)
                            current_balance += val
                    elif row['Type'] == 'TradePnL':
                        current_balance += val
                        if current_shares > 0: nav = current_balance / current_shares

                    history_timeline.append({'Date': row['Date'], 'Real_Balance': current_balance, 'NAV': nav})

                if 'unrealized_pnl' in locals() and unrealized_pnl != 0:
                    current_balance += unrealized_pnl
                    if current_shares > 0: nav = current_balance / current_shares
                    history_timeline.append({'Date': pd.Timestamp.now().normalize(), 'Real_Balance': current_balance, 'NAV': nav})

                st.session_state['current_capital'] = current_balance
                nav_df = pd.DataFrame(history_timeline)
                
                import plotly.graph_objects as go
                from plotly.subplots import make_subplots
                fig = make_subplots(specs=[[{"secondary_y": True}]])
                fig.add_trace(go.Scatter(x=nav_df['Date'], y=nav_df['Real_Balance'], name="真實總資金 ($)", line=dict(color='#2E86C1', width=3), mode='lines+markers'), secondary_y=False)
                fig.add_trace(go.Scatter(x=nav_df['Date'], y=nav_df['NAV'], name="單位淨值 (NAV)", line=dict(color='#E67E22', width=3), mode='lines+markers'), secondary_y=True)
                fig.update_layout(plot_bgcolor='rgba(0,0,0,0)', hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
                fig.update_yaxes(title_text="真實金額 (USD)", secondary_y=False, showgrid=True, gridcolor='lightgray')
                fig.update_yaxes(title_text="交易實力淨值 (NAV)", secondary_y=True, showgrid=False)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("尚無入金或交易資料可繪製資金曲線。")
                st.session_state['current_capital'] = 0.0

        with tab_daily:
            if not display_df.empty:
                daily_pnl = display_df.groupby('Exit_Date')['pnl'].sum().reset_index()
                daily_pnl['Color'] = np.where(daily_pnl['pnl'] > 0, '#089981', '#f23645')
                fig_bar = px.bar(daily_pnl, x='Exit_Date', y='pnl', title='Daily P&L')
                fig_bar.update_traces(marker_color=daily_pnl['Color'])
                st.plotly_chart(fig_bar, use_container_width=True)
            else: st.info("尚無資料繪製每日損益。")

        st.divider()

        left, right = st.columns([1, 4])
        with left:
            with st.container(height=900, border=False):
                st.subheader("🌍 大盤與市況導航")
                today_str = pd.Timestamp.now().strftime('%Y-%m-%d')
                
                if st.button(f"📝 填寫/檢視今日市況 ({today_str})", key="btn_mkt_today", use_container_width=True):
                    st.session_state.view_mode = "market"
                    st.session_state.current_market_date = today_str

                with st.expander("🔍 查詢指定日期市況"):
                    search_date = st.date_input("選擇日期", value=pd.Timestamp.now(), label_visibility="collapsed")
                    if st.button("前往該日市況", use_container_width=True):
                        st.session_state.view_mode = "market"
                        st.session_state.current_market_date = search_date.strftime('%Y-%m-%d')
                        st.rerun()

                st.divider()

                if 'portfolio_df' in locals() and not portfolio_df.empty:
                    st.subheader("🔥 投資組合 (未平倉)")
                    curr_cap = st.session_state.get('current_capital', 0)
                    proj_cap = st.session_state.get('projected_capital', 0)
                    import plotly.graph_objects as go
                    
                    proj_color = '#089981' if proj_cap >= curr_cap else '#f23645'
                    fig_risk = go.Figure(data=[
                        go.Bar(name='當前總資金 (市價)', x=['總帳戶價值'], y=[curr_cap], marker_color='#2196F3', text=f"${curr_cap:,.0f}", textposition='auto'),
                        go.Bar(name='全數觸發停損後保底資金', x=['總帳戶價值'], y=[proj_cap], marker_color=proj_color, text=f"${proj_cap:,.0f}", textposition='auto')
                    ])
                    fig_risk.update_layout(barmode='group', height=300, margin=dict(l=0, r=0, t=30, b=0), plot_bgcolor='rgba(0,0,0,0)', yaxis=dict(showgrid=True, gridcolor='#e0e3eb'), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
                    st.plotly_chart(fig_risk, use_container_width=True)
                    st.caption("💡 註：保底資金是假設所有未平倉部位，在當下直接精準打到您的「追蹤止損點（若無則為初始停損）」所剩餘的總資金。")
                    
                    st.dataframe(
                        portfolio_df,
                        column_config={
                            "Symbol": st.column_config.TextColumn("代號"), "Avg Price": st.column_config.NumberColumn("平均成本", format="$%.3f"),
                            "Latest Price": st.column_config.NumberColumn("最新報價", format="$%.2f"), "Active Stop": st.column_config.NumberColumn("生效停損點", format="$%.2f"), 
                            "Unrealized P&L": st.column_config.NumberColumn("未實現損益", format="$%.0f"), "Unrealized P&L %": st.column_config.NumberColumn("未實現損益 %", format="%.2f%%"),
                            "Projected P&L": st.column_config.NumberColumn("觸價後損益 (保底)", format="$%.0f"), "Cost Basis": st.column_config.NumberColumn("總成本", format="$%.0f"),
                            "Position": st.column_config.NumberColumn("持倉股數"), "% of Net Liq": st.column_config.NumberColumn("資金佔比", format="%.2f%%"),
                            "Market Value": None, "strategy_name": None, "trade_id": None
                        }, hide_index=True, use_container_width=True
                    )
                    
                    st.markdown("##### 🔍 點擊進入個股追蹤 / 盤前規劃：")
                    btn_cols = st.columns(min(len(portfolio_df), 6)) 
                    for idx, p_row in portfolio_df.iterrows():
                        with btn_cols[idx % 6]:
                            tag_str = f"[{p_row['strategy_name']}] " if p_row['strategy_name'] != "未分類" else ""
                            if st.button(f" {tag_str}{p_row['Symbol']}", key=f"btn_port_{p_row['trade_id']}", use_container_width=True):
                                st.session_state.view_mode = "trade"
                                st.session_state.current_trade = open_df[open_df['trade_id'] == p_row['trade_id']].iloc[0].to_dict()
                                st.rerun()
                    st.divider()

                st.subheader("📅 已平倉回顧")
                if not display_df.empty:
                    daily = display_df.groupby('Exit_Date')['pnl'].sum().sort_index(ascending=False)
                    for d, val in daily.items():
                        clr = "green" if val >= 0 else "red"
                        with st.expander(f"{d} | PnL: :{clr}[${val:,.2f}]"):
                            if st.button(f"🌍 檢視 {d} 大盤市況", key=f"btn_mkt_{d}", use_container_width=True):
                                st.session_state.view_mode = "market"; st.session_state.current_market_date = d

                            # ✅ 防撞名強化版寫法
                            items = display_df[display_df['Exit_Date'] == d]
                            for idx, item in items.iterrows():  # 💡 1. 把原本的底線 _ 改成 idx，抓出專屬流水號
                                tag_str = f"[{item['strategy_name']}] " if item['strategy_name'] != "未分類" else ""
                                status_icon = "🟢" if item['pnl'] > 0 else ("🔴" if item['pnl'] < 0 else "⚪")
                                
                                # 💡 2. 在 key 後面補上 _{idx}，讓每顆按鈕獲得絕對獨一無二的身分證
                                if st.button(f"{status_icon} {tag_str}{item['Symbol']} | ${item['pnl']:,.2f}", key=f"{item['trade_id']}_{idx}"):
                                    st.session_state.view_mode = "trade"; st.session_state.current_trade = item.to_dict()

        with right:
            with st.container(border=False):
                view_mode = st.session_state.get('view_mode', 'trade')

                if view_mode == 'market' and 'current_market_date' in st.session_state:
                    m_date = st.session_state.current_market_date
                    st.subheader(f"🌍 大盤與整體市況日誌: {m_date}")

                    m_note_res = conn.query(f"SELECT note FROM market_notes WHERE date='{m_date}'", ttl=0)
                    mkt_tid = f"MARKET_{m_date}"
                    mkt_imgs = conn.query(f"SELECT image_path FROM trade_images WHERE trade_id='{mkt_tid}' AND category='market'", ttl=0)
                    m_note_db = m_note_res.iloc[0]['note'] if not m_note_res.empty else ""

                    st.markdown("##### 📝 市場觀察與情緒紀錄")
                    st.text_area("記錄當天大盤多空趨勢、重要總經數據公佈或當下市場情緒...", value=m_note_db, height=200, key=f"mkt_note_{m_date}", on_change=auto_save_market_note)

                    st.divider()
                    st.markdown("##### 📋 總經數據或市況截圖")
                    
                    if not mkt_imgs.empty:
                        img_cols = st.columns(2)
                        for idx, g_row in mkt_imgs.iterrows():
                            g_url = g_row['image_path']
                            with img_cols[idx % 2]:
                                st.markdown(f'<div style="width: 100%; height: 300px; display: flex; justify-content: center; align-items: center; overflow: hidden; margin-bottom: 10px;"><img src="{g_url}" style="max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 5px;"></div>', unsafe_allow_html=True)
                                if st.button("🗑️ 刪除圖片", key=f"del_mkt_{g_url}"):
                                    with conn.session as s: s.execute(text("DELETE FROM trade_images WHERE image_path=:path"), {"path": g_url}); s.commit()
                                    try: supabase.storage.from_(STORAGE_BUCKET).remove([g_url.split('/')[-1]])
                                    except: pass
                                    st.rerun()
                    
                    st.divider()
                    up_col, paste_col = st.columns(2)
                    with up_col:
                        up_imgs = st.file_uploader("📂 上傳市況圖", accept_multiple_files=True, key=f"u_{mkt_tid}")
                        if up_imgs:
                            changed = False
                            for up_img in up_imgs:
                                file_name = f"{mkt_tid}_{up_img.name}"
                                file_bytes = up_img.getvalue()
                                try:
                                    supabase.storage.from_(STORAGE_BUCKET).upload(file=file_bytes, path=file_name, file_options={"content-type": "image/png"})
                                    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(file_name)
                                    with conn.session as s: s.execute(text("INSERT INTO trade_images (trade_id, image_path, category) VALUES (:tid, :path, 'market')"), {"tid": mkt_tid, "path": public_url}); s.commit()
                                    changed = True
                                except Exception as e: st.error(f"上傳失敗: {e}")
                            if changed: st.rerun()

                    with paste_col:
                        st.write("📋 點擊按鈕貼上市況圖")
                        pasted_img = paste_image_button("一鍵貼上市況圖", key=f"p_{mkt_tid}")
                        if pasted_img and pasted_img.image_data is not None:
                            img_hash = hashlib.md5(pasted_img.image_data.tobytes()).hexdigest()
                            if st.session_state.get(f"last_hash_{mkt_tid}") != img_hash:
                                file_name = f"{mkt_tid}_{uuid.uuid4().hex[:8]}.png"
                                img_byte_arr = io.BytesIO()
                                pasted_img.image_data.save(img_byte_arr, format='PNG')
                                try:
                                    supabase.storage.from_(STORAGE_BUCKET).upload(file=img_byte_arr.getvalue(), path=file_name, file_options={"content-type": "image/png"})
                                    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(file_name)
                                    with conn.session as s: s.execute(text("INSERT INTO trade_images (trade_id, image_path, category) VALUES (:tid, :path, 'market')"), {"tid": mkt_tid, "path": public_url}); s.commit()
                                    st.session_state[f"last_hash_{mkt_tid}"] = img_hash
                                    st.rerun()
                                except Exception as e: st.error(f"貼上上傳失敗: {e}")

                elif view_mode == 'trade' and 'current_trade' in st.session_state:
                    s = st.session_state.current_trade; tid = s['trade_id']

                    head_col1, head_col2 = st.columns([2, 1])
                    if "OPEN_" in tid:
                        with head_col1: st.subheader(f"📝 盤前與持倉追蹤: {s['Symbol']}")
                    else:
                        with head_col1: st.subheader(f"📝 檢討: {s['Symbol']} ({s['Exit_Date']})")
                    
                    with head_col2:
                        current_strat_res = conn.query(f"SELECT strategy_name FROM trade_strategy_map WHERE trade_id='{tid}'", ttl=0)
                        disc_res = conn.query(f"SELECT discipline FROM notes WHERE trade_id='{tid}'", ttl=0)

                        current_strat = current_strat_res.iloc[0]['strategy_name'] if not current_strat_res.empty else "未分類"
                        discipline_db = disc_res.iloc[0]['discipline'] if not disc_res.empty else "未評估"
                        
                        strat_options = ["未分類"] + available_strats
                        try: strat_index = strat_options.index(current_strat)
                        except ValueError: strat_index = 0
                        st.selectbox("指派策略標籤", strat_options, index=strat_index, key=f"strat_select_{tid}", on_change=save_trade_strategy)

                        disc_options = ["未評估", "✅ 符合紀律", "❌ 不符合紀律"]
                        try: disc_index = disc_options.index(discipline_db)
                        except ValueError: disc_index = 0
                        st.selectbox("紀律與情緒評估", disc_options, index=disc_index, key=f"disc_{tid}", on_change=auto_save_discipline)

                    note_res = conn.query(f"SELECT note, pre_plan, market_cond, initial_sl, max_risk_pct, trailing_sl FROM notes WHERE trade_id='{tid}'", ttl=0)
                    pre_plan_imgs = conn.query(f"SELECT image_path FROM trade_images WHERE trade_id='{tid}' AND category='pre_plan'", ttl=0)
                    general_imgs = conn.query(f"SELECT image_path FROM trade_images WHERE trade_id='{tid}' AND category='general'", ttl=0)
                    
                    if not note_res.empty:
                        note_db = note_res.iloc[0]['note'] if pd.notna(note_res.iloc[0]['note']) else ""
                        pre_plan_db = note_res.iloc[0]['pre_plan'] if pd.notna(note_res.iloc[0]['pre_plan']) else ""
                        initial_sl_db = note_res.iloc[0]['initial_sl'] if pd.notna(note_res.iloc[0]['initial_sl']) else 0.0
                        max_risk_pct_db = note_res.iloc[0]['max_risk_pct'] if pd.notna(note_res.iloc[0]['max_risk_pct']) else 1.0
                        trailing_sl_db = note_res.iloc[0]['trailing_sl'] if pd.notna(note_res.iloc[0]['trailing_sl']) else 0.0
                    else:
                        note_db, pre_plan_db, initial_sl_db, max_risk_pct_db, trailing_sl_db = "", "", 0.0, 1.0, 0.0

                    st.divider()
                    st.markdown(f"##### 📈 {s['Symbol']} 走勢與交易點位 (TradingView)")
                    with st.spinner("正在取得報價與渲染圖表..."):
                        transactions_history = s.get('transactions', [])
                        tv_chart = draw_tv_chart(s['Symbol'], transactions_history, initial_sl=initial_sl_db)
                        if tv_chart is None: st.warning(f"無法從 Yahoo 獲取 {s['Symbol']} 的歷史報價。請確認代號。")
                        elif isinstance(tv_chart, str): st.error(f"圖表載入失敗: {tv_chart}")
                        else: tv_chart.load()
                    st.divider()

                    left_main_col, right_main_col = st.columns([6, 1])
                    with left_main_col:
                        text_col1, text_col2 = st.columns(2)
                        with text_col1:
                            st.markdown("##### 🏹 盤前規劃")
                            st.text_area("記錄進場前的想法...", value=pre_plan_db, height=130, key=f"pre_plan_{tid}", on_change=auto_save_pre_plan, label_visibility="collapsed")
                        with text_col2:
                            st.markdown("##### 📝 交易檢討")
                            st.text_area("撰寫心得紀錄 (自動存檔)...", value=note_db, height=130, key=f"note_{tid}", on_change=auto_save_note, label_visibility="collapsed")

                        st.markdown("<hr style='margin: 10px 0;'>", unsafe_allow_html=True)
                        
                        if not general_imgs.empty:
                            img_cols = st.columns(2) 
                            for idx, g_row in general_imgs.iterrows():
                                g_url = g_row['image_path']
                                with img_cols[idx % 2]: 
                                    img_html = f'''
                                    <style>
                                        .review-zoom-box {{ width: 100%; height: 220px; display: flex; justify-content: center; align-items: center; overflow: hidden; background-color: #f8f9fa; border-radius: 6px; border: 1px solid #e0e3eb; cursor: zoom-in; }}
                                        .review-zoom-box img {{ max-width: 100%; max-height: 100%; object-fit: contain; }}
                                        img:fullscreen {{ max-width: 100% !important; max-height: 100% !important; object-fit: contain !important; background-color: #000000 !important; }}
                                    </style>
                                    <div class="review-zoom-box"><img src="{g_url}" title="連點兩下全螢幕放大" ondblclick="toggleFullscreen(this)"></div>
                                    <script>
                                        function toggleFullscreen(elem) {{
                                            if (!document.fullscreenElement) {{ if (elem.requestFullscreen) {{ elem.requestFullscreen(); }} else if (elem.webkitRequestFullscreen) {{ elem.webkitRequestFullscreen(); }}
                                            }} else {{ if (document.exitFullscreen) {{ document.exitFullscreen(); }} }}
                                        }}
                                    </script>
                                    '''
                                    st.components.v1.html(img_html, height=230)
                                    if st.button("🗑️ 刪除此圖片", key=f"del_gen_{g_url}"):
                                        with conn.session as sq: sq.execute(text("DELETE FROM trade_images WHERE image_path=:path"), {"path": g_url}); sq.commit()
                                        try: supabase.storage.from_(STORAGE_BUCKET).remove([g_url.split('/')[-1]])
                                        except: pass
                                        st.rerun()

                        st.write("📋 點擊按鈕貼上檢討圖")
                        pasted_img = paste_image_button("一鍵貼上檢討圖", key=f"p_{tid}")
                        if pasted_img and pasted_img.image_data is not None:
                            img_hash = hashlib.md5(pasted_img.image_data.tobytes()).hexdigest()
                            if st.session_state.get(f"last_hash_{tid}") != img_hash:
                                file_name = f"{tid}_{uuid.uuid4().hex[:8]}.png"
                                img_byte_arr = io.BytesIO()
                                pasted_img.image_data.save(img_byte_arr, format='PNG')
                                try:
                                    supabase.storage.from_(STORAGE_BUCKET).upload(file=img_byte_arr.getvalue(), path=file_name, file_options={"content-type": "image/png"})
                                    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(file_name)
                                    with conn.session as sq: sq.execute(text("INSERT INTO trade_images (trade_id, image_path, category) VALUES (:tid, :path, 'general')"), {"tid": tid, "path": public_url}); sq.commit()
                                    st.session_state[f"last_hash_{tid}"] = img_hash
                                    st.rerun()
                                except Exception as e: st.error(f"貼上上傳失敗: {e}")

                        st.info("💡 內容將在您停止輸入或切換頁面時自動儲存。")

                    with right_main_col:
                        st.markdown("##### 🛡️ 風險與部位控管")
                        buys = [t for t in s.get('transactions', []) if t['type'] == 'Buy']
                        total_buy_qty = sum(t['qty'] for t in buys)
                        total_buy_cost = sum(t['price'] * t['qty'] for t in buys)
                        avg_buy_price = total_buy_cost / total_buy_qty if total_buy_qty > 0 else 0
                        st.caption(f"當前動態總資金: ${current_capital:,.0f}")
                        st.caption(f"平均買進成本: ${avg_buy_price:.2f}")

                        sl_col1, sl_col2 = st.columns(2)
                        with sl_col1: new_sl = st.number_input("初始停損點 ($)", min_value=0.0, value=float(initial_sl_db), step=0.1, key=f"sl_{tid}", on_change=auto_save_risk_params)
                        with sl_col2: new_trail = st.number_input("追蹤止損點 ($)", min_value=0.0, value=float(trailing_sl_db), step=0.1, key=f"trail_{tid}", on_change=auto_save_risk_params)
                            
                        risk_options = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
                        default_risk_index = risk_options.index(max_risk_pct_db) if max_risk_pct_db in risk_options else 3
                        new_risk = st.selectbox("每筆最大損失 (%)", options=risk_options, index=default_risk_index, key=f"risk_{tid}", on_change=auto_save_risk_params)

                        sl_pct = 0.0; actual_pos_size_pct = 0.0; actual_position_dollar = total_buy_cost
                        actual_risk_dollar = 0.0; actual_risk_pct = 0.0; r_multiple = 0.0

                        if current_capital > 0: actual_pos_size_pct = (actual_position_dollar / current_capital) * 100
                        if avg_buy_price > 0 and new_sl > 0:
                            sl_dist = avg_buy_price - new_sl 
                            sl_pct = (sl_dist / avg_buy_price) * 100
                            if sl_pct > 0:
                                actual_risk_dollar = sl_dist * total_buy_qty
                                if current_capital > 0: actual_risk_pct = (actual_risk_dollar / current_capital) * 100

                        actual_pnl = s.get('pnl', 0)
                        if actual_risk_dollar > 0 and actual_pnl != 0: r_multiple = actual_pnl / actual_risk_dollar

                        with st.container(border=True):
                            st.metric("實際總資金占比", f"{actual_pos_size_pct:.2f}%")
                            st.metric("實際買入股數", f"{int(total_buy_qty)} 股")
                            st.metric("實際投入金額", f"${actual_position_dollar:,.0f}")
                            
                            if sl_pct > 0:
                                st.divider()
                                st.metric("建倉初始風險 %", f"{actual_risk_pct:.2f}%", delta=f"設定上限: {new_risk}%", delta_color="off")
                                active_stop = new_trail if new_trail > 0 else new_sl
                                locked_pnl = (active_stop - avg_price) * total_buy_qty
                                
                                if locked_pnl >= 0: st.metric("若觸價將鎖定利潤", f"${locked_pnl:,.0f}", delta="已保本/獲利", delta_color="normal")
                                else: st.metric("若觸價將面臨虧損", f"${locked_pnl:,.0f}", delta="風險仍在", delta_color="inverse")
                                
                                if actual_pos_size_pct > 100: st.error("⚠️ 實際倉位超過 100%，請確認是否過度使用槓桿。")
                                if actual_risk_pct > new_risk: st.warning(f"⚠️ 建倉實際風險 ({actual_risk_pct:.2f}%) 已超出單筆上限 ({new_risk}%)！")
                            else:
                                st.divider()
                                st.metric("初始停損與風險", "請輸入有效的停損點")

                        if s.get('pnl', 0) != 0:
                            st.divider()
                            r_color = "normal" if r_multiple >= 0 else "inverse"
                            st.metric("實現 R 倍數", f"{r_multiple:.2f} R", delta=f"{r_multiple:.2f}", delta_color=r_color)

                    st.divider()
                    st.markdown("##### 📋 規劃圖附件")
                    
                    if not pre_plan_imgs.empty:
                        for _, p_row in pre_plan_imgs.iterrows():
                            p_url = p_row['image_path']
                            with st.container(border=True):
                                img_html = f'<div style="width: 100%; display: flex; justify-content: center; align-items: flex-start;"><img src="{p_url}" style="width: 100%; max-height: 320px; object-fit: contain; border-radius: 5px;"></div>'
                                st.markdown(img_html, unsafe_allow_html=True)
                            if st.button("🗑️ 刪除", key=f"del_pre_{p_url}"):
                                with conn.session as sq: sq.execute(text("DELETE FROM trade_images WHERE image_path=:path"), {"path": p_url}); sq.commit()
                                try: supabase.storage.from_(STORAGE_BUCKET).remove([p_url.split('/')[-1]])
                                except: pass
                                st.rerun()
                    else: st.caption("尚未貼上規劃圖")
                    
                    p_paste = paste_image_button("貼上", key=f"pp_paste_{tid}")
                    if p_paste and p_paste.image_data is not None:
                        p_hash = hashlib.md5(p_paste.image_data.tobytes()).hexdigest()
                        if st.session_state.get(f"pp_hash_{tid}") != p_hash:
                            file_name = f"PP_{tid}_{uuid.uuid4().hex[:5]}.png"
                            img_byte_arr = io.BytesIO()
                            p_paste.image_data.save(img_byte_arr, format='PNG')
                            try:
                                supabase.storage.from_(STORAGE_BUCKET).upload(file=img_byte_arr.getvalue(), path=file_name, file_options={"content-type": "image/png"})
                                public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(file_name)
                                with conn.session as sq: sq.execute(text("INSERT INTO trade_images (trade_id, image_path, category) VALUES (:tid, :path, 'pre_plan')"), {"tid": tid, "path": public_url}); sq.commit()
                                st.session_state[f"pp_hash_{tid}"] = p_hash
                                st.rerun()
                            except Exception as e: st.error(f"貼上上傳失敗: {e}")
                else: st.info("請點擊左側標的進行詳細檢討。")
else:
    st.title("📈 交易數據中心")
    st.info("請於左側上傳 CSV 檔案，或點擊側邊欄「系統設定與匯入」同步雲端資料。")
    st.markdown("""
    <div style='text-align: center; color: gray; margin-top: 80px; padding: 40px; border-radius: 10px; background-color: #f8f9fa; border: 2px dashed #e0e3eb;'>
        <h2>📊 尚未載入交易紀錄</h2>
        <p style='font-size: 16px; margin-top: 10px;'>系統將自動計算您的 R 倍數、勝率與資金曲線，並為您的每筆交易建立專屬的覆盤空間。</p>
        <p style='font-size: 14px; opacity: 0.7;'>支援 IBKR / 手動匯出的 Daily_Report.csv</p>
    </div>
    """, unsafe_allow_html=True)
