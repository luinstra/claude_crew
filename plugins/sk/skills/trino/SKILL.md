---
name: trino
description: Use whenever a query involves Trino — writing/optimizing Trino SQL, defining Kotlin/Exposed tables backed by Trino, or configuring query routing (e.g., QueryRouter, DataSource.TRINO). Triggers: the word "Trino" anywhere; catalog-qualified table paths like `hive.analytics.*`, `iceberg.warehouse.*`, or any `catalog.schema.table` pattern; Trino-specific functions such as `approx_distinct`, `approx_percentile`, or HyperLogLog; cursor-based pagination or streaming for large analytical result sets; OLAP window functions (ROW_NUMBER, RANK, LAG/LEAD) over analytical tables; read-only analytics table definitions; partition pruning, column pruning, limit pushdown, or offset-vs-cursor pagination in an analytics context; or references to a hive/iceberg catalog or routing queries to different data sources.
---

# Trino Patterns

Trino integration for OLAP queries and analytics workloads.

## When to Use Trino vs Postgres

| Use Case | Database | Reason |
|----------|----------|--------|
| CRUD operations | Postgres | Transactional, low latency |
| Single record lookup | Postgres | Index-optimized |
| Real-time updates | Postgres | ACID transactions |
| Large aggregations | Trino | Distributed processing |
| Cross-dataset joins | Trino | Federated queries |
| Historical analytics | Trino | Columnar storage |
| Ad-hoc exploration | Trino | Interactive queries |

## Catalog.Schema.Table Addressing

Trino uses three-level naming:

```
catalog.schema.table
```

Examples:
```sql
-- Hive catalog
SELECT * FROM hive.analytics.user_events

-- Iceberg catalog
SELECT * FROM iceberg.warehouse.transactions

-- PostgreSQL catalog (federated)
SELECT * FROM postgresql.public.users
```

In Exposed:
```kotlin
object UserEventsTable : Table("hive.analytics.user_events") {
    val userId = long("user_id")
    val eventType = varchar("event_type", 100)
    val timestamp = datetime("event_timestamp")
}
```

## Read-Only Nature

Trino connections are **read-only**. DDL and DML operations are not supported:

```kotlin
// Will throw UnsupportedOperationException
TrinoTable.insert { ... }
TrinoTable.update { ... }
TrinoTable.deleteWhere { ... }
```

Write to your OLTP database, read aggregations from Trino.

## Query Optimization

### Large Result Sets

Avoid offset-based pagination — Trino re-executes the full query for each page. Instead, use cursor-based iteration or streaming:

```kotlin
// DO: Cursor-based — filter by last seen value
fun processLargeDataset(database: Database, afterTimestamp: Instant?) {
    val batchSize = 10000

    var cursor = afterTimestamp
    while (true) {
        val batch = transaction(database) {
            UserEventsTable.selectAll()
                .apply { cursor?.let { where { UserEventsTable.timestamp greater it } } }
                .orderBy(UserEventsTable.timestamp)
                .limit(batchSize)
                .map { it.toEvent() }
        }
        if (batch.isEmpty()) break

        batch.forEach { processEvent(it) }
        cursor = batch.last().timestamp
    }
}

// DON'T: Offset-based — re-scans from the start every time
fun processLargeDataset(query: Query) {
    var offset = 0
    while (true) {
        val batch = query.limit(10000).offset(offset).toList()  // Gets slower with each page
        if (batch.isEmpty()) break
        offset += 10000
    }
}
```

### Limit Pushdown

Always include limits to avoid full table scans:

```kotlin
// Good - limited scan
UserEventsTable.selectAll()
    .where { eventType eq "purchase" }
    .limit(1000)

// Bad - full table scan
UserEventsTable.selectAll()
    .where { eventType eq "purchase" }
```

### Column Pruning

Request only needed columns:

```kotlin
// Good - only fetches needed columns
UserEventsTable.select(UserEventsTable.userId, UserEventsTable.total)
    .where { ... }

// Bad - fetches all columns
UserEventsTable.selectAll()
    .where { ... }
```

## Window Functions

Window functions are the bread and butter of analytical queries in Trino:

### Ranking

```kotlin
// In raw SQL via Exposed's custom expressions or literal SQL
// ROW_NUMBER, RANK, DENSE_RANK

// Example: find each customer's most recent order
val sql = """
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY customer_id
            ORDER BY created_at DESC
        ) AS rn
        FROM iceberg.warehouse.orders
    ) WHERE rn = 1
""".trimIndent()
```

### Running Aggregations

```sql
-- Running total of sales per customer
SELECT
    customer_id,
    order_date,
    amount,
    SUM(amount) OVER (
        PARTITION BY customer_id
        ORDER BY order_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_total
FROM hive.analytics.orders

-- Moving average (last 7 days)
SELECT
    event_date,
    daily_revenue,
    AVG(daily_revenue) OVER (
        ORDER BY event_date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS moving_avg_7d
FROM hive.analytics.daily_revenue
```

