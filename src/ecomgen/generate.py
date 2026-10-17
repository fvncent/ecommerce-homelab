"""Deterministic entity generation. Pure: no I/O, no DB. Rows are NamedTuples; load them by name
(`_fields` as the column list), never positionally.

Identity strategy:
- Entity N of a kind is `<prefix>_N` (N >= 1, no leading zeros) and is generated from its own RNG, seeded by
  sha256(seed:entity:N). Any row, and so any chunk, can be regenerated alone: output doesn't depend on
  chunk_rows, chunk order or what ran before (needed for resume).
- Child IDs use a fixed stride so they're computable without counting earlier orders:
  itm_{(order-1)*MAX_LINES + line_no}, pay_{(order-1)*MAX_ATTEMPTS + attempt_no}. IDs are sparse by design.
- FK-safe order: ENTITY_ORDER. Category parents always have a lower ID.

Portability: only rng.random() (Mersenne Twister, platform-independent) with integer/Decimal arithmetic; no
float transcendental functions, whose last-bit results can differ between libm implementations.
"""

import hashlib
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple, TypeVar

from ecomgen.config import Config

ENTITY_ORDER = ("sellers", "categories", "customers", "products", "orders")
MAX_LINES = 5
MAX_ATTEMPTS = 3
# Bump whenever generated rows change for the same inputs (the golden test is keyed by it): resume refuses
# runs written under another data version, since regenerated chunks would no longer match committed ones.
DATA_VERSION = 1
PROBE_TRIES = 32
CURRENCY = "USD"  # MVP: single-currency marketplace; per-market currencies come later
MASTER_DATA_HISTORY = timedelta(days=730)  # master data is created in [clock_start - 730d, clock_start)
FREE_SHIPPING_FROM = Decimal("50.00")
SHIPPING_FEE = Decimal("5.99")
CENT = Decimal("0.01")

T = TypeVar("T")


class GenerationError(RuntimeError):
    pass


class Seller(NamedTuple):
    seller_id: str
    display_name: str
    region: str
    status: str
    created_at: datetime
    updated_at: datetime


class Category(NamedTuple):
    category_id: str
    parent_category_id: str | None
    name: str


class Customer(NamedTuple):
    customer_id: str
    full_name: str
    email: str
    country: str
    segment: str
    status: str
    created_at: datetime
    updated_at: datetime


class Product(NamedTuple):
    product_id: str
    seller_id: str
    category_id: str
    name: str
    price: Decimal
    currency: str
    active: bool
    created_at: datetime
    updated_at: datetime


class Order(NamedTuple):
    order_id: str
    customer_id: str
    placed_at: datetime
    status: str
    currency: str
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    shipping: Decimal
    total: Decimal
    updated_at: datetime


class OrderItem(NamedTuple):
    order_item_id: str
    order_id: str
    line_no: int
    product_id: str
    quantity: int
    unit_price: Decimal
    discount: Decimal
    tax: Decimal
    line_total: Decimal


class PaymentAttempt(NamedTuple):
    payment_id: str
    order_id: str
    attempt_no: int
    provider: str
    method: str
    state: str
    amount: Decimal
    currency: str
    created_at: datetime
    updated_at: datetime


# ---------- static vocabularies (synthetic) ----------

TAXONOMY = {
    "Electronics": ["Headphones", "Chargers", "Speakers", "Keyboards"],
    "Home": ["Lamps", "Cookware", "Bedding", "Storage"],
    "Fashion": ["Jackets", "Sneakers", "Scarves", "Backpacks"],
    "Sports": ["Yoga Mats", "Bottles", "Rackets", "Helmets"],
    "Books": ["Novels", "Cookbooks", "Atlases", "Comics"],
    "Toys": ["Puzzles", "Blocks", "Board Games", "Kites"],
    "Beauty": ["Soaps", "Brushes", "Mirrors", "Lotions"],
    "Garden": ["Planters", "Hoses", "Seeds", "Shears"],
}
# cat_1..cat_8 are roots; leaves follow in taxonomy order, so every parent ID is lower than its children's.
CATEGORIES: tuple[Category, ...] = tuple(
    [Category(f"cat_{i}", None, root) for i, root in enumerate(TAXONOMY, 1)]
    + [
        Category(f"cat_{len(TAXONOMY) + 1 + j}", f"cat_{1 + j // 4}", leaf)
        for j, leaf in enumerate(leaf for leaves in TAXONOMY.values() for leaf in leaves)
    ]
)
LEAF_CATEGORIES = tuple(c for c in CATEGORIES if c.parent_category_id is not None)

