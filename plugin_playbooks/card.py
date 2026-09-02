"""Delegation progress card — plans/013 phase 2, refreshed by plans/020 phase 3.

`render_delegation_card(...)` builds ONE self-contained HTML document
(inline CSS + JS) posted into the chat via ``ctx.post_chat_card``. The chat
UI renders it in a sandboxed srcdoc iframe (opaque origin, scripts allowed);
the document polls the capability-token status route and re-renders itself.
Height is auto-reported with ``postMessage({type:"luna:embed:height"})``.

Layout per vision/ux_guidelines.md: eyebrow → bottom-line headline →
support line → phase rows with status dots → LIVE activity feed (open by
default — the owner watches the delegate work) → result. Dark tokens, no
gradient (this is a status card, not a hero), owner words only — the phase
labels come from the server's fixed vocabulary.

plans/020 phase 3 additions, built on the richer phase-2 events:
- the feed is open by default and auto-scrolls to the newest entry
  (respecting a user who scrolled up or closed it);
- every tool row carries a verdict tick (✓ / ✗ from the event's ``ok``) or
  a pulsing dot while in flight, plus a faint one-value args hint;
- a steps counter (used / budget) ticks in the header while running;
- terminal states color a thin top border as well as the headline.
"""

from __future__ import annotations

import html
import json

_POLL_MS = 1500
_PHASES = ["Understand", "Change", "Prove", "Ship"]
# Mirror of delegation._MAX_TURNS (card.py can't import delegation — the
# import runs the other way). Pinned together by a drift test.
_MAX_STEPS = 40


