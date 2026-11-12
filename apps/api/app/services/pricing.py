"""Model prices, for estimating the cost of usage. Configuration, not business logic.

Prices are per million tokens, in one currency, under a version label that is stored with
every usage record, so a record can always be traced to the prices it was costed with.
Replace the whole table with USAGE_PRICING_JSON; nothing else needs to change.

The default table (`DEFAULT_PRICING` in app/core/config.py, with all other configuration)
holds OpenAI's published list prices for the models this application uses, as known when it
was written. They are estimates: they ignore discounts, cached input, taxes and any price
change since, and are not an invoice. Check them against the provider's current pricing
before relying on them.
"""

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache

from app.core.config import DEFAULT_PRICING, get_settings

MILLION = Decimal(1_000_000)
COST_QUANTUM = Decimal("0.00000001")  # matches NUMERIC(16, 8)

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")


class PricingError(ValueError):
    """The price table is malformed. Raised at startup, never mid-request."""


@dataclass(frozen=True, slots=True)
class Price:
    input_per_million: Decimal
    output_per_million: Decimal


@dataclass(frozen=True, slots=True)
class Pricing:
    version: str
    currency: str
    models: dict[str, Price]

    def price_for(self, model: str | None) -> Price | None:
        """The price of `model`, matched on the longest configured name it starts with, so a
        dated snapshot ("<model>-2025-04-14") uses its family's price. None if unpriced."""
        if not model:
            return None
        best = max((name for name in self.models if model.startswith(name)), key=len, default=None)
        return self.models[best] if best is not None else None

    def cost(self, model: str | None, input_tokens: int, output_tokens: int) -> Decimal | None:
        """Estimated cost, or None when the model has no configured price."""
        price = self.price_for(model)
        if price is None:
            return None
        raw = (
            Decimal(input_tokens) * price.input_per_million
            + Decimal(output_tokens) * price.output_per_million
        ) / MILLION
        return raw.quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal(value: object, where: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PricingError(f"{where} is not a number") from error
    if not number.is_finite() or number < 0:
        raise PricingError(f"{where} must be a non-negative number")
    return number


def parse_pricing(data: object) -> Pricing:
    if not isinstance(data, dict):
        raise PricingError("pricing must be a JSON object")
    version, currency, models = data.get("version"), data.get("currency"), data.get("models")
    if not isinstance(version, str) or not 1 <= len(version) <= 64:
        raise PricingError("pricing version must be a string of 1 to 64 characters")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise PricingError("pricing currency must be a three-letter code such as USD")
    if not isinstance(models, dict) or not models:
        raise PricingError("pricing must list at least one model")
    prices: dict[str, Price] = {}
    for name, entry in models.items():
        if not isinstance(name, str) or not MODEL_NAME.match(name):
            raise PricingError(f"invalid model name in pricing: {name!r}")
        if not isinstance(entry, dict):
            raise PricingError(f"pricing for {name} must be an object")
        prices[name] = Price(
            _decimal(entry.get("input_per_million"), f"{name} input_per_million"),
            _decimal(entry.get("output_per_million", 0), f"{name} output_per_million"),
        )
    return Pricing(version, currency, prices)


@lru_cache
def get_pricing() -> Pricing:
    configured = get_settings().usage_pricing_json.strip()
    if not configured:
        return parse_pricing(DEFAULT_PRICING)
    try:
        data = json.loads(configured)
    except json.JSONDecodeError as error:
        raise PricingError("USAGE_PRICING_JSON is not valid JSON") from error
    return parse_pricing(data)
