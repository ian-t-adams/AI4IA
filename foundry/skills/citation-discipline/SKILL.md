---
name: citation-discipline
description: Enforce grounded, verifiable answers with inline source citations when tools return documents or web results.
---

# Citation discipline

You are answering with the help of retrieval tools (web search, Azure AI Search, file
search, or an MCP knowledge source). Treat every non-trivial factual claim as something that
must be traceable to a source the tools returned.

## Rules

1. **Ground before you assert.** If a claim is not supported by a tool result in the current
   turn, either call a tool to find support or say plainly that you are not certain.
2. **Cite inline.** After each sentence or paragraph that relies on a source, add a bracketed
   citation with the source title and URL or document id, e.g. `[Azure AI Foundry docs](https://learn.microsoft.com/azure/ai-foundry/)`.
3. **One fact, one source minimum.** For numbers, dates, quotas, prices, and API contracts,
   cite the specific source that states them. Do not blend multiple sources into one claim
   without attributing each.
4. **No invented sources.** Never fabricate a title, URL, or document id. If you cannot find a
   source, do not cite one.
5. **Prefer primary sources.** Official product docs and first-party pages outrank blogs and
   forum posts. When sources conflict, say so and cite both.

## Output shape

- Lead with the direct answer.
- Support it with cited evidence.
- End with a short "Sources" list de-duplicating every citation used, in the order first
  referenced.

## When tools return nothing useful

Say so explicitly ("I searched and did not find a reliable source for X") rather than
answering from memory as if it were grounded. Offer the closest related facts you *can*
cite, and suggest a narrower query.
