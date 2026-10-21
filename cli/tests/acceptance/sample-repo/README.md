# invoices

A tiny invoicing library. Amounts are whole cents.

## Usage

```python
from invoices.money import format_cents, total

format_cents(total([1050, 250]))  # '13.00'
```

## Tests

```
python -m pytest
```
