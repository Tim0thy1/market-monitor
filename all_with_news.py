#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
import sys
import threading
import pytz
import json
import re
import pickle
from datetime import datetime, timezone
from yahooquery import Ticker
from googletrans import Translator

# ====== 虚拟币成本 ======
costs = {
    "BTCUSDT": 0.0,
    "ETHUSDT": 3811,
    "BNBUSDT": 0.0
}

# ====== 美股文件路径 ======
STOCK_FILE = "stocks.txt"

# ====== 新闻API URL ======
NEWS_API_URL = "https://static.mktnews.net/json/flash/en.json"

# ====== 新闻翻译缓存文件 ======
NEWS_CACHE_FILE = "news_translation_cache.pkl"

# ====== 控制退出和手动刷新 ======
stop_flag = False
manual_refresh_flag = False
show_more_news = False

# ====== 辅助函数 ======
def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def key_listener():
    global stop_flag, manual_refresh_flag, show_more_news
    while True:
        key = sys.stdin.read(1).lower()
        if key == 'q':
            stop_flag = True
            break
        elif key == 'w':
            manual_refresh_flag = True
            show_more_news = True
            print("\n🔄 正在手动刷新所有数据...")
            sys.stdout.flush()  # 立即显示提示信息

# ====== Gate.io ======
def fetch_prices_from_gate():
    prices = {}
    try:
        for symbol in ["BTC_USDT", "ETH_USDT", "BNB_USDT"]:
            r = requests.get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={symbol}", timeout=5)
            r.raise_for_status()
            data = r.json()[0]
            sym_clean = symbol.replace("_", "")
            prices[sym_clean] = float(data["last"])
    except Exception as e:
        print(f"❌ Gate API 获取失败：{e}")
    return prices

# ====== 判断美东时段并返回对应的 price/change 字段 key ======
def detect_session():
    tz = pytz.timezone("America/New_York")
    now = datetime.now(tz)
    h = now.hour + now.minute / 60
    
    # 根据美东时间判断当前应该使用的价格字段
    if 4 <= h < 9.5:
        # 盘前交易时段：04:00-09:30 AM EDT
        phase = "盘前交易"
        active_price_key = "preMarketPrice"
        active_change_key = "preMarketChangePercent"
    elif 9.5 <= h < 16:
        # 正常交易时段：09:30 AM - 04:00 PM EDT
        phase = "正常交易"
        active_price_key = "regularMarketPrice"
        active_change_key = "regularMarketChangePercent"
    elif 16 <= h < 20:
        # 盘后交易时段：04:00 PM - 08:00 PM EDT
        phase = "盘后交易"
        active_price_key = "postMarketPrice"
        active_change_key = "postMarketChangePercent"
    else:
        # 隔夜时段：08:00 PM - 次日 04:00 AM EDT
        phase = "隔夜时段"
        active_price_key = "overnightMarketPrice"
        active_change_key = "overnightMarketChangePercent"
    
    return now.strftime("%Y-%m-%d %H:%M:%S"), phase, active_price_key, active_change_key

