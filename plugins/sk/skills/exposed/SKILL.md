---
name: exposed
description: Use this skill whenever the user is working with Kotlin Repository classes, transaction blocks, database table definitions, ResultRow mappings, or any database operation in a Kotlin/JVM project. Trigger for: creating or modifying Repository classes (e.g. findById, create, update methods), batch inserts (batchInsert, batchUpsert), insertReturning/updateReturning, fixing N+1 query problems, moving logic in or out of transaction blocks, defining or altering database tables, writing join queries and mapping results to domain objects, or handling events around database transactions. This covers JetBrains Exposed DSL patterns. Activate even if the user says "repository", "transaction", "table", "insert", "query", or "N+1" without mentioning Exposed or ORM explicitly. Do NOT activate for non-Kotlin SQL (e.g. Trino, raw analytics queries), REST/HTTP endpoint logic, Redis/caching, Gradle build tasks, or pure domain modeling with data classes and sealed interfaces.
---

# Exposed ORM Patterns

JetBrains Exposed ORM conventions for Kotlin database access.

**This project uses Exposed's DSL API exclusively** (not the DAO/Entity API). All database access goes through the typesafe DSL with explicit `transaction` blocks.

## Repository Pattern

```kotlin
class UserRepository(private val database: Database) {

    fun findById(id: UserId): User? = transaction(database) {
        UserTable.selectAll()
            .where { UserTable.id eq id.value }
            .firstOrNull()
            ?.toUser()
    }

    fun create(user: User): User = transaction(database) {
        UserTable.insert {
            it[id] = user.id.value
            it[name] = user.name
            it[email] = user.email
            it[createdAt] = user.createdAt
        }
        user
    }

    fun update(user: User): User = transaction(database) {
        UserTable.update({ UserTable.id eq user.id.value }) {
            it[name] = user.name
            it[email] = user.email
        }
        user
    }

    fun delete(id: UserId) = transaction(database) {
        UserTable.deleteWhere { UserTable.id eq id.value }
    }
}
```

## ResultRow Mapping

Map `ResultRow` to domain models with an extension function on the table's companion or as a private function:

```kotlin
// Extension on ResultRow
private fun ResultRow.toUser() = User(
    id = UserId(this[UserTable.id].value),
    name = this[UserTable.name],
    email = this[UserTable.email],
    createdAt = this[UserTable.createdAt],
)

// Usage in repository
fun findById(id: UserId): User? = transaction(database) {
    UserTable.selectAll()
        .where { UserTable.id eq id.value }
        .firstOrNull()
        ?.toUser()
}

// For joins, accept the specific tables as context
private fun ResultRow.toOrderWithCustomer() = OrderWithCustomer(
    order = Order(
        id = OrderId(this[OrderTable.id].value),
        status = this[OrderTable.status],
    ),
    customerName = this[CustomerTable.name],
)
```

**Keep mapping functions close to the repository that uses them.** Don't scatter them across the codebase.

## Transaction Management

**Always wrap database operations in `transaction`:**

```kotlin
// Standard transaction
fun doWork() = transaction(database) {
    val user = UserTable.selectAll().where { ... }.first()
    OrderTable.insert { ... }
}

// Read-only transaction (optimization hint)
fun readData() = transaction(database, readOnly = true) {
    UserTable.selectAll().toList()
}
```

## Table Definitions

```kotlin
// Long primary key (most common)
object UserTable : LongIdTable("users") {
    val name = varchar("name", 255)
    val email = varchar("email", 255)
    val createdAt = datetime("created_at")
}

// UUID primary key
object OrderTable : UUIDTable("orders") {
    val customerId = long("customer_id")
    val status = enumerationByName<OrderStatus>("status", 50)
}

// Composite primary key
object OrderItemTable : Table("order_items") {
    val orderId = reference("order_id", OrderTable)
    val productId = reference("product_id", ProductTable)
    val quantity = integer("quantity")

    override val primaryKey = PrimaryKey(orderId, productId)
}
```

## Custom Column Types

### JSONB (PostgreSQL)

```kotlin
object ConfigTable : LongIdTable("configs") {
    val settings = jsonb<Settings>("settings", Json.Default)
}

// Insert
ConfigTable.insert {
    it[settings] = Settings(theme = "dark", locale = "en")
}
```