FIRST_NAMES = ("Ada", "Ben", "Chen", "Dana", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jo",
               "Kofi", "Lena", "Mateo", "Nia", "Omar", "Priya", "Quinn", "Rosa", "Sam", "Tariq")
LAST_NAMES = ("Example", "Sample", "Testwood", "Mockford", "Placeholder", "Fakeley", "Demo", "Synth",
              "Dummer", "Stubbs", "Fixture", "Lorem")
COUNTRY_TAX = {"US": Decimal("0.07"), "CA": Decimal("0.13"), "GB": Decimal("0.20"),
               "DE": Decimal("0.19"), "FR": Decimal("0.20"), "AU": Decimal("0.10")}
SELLER_WORDS = (("Blue", "North", "Prime", "Urban", "Golden", "Quiet"), ("Fox", "Harbor", "Peak", "Grove", "Lane"))
ADJECTIVES = ("Compact", "Classic", "Deluxe", "Eco", "Pro", "Travel", "Smart", "Vintage")
PROVIDERS = ("payco-sim", "stripe-sim", "adyen-sim")


# ---------- deterministic randomness ----------

def _rng(seed: int, entity: str, n: int) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{entity}:{n}".encode()).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))


def _pick(rng: random.Random, weighted: tuple[tuple[T, int], ...]) -> T:
    """Integer-weighted choice using one rng.random() draw."""
    r = int(rng.random() * sum(w for _, w in weighted))
    for value, w in weighted:
        if r < w:
            return value
        r -= w
    raise AssertionError("unreachable")


def _int(rng: random.Random, lo: int, hi: int) -> int:
    """Uniform integer in [lo, hi]."""
    return lo + int(rng.random() * (hi - lo + 1))


def _choice(rng: random.Random, seq: tuple[T, ...]) -> T:
    return seq[_int(rng, 0, len(seq) - 1)]


