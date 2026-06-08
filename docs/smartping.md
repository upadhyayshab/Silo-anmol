# Smartping API Reference Docs

## Overview
This document outlines the API reference for sending automated WhatsApp messages via Smartping's WhatsApp Business API campaign integration.

## HTTP Request
**Method:** `POST`  
**Endpoint:** `https://backend.api-wa.co/campaign/smartping/api/v2`

## Prerequisites
Before using this API, ensure the following conditions are met:
* You have a verified WhatsApp Business API.
* You have approved template messages.
* You have already created a Live API Campaign.

## Campaign Mapping Rule
* The backend should resolve `campaignName` from the SmartPing campaign registry table, not from hardcoded Python constants.
* `templateParams` must be sent in the exact placeholder order defined by the SmartPing template.
* For the current abandoned-cart live message, the param order is `first_name`, `product_name`, `savings_amount`.

## Request Payload Schema
The request body must be a JSON object containing the following fields:

### Required Fields
| Field | Type | Description |
| :--- | :--- | :--- |
| `apiKey` | string | API key generated from the dashboard. |
| `campaignName` | string | Name of the campaign to send. Status must be 'Live'. |
| `destination` | string | Mobile number of the user with country dial-code (e.g., +91742XXXX805). Defaults to India (+91) if unresolved. |
| `userName` | string | Name of the user to whom the campaign is sent. |

### Optional Fields
| Field | Type | Description |
| :--- | :--- | :--- |
| `source` | string | Source of the lead for segmentation (e.g., 'Facebook forms', 'Website lead'). |
| `media` | object | Contains `url` and `filename` of the media to be sent with the template. |
| `templateParams` | array of strings | Values to fill in a template message's placeholders. |
| `tags` | array of strings | Tag names to assign to the user. |
| `attributes` | object | Key-value pairs (strings only) to set as user attributes. |

### JSON Representation Example
```
{
  "apiKey": "your_api_key_here",
  "campaignName": "your_campaign_name",
  "destination": "+91742XXXX805",
  "userName": "John Doe",
  "source": "Website lead",
  "media": {
    "url": "[https://example.com/media.pdf](https://example.com/media.pdf)",
    "filename": "document.pdf"
  },
  "templateParams": ["param1", "param2"],
  "tags": ["tag1", "tag2"],
  "attributes": {
    "attribute_name": "attribute_value"
  }
}
```
