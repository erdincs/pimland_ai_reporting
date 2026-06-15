# API Bug Report — productStory Filter Non-Functional

**Date:** 2026-06-15  
**Severity:** High  
**Component:** Pimland MCP API — POST /api/Product/get_products_by_filter  
**Environment:** agentup-mcp-test.pimland.com:30001  
**Reporter:** ADL Integration / Pimland Reporting Team  

---

## Summary

The `productStory` parameter in `POST /api/Product/get_products_by_filter` does not filter
products by story. The endpoint either returns the entire product catalog (~4,281 products)
regardless of the story ID supplied, or returns 0 products — never the correct story-specific
subset.

---

## Steps to Reproduce

**Auth — obtain Bearer token:**
```http
POST https://ids.pimland.com/connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=password
&client_id=<client_id>
&client_secret=<client_secret>
&username=adl_integration
&password=<password>
&scope=PimlandAPI.read PimlandAPI.write openid offline_access
```

**Filter request (repeat for each story ID below):**
```http
POST https://agentup-mcp-test.pimland.com/30001/tools/post_api_Product_get_products_by_filter/execute
Authorization: Bearer <token>
Content-Type: application/json

{
  "productStory": [<story_id>],
  "pageSize": 5,
  "pageNumber": 1
}
```

---

## Test Results

All tests performed on 2026-06-15. Story IDs obtained from
`POST /api/MasterData/product_stories/get_product_stories` (working correctly, returns 255 stories).

| Story Code | Story ID | Expected Result | Actual Result | Response Time |
|------------|----------|-----------------|---------------|---------------|
| AC01       | 4        | Story-specific products | **4,281 products (full catalog)** | ~30 seconds |
| BC02       | 8        | Story-specific products | **4,281 products (full catalog)** | ~30 seconds |
| B01        | 15       | Story-specific products | **4,281 products (full catalog)** | ~30 seconds |
| W16-2      | 90       | Story-specific products | **0 products** | <1 second |

**Key observation — same first product for all story IDs when non-zero result:**
```json
{
  "totalResultCount": 4281,
  "currentPage": 1,
  "currentResultCount": 5,
  "products": [
    { "stockCode": "12746471001", ... }
  ]
}
```
The identical `totalResultCount` (4,281) and identical first `stockCode` across three
unrelated story IDs proves the filter parameter is being ignored server-side.

---

## Expected vs Actual Behavior

| Behavior | Expected | Actual |
|----------|----------|--------|
| Result set | Only products assigned to the specified story | Either all 4,281 products or 0 |
| `totalResultCount` | Story-specific count (e.g. 85 for W16-2) | Always 4,281 or always 0 |
| Response time | Similar to other filter parameters (<2s) | ~30 seconds when non-zero |

---

## Other Filter Parameters (Working Correctly for Comparison)

The following parameters in the same endpoint work as expected:

| Parameter | Behavior |
|-----------|----------|
| `stockCode` | Filters correctly by stock code |
| `season`    | Filters correctly by season code |
| `brandId`   | Filters correctly by brand |

Only `productStory` is broken. `productTheme` was not tested.

---

## Impact

1. **Product-Story sync is impossible** — any integration that maps stories to products
   via this endpoint will either import wrong data (full catalog) or no data (0 results).

2. **Affected integration:** We attempted a batch sync to populate story assignments for
   4,140 products across 255 stories. Every story returned either full catalog or 0 products,
   making the sync result unreliable. We have since reverted all incorrect assignments.

3. **Reporting gap:** Story-based sales analytics (`hikaye analizi`) cannot be generated
   until this is resolved.

---

## Suspected Root Cause

The server-side query builder for `get_products_by_filter` appears to discard
the `productStory` condition when building the SQL/ORM query, falling through to
an unfiltered result set. The ~30 second response time (vs <1 second for `stockCode`)
suggests a full-table scan without an applied WHERE clause.

---

## Suggested Fix

Verify that the `productStory` parameter is:
1. Parsed from the request body as an integer array
2. Applied as a WHERE/JOIN condition (e.g. `WHERE product.storyId IN (:storyIds)`)
3. Indexed on the `productStory` FK column for performance

---

## Workaround (None Available)

There is no client-side workaround. The `product_stories/get_product_stories` master
endpoint works correctly and returns all 255 stories with their IDs and reference codes,
but without a working product filter, it is not possible to resolve the story → product
mapping from the API side.

---

## Additional Context

- Total product catalog size: **4,140 products** (our local DB) / **4,281** (API response)
- Total story count: **255**
- MCP tool name: `post_api_Product_get_products_by_filter`
- MCP server: `agentup-mcp-test.pimland.com:30001`
- Auth server: `ids.pimland.com`

---

*Report prepared by Pimland Reporting integration team.*
*Contact: erdinc.sezer@upagon.com*
