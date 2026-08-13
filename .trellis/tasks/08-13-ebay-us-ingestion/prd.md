# Add eBay US ingestion skill

## Goal

Add a dedicated Part 1 Agent Skill for sourcing products from eBay's United
States marketplace and persisting every selection result through the existing
Aurora ingestion tools.

## Requirements

- Add an independently discoverable `product-selection-ebay-us` Skill under
  `part1_data_ingestion/agent/skills/`.
- Route explicit eBay US / United States / `ebay.com` requests to the new
  Skill without changing the existing default eBay UK behavior.
- Search eBay US through `https://www.ebay.com`, prefer Buy It Now listings,
  skip sponsored entries, and retain the existing handling for used or
  refurbished listings.
- Preserve the existing eBay selection flow, hard gates, estimation rules,
  verdict vocabulary, and immediate persistence behavior.
- Use US marketplace values consistently: USD prices, `site=US`, a fixed
  USD-to-CNY demo exchange rate of 7, and US-oriented compliance guidance.
- Continue to call `save_verdict` for every evaluated product and additionally
  call `save_candidate` for every `采集` result.
- Document an eBay US invocation example and list the new Skill in the Part 1
  documentation.
- Do not change database schemas, ingestion tool signatures, or the existing
  eBay UK and Amazon Skills.

## Acceptance Criteria

- [x] `product-selection-ebay-us/SKILL.md` has unique metadata that clearly
      matches explicit eBay US requests.
- [x] The Skill's search URLs, currency labels, thresholds, calculations,
      certification guidance, and persistence instructions are internally
      consistent with the US marketplace.
- [x] Both `save_verdict` and `save_candidate` instructions explicitly require
      `site=US`.
- [x] The existing UK Skill remains the default for requests that do not name
      a marketplace.
- [x] Part 1 documentation contains a copy-pasteable eBay US search-and-ingest
      example.
- [x] Static validation confirms all Skill metadata blocks are parseable and
      the new Skill contains no UK marketplace URL, GBP symbol, or `site=UK`
      instruction.

## Notes

- This is a lightweight, documentation-driven Agent Skill addition. The
  runtime already discovers every `skills/*/SKILL.md`, so no runtime code or
  infrastructure change is expected.
