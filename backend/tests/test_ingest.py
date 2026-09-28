import io
import tempfile
import zipfile
from datetime import date, timedelta
from unittest.mock import patch

import pytest
import polars as pl

from app.engine import adjust
from app.engine import db as market_db
from app.ingest import amfi, bhavcopy
from app.ingest import actions as actions_mod
from app.ingest.universe import parse_equity_list, sync_securities

UDIFF_SAMPLE = pl.DataFrame(
    {
        "TradDt": ["2026-01-01", "2026-01-01"],
        "BizDt": ["2026-01-01", "2026-01-01"],
        "Sgmt": ["CM", "CM"],
        "Src": ["1", "1"],
        "FinInstrmTp": ["STK", "STK"],
        "FinInstrmId": ["1", "2"],
        "ISIN": ["INE002A01018", "INE999X01010"],
        "TckrSymb": ["RELIANCE", "SMECODE"],
        "SctySrs": ["EQ", "SM"],
        "OpnPric": [100.0, 10.0],
        "HghPric": [110.0, 11.0],
        "LwPric": [99.0, 9.0],
        "ClsPric": [105.0, 10.5],
        "TtlTradgVol": [1000, 500],
    }
)

LEGACY_CSV = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n"
    "RELIANCE,EQ,100,110,99,105,104,101,1000,105000,01-Jan-2000,10,INE002A01018\n"
    "SMECODE,SM,10,11,9,10.5,10,10,500,5000,01-Jan-2000,5,INE999X01010\n"
)


def test_parse_udiff_excludes_sme() -> None:
    out = bhavcopy.parse_udiff(UDIFF_SAMPLE)
    assert out.height == 1
    assert out["symbol"][0] == "RELIANCE"
    assert out["ts"][0] == date(2026, 1, 1)
    assert out["close"][0] == 105.0
    assert out["volume"][0] == 1000
    assert out["series"][0] == "EQ"
    assert out["instrument_type"][0] == "stock"


def test_parse_legacy_excludes_sme() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cm01JAN2000bhav.csv", LEGACY_CSV)
    out = bhavcopy.parse_legacy(buf.getvalue(), date(2000, 1, 1))
    assert out.height == 1
    assert out["symbol"][0] == "RELIANCE"
    assert out["ts"][0] == date(2000, 1, 1)
    assert out["close"][0] == 105.0
    assert out["volume"][0] == 1000
    assert out["instrument_type"][0] is None


def test_parse_purpose() -> None:
    assert actions_mod.parse_purpose("FACE VALUE SPLIT FROM RS.10/- TO RS.2/-") == ("split", 5.0)
    assert actions_mod.parse_purpose("SPLIT OF FV RS 10 TO RS 5") == ("split", 2.0)
    assert actions_mod.parse_purpose("BONUS 1:1") == ("bonus", 2.0)
    assert actions_mod.parse_purpose("Bonus Ratio 3 : 2") == ("bonus", 2.5)
    assert actions_mod.parse_purpose("ANNUAL GENERAL MEETING") == ("other", None)


def test_parse_action_item() -> None:
    item = {
        "symbol": "reliance",
        "exDate": "16-Jul-2024",
        "subject": "FACE VALUE SPLIT FROM RS.10/- TO RS.2/-",
        "isin": "INE002A01018",
    }
    parsed = actions_mod.parse_action_item(item)
    assert parsed is not None
    assert parsed["symbol"] == "RELIANCE"
    assert parsed["ex_date"] == date(2024, 7, 16)
    assert parsed["action_type"] == "split"
    assert parsed["factor"] == 5.0


def test_apply_adjustments_split() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["X", "X", "X", "X"],
            "ts": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)],
            "open": [100.0, 100.0, 50.0, 50.0],
            "high": [110.0, 110.0, 55.0, 55.0],
            "low": [90.0, 90.0, 45.0, 45.0],
            "close": [105.0, 105.0, 52.5, 52.5],
        }
    )
    actions = pl.DataFrame(
        {
            "symbol": ["X"],
            "ex_date": [date(2026, 1, 3)],
            "factor": [2.0],
        }
    )
    out = adjust.apply_corporate_adjustments(df, actions)
    assert out["close"][0] == 52.5
    assert out["close"][1] == 52.5
    assert out["close"][2] == 52.5
    assert out["close"][3] == 52.5
    assert out["high"][1] == 55.0
    assert out["high"][2] == 55.0


def test_apply_adjustments_compound() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["X", "X", "X"],
            "ts": [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
            "open": [80.0, 40.0, 20.0],
            "high": [88.0, 44.0, 22.0],
            "low": [72.0, 36.0, 18.0],
            "close": [84.0, 42.0, 21.0],
        }
    )
    actions = pl.DataFrame(
        {
            "symbol": ["X", "X"],
            "ex_date": [date(2026, 1, 2), date(2026, 1, 3)],
            "factor": [2.0, 2.0],
        }
    )
    out = adjust.apply_corporate_adjustments(df, actions)
    assert out["close"][0] == 21.0
    assert out["close"][1] == 21.0
    assert out["close"][2] == 21.0


def test_apply_adjustments_ignores_other_actions() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["X", "X"],
            "ts": [date(2026, 1, 1), date(2026, 1, 2)],
            "open": [100.0, 100.0],
            "high": [110.0, 110.0],
            "low": [90.0, 90.0],
            "close": [105.0, 105.0],
        }
    )
    actions = pl.DataFrame(
        {
            "symbol": ["X"],
            "ex_date": [date(2026, 1, 2)],
            "factor": [None],
        }
    )
    out = adjust.apply_corporate_adjustments(df, actions)
    assert out["close"].to_list() == [105.0, 105.0]


def test_parse_nav_all() -> None:
    content = (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment/ ISIN Growth;"
        "Scheme Name;Net Asset Value;Date\n"
        "\n"
        "Mutual Fund\n"
        "119551;INF209K01157;INF209K01165;Aditya Birla Sun Life Banking & PSU Debt Fund-DIRECT-Growth;272.3504;06-Mar-2026\n"
        "119552;INF209K01179;;Aditya Birla Sun Life Liquid Fund-DIRECT-Payout;300.1234;06-Mar-2026\n"
        "BADLINE;NOTANISIN;;Broken;0;06-Mar-2026\n"
        "\n"
        "ETF\n"
        "119597;INF737KA17B4;;Nippon India ETF Nifty 50 BeES;250.00;06-Mar-2026\n"
    )
    out = amfi.parse_nav_all(content)
    assert out.height == 3
    assert out["symbol"][0] == "INF209K01157"
    assert out["close"][0] == 272.3504
    assert out["ts"][0] == date(2026, 3, 6)
    assert out["series"][0] == "MF"
    assert out["volume"][0] is None
    etf_rows = out.filter(pl.col("name").str.contains("ETF"))
    assert etf_rows["instrument_type"].to_list() == ["etf"]


