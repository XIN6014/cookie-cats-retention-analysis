-- name: data_quality
SELECT
    COUNT(*) AS row_count,
    COUNT(DISTINCT userid) AS unique_users,
    SUM(CASE WHEN version IS NULL THEN 1 ELSE 0 END) AS missing_version,
    SUM(CASE WHEN retention_1 IS NULL THEN 1 ELSE 0 END) AS missing_retention_1,
    SUM(CASE WHEN retention_7 IS NULL THEN 1 ELSE 0 END) AS missing_retention_7
FROM players;

-- name: group_metrics
SELECT
    version,
    COUNT(*) AS users,
    ROUND(100.0 * AVG(retention_1), 2) AS d1_retention_pct,
    ROUND(100.0 * AVG(retention_7), 2) AS d7_retention_pct,
    ROUND(AVG(sum_gamerounds), 2) AS mean_rounds,
    ROUND(100.0 * AVG(CASE WHEN sum_gamerounds = 0 THEN 1.0 ELSE 0.0 END), 2) AS zero_round_pct,
    MAX(sum_gamerounds) AS max_rounds
FROM players
GROUP BY version
ORDER BY version;

-- name: group_share
SELECT
    version,
    COUNT(*) AS users,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM players), 3) AS user_share_pct
FROM players
GROUP BY version
ORDER BY version;

-- name: retention_by_round_bucket
SELECT
    version,
    CASE
        WHEN sum_gamerounds = 0 THEN '0'
        WHEN sum_gamerounds BETWEEN 1 AND 9 THEN '1-9'
        WHEN sum_gamerounds BETWEEN 10 AND 29 THEN '10-29'
        WHEN sum_gamerounds BETWEEN 30 AND 49 THEN '30-49'
        WHEN sum_gamerounds BETWEEN 50 AND 99 THEN '50-99'
        ELSE '100+'
    END AS round_bucket,
    COUNT(*) AS users,
    ROUND(100.0 * AVG(retention_1), 2) AS d1_retention_pct,
    ROUND(100.0 * AVG(retention_7), 2) AS d7_retention_pct,
    ROUND(AVG(sum_gamerounds), 2) AS mean_rounds
FROM players
GROUP BY version, round_bucket
ORDER BY
    version,
    CASE round_bucket
        WHEN '0' THEN 0
        WHEN '1-9' THEN 1
        WHEN '10-29' THEN 2
        WHEN '30-49' THEN 3
        WHEN '50-99' THEN 4
        ELSE 5
    END;

-- name: top_round_outliers
SELECT
    userid,
    version,
    sum_gamerounds,
    retention_1,
    retention_7
FROM players
ORDER BY sum_gamerounds DESC
LIMIT 10;

-- name: retention_counts
SELECT
    version,
    SUM(retention_1) AS d1_retained_users,
    COUNT(*) AS d1_total_users,
    SUM(retention_7) AS d7_retained_users,
    COUNT(*) AS d7_total_users
FROM players
GROUP BY version
ORDER BY version;

