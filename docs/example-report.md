<!-- dbt-impact -->
## dbt impact analysis

**3 breaking reference(s)** and **2 exposure(s) at risk**.

| Models | |
|---|---|
| Modified | `stg_customers`, `stg_orders` |

### Breaking

| Downstream model | Reads removed column | Caused by |
|---|---|---|
| `customers` | `stg_customers.first_name` | `stg_customers` |
| `customer_contacts` | `stg_customers.email` | `stg_customers` |
| `customers` | `orders.status` | `stg_orders` |

### Exposures at risk

| Exposure | Type | Owner | Depends on | Why | Columns |
|---|---|---|---|---|---|
| `crm_sync` | application | CRM team | `customer_contacts` | upstream model breaks | `email` |
| `revenue_dashboard` | dashboard | Analytics | `customers`, `orders` | upstream model breaks | `first_name`, `status` |

### Propagated through `select *`

These models don't name the column, but it disappears from their output:

- `orders` loses `status` (from `stg_orders`)

<details><summary>Column changes</summary>

| Model | Column | Change |
|---|---|---|
| `orders` | `status` | removed |
| `stg_customers` | `first_name` | removed |
| `stg_customers` | `email` | removed |
| `stg_customers` | `given_name` | added |
| `stg_orders` | `status` | removed |

</details>

> Looks like `stg_customers.first_name` was renamed to `given_name`. Downstream models still using `first_name` will fail.