def test_insert_ohlcv_does_not_overwrite() -> None:
    market_db.merge_ohlcv(
        pl.DataFrame(
            {
                "symbol": ["TEST1"],
                "ts": [date(2026, 5, 1)],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [100],
            }
        )
    )
    market_db.insert_ohlcv(
        pl.DataFrame(
            {
                "symbol": ["TEST1"],
                "ts": [date(2026, 5, 1)],
                "open": [99.0],
                "high": [99.0],
                "low": [99.0],
                "close": [99.0],
                "volume": [999],
            }
        )
    )
    market_db.insert_ohlcv(
        pl.DataFrame(
            {
                "symbol": ["TEST1"],
                "ts": [date(2026, 5, 2)],
                "open": [10.5],
                "high": [11.5],
                "low": [10.0],
                "close": [11.0],
                "volume": [200],
            }
        )
    )
    rows = market_db.fetch_ohlcv("TEST1")
    assert rows.height == 2
    assert rows["close"][0] == 10.5
    assert rows["volume"][0] == 100
    assert rows["close"][1] == 11.0


def test_parse_equity_list_and_sync() -> None:
    csv = (
        "SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE\n"
        "RELIANCE,Reliance Industries Limited,EQ,29-Nov-1995,10,1,INE002A01018,10\n"
        "NIFTYBEES,Nippon India ETF Nifty 50 BeES,EQ,08-Jan-2002,10,1,INF737KA17B4,10\n"
        "SMECODE,Some SME Limited,SM,01-Jan-2020,10,1,INE999X01010,10\n"
    )
    df = parse_equity_list(csv.encode())
    assert df.height == 2
    symbols = df["symbol"].to_list()
    assert "RELIANCE" in symbols
    assert "SMECODE" not in symbols
    assert df["instrument_type"][df["symbol"].to_list().index("NIFTYBEES")] == "etf"

    n = sync_securities(df)
    assert n == 2

    from sqlalchemy.orm import Session

    from app.state.db import SessionLocal
    from app.state.models import Security

    with SessionLocal() as session:
        sec = session.query(Security).filter_by(symbol="RELIANCE").first()
        assert sec is not None
        assert sec.isin == "INE002A01018"
        assert sec.instrument_type == "stock"


def test_load_actions_from_db() -> None:
    from sqlalchemy.orm import Session

    from app.state.db import SessionLocal
    from app.state.models import CorporateAction

    with SessionLocal() as session:
        session.add(
            CorporateAction(
                symbol="TESTX",
                ex_date=date(2026, 2, 1),
                action_type="split",
                factor=4.0,
                purpose="SPLIT OF FV RS 4 TO RS 1",
            )
        )
        session.commit()

    actions = adjust.load_actions(["TESTX"])
    assert actions.height == 1
    assert actions["symbol"][0] == "TESTX"
    assert actions["ex_date"][0] == date(2026, 2, 1)
    assert actions["factor"][0] == 4.0


def test_parse_mfapi_history() -> None:
    from app.ingest.amfi import parse_mfapi_history

    payload = {
        "meta": {
            "scheme_code": 118989,
            "scheme_name": "Parag Parikh Flexi Cap Fund Direct Growth",
            "isin_growth_reinvestment": "INF879O01019",
        },
        "data": [
            {"date": "02-01-2006", "nav": "10.1234"},
            {"date": "03-01-2006", "nav": "10.55"},
            {"date": "bad-date", "nav": "1"},
        ],
    }
    df = parse_mfapi_history(payload)
    assert df.height == 2
    assert df["symbol"][0] == "INF879O01019"
    assert df["close"][0] == 10.1234
    assert df["ts"][0] == date(2006, 1, 2)

    missing_isin = parse_mfapi_history({"meta": {"scheme_code": 1}, "data": []})
    assert missing_isin.height == 0


def test_indices_upsert_and_fetch() -> None:
    from datetime import datetime

    from app.engine import db as market_db

    df = pl.DataFrame(
        {
            "symbol": ["NIFTY_500"] * 4,
            "ts": [datetime(2026, 2, 1 + i) for i in range(4)],
            "close": [20000.0 + i for i in range(4)],
        }
    )
    assert market_db.insert_indices(df) == 4
    # additive: no overwrite on conflict
    assert market_db.insert_indices(df) == 0
    out = market_db.fetch_indices("NIFTY_500")
    assert out.height == 4
    assert out["close"][0] == 20000.0


def test_parse_scrip_csv() -> None:
    from app.ingest.zip_archive import parse_scrip_csv

    csv = (
        "Date,Symbol,Series,Prev Close,Open,High,Low,Last,Close,VWAP,Volume,Turnover,Trades,Deliverable Volume,%Deliverble\n"
        "2005-06-01,RELIANCE,EQ,800,805,810,799,,805.75,805.26,3717450,76305138500000.0,,,\n"
        "2005-06-01,SMECODE,SM,10,10.5,11,9,10,10.5,10,500,5000,,,\n"
        "bad-date,RELIANCE,EQ,800,805,810,799,,806,805,3717450,76305138500000.0,,,\n"
    )
    out = parse_scrip_csv(csv.encode())
    assert out.height == 1
    assert out["symbol"][0] == "RELIANCE"
    assert out["close"][0] == 805.75
    assert out["volume"][0] == 3717450
    assert str(out["ts"][0])[:10] == "2005-06-01"


def test_parse_index_csv_and_slug() -> None:
    from app.ingest.zip_archive import parse_index_csv, slugify_index

    csv = "Date,Open,High,Low,Close,Volume,Turnover\n2005-06-01,2000,2010,1999,2005.5,1000,500\n"
    out = parse_index_csv(csv.encode(), "NIFTY_50")
    assert out.height == 1
    assert out["close"][0] == 2005.5
    assert out["symbol"][0] == "NIFTY_50"

    assert slugify_index("NIFTY 50") == "NIFTY_50"
    assert slugify_index("NIFTY GS 10YR CLN") == "NIFTY_GS_10YR_CLN"
    assert slugify_index("india vix") == "INDIA_VIX"


def test_parse_mfapi_list() -> None:
    from app.ingest.amfi import parse_mfapi_list

    payload = [
        {"schemeCode": 100027, "schemeName": "Old Dead Fund", "isinGrowth": None, "isinDivReinvestment": None},
        {"schemeCode": 118989, "schemeName": "HDFC Mid Cap Direct Growth", "isinGrowth": "INF179K01XQ0", "isinDivReinvestment": None},
        {"schemeCode": 118990, "schemeName": "HDFC Mid Cap Direct IDCW", "isinGrowth": "INF179K01XQ0", "isinDivReinvestment": None},
        {"schemeCode": 119551, "schemeName": "Nippon India ETF", "isinGrowth": "INF204KA1CC4", "isinDivReinvestment": "INF204KA1CD2"},
    ]
    df = parse_mfapi_list(payload)
    assert df.height == 3  # dead scheme dropped; duplicate ISINs kept (dedup at sync time)
    isins = set(df["isin"].to_list())
    assert isins == {"INF179K01XQ0", "INF204KA1CC4"}


