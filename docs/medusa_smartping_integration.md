# Medusa to SmartPing Integration: Abandoned Cart Events

This document provides the API specifications for integrating the Medusa backend with the ERP's SmartPing notification service for **Abandoned Cart** events. It is structured to help you or an LLM implement the integration effortlessly.

## Overview

The ERP uses a drip campaign strategy for abandoned carts. There are up to three progressive nudges. The **Recommended Approach** is to trigger each nudge individually from Medusa exactly when it is due.

- **Idempotency & Duplicate Prevention:** The API uses `business_event_ref` (e.g., the Medusa Cart ID, optionally with the nudge number) combined with the event details as an idempotency key. Sending the same `business_event_ref` multiple times will not schedule duplicate messages, ensuring safe retries.

---

## 1. Trigger a Single Event Nudge (Recommended)

In this approach, Medusa (or its worker) tracks the abandoned cart delays (e.g., 30 mins, 6 hours, 12 hours) and triggers each nudge precisely when it's time to send it.

**Endpoint:** 
`POST /api/v1/smartping/single-event/{event_key}/enqueue`

### Available Event Keys and Required Parameters

1. **`cart_abandonment_nudge_1`** (Send 30 mins after cart abandoned)
   - *Required Context*: `first_name`, `product_name`, `savings_amount`

2. **`cart_abandonment_nudge_2`** (Send 6 hrs after cart abandoned)
   - *Required Context*: `first_name`, `product_name`, `cart_value`, `prepaid_price`

3. **`cart_abandonment_nudge_3`** (Send 12 hrs after cart abandoned)
   - *Required Context*: `first_name`, `product_name`, `prepaid_price`

### Sample Request (For Nudge 1)

```http
POST /api/v1/smartping/single-event/cart_abandonment_nudge_1/enqueue
Content-Type: application/json
```

```json
{
  "business_event_ref": "cart_01HFWX..._nudge_1", 
  "destination": "+919535328180",                     
  "user_name": "Adnaan User",                            
  "context": {                                      
    "first_name": "Adnaan",
    "product_name": "Gausampurna 50kg Pack",
    "savings_amount": "500"
  },
  "max_retries": 3,
  "send_at": "2026-06-08T15:30:00Z"
}
```

> [!TIP]
> **Why use this?** This gives Medusa the maximum control over the exact timing of each message. If a customer converts after Nudge 1, Medusa simply does not trigger Nudge 2 and Nudge 3, avoiding the need for a separate cancellation flow in the ERP.

---

## 2. Trigger the Full Drip Sequence (Alternative)

Alternatively, you can schedule all three nudges at once. The ERP will handle the delays internally. (Note: If the user purchases later, you will need a mechanism in the ERP to cancel pending nudges).

**Endpoint:** 
`POST /api/v1/smartping/events/cart_abandoned/enqueue`

### Context Parameters Required
When triggering the full business event, the `context` object must contain all parameters used across all the nudges:
- `first_name`: Customer's first name
- `product_name`: Name of the abandoned product
- `savings_amount`: Amount the customer saves
- `cart_value`: Total value of the cart
- `prepaid_price`: Discounted price for prepaid orders

### Sample Request

```http
POST /api/v1/smartping/events/cart_abandoned/enqueue
Content-Type: application/json
```

```json
{
  "business_event_ref": "cart_01HFWX...", 
  "destination": "+919535328180",              
  "user_name": "Adnaan User",                      
  "context": {                                
    "first_name": "Adnaan",
    "product_name": "Gausampurna 50kg Pack",
    "savings_amount": "500",
    "cart_value": "4500",
    "prepaid_price": "4000"
  },
  "max_retries": 3,
  "send_at": "2026-06-08T15:30:00Z"
}
```

---

## Payload Schema Details

- **`business_event_ref`** *(String)*: **CRITICAL.** Unique identifier for this event to prevent duplicate messages. Usually, this is the `cart.id` from Medusa. For single nudges, it's highly recommended to append the nudge number (e.g., `cart_123_nudge_1`) to separate idempotency across nudges.
- **`destination`** *(String)*: Customer's WhatsApp number. Must include the country code (e.g., `+919535328180`).
- **`user_name`** *(String)*: Customer's full name.
- **`context`** *(Object)*: Template variables mapping to the specific event's requirements. These fields directly populate the WhatsApp template variables.
- **`max_retries`** *(Integer)*: Number of retry attempts if the provider (SmartPing) fails. Recommended value: `3`.
- **`send_at`** *(String, Optional)*: ISO 8601 datetime string. If provided, it overrides the database-configured delay and schedules the message(s) relative to this exact UTC time.
