// plans/032 phase 10 — graph fixtures generated from plugin_playbooks/v2/graph.py
// over tests/test_v2_checker.py EXAMPLE / QUEUE_EXAMPLE (kept in sync by hand).
import type { V2Graph } from '../v2/types'

export const EXAMPLE_CODE = "async def run(ctx, inputs):\n    rows = await ctx.tool(\"fetch_list\", url=inputs[\"url\"])\n    good = [r for r in rows[\"items\"] if r[\"score\"] > 3]\n    summaries = []\n    for r in good:\n        s = await ctx.llm(f\"Summarize {r['title']}\", output={\"s\": \"str\"})\n        summaries.append(s[\"s\"])\n    await ctx.approve(show=summaries)\n    await ctx.tool(\"send_message\", to=inputs[\"owner\"], text=\"\\n\".join(summaries))\n    return {\"count\": len(summaries)}\n"
export const QUEUE_CODE = "async def run(ctx, inputs):\n    queue = list(inputs[\"urls\"])\n    pages = []\n    failed = []\n    while queue:\n        url = queue.pop(0)\n        try:\n            page = await ctx.tool(\"fetch_page\", url=url, _retry=2)\n        except ctx.ToolError as e:\n            failed.append({\"url\": url, \"error\": str(e)})\n            continue\n        pages.append(page)\n        for link in page.get(\"links\", []):\n            if link not in queue and link not in [p[\"url\"] for p in pages]:\n                queue.append(link)\n    if not pages:\n        raise ValueError(\"nothing fetched: \" + \", \".join(f[\"url\"] for f in failed))\n    summaries = await ctx.gather(*[\n        ctx.llm(f\"Summarize {p['text']}\", output={\"s\": \"str\"}, _id=\"summary\")\n        for p in pages\n    ])\n    report = \"\\n\".join(s[\"s\"] for s in summaries)\n    await ctx.tool(\"send_message\", to=inputs[\"owner\"], text=report)\n    return {\"pages\": len(pages), \"failed\": failed}\n"

export const EXAMPLE_GRAPH: V2Graph = {
  "name": "pb",
  "version": 1,
  "format": "python",
  "triggers": [
    {
      "event": "manual"
    }
  ],
  "node_ids": [
    "trigger-0",
    "step-rows",
    "compute-for-s",
    "for-s",
    "step-s",
    "compute-end-for-s",
    "step-approve",
    "step-send_message",
    "compute-end-run"
  ],
  "root": {
    "id": "run",
    "items": [
      {
        "node": "step-rows",
        "kind": "tool",
        "call_site_id": "rows",
        "label": "rows",
        "sublabel": "fetch_list",
        "line": 2,
        "col": 17,
        "end_line": 2,
        "loop_depth": 0,
        "in_try": false
      },
      {
        "node": "compute-for-s",
        "kind": "compute",
        "label": "good = [r for r in rows[\"items\"] if r[\"…",
        "line": 3,
        "end_line": 4,
        "lines": 2
      },
      {
        "node": "for-s",
        "kind": "for",
        "label": "for r in good",
        "line": 5,
        "end_line": 7,
        "body": {
          "id": "for-s",
          "items": [
            {
              "node": "step-s",
              "kind": "llm",
              "call_site_id": "s",
              "label": "s",
              "sublabel": null,
              "line": 6,
              "col": 18,
              "end_line": 6,
              "loop_depth": 1,
              "in_try": false
            },
            {
              "node": "compute-end-for-s",
              "kind": "compute",
              "label": "summaries.append(s[\"s\"])",
              "line": 7,
              "end_line": 7,
              "lines": 1
            }
          ]
        }
      },
      {
        "node": "step-approve",
        "kind": "approve",
        "call_site_id": "approve",
        "label": "approve",
        "sublabel": null,
        "line": 8,
        "col": 10,
        "end_line": 8,
        "loop_depth": 0,
        "in_try": false
      },
      {
        "node": "step-send_message",
        "kind": "tool",
        "call_site_id": "send_message",
        "label": "send_message",
        "sublabel": "send_message",
        "line": 9,
        "col": 10,
        "end_line": 9,
        "loop_depth": 0,
        "in_try": false
      },
      {
        "node": "compute-end-run",
        "kind": "compute",
        "label": "return {\"count\": len(summaries)}",
        "line": 10,
        "end_line": 10,
        "lines": 1
      }
    ]
  }
} as V2Graph