def test_parse_bhavcopy_dir_formats() -> None:
    from app.ingest.bhavcopy_dir import fname_to_date, parse_bhavcopy_csv

    assert fname_to_date("01APR2002.csv") == date(2002, 4, 1)
    assert fname_to_date("3FEB2025.csv") == date(2025, 2, 3)
    assert fname_to_date("junk.csv") is None

    legacy = (
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,\n"
        "RELIANCE,EQ,100,110,99,105,104,101,1000,105000,1-APR-2002,\n"
        "SMECODE,SM,10,11,9,10.5,10,10,500,5000,1-APR-2002,\n"
    )
    out = parse_bhavcopy_csv(legacy.encode(), date(2002, 4, 1))
    assert out.height == 1
    assert out["close"][0] == 105.0

    mfapi_style = (
        'SYMBOL," SERIES"," DATE1"," PREV_CLOSE"," OPEN_PRICE"," HIGH_PRICE"," LOW_PRICE"," LAST_PRICE"," CLOSE_PRICE"," AVG_PRICE"," TTL_TRD_QNTY"," TURNOVER_LACS"," NO_OF_TRADES"," DELIV_QTY"," DELIV_PER"\n'
        '1018GS2026," GS"," 03-Feb-2025"," 110.50"," 110.00"," 110.50"," 110.00"," 110.05"," 110.09"," 110.16"," 1953"," 2.15"," 4"," 1953"," 100.00"\n'
        'RELIANCE," EQ"," 03-Feb-2025"," 2200"," 2210"," 2230"," 2195"," 2225"," 2222.5"," 2218"," 100000"," 220"," 5000"," 60000"," 60.00"\n'
    )
    out2 = parse_bhavcopy_csv(mfapi_style.encode(), date(2025, 2, 3))
    assert out2.height == 1  # GS series filtered
    assert out2["symbol"][0] == "RELIANCE"
    assert out2["close"][0] == 2222.5
    assert out2["volume"][0] == 100000


def test_merge_ohlcv() -> None:
    from datetime import datetime

    from app.engine import db as market_db

    df = pl.DataFrame(
        {
            "symbol": ["A"] * 2,
            "ts": [datetime(2026, 1, 1), datetime(2026, 1, 2)],
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
            "volume": [100, 200],
        }
    )
    market_db.insert_ohlcv(df)
    # merge with different values + a NEW day: existing days updated, new day added
    merge = pl.DataFrame(
        {
            "symbol": ["A"] * 2,
            "ts": [datetime(2026, 1, 1), datetime(2026, 1, 3)],
            "open": [20.0, 30.0],
            "high": [22.0, 33.0],
            "low": [19.0, 29.0],
            "close": [21.0, 32.0],
            "volume": [500, 300],
        }
    )
    market_db.merge_ohlcv(merge)
    out = market_db.fetch_ohlcv("A").sort("ts")
    assert out.height == 3  # new day added
    assert out["close"][0] == 21.0  # updated
    assert out["close"][1] == 12.0  # day not in merge preserved
    assert out["close"][2] == 32.0


def _day_frame(day: date, close: float, sym_a: str = "INCR1", sym_b: str = "INCR2") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [sym_a, sym_b],
            "ts": [day, day],
            "open": [close - 1.0, close - 1.0],
            "high": [close + 1.0, close + 1.0],
            "low": [close - 2.0, close - 2.0],
            "close": [close, close],
            "volume": [100, 200],
        }
    )


def test_incremental_day_by_day_ingest_preserves_history() -> None:
    """Regression: ingest_bhavcopy() merges one trading day at a time.

    Feeding days one at a time must accumulate, never replace -- otherwise a
    single-day batch silently deletes every earlier row for its symbols.
    """
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    for i, day in enumerate(days):
        market_db.merge_ohlcv(_day_frame(day, 100.0 + i))

    out = market_db.fetch_ohlcv("INCR1").sort("ts")
    assert out.height == len(days)
    got_days = [d.date() if hasattr(d, "date") else d for d in out["ts"].to_list()]
    assert got_days == days
    assert out["close"][-1] == 102.0  # latest day present
    assert out["close"][0] == 100.0  # first day still present

    # a partial re-fetch of one day corrects it without touching its neighbours
    market_db.merge_ohlcv(_day_frame(days[1], 999.0))
    corrected = market_db.fetch_ohlcv("INCR1").sort("ts")
    assert corrected.height == len(days)
    assert corrected["close"][1] == 999.0
    assert corrected["close"][0] == 100.0


def test_replace_ohlcv_wipes_all_history_for_batch_symbols() -> None:
    """Pins the destructive contract of replace_ohlcv.

    It deletes every row for the batch's symbols first, so it is only safe for
    a full-range reload. This is why incremental ingest must use merge_ohlcv.
    """
    for day in (date(2026, 1, 5), date(2026, 1, 6)):
        market_db.merge_ohlcv(_day_frame(day, 100.0, "REPL1", "REPL2"))

    market_db.replace_ohlcv(_day_frame(date(2026, 1, 7), 105.0, "REPL1", "REPL2"))

    assert market_db.fetch_ohlcv("REPL1").height == 1  # only the batch day survives
    assert market_db.fetch_ohlcv("REPL2").height == 1


def test_replace_ohlcv_is_not_called_by_production_code() -> None:
    """Guard: no module under app/ may use the destructive writer.

    merge_ohlcv (additive) and insert_ohlcv (append-only) are the only writers
    that ingest paths are allowed to call.
    """
    import pathlib

    app_dir = pathlib.Path(market_db.__file__).resolve().parent.parent
    offenders = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if p.name != "db.py" and "replace_ohlcv" in p.read_text(encoding="utf8")
    ]
    assert offenders == [], (
        f"replace_ohlcv is destructive (deletes all history for the batch's "
        f"symbols); use merge_ohlcv instead. Offending files: {offenders}"
    )


BULK_LATEST_SAMPLE = [
    {
        "schemeCode": 119598,
        "schemeName": "Mirae Asset Large Cap Fund - Direct Plan - Growth",
        "isinGrowth": "INF179K01F31",
        "isinDivReinvestment": None,
        "nav": "101.29120",
        "date": "25-09-2026",
    },
    {
        "schemeCode": 119599,
        "schemeName": "Nippon India ETF Nifty 50 BeES",
        "isinGrowth": "INF737KA17B4",
        "isinDivReinvestment": None,
        "nav": "250.00",
        "date": "25-09-2026",
    },
    {
        "schemeCode": 111111,
        "schemeName": "Scheme With No Valuation",
        "isinGrowth": "INF179K01F32",
        "isinDivReinvestment": None,
        "nav": None,
        "date": "25-09-2026",
    },
    {
        "schemeCode": 119600,
        "schemeName": "Duplicate ISIN Second Code",
        "isinGrowth": "INF179K01F31",
        "isinDivReinvestment": None,
        "nav": "9.99",
        "date": "25-09-2026",
    },
    {
        "schemeCode": 222222,
        "schemeName": "Reinvestment Isin Only Fund",
        "isinGrowth": None,
        "isinDivReinvestment": "INF204KC1337",
        "nav": "12.5",
        "date": "25-09-2026",
    },
]


