---
name: exposed
description: JetBrains Exposed ORM patterns. Use when writing repositories, database queries, transactions, or table definitions.
---

# Exposed ORM Patterns

JetBrains Exposed ORM conventions for Kotlin database access.

## When This Activates

- "create a repository"
- "database query"
- "transaction"
- "exposed table"

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