### Arrays (PostgreSQL)

```kotlin
object ItemTable : LongIdTable("items") {
    val tags = array<String>("tags")
}

// Insert
ItemTable.insert {
    it[tags] = listOf("featured", "new")
}

// Query array contains
ItemTable.selectAll().where {
    ItemTable.tags contains "featured"
}
```

### Enums by Name

```kotlin
object RecordTable : LongIdTable("records") {
    val status = enumerationByName<Status>("status", 50)
}
```

## Advanced Patterns

### CTE (Common Table Expressions)

```kotlin
val recentUsers = Cte("recent_users") {
    UserTable.selectAll().where { UserTable.createdAt greater thirtyDaysAgo }
}

recentUsers.selectAll().where { recentUsers[UserTable.status] eq "active" }
```

### Insert/Update Returning

```kotlin
// Insert returning
val inserted = UserTable.insertReturning(listOf(UserTable.id, UserTable.name)) {
    it[name] = "John"
}

// Update returning
val updated = UserTable.updateReturning(listOf(UserTable.name)) {
    it[name] = "Jane"
}
```

### Batch Operations

```kotlin
// Batch insert
UserTable.batchInsert(users) { user ->
    this[UserTable.name] = user.name
    this[UserTable.email] = user.email
}

// Batch update
users.forEach { user ->
    UserTable.update({ UserTable.id eq user.id.value }) {
        it[name] = user.name
    }
}
```

### Upsert (Insert or Update)

```kotlin
// Insert or update on conflict
UserTable.upsert(UserTable.email) {
    it[name] = "John"
    it[email] = "john@example.com"
    it[updatedAt] = Instant.now()
}

// Batch upsert
UserTable.batchUpsert(users, UserTable.email) { user ->
    this[UserTable.name] = user.name
    this[UserTable.email] = user.email
    this[UserTable.updatedAt] = Instant.now()
}
```

### Insert Ignore

```kotlin
// Skip rows that violate unique constraints
UserTable.insertIgnore {
    it[email] = "john@example.com"
    it[name] = "John"
}
```

## Testing

Use Kotest with TestContainers:

```kotlin
class UserRepositoryTest : FunSpec({
    val postgres = PostgreSQLContainer("postgres:16")

    beforeSpec { postgres.start() }
    afterSpec { postgres.stop() }

    test("creates user") {
        val database = Database.connect(postgres.jdbcUrl, postgres.username, postgres.password)
        val repo = UserRepository(database)

        val user = repo.create(User(...))

        user.id shouldNotBe null
    }
})
```

## Common Mistakes

**Database operations outside transaction**
```kotlin
// DON'T
fun findUser(id: UserId): User? {
    return UserTable.selectAll().where { ... }.firstOrNull()  // No transaction!
}

// DO
fun findUser(id: UserId): User? = transaction(database) {
    UserTable.selectAll().where { UserTable.id eq id.value }.firstOrNull()?.toUser()
}
```

---

**N+1 queries**
```kotlin
// DON'T
val orders = OrderTable.selectAll().map { it.toOrder() }
orders.forEach { order ->
    val items = ItemTable.selectAll().where { ItemTable.orderId eq order.id }  // N queries!
}

// DO - Batch fetch
val orders = OrderTable.selectAll().map { it.toOrder() }
val orderIds = orders.map { it.id }
val itemsByOrder = ItemTable.selectAll()
    .where { ItemTable.orderId inList orderIds }
    .groupBy { it[ItemTable.orderId] }
```

---

**Publishing events inside transaction**
```kotlin
// DON'T - If event publish fails, insert still commits!
transaction(database) {
    UserTable.insert { ... }
    eventPublisher.publish(UserCreatedEvent(...))
}

// DO - Publish after commit
transaction(database) {
    UserTable.insert { ... }
}
eventPublisher.publish(UserCreatedEvent(...))

// OR use transactional outbox pattern
```

## Checklist

When working with Exposed:

- [ ] All database operations wrapped in `transaction`
- [ ] Events published outside transaction (or use outbox)
- [ ] No N+1 queries
- [ ] Tests use TestContainers