def test_parse_nav_all_skips_na_nav() -> None:
    # AMFI publishes "N.A." for schemes with no valuation that day. A single such
    # row used to abort the whole file with ValueError.
    content = (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment/ ISIN Growth;"
        "Scheme Name;Net Asset Value;Date\n"
        "119551;INF209K01157;INF209K01165;Banking & PSU Debt Fund-DIRECT-Growth;272.3504;06-Mar-2026\n"
        "119552;INF209K01179;;Liquid Fund-DIRECT-Payout;N.A.;06-Mar-2026\n"
        "119553;INF209K01180;;Another Fund;12.5;06-Mar-2026\n"
    )
    out = amfi.parse_nav_all(content)
    assert out.height == 2
    assert out["symbol"].to_list() == ["INF209K01157", "INF209K01180"]


def test_parse_mfapi_bulk_latest() -> None:
    out = amfi.parse_mfapi_bulk_latest(BULK_LATEST_SAMPLE)
    # null nav skipped, and the duplicate ISIN keeps the first code only
    assert out.height == 3
    assert out["symbol"].to_list() == ["INF179K01F31", "INF737KA17B4", "INF204KC1337"]
    assert out["close"].to_list() == [101.2912, 250.0, 12.5]
    assert out["ts"].to_list() == [date(2026, 9, 25)] * 3
    assert out["series"].to_list() == ["MF"] * 3
    assert out["volume"].to_list() == [None] * 3
    assert out["amfi_code"].to_list() == ["119598", "119599", "222222"]
    etfs = out.filter(pl.col("instrument_type") == "etf")
    assert etfs["symbol"].to_list() == ["INF737KA17B4"]


def test_parse_mfapi_bulk_latest_scopes_to_known_codes() -> None:
    out = amfi.parse_mfapi_bulk_latest(BULK_LATEST_SAMPLE, known_codes={"119598", "119599"})
    assert out.height == 2
    assert set(out["amfi_code"].to_list()) == {"119598", "119599"}


def test_parse_mfapi_latest_single_scheme() -> None:
    payload = {"data": [{"date": "25-09-2026", "nav": "101.29120"}]}
    out = amfi.parse_mfapi_latest(payload, "INF179K01F31", "Mirae Asset Large Cap Fund")
    assert out.height == 1
    assert out["close"][0] == 101.2912
    assert out["ts"][0] == date(2026, 9, 25)
    assert out["symbol"][0] == "INF179K01F31"
    assert amfi.parse_mfapi_latest({"data": []}, "INF179K01F31", "x").is_empty()


def test_reject_stale_rejects_frozen_snapshot() -> None:
    # AMFI's api./portal. hosts answer 200 with a 2022-01-23 snapshot. Merging it
    # would silently replace live NAVs with four-year-old values.
    frozen = pl.DataFrame(
        {
            "symbol": ["INF179K01F31"],
            "ts": [date(2022, 1, 23)],
            "open": [None],
            "high": [None],
            "low": [None],
            "close": [50.0],
            "volume": [None],
            "series": ["MF"],
        }
    )
    reason = amfi._reject_stale(frozen)
    assert reason is not None
    assert "2022-01-23" in reason

    recent = frozen.with_columns(pl.lit(date.today()).alias("ts"))
    assert amfi._reject_stale(recent) is None
    assert "no rows parsed" in amfi._reject_stale(frozen.clear())


def test_drop_regressions_never_rewinds_history() -> None:
    market_db.merge_ohlcv(
        pl.DataFrame(
            {
                "symbol": ["INF179K01F31"],
                "ts": [date(2026, 9, 25)],
                "open": [None],
                "high": [None],
                "low": [None],
                "close": [101.2912],
                "volume": [None],
                "series": ["MF"],
            }
        )
    )
    incoming = pl.DataFrame(
        {
            "symbol": ["INF179K01F31", "INF737KA17B4"],
            # one older than what we hold, one brand new symbol
            "ts": [date(2026, 9, 24), date(2026, 9, 26)],
            "open": [None, None],
            "high": [None, None],
            "low": [None, None],
            "close": [999.0, 250.0],
            "volume": [None, None],
            "series": ["MF", "MF"],
        }
    )
    kept, dropped = amfi._drop_regressions(incoming)
    assert dropped == 1
    assert kept["symbol"].to_list() == ["INF737KA17B4"]


def test_ingest_nav_latest_falls_back_when_amfi_unreachable(monkeypatch) -> None:
    import requests

    def _boom(*_args, **_kwargs):
        raise requests.exceptions.ConnectTimeout("NAVAll unreachable")

    monkeypatch.setattr(amfi, "ingest_portal_recent", _boom)
    monkeypatch.setattr(amfi, "_ingest_navall", _boom)
    monkeypatch.setattr(
        amfi,
        "ingest_mfapi_latest",
        lambda **_kwargs: {
            "source": "mfapi_bulk",
            "parsed": 2,
            "rows": 2,
            "rejected": None,
            "latest_ts": date(2026, 9, 25),
            "ok": True,
        },
    )

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("per-scheme fallback should not run when bulk succeeds")

    monkeypatch.setattr(amfi, "ingest_mfapi_latest_per_scheme", _must_not_run)

    result = amfi.ingest_nav_latest()
    assert result["source"] == "mfapi_bulk"
    assert result["rows"] == 2
    assert any("amfi_portal" in e for e in result["errors"])
    assert any("amfi_navall" in e for e in result["errors"])


def test_ingest_nav_latest_reports_total_failure(monkeypatch) -> None:
    def _rejected(*_args, **_kwargs):
        return {"source": "x", "parsed": 0, "rows": 0, "rejected": "stale", "latest_ts": None, "ok": False}

    monkeypatch.setattr(amfi, "ingest_portal_recent", _rejected)
    monkeypatch.setattr(amfi, "_ingest_navall", _rejected)
    monkeypatch.setattr(amfi, "ingest_mfapi_latest", _rejected)
    monkeypatch.setattr(amfi, "ingest_mfapi_latest_per_scheme", _rejected)

    result = amfi.ingest_nav_latest()
    assert result["source"] is None
    assert result["rows"] == 0
    assert result["ok"] is False
    assert len(result["errors"]) == 4


def test_nav_latest_prefers_portal_over_navall_and_mfapi(monkeypatch) -> None:
    """The portal is the only reachable AMFI host, so it is the first rung."""

    def _portal(*_args, **_kwargs):
        return {
            "source": "amfi_portal",
            "parsed": 9100,
            "rows": 120,
            "skipped_regress": 8980,
            "rejected": None,
            "latest_ts": date(2026, 9, 27),
            "ok": True,
            "errors": [],
        }

    monkeypatch.setattr(amfi, "ingest_portal_recent", _portal)
    monkeypatch.setattr(amfi, "_ingest_navall", lambda *_a, **_k: pytest.fail("portal should win"))
    monkeypatch.setattr(amfi, "ingest_mfapi_latest", lambda **_k: pytest.fail("portal should win"))

    result = amfi.ingest_nav_latest()
    assert result["source"] == "amfi_portal"
    assert result["rows"] == 120
    assert result["errors"] == []


