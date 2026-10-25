# Form filling: what the crawler does and where you help

All of this happens inside `appmap crawl`; you never fill forms yourself. Read this to interpret
results and to write good fixtures.

## Discovery

- Every `<form>` is recorded, plus **inferred forms**: clusters of inputs with a submit-like
  control and no `<form>` element (common in React/Vue apps). `forms.inferred = 1` marks them.
- Fields record name (or label), type, widget, constraints (`required`, `pattern`, `min`, `max`,
  `maxlength`), options, and ranked locators. The form records method (a hidden `_method`
  override wins), the endpoint its submission hit, and the submit control's locators.
- Before submitting, each form is classified like any other action. Forms that look destructive
  (delete, change password/email, pay, send/invite, deactivate, logout), forms that open a
  `confirm()` dialog, and — in production — every state-changing form are **held for approval**
  and not submitted (`forms.succeeded` stays NULL).

## Data generation

| Field | Generated value |
|---|---|
| free text / textarea / rich text | the run marker (`appmap-<run_id>`), trimmed to `maxlength` and fitted to simple `pattern`s |
| email | `<marker>@example.test` |
| phone / tel | `+15555550100` |
| URL | `https://example.test/<marker>` |
| password | `<marker>-Aa1!` |
| number / range | midpoint of `min`/`max`, else `min`, else `max`, else 1 |
| date, time, month, week, datetime-local | today, in the input's format |
| select / radio / combobox | first non-empty option |
| multi-select | first option |
| checkbox | checked |
| file | a small benign sample: `.txt`, `.png`, `.pdf` or `.csv` chosen from `accept` |
| colour | `#336699` |

Controlled inputs are filled with real keystroke events; date pickers, comboboxes/autocompletes,
multi-selects, rich-text editors and file inputs have dedicated handlers.

## Validation loop

1. Fill and submit.
2. Collect validation messages: HTML5 validity, inline error text near fields, alert/toast text,
   and JSON error bodies from 4xx responses (matched to fields by name or wording).
3. Turn messages into hints (`max:N`, `min:N`, `len:A-B`, `email`, `url`, `phone`, `number`,
   `pattern`, `required`) and retry with adjusted values, up to `limits.max_form_retries`
   (default 3). Retrying stops early when a retry would produce no new hint.
4. Record both failing and succeeding submissions: `forms.succeeded` (1/0), field
   `validation_messages`, and every exchange. The validation behaviour itself is useful to the
   test-generation consumer.

A form attempted once is not resubmitted by later crawl invocations of the same run.

## When to write fixtures

Write fixtures when the gaps or the failed-forms query show the **same fields failing for
domain reasons** the generator cannot guess:

- format rules not expressed as `pattern` (VAT/tax ids, IBANs, postcodes, SKUs, internal codes);
- values that must reference existing data (customer number, project key, existing username);
- business rules ("end date after start date", "quantity ≤ stock");
- a required checkbox/option combination the first option does not satisfy.

Do not write fixtures for failures caused by server errors (5xx → `observations`), CAPTCHAs,
or held actions. Never use real personal data; use clearly fake but valid values, or values the
operator gives you for this environment (e.g. a test customer id).

## fixtures.yaml format

```yaml
fields:
  "^(vat|tax)_?(id|number)$": "GB123456789"   # regex on field name or label, case-insensitive
  "postcode|postal|zip": "SW1A 1AA"
  "^iban$": "GB82WEST12345698765432"
  "customer.?(id|number)": "C-100042"          # operator-supplied existing test record
  "project.?name": "{marker} project"          # {marker} → the run marker
  "^quantity$": "1"
  "terms|consent": true
```

- Keys are Python regexes searched (not anchored unless you anchor them) in the field's name
  and label, case-insensitively; **the first matching key wins**, so put specific keys first.
- String values may contain `{marker}`; keep the marker in free-text values you add, so created
  records stay identifiable for cleanup.
- Wire it in with `form_data.fixtures: ./fixtures.yaml` (path relative to the config file). The
  config is re-read on every command, so the next `appmap crawl` uses it.

## Useful queries

```
appmap query --run <run> --format table sql "SELECT succeeded, count(*) FROM forms GROUP BY succeeded"
appmap query --run <run> forms-posting-to --endpoint '/api/%'
appmap query --run <run> sql "SELECT name, type, constraints, values_tried, validation_messages FROM fields WHERE form_id = 12"
```