export const QUEUE_GRAPH: V2Graph = {
  "name": "q",
  "version": 1,
  "format": "python",
  "triggers": [
    {
      "event": "manual"
    }
  ],
  "node_ids": [
    "trigger-0",
    "compute-while-page",
    "while-page",
    "compute-try-page",
    "try-page",
    "step-page",
    "compute-end-try-page-except-1",
    "compute-end-while-page",
    "compute-gather-summary",
    "gather-summary",
    "step-summary",
    "compute-step-send_message",
    "step-send_message",
    "compute-end-run"
  ],
  "root": {
    "id": "run",
    "items": [
      {
        "node": "compute-while-page",
        "kind": "compute",
        "label": "queue = list(inputs[\"urls\"])",
        "line": 2,
        "end_line": 4,
        "lines": 3
      },
      {
        "node": "while-page",
        "kind": "while",
        "label": "while queue",
        "line": 5,
        "end_line": 15,
        "body": {
          "id": "while-page",
          "items": [
            {
              "node": "compute-try-page",
              "kind": "compute",
              "label": "url = queue.pop(0)",
              "line": 6,
              "end_line": 6,
              "lines": 1
            },
            {
              "node": "try-page",
              "kind": "error_boundary",
              "label": "try",
              "line": 7,
              "end_line": 11,
              "body": {
                "id": "try-page",
                "items": [
                  {
                    "node": "step-page",
                    "kind": "tool",
                    "call_site_id": "page",
                    "label": "page",
                    "sublabel": "fetch_page",
                    "line": 8,
                    "col": 25,
                    "end_line": 8,
                    "loop_depth": 1,
                    "in_try": true
                  }
                ]
              },
              "handlers": [
                {
                  "label": "except ctx.ToolError",
                  "line": 9,
                  "end_line": 11,
                  "body": {
                    "id": "try-page-except-1",
                    "items": [
                      {
                        "node": "compute-end-try-page-except-1",
                        "kind": "compute",
                        "label": "failed.append({\"url\": url, \"error\": str…",
                        "line": 10,
                        "end_line": 11,
                        "lines": 2
                      }
                    ]
                  }
                }
              ],
              "finally": null
            },
            {
              "node": "compute-end-while-page",
              "kind": "compute",
              "label": "pages.append(page)",
              "line": 12,
              "end_line": 15,
              "lines": 4
            }
          ]
        }
      },
      {
        "node": "compute-gather-summary",
        "kind": "compute",
        "label": "if not pages:",
        "line": 16,
        "end_line": 17,
        "lines": 2
      },
      {
        "node": "gather-summary",
        "kind": "gather",
        "call_site_id": null,
        "label": "gather (1)",
        "sublabel": null,
        "line": 18,
        "col": 22,
        "end_line": 21,
        "loop_depth": 1,
        "in_try": false,
        "args": [
          {
            "node": "step-summary",
            "kind": "llm",
            "call_site_id": "summary",
            "label": "summary",
            "sublabel": null,
            "line": 19,
            "col": 8,
            "end_line": 19,
            "loop_depth": 1,
            "in_try": false
          }
        ]
      },
      {
        "node": "compute-step-send_message",
        "kind": "compute",
        "label": "report = \"\\n\".join(s[\"s\"] for s in summ…",
        "line": 22,
        "end_line": 22,
        "lines": 1
      },
      {
        "node": "step-send_message",
        "kind": "tool",
        "call_site_id": "send_message",
        "label": "send_message",
        "sublabel": "send_message",
        "line": 23,
        "col": 10,
        "end_line": 23,
        "loop_depth": 0,
        "in_try": false
      },
      {
        "node": "compute-end-run",
        "kind": "compute",
        "label": "return {\"pages\": len(pages), \"failed\": …",
        "line": 24,
        "end_line": 24,
        "lines": 1
      }
    ]
  }
} as V2Graph