# --- AMFI portal history report (DownloadNAVHistoryReport_Po.aspx) -------------

_PORTAL_OK = (
    "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
    "ISIN Div Reinvestment;Net Asset Value;Date\r\n"
    "\r\n"
    "Open Ended Schemes ( Money Market )\r\n"
    "\r\n"
    "119598;SBI Large Cap Fund - Direct Plan - Growth;Direct Plan;Growth;"
    "INF200K01QX4;;101.29120;25-Sep-2026\r\n"
    "119598;SBI Large Cap Fund - Direct Plan - Growth;Direct Plan;Growth;"
    "INF200K01QX4;;100.89490;24-Sep-2026\r\n"
    "107693;Quantum Gold ETF - Direct Plan;Direct Plan;Growth;INF082J01408;;"
    "124.76500;25-Sep-2026\r\n"
    "999999;Untracked Fund;Direct Plan;Growth;INF999999999;;5.0;25-Sep-2026\r\n"
    "119598;Broken Scheme;;;;N.A.;25-Sep-2026\r\n"
    "119598;No Isin Scheme;Direct Plan;Growth;;;50.0;25-Sep-2026\r\n"
)

# What AMFI returns for a too-wide date window: HTTP 200, HTML form, no data.
_PORTAL_ERROR_PAGE = (
    '<!DOCTYPE html PUBLIC "-//W3.0//DTD XHTML 1.0 Frameset//EN">\n'
    '<html><body><span class="labelRed">Application Error! Please try again later'
    "</span></body></html>"
)


def test_portal_body_error_rejects_application_error_page() -> None:
    """A too-wide window is HTTP 200 with an HTML page, not a non-200."""
    assert amfi._portal_body_error(_PORTAL_ERROR_PAGE) is not None


def test_portal_body_error_rejects_unexpected_body() -> None:
    assert amfi._portal_body_error("<html>nope</html>") is not None
    assert amfi._portal_body_error("") is not None


def test_portal_body_error_accepts_report() -> None:
    assert amfi._portal_body_error(_PORTAL_OK) is None


def test_parse_portal_history_reads_8_field_rows() -> None:
    df = amfi.parse_portal_history(_PORTAL_OK, known_codes={"119598", "107693"})
    assert df.height == 3
    row = df.filter(pl.col("symbol") == "INF200K01QX4").sort("ts").row(0, named=True)
    assert row["ts"] == date(2026, 9, 24)
    assert row["close"] == 100.89490
    assert row["series"] == "MF"
    assert row["amfi_code"] == "119598"
    # name column already carries plan + option
    assert row["name"] == "SBI Large Cap Fund - Direct Plan - Growth"
    # ETF classification survives the portal's own layout
    etf = df.filter(pl.col("symbol") == "INF082J01408").row(0, named=True)
    assert etf["instrument_type"] == "etf"


def test_parse_portal_history_skips_sections_na_and_untracked() -> None:
    """Header/AMC lines, N.A. NAVs, ISIN-less and untracked schemes are dropped."""
    df = amfi.parse_portal_history(_PORTAL_OK, known_codes={"119598", "107693"})
    assert set(df["symbol"].to_list()) == {"INF200K01QX4", "INF082J01408"}
    # 999999 is a real row but not a scheme we track -> universe must not grow
    assert "INF999999999" not in df["symbol"].to_list()
    # the error page must never parse into rows
    assert amfi.parse_portal_history(_PORTAL_ERROR_PAGE, known_codes={"119598"}).is_empty()


def test_portal_chunks_cover_range_without_gaps_or_overlap() -> None:
    start, end = date(2026, 9, 1), date(2026, 9, 27)
    chunks = list(amfi.portal_chunks(start, end, 7))
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert (next_start - prev_end).days == 1
    assert all((b - a).days <= 6 for a, b in chunks)


def test_reject_stale_none_bypasses_recency_check() -> None:
    """Historical audit windows are legitimately old; the guard must not fire."""
    old = pl.DataFrame(
        {"ts": [date(2024, 1, 5)], "close": [1.0]},
        schema={"ts": pl.Date, "close": pl.Float64},
    )
    assert amfi._reject_stale(old, 7) is not None
    assert amfi._reject_stale(old, None) is None
    assert amfi._reject_stale(old) is not None


def test_drop_regressions_force_allows_restatement() -> None:
    df = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "ts": [date(2026, 9, 25), date(2026, 9, 26)],
            "close": [10.0, 11.0],
        },
        schema={"symbol": pl.String, "ts": pl.Date, "close": pl.Float64},
    )
    with patch.object(amfi.market_db, "get_connection") as conn:
        conn.return_value.__enter__.return_value.execute.return_value.pl.return_value = (
            pl.DataFrame({"symbol": ["AAA"], "max_ts": [date(2026, 9, 26)]})
        )
        kept, skipped = amfi._drop_regressions(df)
        assert kept.height == 0 and skipped == 2
        forced, fskipped = amfi._drop_regressions(df, force=True)
        assert forced.height == 2 and fskipped == 0


def test_nav_ladder_prefers_portal_and_stops_on_ok() -> None:
    """A rung that returns no new rows must still count as satisfied.

    Gating on the row count made an already-current database fall through every
    rung and land on the 9,200-request per-scheme fallback.
    """
    calls: list[str] = []

    def portal(**_kw):
        calls.append("portal")
        return {"source": "amfi_portal", "rows": 0, "ok": True, "parsed": 9000, "errors": []}

    def navall(*_a, **_kw):
        calls.append("navall")
        return {"source": "amfi_navall", "rows": 0, "ok": True, "errors": []}

    with (
        patch.object(amfi, "ingest_portal_recent", portal),
        patch.object(amfi, "_ingest_navall", navall),
        patch.object(amfi, "ingest_mfapi_latest") as bulk,
        patch.object(amfi, "ingest_mfapi_latest_per_scheme") as per,
    ):
        result = amfi.ingest_nav_latest()
    assert result["source"] == "amfi_portal"
    assert result["rows"] == 0
    # zero rows on a healthy day must not fall through
    assert calls == ["portal"]
    bulk.assert_not_called()
    per.assert_not_called()


def test_nav_ladder_falls_through_when_rung_not_ok() -> None:
    with (
        patch.object(
            amfi, "ingest_portal_recent", return_value={"ok": False, "rows": 0, "rejected": "no rows parsed"}
        ),
        patch.object(amfi, "_ingest_navall", side_effect=RuntimeError("blocked")),
        patch.object(
            amfi, "ingest_mfapi_latest", return_value={"source": "mfapi_bulk", "rows": 42, "ok": True}
        ),
        patch.object(amfi, "ingest_mfapi_latest_per_scheme") as per,
    ):
        result = amfi.ingest_nav_latest()
    assert result["source"] == "mfapi_bulk"
    assert result["rows"] == 42
    per.assert_not_called()
    assert any("amfi_portal" in e for e in result["errors"])
    assert any("amfi_navall" in e for e in result["errors"])


