import argparse

SESSION_SUMMARY = """
SELECT s.id, s.started_at, s.ended_at, s.environment, s.is_live,
       s.git_commit,
       (SELECT count(*) FROM quotes q WHERE q.session_id = s.id) AS quotes,
       (SELECT count(*) FROM orders o WHERE o.session_id = s.id) AS orders,
       (SELECT count(*) FROM orders o WHERE o.session_id = s.id
          AND o.status = 'rejected') AS rejected,
       (SELECT count(*) FROM fills f JOIN orders o ON o.id = f.order_id
          WHERE o.session_id = s.id) AS fills
FROM sessions s ORDER BY s.id DESC LIMIT %s
"""

ADVERSE_SELECTION = """
WITH filled AS (
    SELECT f.id, f.market_id, f.ts, f.book_side, f.price_cents, f.contracts
    FROM fills f
    JOIN orders o ON o.id = f.order_id
    WHERE %s::bigint IS NULL OR o.session_id = %s::bigint
),
marked AS (
    SELECT filled.*,
           (SELECT b.mid_cents FROM book_snapshots b
            WHERE b.market_id = filled.market_id
              AND b.ts >= filled.ts + (%s || ' seconds')::interval
            ORDER BY b.ts LIMIT 1) AS mid_after
    FROM filled
)
SELECT count(*) AS fills,
       round(avg(CASE WHEN book_side = 'bid' THEN mid_after - price_cents
                      ELSE price_cents - mid_after END), 2) AS avg_edge_cents,
       round(sum(CASE WHEN book_side = 'bid' THEN mid_after - price_cents
                      ELSE price_cents - mid_after END
                 * contracts) / 100.0, 2) AS total_dollars
FROM marked WHERE mid_after IS NOT NULL
"""

FAIR_VALUE_LEAD_LAG = """
WITH paired AS (
    SELECT f.market_id, f.ts, f.probability * 100 AS fair_cents,
           (SELECT b.mid_cents FROM book_snapshots b
            WHERE b.market_id = f.market_id AND b.ts <= f.ts
            ORDER BY b.ts DESC LIMIT 1) AS mid_now,
           (SELECT b.mid_cents FROM book_snapshots b
            WHERE b.market_id = f.market_id
              AND b.ts >= f.ts + (%s || ' seconds')::interval
            ORDER BY b.ts LIMIT 1) AS mid_later
    FROM fair_values f
    WHERE %s::bigint IS NULL OR f.market_id = %s::bigint
)
SELECT count(*) AS observations,
       round(corr(fair_cents - mid_now, mid_later - mid_now)::numeric, 4)
           AS lead_correlation,
       round(avg(abs(fair_cents - mid_now))::numeric, 2) AS avg_gap_cents
FROM paired WHERE mid_now IS NOT NULL AND mid_later IS NOT NULL
"""

FILL_QUALITY_BY_SPREAD = """
SELECT b.spread_cents,
       count(*) AS fills,
       round(avg(f.price_cents), 1) AS avg_fill_price,
       round(avg(b.mid_cents), 1) AS avg_mid_at_fill
FROM fills f
JOIN LATERAL (
    SELECT * FROM book_snapshots b
    WHERE b.market_id = f.market_id AND b.ts <= f.ts
    ORDER BY b.ts DESC LIMIT 1
) b ON true
GROUP BY b.spread_cents ORDER BY b.spread_cents
"""


def print_rows(title, columns, rows):
    print(f"\n{title}")
    if not rows:
        print("  (no data)")
        return
    widths = [max(len(str(column)), max(len(str(row[index]))
                                        for row in rows))
              for index, column in enumerate(columns)]
    header = "  ".join(str(column).ljust(width)
                       for column, width in zip(columns, widths))
    print("  " + header)
    print("  " + "  ".join("-" * width for width in widths))
    for row in rows:
        print("  " + "  ".join(str(value).ljust(width)
                               for value, width in zip(row, widths)))


def query(connection, statement, parameters=()):
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        columns = [description[0] for description in cursor.description]
        return columns, cursor.fetchall()


def run_report(args):
    import db
    with db.connect(args.url) as connection:
        columns, rows = query(connection, SESSION_SUMMARY, (args.sessions,))
        print_rows("Recent sessions", columns, rows)

        columns, rows = query(connection, ADVERSE_SELECTION,
                              (args.session, args.session, args.horizon))
        print_rows(f"Adverse selection at +{args.horizon}s "
                   f"(positive means the market moved your way)",
                   columns, rows)

        columns, rows = query(connection, FAIR_VALUE_LEAD_LAG,
                              (args.horizon, args.market, args.market))
        print_rows(f"Does the SGO fair lead the book by {args.horizon}s? "
                   f"(positive correlation means yes)", columns, rows)

        columns, rows = query(connection, FILL_QUALITY_BY_SPREAD)
        print_rows("Fill quality by book spread", columns, rows)


def main():
    parser = argparse.ArgumentParser(
        description="Analyse recorded sessions: adverse selection, whether "
                    "the SGO fair leads the Kalshi book, and fill quality.")
    parser.add_argument("--url", default=None)
    parser.add_argument("--session", type=int, default=None,
                        help="restrict to one session id")
    parser.add_argument("--market", type=int, default=None,
                        help="restrict lead-lag to one market id")
    parser.add_argument("--horizon", type=int, default=30,
                        help="seconds after a fill or fair value to mark at")
    parser.add_argument("--sessions", type=int, default=10)
    args = parser.parse_args()
    run_report(args)


if __name__ == "__main__":
    main()
