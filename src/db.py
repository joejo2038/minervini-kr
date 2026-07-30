"""DuckDB 저장소.

전 종목 일봉을 한 파일에 담습니다. 2,700종목 x 3년이면 대략 200만 행인데
DuckDB에서는 수 초 안에 처리됩니다. Postgres까지 갈 필요 없습니다.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    code    VARCHAR NOT NULL,
    date    DATE    NOT NULL,
    open    DOUBLE,
    high    DOUBLE,
    low     DOUBLE,
    close   DOUBLE,
    volume  BIGINT,
    value   BIGINT,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS listing (
    code       VARCHAR PRIMARY KEY,
    name       VARCHAR,
    market     VARCHAR,
    marcap     BIGINT,   -- 원 단위
    shares     BIGINT,
    updated_at DATE
);

CREATE TABLE IF NOT EXISTS fundamentals (
    code VARCHAR NOT NULL,
    date DATE    NOT NULL,
    bps  DOUBLE,
    per  DOUBLE,
    pbr  DOUBLE,
    eps  DOUBLE,
    div  DOUBLE,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS flows (
    code       VARCHAR NOT NULL,
    date       DATE    NOT NULL,
    inst_net   BIGINT,   -- 기관합계 순매수 (원)
    frgn_net   BIGINT,   -- 외국인합계 순매수 (원)
    retail_net BIGINT,   -- 개인 순매수 (원)
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS indices (
    code   VARCHAR NOT NULL,
    date   DATE    NOT NULL,
    close  DOUBLE,
    volume BIGINT,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.db_path))
        self.con.execute(SCHEMA)

    # ---------- 쓰기 ----------

    def upsert(self, table: str, df: pd.DataFrame, keys: list[str]) -> int:
        """중복 키는 덮어씁니다. 빈 프레임이면 조용히 통과."""
        if df is None or df.empty:
            return 0
        cols = list(df.columns)
        self.con.register("_staging", df)
        key_pred = " AND ".join(f"t.{k} = s.{k}" for k in keys)
        self.con.execute(
            f"DELETE FROM {table} t USING _staging s WHERE {key_pred}"
        )
        self.con.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _staging"
        )
        self.con.unregister("_staging")
        return len(df)

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("DELETE FROM meta WHERE key = ?", [key])
        self.con.execute("INSERT INTO meta VALUES (?, ?)", [key, value])

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return row[0] if row else default

    # ---------- 읽기 ----------

    def last_price_date(self, code: str | None = None) -> pd.Timestamp | None:
        if code:
            row = self.con.execute(
                "SELECT max(date) FROM prices WHERE code = ?", [code]
            ).fetchone()
        else:
            row = self.con.execute("SELECT max(date) FROM prices").fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    def price_panel(self, codes: list[str] | None = None,
                    start: str | None = None) -> pd.DataFrame:
        """long-format 일봉을 반환합니다. code, date로 정렬되어 있습니다."""
        where, params = [], []
        if codes:
            placeholders = ", ".join("?" * len(codes))
            where.append(f"code IN ({placeholders})")
            params += codes
        if start:
            where.append("date >= ?")
            params.append(start)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return self.con.execute(
            f"SELECT * FROM prices {clause} ORDER BY code, date", params
        ).df()

    def listing_df(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM listing").df()

    def latest_fundamentals(self) -> pd.DataFrame:
        return self.con.execute(
            """
            SELECT f.* FROM fundamentals f
            JOIN (SELECT code, max(date) AS d FROM fundamentals GROUP BY code) m
              ON f.code = m.code AND f.date = m.d
            """
        ).df()

    def flow_sums(self, window_start: str) -> pd.DataFrame:
        return self.con.execute(
            """
            SELECT code,
                   sum(inst_net)   AS inst_net,
                   sum(frgn_net)   AS frgn_net,
                   sum(retail_net) AS retail_net
            FROM flows WHERE date >= ?
            GROUP BY code
            """,
            [window_start],
        ).df()

    def index_series(self, code: str) -> pd.DataFrame:
        return self.con.execute(
            "SELECT date, close FROM indices WHERE code = ? ORDER BY date", [code]
        ).df()

    def row_count(self, table: str) -> int:
        return self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def repair_values(self) -> int:
        """
        거래대금이 0인 행을 종가 x 거래량으로 채웁니다.

        FinanceDataReader가 거래대금을 주지 않기 때문에 초기 버전에서 받은
        데이터는 value가 전부 0입니다. 그대로 두면 유동성 필터가 전 종목을
        걸러냅니다. 다시 내려받지 않고 여기서 보정합니다.
        """
        n = self.con.execute(
            "SELECT count(*) FROM prices WHERE (value IS NULL OR value = 0) AND volume > 0"
        ).fetchone()[0]
        if n:
            self.con.execute(
                """
                UPDATE prices
                SET value = CAST(close * volume AS BIGINT)
                WHERE (value IS NULL OR value = 0) AND volume > 0
                """
            )
        return int(n)

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