def test_collect_portal_history_survives_a_failing_chunk(monkeypatch) -> None:
    """One flaky response must not void the whole range."""
    seen: list[tuple[int, date, date]] = []

    def fake_fetch(frmdt, todt, tp=1, **_kw):
        seen.append((tp, frmdt, todt))
        if tp == 2:
            raise RuntimeError("portal returned an 'Application Error!' page")
        return _PORTAL_OK

    with patch.object(amfi, "fetch_portal_history", fake_fetch):
        df, errors = amfi.collect_portal_history(
            date(2026, 9, 24),
            date(2026, 9, 25),
            known_codes={"119598", "107693"},
        )
    assert {t for t, _, _ in seen} == {1, 2, 3}
    assert any("tp2" in e for e in errors)
    assert df.height == 3
    # one row per (symbol, ts) even if a scheme appears in two report types
    assert df.unique(subset=["symbol", "ts"]).height == df.height


def _mf_nav_rows(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    """Frame in the same shape the portal parser produces."""
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "ts": [r[1] for r in rows],
            "open": [None] * len(rows),
            "high": [None] * len(rows),
            "low": [None] * len(rows),
            "close": [r[2] for r in rows],
            "volume": [None] * len(rows),
            "series": ["MF"] * len(rows),
            "instrument_type": ["mf"] * len(rows),
            "name": ["Test Scheme"] * len(rows),
            "amfi_code": ["100001"] * len(rows),
        }
    )


def test_audit_range_reports_missing_extra_and_mismatch(monkeypatch) -> None:
    from app.ingest import nav_audit

    amfi = _mf_nav_rows(
        [
            ("INF111AAA001", date(2026, 9, 24), 10.0),  # missing for us
            ("INF111AAA002", date(2026, 9, 24), 20.0),  # matches
            ("INF111AAA003", date(2026, 9, 24), 30.0),  # we hold a different value
        ]
    )
    monkeypatch.setattr(nav_audit, "collect_portal_history", lambda *a, **k: (amfi, []))
    monkeypatch.setattr(
        nav_audit,
        "_stored_navs",
        lambda *a, **k: pl.DataFrame(
            {
                "symbol": ["INF111AAA002", "INF111AAA003", "INF111AAA004"],
                "ts": [date(2026, 9, 24)] * 3,
                "close": [20.0, 31.0, 40.0],
            },
            schema={"symbol": pl.Utf8, "ts": pl.Date, "close": pl.Float64},
        ),
    )

    report = nav_audit.audit_range(date(2026, 9, 24), date(2026, 9, 24))
    assert report["missing"] == 1
    assert report["extra"] == 1
    assert report["mismatched"] == 1
    assert report["missing_by_symbol"] == {"INF111AAA001": 1}
    assert report["extra_by_symbol"] == {"INF111AAA004": 1}
    assert report["mismatch_sample"][0]["amfi"] == 30.0
    assert report["mismatch_sample"][0]["stored"] == 31.0


def test_audit_range_ignores_sub_tolerance_differences(monkeypatch) -> None:
    """A 1-unit wobble in the last published digit is not corruption.

    These pairs differ by exactly 1e-4, which is the boundary a bare
    `abs(a - b) > 1e-4` gets wrong. 12.6765-12.6764 happens to land under the
    threshold in floating point while 1021.4097-1021.4096 lands over it, so a
    real corpus of last-digit restatements reports inconsistently.
    """
    from app.ingest import nav_audit

    cases = [(12.6765, 12.6764), (1021.4097, 1021.4096), (10.0, 10.0001)]
    amfi = _mf_nav_rows(
        [(f"INF111AAA{100 + i}", date(2026, 9, 24), a) for i, (a, _) in enumerate(cases)]
    )
    monkeypatch.setattr(nav_audit, "collect_portal_history", lambda *a, **k: (amfi, []))
    monkeypatch.setattr(
        nav_audit,
        "_stored_navs",
        lambda *a, **k: pl.DataFrame(
            {
                "symbol": [f"INF111AAA{100 + i}" for i in range(len(cases))],
                "ts": [date(2026, 9, 24)] * len(cases),
                "close": [b for _, b in cases],
            },
            schema={"symbol": pl.Utf8, "ts": pl.Date, "close": pl.Float64},
        ),
    )
    assert nav_audit.audit_range(date(2026, 9, 24), date(2026, 9, 24))["mismatched"] == 0


def test_audit_range_flags_differences_beyond_last_digit(monkeypatch) -> None:
    """Anything wider than a one-unit wobble is a real restatement."""
    from app.ingest import nav_audit

    cases = [(41.5012, 41.5315), (452.7301, 452.6292), (10.0, 10.0002)]
    amfi = _mf_nav_rows(
        [(f"INF111EEE{100 + i}", date(2026, 9, 24), a) for i, (a, _) in enumerate(cases)]
    )
    monkeypatch.setattr(nav_audit, "collect_portal_history", lambda *a, **k: (amfi, []))
    monkeypatch.setattr(
        nav_audit,
        "_stored_navs",
        lambda *a, **k: pl.DataFrame(
            {
                "symbol": [f"INF111EEE{100 + i}" for i in range(len(cases))],
                "ts": [date(2026, 9, 24)] * len(cases),
                "close": [b for _, b in cases],
            },
            schema={"symbol": pl.Utf8, "ts": pl.Date, "close": pl.Float64},
        ),
    )
    report = nav_audit.audit_range(date(2026, 9, 24), date(2026, 9, 24))
    assert report["mismatched"] == 3


def test_backfill_inserts_missing_keys_only(monkeypatch) -> None:
    """Backfill must add absent keys and leave existing values alone.

    The mismatch and the extra row both have to survive untouched -- closing a
    coverage gap is not a licence to restate or prune.
    """
    from app.ingest import nav_audit

    amfi = _mf_nav_rows(
        [
            ("INF111BBB001", date(2026, 9, 23), 10.0),  # missing
            ("INF111BBB001", date(2026, 9, 24), 11.0),  # missing
            ("INF111BBB002", date(2026, 9, 24), 30.0),  # we hold 31.0 -- restatement
        ]
    )
    monkeypatch.setattr(nav_audit, "collect_portal_history", lambda *a, **k: (amfi, []))
    monkeypatch.setattr(
        nav_audit,
        "_stored_navs",
        lambda *a, **k: pl.DataFrame(
            {
                "symbol": ["INF111BBB002", "INF111BBB003"],
                "ts": [date(2026, 9, 24)] * 2,
                "close": [31.0, 40.0],
            },
            schema={"symbol": pl.Utf8, "ts": pl.Date, "close": pl.Float64},
        ),
    )
    captured: list[pl.DataFrame] = []
    monkeypatch.setattr(
        nav_audit.market_db, "insert_ohlcv", lambda df, **k: captured.append(df) or df.height
    )

    result = nav_audit.backfill_missing(date(2026, 9, 23), date(2026, 9, 24))

    assert result["missing"] == 2
    assert result["inserted"] == 2
    # Only the two absent keys are handed to the writer.
    assert sorted(captured[0]["symbol"].unique().to_list()) == ["INF111BBB001"]
    assert captured[0].height == 2


