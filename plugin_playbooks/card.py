"""Delegation progress card — plans/013 phase 2.

`render_delegation_card(...)` builds ONE self-contained HTML document
(inline CSS + JS) posted into the chat via ``ctx.post_chat_card``. The chat
UI renders it in a sandboxed srcdoc iframe (opaque origin, scripts allowed);
the document polls the capability-token status route and re-renders itself.
Height is auto-reported with ``postMessage({type:"luna:embed:height"})``.

Layout per vision/ux_guidelines.md: eyebrow → bottom-line headline →
support line → phase rows with status dots → collapsed detail feed. Dark
tokens, no gradient (this is a status card, not a hero), owner words only —
the phase labels come from the server's fixed vocabulary.
"""

from __future__ import annotations

import html
import json

_POLL_MS = 1500
_PHASES = ["Understand", "Change", "Prove", "Ship"]


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
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:transparent}}
body{{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:var(--text);font-size:14px;line-height:1.45;
  -webkit-font-smoothing:antialiased}}
.card{{background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:16px 18px;max-width:560px}}
.top{{display:flex;align-items:baseline;justify-content:space-between;gap:12px}}
.eyebrow{{font-size:11px;font-weight:600;letter-spacing:.12em;
  text-transform:uppercase;color:var(--violet);display:flex;
  align-items:center;gap:8px}}
.chip{{font-size:11px;font-weight:500;letter-spacing:0;text-transform:none;
  color:var(--dim);background:var(--panel-2);border:1px solid var(--line);
  border-radius:6px;padding:1px 7px}}
.elapsed{{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums;
  white-space:nowrap}}
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
  @keyframes pulse{{50%{{opacity:.35}}}}
}}
details{{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}}
summary{{cursor:pointer;color:var(--faint);font-size:12px;list-style:none;
  user-select:none}}
summary::before{{content:"▸ "}}
details[open] summary::before{{content:"▾ "}}
.feed{{margin-top:8px;display:flex;flex-direction:column;gap:4px;
  max-height:260px;overflow-y:auto;font-size:12px}}
.ev{{display:flex;gap:8px;align-items:baseline}}
.ev .lbl{{color:var(--dim);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}}
.ev.thought .lbl{{color:var(--faint);font-style:italic}}
.ev .ms{{margin-left:auto;color:var(--faint);
  font-variant-numeric:tabular-nums;white-space:nowrap}}
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
    <div class="elapsed" id="elapsed"></div>
  </div>
  <h1 id="headline">Starting…</h1>
  <div class="support" id="support">Handing the job to the delegate</div>
  <div class="phases" id="phases"></div>
  <div class="waiting" id="waiting"></div>
  <details id="detail"><summary>What it did</summary>
    <div class="feed" id="feed"></div>
  </details>
  <div class="result" id="result"></div>
</div>
<script>
(function(){{
var BOOT={boot};
var card=document.getElementById('card'),headline=document.getElementById('headline'),
    support=document.getElementById('support'),phasesEl=document.getElementById('phases'),
    feed=document.getElementById('feed'),resultEl=document.getElementById('result'),
    elapsedEl=document.getElementById('elapsed'),waitingEl=document.getElementById('waiting');
var startedAt=null,finishedAt=null,stopped=false,failedPolls=0,lastSupport='';
var WAIT_WORDS={{playbook_promote:'make the change live',
  playbook_rollback:'roll back the live version',
  playbook_edit_force:'force past failing specs',
  playbook_manifest_set:"change the playbook's contract",
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

  phasesEl.innerHTML=BOOT.phases.map(function(p,i){{
    var cls='phase';
    if(i<ps.current)cls+=' done';
    else if(i===ps.current)cls+=(st.status==='done')?' done':
      (st.status==='failed')?' bad':' current';
    var n=ps.counts[i]?'<span class="n">'+ps.counts[i]+
      (ps.counts[i]>1?' steps':' step')+'</span>':'';
    return '<div class="'+cls+'"><span class="dot"></span>'+esc(p)+n+'</div>';
  }}).join('');

  feed.innerHTML=events.map(function(e){{
    var ms=(e.kind==='tool'&&e.ms!=null)?
      '<span class="ms">'+(e.ms>=1000?(e.ms/1000).toFixed(1)+'s':e.ms+'ms')+'</span>':'';
    return '<div class="ev '+esc(e.kind)+'"><span class="lbl">'+
      esc(e.label)+'</span>'+ms+'</div>';
  }}).join('');

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

function poll(){{
  fetch('/api/p/plugin-playbooks/delegations/'+BOOT.id+'/card?token='+
        encodeURIComponent(BOOT.token))
    .then(function(r){{if(!r.ok)throw new Error(r.status);return r.json();}})
    .then(function(st){{failedPolls=0;render(st);}})
    .catch(function(){{
      failedPolls++;
      if(failedPolls>2){{support.textContent='Connection lost — retrying';}}
    }})
    .finally(function(){{
      if(!stopped)setTimeout(poll,BOOT.pollMs);
    }});
}}

setInterval(function(){{if(!stopped){{elapsedEl.textContent=fmtElapsed();}}}},1000);
window.addEventListener('resize',reportHeight);
reportHeight();
poll();
}})();
</script>
</body></html>"""
