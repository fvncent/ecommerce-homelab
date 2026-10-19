"""Deterministic entity generation. Pure: no I/O, no DB. Rows are NamedTuples; load them by name
(`_fields` as the column list), never positionally.

Identity strategy:
- Entity N of a kind is `<prefix>_N` (N >= 1, no leading zeros) and is generated from its own RNG, seeded by
  sha256(seed:entity:N). Any row, and so any chunk, can be regenerated alone: output doesn't depend on
  chunk_rows, chunk order or what ran before (needed for resume).
- Child IDs use a fixed stride so they're computable without counting earlier orders:
  itm_{(order-1)*MAX_LINES + line_no}, pay_{(order-1)*MAX_ATTEMPTS + attempt_no}. IDs are sparse by design.
- FK-safe order: ENTITY_ORDER. Category parents always have a lower ID.

Simulation clock: `World.order(n, as_of)` is order N as it stands at time `as_of` (default clock_end): the same
draws, with only the events strictly before as_of applied (an event at exactly as_of belongs to what comes next).
Orders are placed in ID order, so the orders that exist at T are 1..orders_by(T). A clock step [previous, as_of)
inserts the orders placed in it and updates earlier ones that changed (`step_chunk`); stepping to clock_end in any
number of steps gives exactly the rows `generate` writes ([clock_start, clock_end)).

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
MASTER_ENTITIES = tuple(e for e in ENTITY_ORDER if e != "orders")  # static: all created before clock_start
MAX_LINES = 5
MAX_ATTEMPTS = 3
# Bump whenever generated rows or files change for the same inputs (the golden tests are keyed by it): resume
# refuses runs written under another data version, since regenerated chunks would no longer match committed ones.
# 2: file feeds (feeds.py) and World.clickstream_events; database rows are unchanged from 1.
# 3: browsing sessions start in time slots (feeds._session_start), so clickstream files differ; rows unchanged.
# 4: clock-step windows are [previous, as_of) (were (previous, as_of]): a record at exactly a step's as_of moves to
#    the next step. One-shot output is unchanged unless a record falls exactly on clock_end (none in the goldens).
DATA_VERSION = 4
PROBE_TRIES = 32
CURRENCY = "USD"  # MVP: single-currency marketplace; per-market currencies come later
MASTER_DATA_HISTORY = timedelta(days=730)  # master data is created in [clock_start - 730d, clock_start)
FREE_SHIPPING_FROM = Decimal("50.00")
SHIPPING_FEE = Decimal("5.99")
CENT = Decimal("0.01")
# Longest time an order keeps changing after it is placed: the slowest path in _lifecycle (first attempt after
# 120 s, two failed attempts of 90 + 600 s, authorization 60 s, processing 21,600 s, shipping 259,200 s, delivery
# 518,400 s; settlement is at most shipped + 259,200 s, so earlier). Every change of an order and its payments is at
# or before placed_at + LIFECYCLE_MAX, so as of any later time it is final and a clock step only revisits orders
# placed at or after previous - LIFECYCLE_MAX.
LIFECYCLE_MAX = timedelta(seconds=120 + 2 * (90 + 600) + 60 + 21_600 + 259_200 + 518_400)

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
    clickstream_events: int = 0  # browsing events in the clickstream feed (feeds.py), besides one per order

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
        if self.clickstream_events < 0:
            raise GenerationError("clickstream_events must be >= 0")

    @classmethod
    def from_config(cls, cfg: Config) -> "World":
        g = cfg.generator
        c = g.counts
        return cls(g.seed, g.clock_start, g.clock_end, c["sellers"], c["customers"], c["products"], c["orders"],
                   c["clickstream_events"])

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

    def _placed(self, rng: random.Random, n: int) -> datetime:
        """Order N sits in slot N of the window (integer seconds), so placed_at never decreases with N."""
        span = int((self.clock_end - self.clock_start).total_seconds())
        lo, hi = span * (n - 1) // self.orders, span * n // self.orders
        return self.clock_start + timedelta(seconds=lo + int(rng.random() * (hi - lo)))

    def placed_at(self, n: int) -> datetime:
        if not 1 <= n <= self.orders:
            raise GenerationError(f"order {n} out of range 1..{self.orders}")
        return self._placed(_rng(self.seed, "orders", n), n)

    def orders_by(self, t: datetime) -> int:
        """Number of orders placed before t (binary search over placed_at, which never decreases)."""
        lo, hi = 0, self.orders
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.placed_at(mid) < t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def order(self, n: int, as_of: datetime | None = None) -> tuple[Order, list[OrderItem], list[PaymentAttempt]]:
        """Order N, its items and its payment attempts as of `as_of` (default clock_end)."""
        return self.order_at(n, (self.clock_end if as_of is None else as_of,))[0]

    def order_at(self, n: int, ends: tuple[datetime, ...]) -> list[tuple[Order, list[OrderItem], list[PaymentAttempt]]]:
        """Order N as of each time in ends: generated once, its timeline cut at each end."""
        if not 1 <= n <= self.orders:
            raise GenerationError(f"order {n} out of range 1..{self.orders}")
        rng = _rng(self.seed, "orders", n)
        oid = f"ord_{n}"
        placed = self._placed(rng, n)
        for end in ends:
            if placed >= end:
                raise GenerationError(f"order {n} is placed at {placed.isoformat()}, not before {end.isoformat()}")

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

        timeline = self._lifecycle(rng, placed)
        out = []
        for end in ends:
            status, updated, payments = timeline.state_at(end, n, total)
            out.append((Order(oid, customer.customer_id, placed, status, CURRENCY, subtotal, discount, tax, shipping,
                              total, updated), items, payments))
        return out

    def _lifecycle(self, rng: random.Random, placed: datetime) -> "_Timeline":
        """Simulate the whole order + payment timeline (every draw, always); cut it at any time with state_at."""
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

        return _Timeline(method, provider, stages, attempts)


@dataclass(frozen=True)
class _Timeline:
    method: str
    provider: str
    stages: list[tuple[str, datetime]]  # order status changes, in time order
    attempts: list[tuple[datetime, list[tuple[str, datetime]]]]  # (created_at, [(state, at), ...]) in time order

    def state_at(self, end: datetime, n: int, total: Decimal) -> tuple[str, datetime, list[PaymentAttempt]]:
        """The state just before `end`: changes at exactly end are not applied yet."""
        status, updated = [s for s in self.stages if s[1] < end][-1]  # CREATED at placed < end always holds
        payments = []
        for attempt_no, (created, states) in enumerate(self.attempts, 1):
            if created >= end:
                break
            state, at = [s for s in states if s[1] < end][-1]
            payments.append(PaymentAttempt(f"pay_{(n - 1) * MAX_ATTEMPTS + attempt_no}", f"ord_{n}", attempt_no,
                                           self.provider, self.method, state, total, CURRENCY, created, at))
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


# ---------- clock steps ----------

def check_step(world: World, previous: datetime | None, as_of: datetime) -> None:
    """A step moves the clock forward within [clock_start, clock_end]."""
    if as_of.utcoffset() != timedelta(0):
        raise GenerationError(f"as_of must be a UTC timestamp, got {as_of!r}")
    if as_of.microsecond:  # every generated time is a whole second; feeds compare second-resolution text
        raise GenerationError(f"as_of must be a whole second, got {as_of.isoformat()}")
    if not world.clock_start <= as_of <= world.clock_end:
        raise GenerationError(f"as_of {as_of.isoformat()} is outside the simulation window "
                              f"{world.clock_start.isoformat()} .. {world.clock_end.isoformat()}")
    if previous is not None and as_of <= previous:
        raise GenerationError(f"as_of {as_of.isoformat()} is not after the previous step ({previous.isoformat()})")


@dataclass(frozen=True)
class StepPlan:
    """A step [previous, as_of) and the orders it writes or revisits: the ones placed before as_of that may differ
    from previous (placed at or after previous - LIFECYCLE_MAX). Orders 1..known were placed before previous."""
    previous: datetime | None
    as_of: datetime
    orders: range
    known: int


def plan_step(world: World, previous: datetime | None, as_of: datetime) -> StepPlan:
    check_step(world, previous, as_of)
    first = 1 if previous is None else world.orders_by(previous - LIFECYCLE_MAX) + 1
    known = 0 if previous is None else world.orders_by(previous)
    return StepPlan(previous, as_of, range(first, world.orders_by(as_of) + 1), known)


@dataclass(frozen=True)
class StepChunk:
    chunk_no: int
    first: int  # order numbers first..last inclusive
    last: int
    inserts: dict[str, list[tuple[object, ...]]]  # orders, order_items, payment_attempts (new rows)
    order_updates: list[tuple[Order, Order]]  # (as of previous, as of as_of): status/updated_at changed
    payment_updates: list[tuple[PaymentAttempt, PaymentAttempt]]  # likewise for state/updated_at


def step_chunk_count(plan: StepPlan, chunk_rows: int) -> int:
    if chunk_rows < 1:
        raise GenerationError(f"chunk_rows must be >= 1, got {chunk_rows}")
    return -(-len(plan.orders) // chunk_rows)


def step_chunk(world: World, plan: StepPlan, chunk_no: int, chunk_rows: int) -> StepChunk:
    """The changes from plan.previous (None: nothing yet) to plan.as_of for one chunk of plan.orders. Orders placed
    at or after previous are new; earlier ones are diffed against their state at previous. Items never change."""
    if not 0 <= chunk_no < step_chunk_count(plan, chunk_rows):
        raise GenerationError(f"step chunk {chunk_no} out of range for {len(plan.orders)} orders / {chunk_rows} "
                              "per chunk")
    sub = plan.orders[chunk_no * chunk_rows:(chunk_no + 1) * chunk_rows]
    inserts: dict[str, list[tuple[object, ...]]] = {"orders": [], "order_items": [], "payment_attempts": []}
    order_updates: list[tuple[Order, Order]] = []
    payment_updates: list[tuple[PaymentAttempt, PaymentAttempt]] = []
    for n in sub:
        if n > plan.known:
            order, items, payments = world.order(n, plan.as_of)
            inserts["orders"].append(order)
            inserts["order_items"].extend(items)
            inserts["payment_attempts"].extend(payments)
            continue
        assert plan.previous is not None
        (before, _, earlier), (order, _, payments) = world.order_at(n, (plan.previous, plan.as_of))
        if (before.status, before.updated_at) != (order.status, order.updated_at):
            order_updates.append((before, order))
        old = {p.attempt_no: p for p in earlier}
        for p in payments:
            o = old.get(p.attempt_no)
            if o is None:
                inserts["payment_attempts"].append(p)
            elif (o.state, o.updated_at) != (p.state, p.updated_at):
                payment_updates.append((o, p))
    return StepChunk(chunk_no, sub[0], sub[-1], inserts, order_updates, payment_updates)