def test_backfill_dry_run_writes_nothing(monkeypatch) -> None:
    from app.ingest import nav_audit

    amfi = _mf_nav_rows([("INF111CCC001", date(2026, 9, 24), 10.0)])
    monkeypatch.setattr(nav_audit, "collect_portal_history", lambda *a, **k: (amfi, []))
    monkeypatch.setattr(
        nav_audit, "_stored_navs", lambda *a, **k: pl.DataFrame(
            schema={"symbol": pl.Utf8, "ts": pl.Date, "close": pl.Float64}
        )
    )
    monkeypatch.setattr(
        nav_audit.market_db,
        "insert_ohlcv",
        lambda *_a, **_k: pytest.fail("dry run must not write"),
    )

    result = nav_audit.backfill_missing(date(2026, 9, 24), date(2026, 9, 24), dry_run=True)
    assert result["missing"] == 1
    assert result["inserted"] == 0
    assert result["dry_run"] is True


def test_backfill_backfills_mid_history_hole_regardless_of_regression_guard(
    monkeypatch,
) -> None:
    """A hole older than a symbol's newest row is the whole point of a backfill.

    The daily ingest path drops anything at or below a symbol's high-water mark,
    so a backfill that reused it would report success while writing nothing.
    """
    from app.ingest import nav_audit

    # We hold the 25th but not the 24th.
    monkeypatch.setattr(
        nav_audit,
        "_stored_navs",
        lambda *a, **k: pl.DataFrame(
            {
                "symbol": ["INF111DDD001"],
                "ts": [date(2026, 9, 25)],
                "close": [12.0],
            },
            schema={"symbol": pl.Utf8, "ts": pl.Date, "close": pl.Float64},
        ),
    )
    amfi = _mf_nav_rows(
        [
            ("INF111DDD001", date(2026, 9, 24), 11.0),  # the hole
            ("INF111DDD001", date(2026, 9, 25), 12.0),  # already have it
        ]
    )
    monkeypatch.setattr(nav_audit, "collect_portal_history", lambda *a, **k: (amfi, []))

    written: list[pl.DataFrame] = []
    monkeypatch.setattr(
        nav_audit.market_db, "insert_ohlcv", lambda df, **k: written.append(df) or df.height
    )

    result = nav_audit.backfill_missing(date(2026, 9, 24), date(2026, 9, 25))
    assert result["missing"] == 1
    assert written[0].height == 1
    assert written[0]["ts"].to_list() == [date(2026, 9, 24)]


def test_rate_limited_session_can_treat_404_as_final(monkeypatch) -> None:
    """A dated file that is absent will not appear on retry, so do not back off.

    Retrying cost 4 extra attempts across 4 candidate URLs with 1+2+4+8s of sleep
    between them -- ~60s of doing nothing on every market holiday.
    """
    from app.ingest import http as http_mod

    calls: list[str] = []
    slept: list[float] = []

    class _Resp:
        status_code = 404
        content = b""

    def _fake_get(url: str, *a: object, **k: object) -> _Resp:
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(http_mod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(http_mod.requests.Session, "get", lambda self, url, **k: _fake_get(url))

    final = http_mod.RateLimitedSession(min_interval=0.0, retry_statuses=http_mod.NO_RETRY_404_STATUSES)
    assert final.get("https://x").status_code == 404
    assert len(calls) == 1, "404 must not be retried when the caller opts out"
    assert slept == [], "no backoff sleep for a final 404"

    calls.clear()
    slept.clear()
    retrying = http_mod.RateLimitedSession(min_interval=0.0, backoff_base=1.0)
    assert retrying.get("https://x").status_code == 404
    assert len(calls) == 5, "404 is still retried by default for other callers"
    assert slept, "the default set still backs off"


def test_rate_limited_session_still_retries_server_errors(monkeypatch) -> None:
    from app.ingest import http as http_mod

    calls: list[str] = []
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)

    class _Resp:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.content = b""

    def _fake_get(url: str, *a: object, **k: object) -> _Resp:
        calls.append(url)
        return _Resp(503 if len(calls) < 3 else 200)

    monkeypatch.setattr(http_mod.requests.Session, "get", lambda self, url, **k: _fake_get(url))
    s = http_mod.RateLimitedSession(min_interval=0.0, backoff_base=1.0,
                                    retry_statuses=http_mod.NO_RETRY_404_STATUSES)
    assert s.get("https://x").status_code == 200
    assert len(calls) == 3, "5xx stays retryable even for bhavcopy"