# ====== 读取 stocks.txt（支持第二列 1/2 标记） ======
def read_stocks(file_path):
    tickers = []
    marks = {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if not parts:
                    continue
                t = parts[0].upper()
                tickers.append(t)
                mark = ""
                if len(parts) > 1:
                    if parts[1] == "1":
                        mark = "🚀"
                    elif parts[1] == "2":
                        mark = "⚡"
                if mark:
                    marks[t] = mark
    except FileNotFoundError:
        return [], {}
    return tickers, marks

# ====== 抓取并构建 DataFrame（优化时段价格获取逻辑） ======
def fetch_all_stocks(file_path, active_price_key, active_change_key):
    tickers, marks = read_stocks(file_path)
    if not tickers:
        return pd.DataFrame()

    tk = Ticker(tickers, params={"overnightPrice": "true"})
    quotes_all = tk.quotes

    rows = []
    for t in tickers:
        q = quotes_all.get(t, {}) if isinstance(quotes_all, dict) else {}
        if not isinstance(q, dict):
            q = {}

        # 所有可能的价格字段对
        field_pairs = [
            ("preMarketPrice", "preMarketChangePercent"),
            ("regularMarketPrice", "regularMarketChangePercent"),
            ("postMarketPrice", "postMarketChangePercent"),
            ("overnightMarketPrice", "overnightMarketChangePercent"),
        ]

        # previous close 用于计算 fallback 的百分比
        prev_close = q.get("regularMarketPrice")

        # 1) 优先获取当前时段的价格和涨跌幅
        active_price = q.get(active_price_key)
        active_change = q.get(active_change_key)

        # 2) 如果当前时段数据不可用，按时间逻辑回退
        if active_price is None:
            # 根据当前时段智能回退
            if active_price_key == "preMarketPrice":
                # 盘前时段：回退到前一日收盘价或隔夜价格
                fallback_order = ["overnightMarketPrice", "regularMarketPrice", "postMarketPrice"]
            elif active_price_key == "regularMarketPrice":
                # 正常交易时段：回退到盘前价格或前一日收盘价
                fallback_order = ["preMarketPrice", "postMarketPrice", "overnightMarketPrice"]
            elif active_price_key == "postMarketPrice":
                # 盘后时段：回退到正常交易价格或盘前价格
                fallback_order = ["regularMarketPrice", "preMarketPrice", "overnightMarketPrice"]
            else:
                # 隔夜时段：回退到盘后价格或正常交易价格
                fallback_order = ["postMarketPrice", "regularMarketPrice", "preMarketPrice"]
            
            for pf in fallback_order:
                p = q.get(pf)
                if p is not None:
                    active_price = p
                    # 找到对应的涨跌幅字段
                    matching_cf = None
                    for pair in field_pairs:
                        if pair[0] == pf:
                            matching_cf = pair[1]
                            break
                    if matching_cf:
                        active_change = q.get(matching_cf)
                    break

        # 3) 涨跌幅直接从API字段获取，不再手动计算

        # 格式化数据，为Last Close添加固定宽度以对齐emoji
        active_price_s = f"{float(active_price):.2f}" if active_price is not None else "N/A"
        active_change_s = f"{float(active_change):+.2f}%" if active_change is not None else "N/A"
        prev_close_s = f"{float(prev_close):.2f}".rjust(8) if prev_close is not None else "N/A".rjust(8)

        prefix = marks.get(t, "")
        priority = 1 if prefix else 0
        
        # 为没有标记的股票添加占位符，保持对齐
        if prefix:
            ticker_display = prefix + "" + t
        else:
            ticker_display = "  " + t  # 两个空格占位符，与⚡长度相同

        rows.append({
            "Last Close": prev_close_s,
            "Ticker": ticker_display,
            "Priority": priority,
            "Price": active_price_s,
            "Change": active_change_s
        })

    df = pd.DataFrame(rows)
    return df

# ====== 新闻模块 ======
def fetch_news_data():
    """获取新闻数据"""
    try:
        # 添加时间戳参数避免缓存
        timestamp = int(time.time() * 1000)
        url = f"{NEWS_API_URL}?t={timestamp}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"❌ 新闻API获取失败: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"❌ 新闻JSON解析失败: {e}")
        return None

def format_news_time(time_str):
    """格式化新闻时间字符串，转换为东8区时间"""
    try:
        # 解析ISO格式时间
        dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
        # 转换为东8区时间
        china_tz = pytz.timezone('Asia/Shanghai')
        china_time = dt.astimezone(china_tz)
        # 返回格式化的时间字符串和datetime对象
        return china_time.strftime('%m-%d %H:%M'), china_time
    except ValueError:
        return time_str[:10], None

def clean_news_content(content):
    """清理新闻内容，移除HTML标签"""
    # 移除HTML标签
    content = re.sub(r'<[^>]+>', '', content)
    # 移除多余的空白字符
    content = re.sub(r'\s+', ' ', content).strip()
    return content

# ====== 新闻翻译缓存管理 ======
def load_translation_cache():
    """加载翻译缓存"""
    try:
        if os.path.exists(NEWS_CACHE_FILE):
            with open(NEWS_CACHE_FILE, 'rb') as f:
                return pickle.load(f)
    except Exception as e:
        print(f"加载翻译缓存失败: {e}")
    return {}

def save_translation_cache(cache):
    """保存翻译缓存"""
    try:
        with open(NEWS_CACHE_FILE, 'wb') as f:
            pickle.dump(cache, f)
    except Exception as e:
        print(f"保存翻译缓存失败: {e}")