### LAG / LEAD

```sql
-- Compare each day to previous day
SELECT
    event_date,
    revenue,
    LAG(revenue) OVER (ORDER BY event_date) AS prev_day_revenue,
    revenue - LAG(revenue) OVER (ORDER BY event_date) AS day_over_day_change
FROM hive.analytics.daily_revenue
```

## Approximate Aggregations

For large datasets where exact counts aren't needed, approximate functions are significantly faster:

```sql
-- Approximate distinct count (HyperLogLog, ~2% error)
SELECT approx_distinct(user_id) AS unique_users
FROM hive.analytics.page_views
WHERE event_date >= DATE '2025-01-01'

-- Approximate percentiles
SELECT
    approx_percentile(response_time_ms, 0.50) AS p50,
    approx_percentile(response_time_ms, 0.95) AS p95,
    approx_percentile(response_time_ms, 0.99) AS p99
FROM hive.analytics.api_requests

-- Use exact COUNT(DISTINCT ...) only when precision matters (billing, compliance)
```

## Partition Pruning

Trino can skip reading entire partitions if your filter matches the partition column. This is often the single biggest performance lever:

```sql
-- DO: Filter on partition column (only reads matching partitions)
SELECT * FROM hive.analytics.events
WHERE event_date = DATE '2025-03-01'
  AND event_type = 'purchase'

-- DON'T: Filter only on non-partition column (full table scan)
SELECT * FROM hive.analytics.events
WHERE event_type = 'purchase'

-- DON'T: Wrap partition column in function (defeats pruning)
SELECT * FROM hive.analytics.events
WHERE YEAR(event_date) = 2025  -- Scans ALL partitions
```

**Tip:** Check the table's partition columns with `SHOW CREATE TABLE catalog.schema.table` before writing queries.

## Query Router Pattern

Route queries based on data source:

```kotlin
class QueryRouter(
    private val postgresRepo: QueryRepository,
    private val trinoRepo: TrinoQueryRepository
) {
    fun query(dataset: Dataset, params: QueryParams): QueryResult {
        return when (dataset.source) {
            DataSource.TRINO -> trinoRepo.query(dataset, params)
            else -> postgresRepo.query(dataset, params)
        }
    }
}
```

## Testing

Trino tests typically use mocks:

```kotlin
class QueryRouterTest : FunSpec({
    val postgresRepo = mockk<QueryRepository>()
    val trinoRepo = mockk<TrinoQueryRepository>()
    val router = QueryRouter(postgresRepo, trinoRepo)

    test("routes TRINO source to Trino repository") {
        val dataset = Dataset(source = DataSource.TRINO)
        val result = QueryResult(...)

        every { trinoRepo.query(dataset, any()) } returns result

        router.query(dataset, params) shouldBe result

        verify { trinoRepo.query(dataset, any()) }
        verify(exactly = 0) { postgresRepo.query(any(), any()) }
    }
})
```

## Common Mistakes

**Trying to write to Trino**
```kotlin
// DON'T - Trino is read-only
transaction {
    AnalyticsTable.insert { ... }
}

// DO - Write to OLTP, read from Trino
postgresTransaction {
    EventTable.insert { ... }
}
val analytics = trinoRepo.query(analyticsDataset)
```

---

**Missing catalog prefix**
```kotlin
// DON'T
object EventsTable : Table("events") {
    // Trino won't know which catalog/schema
}

// DO
object EventsTable : Table("hive.analytics.events") {
    // Explicit catalog and schema
}
```

---

**No limit on analytical queries**
```kotlin
// DON'T - will scan entire table
UserEventsTable.selectAll()

// DO - always include reasonable limits
UserEventsTable.selectAll().limit(10000)
```

---

**Expecting transactions**
```kotlin
// DON'T - Trino doesn't support transactions
// Each query is independent, not transactional
```

---

**Wrapping partition columns in functions**
```sql
-- DON'T — defeats partition pruning
WHERE YEAR(event_date) = 2025

-- DO — direct comparison enables pruning
WHERE event_date >= DATE '2025-01-01'
  AND event_date < DATE '2026-01-01'
```

## Checklist

When working with Trino:

- [ ] Table names include `catalog.schema.table`
- [ ] Query includes reasonable `limit`
- [ ] Only SELECT operations (no INSERT/UPDATE/DELETE)
- [ ] Column pruning — select only needed fields
- [ ] Partition columns filtered directly (no wrapping functions)
- [ ] Use cursor-based iteration for large result sets (not offset)
- [ ] Consider `approx_distinct` / `approx_percentile` for large datasets
