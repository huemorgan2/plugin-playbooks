"""plans/014 QA driver — real server, real LLM, ground truth via API + DB.

Run from the luna checkout with PYTHONPATH=scripts and the same
LUNA_DATABASE_URL the QA server uses (qa_drive helpers write User.setup
directly).
"""

import asyncio
import json
import os
import sys

import httpx
import qa_drive
from qa_drive import _complete_onboarding_in_db, new_conversation, send, signup


async def _safe_pending(c, h):
    # /api/approvals returns a non-JSON body on this build; never let the
    # auto-approver take down the turn.
    try:
        r = await c.get("/api/approvals?status=pending", headers=h)
        data = r.json() if r.status_code == 200 else []
        return data if isinstance(data, list) else data.get("approvals", [])
    except Exception:
        return []


qa_drive.pending = _safe_pending

BASE = "http://127.0.0.1:8767"
PB = "qa-014-failing"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{' — ' + str(detail)[:300] if detail else ''}")


async def db_scalar(sql):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(os.environ["LUNA_DATABASE_URL"])
    async with eng.connect() as conn:
        val = (await conn.execute(text(sql))).scalar()
    await eng.dispose()
    return val


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=180.0) as c:
        # Run 1 already signed up (server is single-user → 409 on re-signup).
        try:
            token = await signup(c)
        except httpx.HTTPStatusError:
            r = await c.post("/api/auth/login",
                             json={"username": "qa1787928990",
                                   "password": "qatestpw1234"})
            r.raise_for_status()
            token = r.json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        # Core no longer exposes PUT /api/identity (405); the part of
        # force_setup that matters is the direct DB onboarding-complete write.
        await _complete_onboarding_in_db("Luna")
        print("owner ready")

        # --- failing playbook: template raises at render → run fails ---
        yaml = "\n".join([
            f"name: {PB}",
            "display_name: QA 014 failing",
            "description: QA playbook for plan 014 (safe to delete)",
            "when_to_use: never — QA only",
            "steps:",
            "  - id: boom",
            "    kind: tool_call",
            "    tool: send_chat_message",
            "    args:",
            "      message: \"{{ 1 / 0 }}\"",
        ])
        r = await c.post("/api/p/plugin-playbooks/playbooks", headers=h,
                         json={"name": PB, "definition_yaml": yaml})
        check("create failing playbook",
              r.status_code in (200, 201) or "exist" in r.text.lower()
              or r.status_code == 409, r.text)

        run_ids = []
        for i in range(2):
            r = await c.post(f"/api/p/plugin-playbooks/playbooks/{PB}/runs",
                             headers=h, json={"inputs": {}, "trigger": "qa"})
            run_ids.append(r.json()["run_id"])
        statuses = {}
        for _ in range(30):
            for rid in run_ids:
                r = await c.get(f"/api/p/plugin-playbooks/playbooks/runs/{rid}", headers=h)
                statuses[rid] = r.json().get("status")
            if all(s in ("failed", "done", "cancelled") for s in statuses.values()):
                break
            await asyncio.sleep(1)
        check("both runs FAILED", all(s == "failed" for s in statuses.values()),
              json.dumps(statuses))

        # --- turn 1: neutral opener — agent must surface the failures ---
        conv = await new_conversation(c, h)
        prose1 = await send(c, h, conv, "hi — anything I should know about?")
        print("\n--- turn 1 prose ---\n", prose1, "\n")
        mentions = ("qa-014-failing" in prose1) or ("QA 014 failing" in prose1) or (
            "fail" in prose1.lower() and "playbook" in prose1.lower())
        check("turn 1 surfaces the failing playbook", mentions, prose1[:200])

        # --- turn 2: owner says ignore → agent should ack ---
        prose2 = await send(
            c, h, conv,
            "ah that one is a known issue, just ignore it / dismiss the notice.")
        print("\n--- turn 2 prose ---\n", prose2, "\n")
        acked = await db_scalar(
            f"select failures_acked_version from playbooks where name = '{PB}'")
        check("ack recorded in DB (failures_acked_version == 1)", acked == 1, acked)

        # --- turn 3: fresh conversation must be quiet about it ---
        conv2 = await new_conversation(c, h)
        prose3 = await send(c, h, conv2, "quick check: how are my playbooks doing?")
        print("\n--- turn 3 prose ---\n", prose3, "\n")
        check("turn 3 does not re-raise the dismissed failure",
              "needing your attention" not in prose3
              and "dojo" not in prose3.lower()
              and not ("fail" in prose3.lower() and PB in prose3),
              prose3[:200])

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


asyncio.run(main())