def get_news_key(item):
    """生成新闻的唯一标识符"""
    # 使用时间和内容的前50个字符作为唯一标识
    time_str = item.get('time', '')
    content = item.get('data', {}).get('content', '')
    content_preview = clean_news_content(content)[:50]
    return f"{time_str}_{hash(content_preview)}"

def translate_news_text_cached(text, translator, cache, news_key):
    """带缓存的翻译功能"""
    if news_key in cache:
        return cache[news_key]
    
    try:
        if len(text.strip()) == 0:
            return text
        result = translator.translate(text, src='en', dest='zh-cn')
        translated = result.text
        cache[news_key] = translated
        return translated
    except Exception as e:
        error_msg = f"翻译失败: {e}"
        cache[news_key] = error_msg
        return error_msg

def fetch_latest_news(count=5):
    """获取最新的新闻，默认5条，可指定数量"""
    data = fetch_news_data()
    if not data or not isinstance(data, list):
        return []
    
    # 加载翻译缓存
    cache = load_translation_cache()
    
    # 初始化翻译器
    translator = Translator()
    
    # 按时间排序，获取最新的指定数量条新闻
    sorted_news = sorted(data, key=lambda x: x.get('time', ''), reverse=True)[:count]
    
    news_list = []
    for item in sorted_news:
        try:
            # 生成新闻唯一标识
            news_key = get_news_key(item)
            
            # 提取基本信息
            time_str = item.get('time', '')
            formatted_time, _ = format_news_time(time_str)
            important = item.get('important', 0)
            
            # 提取新闻内容
            data_section = item.get('data', {})
            content = data_section.get('content', '')
            
            # 清理内容
            clean_content_text = clean_news_content(content)
            
            # 重要性标识
            importance_mark = "🔥" if important == 1 else "📰"
            
            # 使用缓存翻译内容
            if clean_content_text:
                # 限制原文长度，避免翻译过长内容
                if len(clean_content_text) > 200:
                    clean_content_text = clean_content_text[:200] + "..."
                translated_content = translate_news_text_cached(clean_content_text, translator, cache, news_key)
                # 限制翻译后的长度
                if len(translated_content) > 100:
                    translated_content = translated_content[:100] + "..."
            else:
                translated_content = "无内容"
            
            news_list.append({
                'time': formatted_time,
                'importance': importance_mark,
                'content': translated_content
            })
            
        except Exception as e:
            continue
    
    # 保存更新后的缓存
    save_translation_cache(cache)
    
    return news_list