def _between(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """Whole-second timestamp in [start, end)."""
    r = rng.random()  # drawn unconditionally so later draws don't depend on span
    span = int((end - start).total_seconds())
    return start + timedelta(seconds=int(r * span)) if span > 0 else start


def _money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------- world ----------

@dataclass(frozen=True)
class World:
    seed: int
    clock_start: datetime
    clock_end: datetime
    sellers: int
    customers: int
    products: int
    orders: int

    def __post_init__(self) -> None:
        for name in ("clock_start", "clock_end"):
            t = getattr(self, name)
            if t.utcoffset() != timedelta(0):
                raise GenerationError(f"{name} must be a UTC timestamp, got {t!r}")
        if self.clock_end <= self.clock_start:
            raise GenerationError("clock_end must be after clock_start")
        for name in ("sellers", "customers", "products", "orders"):
            if getattr(self, name) < 1:
                raise GenerationError(f"{name} must be >= 1")

    @classmethod
    def from_config(cls, cfg: Config) -> "World":
        g = cfg.generator
        c = g.counts
        return cls(g.seed, g.clock_start, g.clock_end, c["sellers"], c["customers"], c["products"], c["orders"])

    def count(self, entity: str) -> int:
        if entity == "categories":
            return len(CATEGORIES)
        if entity not in ENTITY_ORDER:
            raise GenerationError(f"unknown entity {entity!r}")
        n: int = getattr(self, entity)
        return n

    def seller(self, n: int) -> Seller:
        rng = _rng(self.seed, "sellers", n)
        history_start = self.clock_start - MASTER_DATA_HISTORY
        created = _between(rng, history_start, self.clock_start)
        name = f"{_choice(rng, SELLER_WORDS[0])} {_choice(rng, SELLER_WORDS[1])} Trading {n}"
        region = _pick(rng, (("NA", 50), ("EU", 35), ("APAC", 15)))
        status = _pick(rng, (("ACTIVE", 92), ("SUSPENDED", 5), ("CLOSED", 3)))
        return Seller(f"sel_{n}", name, region, status, created, _between(rng, created, self.clock_start))

    def customer(self, n: int) -> Customer:
        rng = _rng(self.seed, "customers", n)
        first, last = _choice(rng, FIRST_NAMES), _choice(rng, LAST_NAMES)
        country = _pick(rng, (("US", 55), ("CA", 10), ("GB", 12), ("DE", 10), ("FR", 8), ("AU", 5)))
        segment = _pick(rng, (("CONSUMER", 80), ("BUSINESS", 15), ("VIP", 5)))
        status = _pick(rng, (("ACTIVE", 90), ("INACTIVE", 8), ("CLOSED", 2)))
        created = _between(rng, self.clock_start - MASTER_DATA_HISTORY, self.clock_start)
        email = f"{first.lower()}.{last.lower()}.{n}@example.com"  # n makes it unique
        return Customer(f"cus_{n}", f"{first} {last}", email, country, segment, status, created,
                        _between(rng, created, self.clock_start))

    def product(self, n: int) -> Product:
        rng = _rng(self.seed, "products", n)
        seller_n = _int(rng, 1, self.sellers)
        seller = self.seller(seller_n)
        category = _choice(rng, LEAF_CATEGORIES)
        lo, hi = _pick(rng, (((299, 1999), 40), ((2000, 9999), 40), ((10000, 49999), 20)))
        price = Decimal(_int(rng, lo, hi)).scaleb(-2)
        r = rng.random()  # drawn unconditionally so other attributes don't depend on seller status
        active = seller.status == "ACTIVE" and r >= 0.05
        created = _between(rng, seller.created_at, self.clock_start)
        name = f"{_choice(rng, ADJECTIVES)} {category.name} {_int(rng, 1, 999)}"
        return Product(f"prd_{n}", seller.seller_id, category.category_id, name, price, CURRENCY, active,
                       created, _between(rng, created, self.clock_start))

    def order(self, n: int) -> tuple[Order, list[OrderItem], list[PaymentAttempt]]:
        if not 1 <= n <= self.orders:
            raise GenerationError(f"order {n} out of range 1..{self.orders}")
        rng = _rng(self.seed, "orders", n)
        oid = f"ord_{n}"
        # Order N sits in slot N of the window (integer seconds), so IDs increase with placed_at.
        span = int((self.clock_end - self.clock_start).total_seconds())
        lo, hi = span * (n - 1) // self.orders, span * n // self.orders
        placed = self.clock_start + timedelta(seconds=lo + int(rng.random() * (hi - lo)))

        found = _probe(rng, self.customers, self.customer, lambda c: c.status == "ACTIVE", set())
        if found is None:
            raise GenerationError("no ACTIVE customers to place orders")
        customer = found[1]

        items: list[OrderItem] = []
        used: set[int] = set()
        tax_rate = COUNTRY_TAX[customer.country]
        for line_no in range(1, _pick(rng, ((1, 50), (2, 25), (3, 15), (4, 7), (5, 3))) + 1):
            found_p = _probe(rng, self.products, self.product, lambda p: p.active, used)
            if found_p is None:
                break  # fewer distinct active products than lines
            used.add(found_p[0])
            product = found_p[1]
            qty = _pick(rng, ((1, 70), (2, 20), (3, 10)))
            gross = qty * product.price
            pct = _pick(rng, ((0, 80), (5, 8), (10, 7), (20, 5)))
            discount = _money(gross * pct / 100)
            tax = _money((gross - discount) * tax_rate)
            items.append(OrderItem(f"itm_{(n - 1) * MAX_LINES + line_no}", oid, line_no, product.product_id,
                                   qty, product.price, discount, tax, gross - discount + tax))
        if not items:
            raise GenerationError("no active products to order")

        subtotal = sum((i.quantity * i.unit_price for i in items), Decimal("0.00"))
        discount = sum((i.discount for i in items), Decimal("0.00"))
        tax = sum((i.tax for i in items), Decimal("0.00"))
        shipping = Decimal("0.00") if subtotal - discount >= FREE_SHIPPING_FROM else SHIPPING_FEE
        total = subtotal - discount + tax + shipping

        status, updated, payments = self._lifecycle(rng, n, placed, total)
        order = Order(oid, customer.customer_id, placed, status, CURRENCY, subtotal, discount, tax, shipping,
                      total, updated)
        return order, items, payments

    def _lifecycle(
        self, rng: random.Random, n: int, placed: datetime, total: Decimal
    ) -> tuple[str, datetime, list[PaymentAttempt]]:
        """Simulate the order + payment timeline and report the state as of clock_end."""
        oid = f"ord_{n}"
        method = _pick(rng, (("CARD", 70), ("PAYPAL", 15), ("WALLET", 10), ("BANK_TRANSFER", 5)))
        provider = _choice(rng, PROVIDERS)
        path = _pick(rng, (("fulfil", 95), ("payment_failed", 2), ("customer_cancel", 3)))
        failed = MAX_ATTEMPTS if path == "payment_failed" else _pick(rng, ((0, 85), (1, 12), (2, 3)))

        def secs(lo: int, hi: int) -> timedelta:
            return timedelta(seconds=_int(rng, lo, hi))

        # attempts: (created_at, [(state, at), ...]) in time order
        attempts: list[tuple[datetime, list[tuple[str, datetime]]]] = []
        t = placed + secs(5, 120)
        for _ in range(failed):
            fail_at = t + secs(10, 90)
            attempts.append((t, [("INITIATED", t), ("FAILED", fail_at)]))
            t = fail_at + secs(60, 600)
        stages = [("CREATED", placed)]
        if path == "payment_failed":
            stages.append(("CANCELLED", attempts[-1][1][-1][1] + secs(60, 1800)))
        else:
            auth = t + secs(5, 60)
            stages.append(("CONFIRMED", auth))
            if path == "customer_cancel":
                cancel = auth + secs(300, 43_200)
                stages.append(("CANCELLED", cancel))
                attempts.append((t, [("INITIATED", t), ("AUTHORIZED", auth), ("VOIDED", cancel)]))
            else:
                processing = auth + secs(600, 21_600)
                shipped = processing + secs(43_200, 259_200)
                delivered = shipped + secs(86_400, 518_400)
                stages += [("PROCESSING", processing), ("SHIPPED", shipped), ("DELIVERED", delivered)]
                settled = shipped + secs(86_400, 259_200)
                attempts.append((t, [("INITIATED", t), ("AUTHORIZED", auth), ("CAPTURED", shipped),
                                     ("SETTLED", settled)]))

        end = self.clock_end
        status, updated = [s for s in stages if s[1] <= end][-1]  # CREATED at placed < clock_end always holds
        payments = []
        for attempt_no, (created, states) in enumerate(attempts, 1):
            if created > end:
                break
            state, at = [s for s in states if s[1] <= end][-1]
            payments.append(PaymentAttempt(f"pay_{(n - 1) * MAX_ATTEMPTS + attempt_no}", oid, attempt_no, provider,
                                           method, state, total, CURRENCY, created, at))
        return status, updated, payments


def _probe(
    rng: random.Random, count: int, make: Callable[[int], T], ok: Callable[[T], bool], exclude: set[int]
) -> tuple[int, T] | None:
    """Uniform choice among eligible (number, entity) by rejection sampling. A linear scan after PROBE_TRIES
    misses guarantees termination; it only runs when eligible entities are rare, at O(count) cost."""
    for _ in range(PROBE_TRIES):
        n = 1 + int(rng.random() * count)
        if n not in exclude:
            e = make(n)
            if ok(e):
                return n, e
    return _scan(rng, count, make, ok, exclude)


def _scan(
    rng: random.Random, count: int, make: Callable[[int], T], ok: Callable[[T], bool], exclude: set[int]
) -> tuple[int, T] | None:
    start = int(rng.random() * count)
    for k in range(count):
        n = (start + k) % count + 1
        if n not in exclude:
            e = make(n)
            if ok(e):
                return n, e
    return None


# ---------- chunks ----------

@dataclass(frozen=True)
class Chunk:
    entity: str
    chunk_no: int
    first: int  # entity numbers first..last inclusive
    last: int
    tables: dict[str, list[tuple[object, ...]]]  # table name -> rows; orders also carry items + payments


def chunk_count(world: World, entity: str, chunk_rows: int) -> int:
    if chunk_rows < 1:
        raise GenerationError(f"chunk_rows must be >= 1, got {chunk_rows}")
    return -(-world.count(entity) // chunk_rows)


def generate_chunk(world: World, entity: str, chunk_no: int, chunk_rows: int) -> Chunk:
    """Rows for entity numbers [chunk_no*chunk_rows + 1, ...]; memory is bounded by chunk_rows."""
    total = world.count(entity)
    if not 0 <= chunk_no < chunk_count(world, entity, chunk_rows):
        raise GenerationError(f"{entity} chunk {chunk_no} out of range for {total} rows / {chunk_rows} per chunk")
    first = chunk_no * chunk_rows + 1
    last = min(total, first + chunk_rows - 1)
    ns = range(first, last + 1)
    tables: dict[str, list[tuple[object, ...]]]
    if entity == "categories":
        tables = {"categories": list(CATEGORIES[first - 1:last])}
    elif entity == "orders":
        tables = {"orders": [], "order_items": [], "payment_attempts": []}
        for n in ns:
            order, items, payments = world.order(n)
            tables["orders"].append(order)
            tables["order_items"].extend(items)
            tables["payment_attempts"].extend(payments)
    else:
        make: Callable[[int], tuple[object, ...]] = {
            "sellers": world.seller, "customers": world.customer, "products": world.product,
        }[entity]
        tables = {entity: [make(n) for n in ns]}
    return Chunk(entity, chunk_no, first, last, tables)


def generate_all(world: World, chunk_rows: int) -> Iterator[Chunk]:
    """Every chunk in FK-safe order, one at a time."""
    for entity in ENTITY_ORDER:
        for chunk_no in range(chunk_count(world, entity, chunk_rows)):
            yield generate_chunk(world, entity, chunk_no, chunk_rows)