def render_delegation_card(
    delegation_id: str, token: str, playbook: str, version: str
) -> str:
    # <-escape: a "</script>" inside the JSON (hostile playbook name)
    # would terminate the script element — json.dumps alone does not prevent
    # that.
    boot = json.dumps({
        "id": str(delegation_id),
        "token": token,
        "playbook": playbook,
        "phases": _PHASES,
        "pollMs": _POLL_MS,
        "maxSteps": _MAX_STEPS,
    }).replace("<", "\\u003c").replace(">", "\\u003e")
    chip = (
        f'<span class="chip">{html.escape(playbook)}</span>' if playbook else ""
    )
    # v={version} exists only as a cache-bust marker in the comment below —
    # srcdoc documents are not cached, but the stamp makes stale-card
    # debugging on live tenants trivial (which build drew this card?).
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- plugin-playbooks delegation card v={html.escape(version)} -->
<style>
:root{{
  --bg:#0b0e14; --panel:#11151f; --panel-2:#161b28; --line:#232a3a;
  --text:#e6e9f2; --dim:#8b93a7; --faint:#5b6275;
  --violet:#8b5cf6; --ok:#3ad29f; --amber:#f5a524; --red:#f4645f;
  --mono:"SF Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent}}
body{{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:var(--text);font-size:14px;line-height:1.45;
  -webkit-font-smoothing:antialiased}}
.card{{background:var(--panel);border:1px solid var(--line);
  border-top:2px solid var(--line);
  border-radius:12px;padding:16px 18px;max-width:560px}}
.card.done{{border-top-color:var(--ok)}}
.card.failed{{border-top-color:var(--red)}}
.card.needs_owner{{border-top-color:var(--amber)}}
.top{{display:flex;align-items:baseline;justify-content:space-between;gap:12px}}
.eyebrow{{font-size:11px;font-weight:600;letter-spacing:.12em;
  text-transform:uppercase;color:var(--violet);display:flex;
  align-items:center;gap:8px;min-width:0}}
.chip{{font-size:11px;font-weight:500;letter-spacing:0;text-transform:none;
  color:var(--dim);background:var(--panel-2);border:1px solid var(--line);
  border-radius:6px;padding:1px 7px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}}
.meta{{display:flex;align-items:baseline;gap:10px;white-space:nowrap}}
.steps{{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}}
.elapsed{{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}}
h1{{font-size:16px;font-weight:600;margin-top:10px;letter-spacing:-.01em}}
.support{{color:var(--dim);font-size:13px;margin-top:3px;min-height:19px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.phases{{margin-top:14px;display:flex;flex-direction:column;gap:7px}}
.phase{{display:flex;align-items:center;gap:10px;font-size:13px;
  color:var(--faint)}}
.phase.current{{color:var(--text)}}
.phase.done{{color:var(--dim)}}
.dot{{width:7px;height:7px;border-radius:50%;flex:0 0 auto;
  border:1.5px solid var(--faint);background:transparent}}
.phase.current .dot{{border-color:var(--amber);background:var(--amber)}}
.phase.done .dot{{border-color:var(--ok);background:var(--ok)}}
.phase.bad .dot{{border-color:var(--red);background:var(--red)}}
.phase .n{{margin-left:auto;font-size:12px;color:var(--faint);
  font-variant-numeric:tabular-nums}}
@media (prefers-reduced-motion:no-preference){{
  .phase.current .dot{{animation:pulse 1.6s ease-in-out infinite}}
  .ev .tick.run{{animation:pulse 1.2s ease-in-out infinite}}
  @keyframes pulse{{50%{{opacity:.35}}}}
}}
details{{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}}
summary{{cursor:pointer;color:var(--faint);font-size:12px;list-style:none;
  user-select:none}}
summary::before{{content:"▸ "}}
details[open] summary::before{{content:"▾ "}}
.feed{{margin-top:8px;display:flex;flex-direction:column;gap:4px;
  max-height:260px;overflow-y:auto;font-size:12px;
  scrollbar-width:thin;scrollbar-color:var(--line) transparent}}
.ev{{display:flex;gap:8px;align-items:baseline;min-width:0}}
.ev .tick{{flex:0 0 12px;text-align:center;font-size:11px;
  color:var(--faint)}}
.ev .tick.ok{{color:var(--ok)}}
.ev .tick.err{{color:var(--red)}}
.ev .tick.run{{color:var(--amber)}}
.ev .lbl{{color:var(--dim);font-family:var(--mono);font-size:11.5px;
  white-space:nowrap;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis}}
.ev .hint{{color:var(--faint);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;flex:0 1 auto;min-width:0}}
.ev.thought .lbl{{color:var(--faint);font-style:italic;
  font-family:inherit;font-size:12px}}
.ev .ms{{margin-left:auto;color:var(--faint);
  font-variant-numeric:tabular-nums;white-space:nowrap;flex:0 0 auto}}
.waiting{{display:none;margin-top:12px;padding:8px 12px;font-size:13px;
  color:var(--amber);background:rgba(245,165,36,.08);
  border:1px solid rgba(245,165,36,.35);border-left-width:3px;
  border-radius:8px}}
.result{{margin-top:12px;padding:10px 12px;background:var(--panel-2);
  border:1px solid var(--line);border-radius:8px;font-size:13px;
  color:var(--dim);white-space:pre-wrap;display:none}}
.card.done h1{{color:var(--ok)}}
.card.failed h1{{color:var(--red)}}
.card.needs_owner h1{{color:var(--amber)}}
</style></head>
<body>
<div class="card" id="card">
  <div class="top">
    <div class="eyebrow">Playbook agent {chip}</div>
    <div class="meta">
      <span class="steps" id="steps"></span>
      <span class="elapsed" id="elapsed"></span>
    </div>
  </div>
  <h1 id="headline">Starting…</h1>
  <div class="support" id="support">Handing the job to the delegate</div>
  <div class="phases" id="phases"></div>
  <div class="waiting" id="waiting"></div>
  <details id="detail" open><summary id="detailsum">Activity</summary>
    <div class="feed" id="feed"></div>
  </details>
  <div class="result" id="result"></div>
</div>
<script>
(function(){{
var BOOT={boot};
// Hosted tenants live under /a/{{slug}} behind the cloud proxy; a
// root-relative fetch would leave the tenant entirely (plans/014). The
// sandboxed iframe can't reach window.parent (opaque origin), but srcdoc
// documents inherit the parent page's base URL, so parse the prefix out of
// document.baseURI. Self-hosted/QA has no prefix -> ''.
var API_BASE=(function(){{
  try{{if(window.parent&&window.parent.__LUNA_BASE)return window.parent.__LUNA_BASE;}}catch(e){{}}
  var m=(document.baseURI||'').match(/^[a-z]+:\\/\\/[^\\/]+(\\/a\\/[^\\/]+)/i);
  return m?m[1]:'';
}})();
var card=document.getElementById('card'),headline=document.getElementById('headline'),
    support=document.getElementById('support'),phasesEl=document.getElementById('phases'),
    feed=document.getElementById('feed'),resultEl=document.getElementById('result'),
    elapsedEl=document.getElementById('elapsed'),waitingEl=document.getElementById('waiting'),
    stepsEl=document.getElementById('steps'),detail=document.getElementById('detail'),
    detailSum=document.getElementById('detailsum');
var startedAt=null,finishedAt=null,stopped=false,failedPolls=0,lastSupport='',
    everPolled=false;
var WAIT_WORDS={{playbook_publish:'make the change live',
  playbook_rollback:'roll back the live version',
  playbook_spec_delete:'delete a spec',
  playbook_set_autonomy:'change how it runs on its own',
  playbook_run_candidate:'test-run the draft version'}};

function esc(s){{var d=document.createElement('span');d.textContent=String(s==null?'':s);return d.innerHTML;}}

function reportHeight(){{
  try{{
    var h=Math.min(1400,Math.ceil(card.getBoundingClientRect().height)+4);
    parent.postMessage({{type:'luna:embed:height',height:h}},'*');
  }}catch(e){{}}
}}

function fmtElapsed(){{
  if(!startedAt)return '';
  var end=finishedAt?new Date(finishedAt):new Date();
  var s=Math.max(0,Math.floor((end-new Date(startedAt))/1000));
  var m=Math.floor(s/60);
  return m>0?(m+'m '+(s%60)+'s'):(s+'s');
}}

function phaseStates(events){{
  var idx={{}};BOOT.phases.forEach(function(p,i){{idx[p]=i;}});
  var counts=[0,0,0,0],maxSeen=-1;
  (events||[]).forEach(function(e){{
    if(e.kind!=='tool')return;
    var i=idx[e.phase];if(i==null)return;
    counts[i]++;if(i>maxSeen)maxSeen=i;
  }});
  return {{counts:counts,current:maxSeen}};
}}

function headlineFor(st,ps){{
  var pb=BOOT.playbook;
  if(st.status==='done')return 'Done'+(pb?' — '+pb+' updated':'');
  if(st.status==='failed')return 'Stopped — something went wrong';
  if(st.status==='needs_owner')return 'Paused — needs your call';
  var cur=ps.current>=0?BOOT.phases[ps.current]:null;
  var verb={{Understand:'Reading',Change:'Making changes',
             Prove:'Testing',Ship:'Shipping'}}[cur]||'Getting started';
  return verb+(pb?' — '+pb:'');
}}

// One faint hint per tool row: the first short string value from the
// scrubbed args (e.g. the playbook name), so the feed reads as a story,
// not a bare list of tool codes.
function hintFor(e){{
  var a=e.args;
  if(!a||typeof a!=='object')return '';
  var keys=Object.keys(a);
  for(var i=0;i<keys.length;i++){{
    var v=a[keys[i]];
    if(typeof v!=='string'||!v||v==='\\u2022\\u2022\\u2022')continue;
    v=v.replace(/\\s+/g,' ').trim();
    if(!v)continue;
    return v.length>40?v.slice(0,40)+'…':v;
  }}
  return '';
}}

function tickFor(e){{
  if(e.ms==null)return '<span class="tick run">●</span>';
  if(e.ok===false)return '<span class="tick err">✗</span>';
  return '<span class="tick ok">✓</span>';
}}

function render(st){{
  startedAt=st.started_at||startedAt;finishedAt=st.finished_at||null;
  var events=st.events||[];
  var ps=phaseStates(events);
  card.className='card '+(st.status||'');
  headline.textContent=headlineFor(st,ps);

  var last=null;
  for(var i=events.length-1;i>=0;i--){{if(events[i].kind==='tool'){{last=events[i];break;}}}}
  if(st.status==='running'){{
    lastSupport=last?('Last step: '+last.label+(last.ms==null?' — in progress':'')):'Warming up';
  }}else if(st.status==='done'){{lastSupport=st.steps_used+' steps';}}
  else{{lastSupport=last?('Stopped after: '+last.label):'';}}
  support.textContent=lastSupport;

  stepsEl.textContent=(st.status==='running'&&st.steps_used>0)?
    (st.steps_used+'/'+BOOT.maxSteps):'';

  phasesEl.innerHTML=BOOT.phases.map(function(p,i){{
    var cls='phase';
    if(i<ps.current)cls+=' done';
    else if(i===ps.current)cls+=(st.status==='done')?' done':
      (st.status==='failed')?' bad':' current';
    var n=ps.counts[i]?'<span class="n">'+ps.counts[i]+
      (ps.counts[i]>1?' steps':' step')+'</span>':'';
    return '<div class="'+cls+'"><span class="dot"></span>'+esc(p)+n+'</div>';
  }}).join('');

  // Auto-scroll only when the reader was already pinned to the newest
  // entry — a user who scrolled up to study something stays put.
  var pinned=feed.scrollHeight-feed.scrollTop-feed.clientHeight<24;
  feed.innerHTML=events.map(function(e){{
    if(e.kind!=='tool'){{
      return '<div class="ev thought"><span class="tick"></span>'+
        '<span class="lbl">'+esc(e.label)+'</span></div>';
    }}
    var ms=(e.ms!=null)?
      '<span class="ms">'+(e.ms>=1000?(e.ms/1000).toFixed(1)+'s':e.ms+'ms')+'</span>':'';
    var hint=hintFor(e);
    return '<div class="ev tool">'+tickFor(e)+'<span class="lbl">'+
      esc(e.label)+'</span>'+(hint?'<span class="hint">'+esc(hint)+'</span>':'')+
      ms+'</div>';
  }}).join('');
  if(pinned)feed.scrollTop=feed.scrollHeight;
  var nTools=events.filter(function(e){{return e.kind==='tool';}}).length;
  detailSum.textContent='Activity'+(nTools?' · '+nTools+
    (nTools>1?' steps':' step'):'');

  var waitTool=(st.status==='running')?st.waiting_for_approval:null;
  if(waitTool){{
    waitingEl.style.display='block';
    waitingEl.textContent='Waiting for your approval — '+
      (WAIT_WORDS[waitTool]||String(waitTool).replace(/_/g,' '));
    support.textContent='Paused until you decide';
  }}else{{
    waitingEl.style.display='none';
  }}

  if(st.status!=='running'&&st.result){{
    resultEl.style.display='block';resultEl.textContent=st.result;
  }}
  elapsedEl.textContent=fmtElapsed();
  if(st.status&&st.status!=='running')stopped=true;
  reportHeight();
}}

function offline(){{
  stopped=true;
  headline.textContent='Working — live updates can\\'t show here';
  support.textContent='The playbook agent keeps going. Ask me how it\\'s going.';
  reportHeight();
}}

function poll(){{
  fetch(API_BASE+'/api/p/plugin-playbooks/delegations/'+BOOT.id+'/card?token='+
        encodeURIComponent(BOOT.token))
    .then(function(r){{
      if(r.status===401||r.status===403){{offline();return null;}}
      if(!r.ok)throw new Error(r.status);
      return r.json();
    }})
    .then(function(st){{if(st){{failedPolls=0;everPolled=true;render(st);}}}})
    .catch(function(){{
      failedPolls++;
      // The hosted proxy's auth errors have no CORS header, so from this
      // opaque-origin sandbox they look like network failures — the status
      // above is unreadable. If the very first polls all fail, this view
      // can't reach the agent at all: say so instead of retrying forever.
      if(!everPolled&&failedPolls>=5){{offline();return;}}
      if(failedPolls>2){{support.textContent='Connection lost — retrying';}}
    }})
    .finally(function(){{
      if(!stopped)setTimeout(poll,BOOT.pollMs);
    }});
}}

detail.addEventListener('toggle',reportHeight);
setInterval(function(){{if(!stopped){{elapsedEl.textContent=fmtElapsed();}}}},1000);
window.addEventListener('resize',reportHeight);
reportHeight();
poll();
}})();
</script>
</body></html>"""
