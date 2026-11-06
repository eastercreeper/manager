package manager

import "github.com/labstack/echo/v4"

const managerPage = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Anime Manager</title><style>
:root{color-scheme:dark;font:15px system-ui;background:#101116;color:#eee}body{max-width:1100px;margin:32px auto;padding:0 18px}
h1,h2{font-weight:650}section{background:#1b1d26;border:1px solid #343744;border-radius:12px;padding:18px;margin:16px 0}
input,button{background:#11131a;color:#eee;border:1px solid #454958;border-radius:7px;padding:9px;margin:4px}
button{cursor:pointer;background:#35304c}button:hover{background:#51466e}
small,.muted{color:#a6a8b5}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.item{border-top:1px solid #343744;padding:10px 0;display:flex;gap:12px;justify-content:space-between;align-items:center}
.error{color:#ff9a9a}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style></head><body>
<h1>Anime Manager</h1><p class="muted">Jellyfin library · AniList overrides · release approvals · media verification</p>
<section><h2>Connection</h2><div class="row"><label>Manager API token <input id="token" type="password" placeholder="Only if MANAGER_API_TOKEN is set"></label><button id="status">Test Jellyfin</button><button id="sync">Sync library</button><button id="reload">Reload</button></div><p id="message" class="muted">Ready.</p></section>
<section><h2>Library and manual AniList mapping</h2><div id="library">Load the library to begin.</div></section>
<section><h2>Submit a release for approval</h2><form id="release" class="row">
<input name="title" placeholder="Release title" required><input name="link" placeholder="magnet or torrent URL" required>
<input name="mediaId" type="number" placeholder="AniList ID"><input name="episodeNumber" type="number" placeholder="Episode">
<input name="savePath" placeholder="Absolute qBittorrent save path" required><input name="category" placeholder="Category" value="anime">
<button>Queue for approval</button></form></section>
<section><h2>Release approvals</h2><div id="approvals">No approvals loaded.</div></section>
<script>
const $=id=>document.getElementById(id);
const api=async(path,options={})=>{const headers={...(options.headers||{})};if(options.body)headers['Content-Type']='application/json';const token=$('token').value.trim();if(token)headers.Authorization='Bearer '+token;const r=await fetch('/api/v1/manager'+path,{...options,headers});const body=await r.json().catch(()=>({error:r.statusText}));if(!r.ok)throw Error(body.error||r.statusText);return body};
const say=t=>$('message').textContent=t;
const el=(tag,text,cls)=>{const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n};
async function loadLibrary(){const data=await api('/library');const root=$('library');root.replaceChildren();for(const s of data.series){const row=el('div','', 'item'),left=el('div');left.append(el('strong',s.name));left.append(el('div',s.path||'No filesystem path','muted'));const form=document.createElement('form');form.className='row';const input=document.createElement('input');input.type='number';input.placeholder='AniList ID';input.value=s.anilistId||'';const save=el('button','Save mapping');form.append(input,save);form.onsubmit=async e=>{e.preventDefault();try{await api('/mappings/'+encodeURIComponent(s.jellyfinId),{method:'PUT',body:JSON.stringify({anilistId:Number(input.value)})});say('Mapping saved for '+s.name)}catch(err){say(err.message)}};left.append(form);row.append(left,el('small',s.jellyfinId));root.append(row)}if(!data.series.length)root.textContent='No series synced yet.';for(const ep of data.episodes){const row=el('div','', 'item');row.append(el('span',ep.seriesName+' · S'+ep.seasonNumber+'E'+ep.episodeNumber+' · '+ep.name));const b=el('button','Verify media');b.onclick=async()=>{try{const v=await api('/episodes/'+encodeURIComponent(ep.jellyfinId)+'/verify',{method:'POST'});say('Readable: '+v.readable+' · English ASS/SSA: '+v.hasEnglishASS+(v.error?' · '+v.error:''))}catch(err){say(err.message)}};row.append(b);root.append(row)}}
async function loadApprovals(){const data=await api('/approvals');const root=$('approvals');root.replaceChildren();for(const a of data){const row=el('div','', 'item'),left=el('div');left.append(el('strong',a.title));left.append(el('div',a.status+' · '+a.savePath,'muted'));row.append(left);if(a.status==='pending'){for(const action of ['approve','reject']){const b=el('button',action);b.onclick=async()=>{try{await api('/approvals/'+a.id+'/'+action,{method:'POST'});say('Release '+action+'d');await loadApprovals()}catch(err){say(err.message)}};row.append(b)}}root.append(row)}if(!data.length)root.textContent='No releases awaiting review.'}
$('status').onclick=async()=>{try{const x=await api('/jellyfin/status');say('Connected to '+x.ServerName+' · Jellyfin '+x.Version)}catch(e){say(e.message)}};
$('sync').onclick=async()=>{try{const x=await api('/jellyfin/sync',{method:'POST'});say('Synced '+x.series+' series and '+x.episodes+' episodes');await loadLibrary()}catch(e){say(e.message)}};
$('reload').onclick=async()=>{try{await loadLibrary();await loadApprovals();say('Reloaded')}catch(e){say(e.message)}};
$('release').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),v=Object.fromEntries(f.entries());v.mediaId=Number(v.mediaId||0);v.episodeNumber=Number(v.episodeNumber||0);try{await api('/approvals',{method:'POST',body:JSON.stringify(v)});e.target.reset();say('Release queued for approval');await loadApprovals()}catch(err){say(err.message)}};
loadLibrary().catch(e=>say(e.message));loadApprovals().catch(e=>say(e.message));
</script></body></html>`

func (s *Service) ui(c echo.Context) error { return c.HTML(200, managerPage) }