def _temp_securities_session(monkeypatch):
    """A throwaway SQLite holding just the securities table.

    The suite's shared data dir has no schema (several tests only ever write
    market data), so exercising the real upsert needs a real table somewhere.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.state.db import Base
    from app.state.models import Security  # noqa: F401  (registers the table)

    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/sec.db")
    Base.metadata.create_all(engine, tables=[Security.__table__])
    return sessionmaker(bind=engine)


def test_sync_securities_bulk_upsert_preserves_every_column(monkeypatch) -> None:
    """One statement, but no data loss.

    Reducing the batch to a key would null out isin/series/instrument_type on
    conflict, and leaving duplicate symbols in would let SQLite's ON CONFLICT keep
    only the first day's row.
    """
    from app.ingest import universe

    Session = _temp_securities_session(monkeypatch)
    monkeypatch.setattr("app.state.db.SessionLocal", Session)
    frame = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB"],
            "name": ["Alpha", "Alpha", "Beta"],
            "series": ["EQ", "EQ", "EQ"],
            "isin": ["INE002A01018", "INE002A01018", None],
            "instrument_type": ["stock", "stock", "stock"],
            "listed_on": [date(2020, 1, 2), date(2020, 1, 2), None],
            "amfi_code": [None, None, None],
            "last_seen": [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 25)],
        }
    )
    assert universe.sync_securities(frame) == 2, "duplicate symbols collapse to one row"

    from app.state.models import Security

    with Session() as session:
        got = {r.symbol: r for r in session.query(Security).all()}
    assert set(got) == {"AAA", "BBB"}
    # The later day must win, since each upsert overwrites the previous one.
    assert got["AAA"].last_seen == date(2026, 9, 25)
    assert got["AAA"].isin == "INE002A01018"
    assert got["AAA"].series == "EQ"
    assert got["AAA"].is_active is True
    assert got["BBB"].name == "Beta"
    assert got["BBB"].instrument_type == "stock"


def test_ingest_bhavcopy_syncs_securities_once_for_the_window(monkeypatch) -> None:
    from app.ingest import bhavcopy

    syncs: list[pl.DataFrame] = []
    monkeypatch.setattr(bhavcopy.market_db, "merge_ohlcv", lambda df, **k: df.height)
    monkeypatch.setattr(bhavcopy, "sync_securities", lambda df: syncs.append(df) or len(df))
    def _fake_fetch(session: object, day: date) -> pl.DataFrame:
        # Same two symbols on every day, with the name changed on the last day,
        # so the collapse to one row per symbol is observable and the newest
        # day's values can be checked.
        return pl.DataFrame(
            {
                "symbol": ["SHARED", "COMMON"],
                "name": ["name-" + day.isoformat()] * 2,
                "series": ["EQ", "EQ"],
                "isin": ["INE002A01018", "INE009A01021"],
                "instrument_type": ["stock", "stock"],
                "listed_on": [None, None],
            }
        )

    monkeypatch.setattr(bhavcopy, "fetch_bhavcopy", _fake_fetch)

    start = date(2026, 9, 21)  # Mon
    total = bhavcopy.ingest_bhavcopy(start, date(2026, 9, 23), rate=0.0)

    assert total == 6
    assert len(syncs) == 1, "securities sync must happen once, not once per day"
    synced = syncs[0]
    assert synced.height == 2, "one row per symbol across the whole window"
    assert set(synced["symbol"].to_list()) == {"SHARED", "COMMON"}
    # Every upserted column has to survive the concat + unique.
    for column in ("isin", "series", "instrument_type", "name", "last_seen"):
        assert column in synced.columns, column
    row = synced.filter(pl.col("symbol") == "SHARED").to_dicts()[0]
    assert row["last_seen"] == date(2026, 9, 23), "newest day wins"
    assert row["name"] == "name-2026-09-23"
    assert row["isin"] == "INE002A01018"


def test_sync_schemes_from_portal_registers_unknown_scheme(monkeypatch) -> None:
    """A scheme AMFI publishes but the master lacks must get registered.

    Everything else in amfi.py parses the portal filtered by the master, so this
    is the only thing that can notice a new launch.
    """
    from app.ingest import amfi as amfi_mod

    frame = _mf_nav_rows(
        [
            ("INF111FFF001", date(2026, 9, 25), 10.0),
            ("INF111FFF002", date(2026, 9, 25), 20.0),
        ]
    ).with_columns(
        pl.Series(["100001", "100002"], dtype=pl.Utf8).alias("amfi_code")
    )
    monkeypatch.setattr(amfi_mod, "collect_portal_history", lambda *a, **k: (frame, []))
    monkeypatch.setattr(amfi_mod, "_known_mf_codes", lambda: {"100001"})
    synced: list[pl.DataFrame] = []
    monkeypatch.setattr(
        amfi_mod, "_sync_amfi_securities", lambda df: synced.append(df) or df.height
    )

    result = amfi_mod.sync_schemes_from_portal(days=14)

    assert result["ok"] is True
    assert result["seen"] == 2
    assert result["new_codes"] == 1
    assert result["new_isins"] == 1
    assert synced[0]["symbol"].unique().to_list() == ["INF111FFF002"]


def test_sync_schemes_from_portal_is_idempotent(monkeypatch) -> None:
    from app.ingest import amfi as amfi_mod

    frame = _mf_nav_rows([("INF111FFF001", date(2026, 9, 25), 10.0)]).with_columns(
        pl.Series(["100001"], dtype=pl.Utf8).alias("amfi_code")
    )
    monkeypatch.setattr(amfi_mod, "collect_portal_history", lambda *a, **k: (frame, []))
    monkeypatch.setattr(amfi_mod, "_known_mf_codes", lambda: {"100001"})
    monkeypatch.setattr(
        amfi_mod, "_sync_amfi_securities", lambda df: pytest.fail("nothing new to sync")
    )

    result = amfi_mod.sync_schemes_from_portal(days=14)
    assert result["new_codes"] == 0
    assert result["seen"] == 1
    assert result["ok"] is True


def test_audit_unknown_schemes_never_imports(monkeypatch) -> None:
    """mfapi is a completeness signal only.

    It carries ~38k scheme codes, ~29k of which AMFI does not publish and 28.6k
    with no ISIN at all, so importing from it would bloat the master with dormant
    entries. This must report and nothing else.
    """
    from app.ingest import amfi as amfi_mod
    from app.state.models import Security

    class _Resp:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> list[dict]:
            return [
                {"schemeCode": 1, "schemeName": "Tracked", "isinGrowth": "INE002A01018",
                 "isinDivReinvestment": None},
                {"schemeCode": 2, "schemeName": "Unknown", "isinGrowth": "INE009A01021",
                 "isinDivReinvestment": None},
                {"schemeCode": 3, "schemeName": "No ISIN", "isinGrowth": None,
                 "isinDivReinvestment": None},
                {"schemeCode": 4, "schemeName": "Bad ISIN", "isinGrowth": "not-an-isin",
                 "isinDivReinvestment": None},
            ]

    Session = _temp_securities_session(monkeypatch)
    with Session() as session:
        session.add(Security(symbol="INE002A01018", series="MF", name="Tracked"))
        session.commit()

    monkeypatch.setattr(amfi_mod.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(amfi_mod, "SessionLocal", Session)
    # Any write path at all is a failure for this function.
    monkeypatch.setattr(
        amfi_mod, "_sync_amfi_securities", lambda df: pytest.fail("must not import")
    )

    report = amfi_mod.audit_unknown_schemes()
    assert report["remote_isins"] == 2, "only well-formed ISINs counted"
    assert report["tracked_isins"] == 1
    assert report["unknown"] == 1
    assert report["unknown_sample"] == ["INE009A01021"]

    # The unknown ISIN must still be absent afterwards.
    with Session() as session:
        symbols = {r[0] for r in session.query(Security.symbol).all()}
    assert symbols == {"INE002A01018"}, "warn-only: nothing written"


def test_sync_schemes_from_portal_chunks_a_short_window(monkeypatch) -> None:
    """Discovery must ask for a range the portal can actually answer.

    The portal returns an HTTP 200 error page for a window that is too wide, which
    arrives as zero rows -- indistinguishable from "no new schemes". An earlier
    version requested `days` back inclusive, i.e. one day more than asked, and
    unchunked.
    """
    from app.ingest import amfi as amfi_mod

    seen: dict[str, object] = {}

    def _fake_collect(start: date, end: date, known_codes: object = None,
                      chunk_days: int | None = None) -> tuple[pl.DataFrame, list[str]]:
        seen["start"] = start
        seen["end"] = end
        seen["known_codes"] = known_codes
        seen["chunk_days"] = chunk_days
        return pl.DataFrame(), []

    monkeypatch.setattr(amfi_mod, "collect_portal_history", _fake_collect)

    result = amfi_mod.sync_schemes_from_portal(days=14)

    assert result["ok"] is False, "an empty response is not a success"
    assert seen["known_codes"] is None, "must be unfiltered or nothing new is visible"
    assert seen["chunk_days"] == amfi_mod.PORTAL_CHUNK_DAYS
    assert (seen["end"] - seen["start"]).days == 13, "14 days inclusive"
