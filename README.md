# polymarket-smart-money-tracker
Analyze trading activity on Polymarket prediction markets and identify active traders using public API data.



目前 demo 已經實現了一個基礎的 smart money 分析 pipeline：
首先，通過 Polymarket API 獲取已結束市場和對應的交易數據；
然後對每個地址進行聚合，計算其在不同 market 中的勝率，並輸出前幾名的排行榜。
在數據處理上，我做了一些篩選來提高結果的代表性，比如：
只保留開盤早期的交易，用來評估提前判斷能力
過濾接近確定性的交易（price 接近 0 或 1）
對同一事件的重覆 market 做去重
設置最小交易次數來降低噪聲

同時，我使用 JSON 做了本地數據持久化，方便後續分析和覆現結果；
並通過分頁（offset）來擴大 sample size，盡量獲取更早期的數據。

目前主要遇到的問題是性能和數據質量之間的 trade-off：
為了獲得更有信息量的 early trades，需要抓取更多歷史數據，但這會帶來較大的 API 請求開銷和運行時間。

Currently, the demo has implemented a basic smart money analysis pipeline:
First, it retrieves data on closed markets and corresponding trades via the Polymarket API;
Then, it aggregates data for each address, calculates its win rate across different markets, and outputs a leaderboard of the top performers.
In terms of data processing, I applied several filters to improve the representativeness of the results, such as:
Retaining only trades from the early stages of market opening to assess the ability to make early predictions
Filtering out near-certain trades (where the price is close to 0 or 1)
Removing duplicates for the same event across multiple markets
Setting a minimum number of trades to reduce noise

Additionally, I used JSON for local data persistence to facilitate subsequent analysis and result reproduction;
and employed pagination (offset) to expand the sample size and capture as much early-stage data as possible.

The main challenge I currently face is the trade-off between performance and data quality:
To obtain more informative early trades, I need to scrape more historical data, but this results in significant API request overhead and increased runtime.
