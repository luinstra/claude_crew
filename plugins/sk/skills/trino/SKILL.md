---
name: trino
description: Trino query patterns for analytics. Use when writing Trino queries, analytics queries, data warehouse queries, or configuring query routing.
---

# Trino Patterns

Trino integration for OLAP queries and analytics workloads.

## When This Activates

- "trino query"
- "analytics query"
- "data warehouse"
- "query router"
- "olap query"

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

Use batching for large results:

```kotlin
fun processLargeDataset(query: Query) {
    var offset = 0
    val batchSize = 10000

    while (true) {
        val batch = query.limit(batchSize).offset(offset).toList()
        if (batch.isEmpty()) break

        batch.forEach { row -> processRow(row) }
        offset += batchSize
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

## Checklist

When working with Trino:

- [ ] Table names include `catalog.schema.table`
- [ ] Query includes reasonable `limit`
- [ ] Only SELECT operations (no INSERT/UPDATE/DELETE)
- [ ] Column pruning - select only needed fields
- [ ] Use batching for large result sets