# ====== 主循环 ======
def main():
    global stop_flag, manual_refresh_flag, show_more_news
    threading.Thread(target=key_listener, daemon=True).start()
    print("按 Q 退出程序，按 W 手动刷新所有数据.\n")
    time.sleep(1)

    last_stock_update = 0
    last_news_update = 0
    stock_df = pd.DataFrame()
    news_list = []

    while not stop_flag:
        now = time.time()
        prices = fetch_prices_from_gate()
        ny_time, phase, active_price_key, active_change_key = detect_session()

        # 检查是否需要手动刷新
        force_refresh = manual_refresh_flag
        if manual_refresh_flag:
            manual_refresh_flag = False  # 重置标志

        # 每10分钟更新一次美股数据（或第一次或手动刷新）
        if now - last_stock_update > 300 or stock_df.empty or force_refresh:
            stock_df = fetch_all_stocks(STOCK_FILE, active_price_key, active_change_key)
            last_stock_update = now

        # 每5分钟更新一次新闻数据（或第一次或手动刷新）
        if now - last_news_update > 300 or not news_list or force_refresh:
            # 根据show_more_news标志决定显示数量
            news_count = 10 if show_more_news else 5
            news_list = fetch_latest_news(news_count)
            last_news_update = now
            
            # 如果是手动刷新触发的，在下一个周期重置为默认显示数量
            if force_refresh and show_more_news:
                # 设置一个标志，在下一个自动刷新周期重置
                pass

        clear_screen()
        
        # 如果是手动刷新，显示刷新完成提示
        if force_refresh:
            print("✅ 手动刷新完成！显示最新数据")
            print()
        
        print("=== 综合行情显示 ===")
        # print(f"⏰ 本地时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        # print(f"   美东时间: {ny_time}  - {phase}  (使用: {active_price_key} / {active_change_key})\n")
        print()

        # 虚拟币部分
        print("💰 虚拟币行情（Gate.io）:")
        for sym in ["BTCUSDT", "ETHUSDT", "BNBUSDT"]:
            price = prices.get(sym)
            if price is None:
                print(f"{sym}: 获取失败")
            else:
                cost = costs.get(sym, 0.0)
                if cost != 0:
                    if cost > 0:
                        # 做多：正常计算
                        pnl = price - cost
                        pnl_pct = pnl / cost * 100
                        position_type = "做多"
                    else:
                        # 做空：负成本价，价格下跌时盈利
                        pnl = abs(cost) - price  # 做空盈亏 = 开仓价格 - 当前价格
                        pnl_pct = pnl / abs(cost) * 100
                        position_type = "做空"
                    
                    print(f"{sym}: {price:,.2f} | {position_type}成本 {abs(cost):,.2f} | 盈亏 {pnl:+.2f} ({pnl_pct:+.2f}%)")
                else:
                    print(f"{sym}: {price:,.2f}")
        print()

        # 美股部分：只显示当前时段 price + change
        if not stock_df.empty:
            df_sorted = stock_df.copy()

            # 把 Change 字符串转换为数值用于排序（将 "N/A" 视作 0）
            def parse_pct(s):
                try:
                    return float(str(s).replace("%", "").replace("+", ""))
                except Exception:
                    return 0.0

            df_sorted["val"] = df_sorted["Change"].apply(parse_pct)
            df_sorted = df_sorted.sort_values(by=["Priority", "val"], ascending=[False, False]).drop(columns=["Priority", "val"])

            # add arrow
            def add_arrow(s):
                if str(s).startswith("+"):
                    return s + " "
                elif str(s).startswith("-"):
                    return s + " "
                return s

            df_sorted["Change"] = df_sorted["Change"].apply(add_arrow)

            print(f"📊 美股行情（当前时段价格 & 涨跌%）:")
            
            # 两列显示：将股票分成两组
            total_stocks = len(df_sorted)
            mid_point = (total_stocks + 1) // 2
            
            left_df = df_sorted.iloc[:mid_point].reset_index(drop=True)
            right_df = df_sorted.iloc[mid_point:].reset_index(drop=True)
            
            # 格式化左右两列的字符串
            left_strings = []
            right_strings = []
            
            for i in range(max(len(left_df), len(right_df))):
                # 左列
                if i < len(left_df):
                    row = left_df.iloc[i]
                    left_str = f"{row['Ticker']:<8} {row['Last Close']:<8} {row['Price']:<8} {row['Change']:<10}"
                else:
                    left_str = " " * 34
                left_strings.append(left_str)
                
                # 右列
                if i < len(right_df):
                    row = right_df.iloc[i]
                    right_str = f"{row['Ticker']:<8} {row['Last Close']:<8} {row['Price']:<8} {row['Change']:<10}"
                else:
                    right_str = ""
                right_strings.append(right_str)
            
            # 打印表头
            header_left = f"{'Ticker':<8} {'Last Close':<8} {'Price':<8} {'Change':<10}"
            header_right = f"{'Ticker':<8} {'Last Close':<8} {'Price':<8} {'Change':<10}"
            print(f"{header_left}    {header_right}")
            print("-" * 70)
            
            # 打印数据行
            for left, right in zip(left_strings, right_strings):
                if right.strip():
                    print(f"{left}    {right}")
                else:
                    print(left)
        else:
            print("📊 未找到美股列表 (请创建 stocks.txt)")

        print()

        # 新闻部分
        if news_list:
            news_count_display = len(news_list)
            print(f"📰 最新财经新闻（最近{news_count_display}条）:")
            print("-" * 70)
            for i, news in enumerate(news_list, 1):
                print(f"{news['time']} {news['importance']} {news['content']}")
        else:
            print("📰 新闻获取失败")

        print(f"\n(虚拟币每60秒刷新 | 美股每10分钟刷新 | 新闻每5分钟刷新 | 按 Q 退出 | 按 W 手动刷新)")
        
        # 在循环中检查是否需要重置新闻显示数量
        for i in range(60):
            if stop_flag:
                break
            # 在自动刷新周期中重置show_more_news标志
            if i == 30 and show_more_news and not manual_refresh_flag:
                show_more_news = False
            time.sleep(1)

    print("\n程序已退出。")

# ====== 启动入口 ======
if __name__ == '__main__':
    if os.name != 'nt':
        import termios, tty
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    try:
        main()
    finally:
        if os.name != 'nt':
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
