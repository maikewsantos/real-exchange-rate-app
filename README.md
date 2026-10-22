# Real Exchange Rate Explorer

A Streamlit app that computes and plots the bilateral real exchange rate (RER) between any two IMF economies, including the Euro area, using monthly CPI and exchange-rate data from the IMF SDMX 3.0 API. Styled with the Kellogg School of Management (Northwestern Purple) palette.

## What it computes

| Symbol | Definition |
|---|---|
| E1(t), E2(t) | IMF period-average exchange rates, local currency units per 1 USD |
| S(t) | Units of Country 2 currency per 1 unit of Country 1 currency: **S(t) = E2(t) / E1(t)** |
| P1(t), P2(t) | Headline CPI index for Country 1 and Country 2 |
| RER(t) | **S(t) × P1(t) / P2(t)** |
| RER\* | Average of RER(t) over the valid months in the selected window |
| Real Index(t) | **100 × RER(t) / RER\*** |

A month is valid only when E1, E2, P1 and P2 all exist and are positive. RER\* and the Real Index are recalculated every time the start or end month changes. Nominal S(t) is always shown in actual currency units and is never normalized.

**Reading the index:** a Real Index above 100 means Country 1's goods are more expensive relative to Country 2's than on average over the sample (a real appreciation of Country 1). Below 100 means a real depreciation of Country 1.

## Installation (Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

The app opens at http://localhost:8501.

## Using the app

1. **Countries:** pick Country 1 and Country 2 in the sidebar. The list comes from the IMF country codelist; if that call fails, a built-in list of major economies is used. You can also type any IMF code (for example `KEN`) in the "other IMF code" box. The Euro area is `G163`.
2. **Sample period:** drag the slider to set the start and end months. Only months where both countries have data are offered.
3. **Chart:** choose Real, Nominal, or Both. "Both" uses two y-axes: Real Index on the left (solid purple), S(t) on the right (dotted gray). A log scale is available for S(t), which helps for high-inflation currencies.
4. **CSV:** the download button exports every month in the selected window.

## CSV columns

| Column | Meaning |
|---|---|
| `month` | YYYY-MM |
| `E1_<C1>_per_USD` | Country 1 currency per USD (1.0 for the USA) |
| `E2_<C2>_per_USD` | Country 2 currency per USD |
| `P1_CPI_<C1>` | Country 1 CPI |
| `P2_CPI_<C2>` | Country 2 CPI |
| `S_<C2>_per_1_<C1>` | Nominal rate S(t) |
| `RER` | S(t) × P1(t) / P2(t), blank for invalid months |
| `valid_month` | True if all four inputs exist and are positive |
| `RER_star` | Average RER over valid months in the window |
| `Real_Index` | 100 × RER / RER\*, blank for invalid months |

Values are written with up to 12 significant digits; nothing is rounded before calculation.

## IMF series used

- Base URL: `https://api.imf.org/external/sdmx/3.0`
- Endpoint: `/data/dataflow/IMF.STA/{DATAFLOW}/+/{KEY}`
- CPI: dataflow `CPI`, key `{ISO3}.CPI._T.IX.M` (all items, index, monthly)
- Exchange rate: dataflow `ER`, key `{ISO3}.XDC_USD.PA_RT.M` (domestic currency per USD, period average, monthly)
- Country list: `/structure/codelist/IMF/CL_COUNTRY/+`

The app requests SDMX-CSV and falls back to SDMX-JSON. Responses are cached for 24 hours. Use the "Clear cache" option in Streamlit's menu to force a refresh.

## Known limitations

- **Coverage varies.** Many countries have CPI or exchange-rate series that start well after 1976, have gaps, or stop early. The app drops incomplete months and reports how many were dropped.
- **Euro area.** `G163` exchange-rate data can only begin with the euro in 1999. Euro-area CPI history may differ in length from the exchange-rate series.
- **USA.** The IMF does not publish a USD-per-USD rate, so the app sets E = 1.0 for the United States.
- **Redenominations and currency changes.** For countries that redenominated or changed currency (for example Brazil, Argentina, Turkey, or euro adopters), check how the IMF expresses the historical series before interpreting long-run nominal levels. The Real Index is less affected because it is a ratio to its own sample average, but a break in the underlying series would still show up.
- **Base years.** CPI base years differ across countries. This does not affect the Real Index (it is normalized by RER\*), but the raw RER level has no economic meaning on its own.
- **API changes.** The IMF SDMX 3.0 API, dataflow versions, or codes may change. If a series stops loading, check the key on https://data.imf.org.

## Files

- `app.py`: the Streamlit app (data access, calculations, charts, CSV export)
- `requirements.txt`: dependencies
- `README.md`: this file

## Credits

Data: International Monetary Fund. Colors: Northwestern Purple (#4E2A84) and its tints and shades, plus Northwestern Rich Black tints.
