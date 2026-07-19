import collections
import http.server
import json
import logging
import threading
import time

latest_snapshot = {}
mid_history = collections.deque(maxlen=600)
log_tail = collections.deque(maxlen=200)


class LogTailHandler(logging.Handler):
    def emit(self, record):
        log_tail.append(
            time.strftime("%H:%M:%S", time.localtime(record.created))
            + f"  {record.levelname:<7} {record.getMessage()}")


def push(**snapshot):
    global latest_snapshot
    if snapshot.get("mid_cents") is not None:
        mid_history.append([snapshot.get("ts", time.time()),
                            snapshot["mid_cents"]])
    snapshot["mid_history"] = list(mid_history)
    snapshot["log"] = list(log_tail)
    latest_snapshot = snapshot


class RequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/state"):
            snapshot = dict(latest_snapshot)
            snapshot["served_at"] = time.time()
            body = json.dumps(snapshot).encode()
            content_type = "application/json"
        else:
            body = PAGE.encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *ignored):
        pass


def start(port: int = 8787):
    logging.getLogger().addHandler(LogTailHandler())
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                             RequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>kalshi mm</title>
<style>
:root{--bg:#0D1520;--panel:#131F2E;--line:#1E3048;--text:#D8E2EE;
--dim:#6C7F94;--amber:#E8A33D;--bid:#3DBE8B;--ask:#E4566B}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);
font:13px/1.45 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
padding:18px;max-width:820px;margin:0 auto}
.eyebrow{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim)}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:14px}
#ticker{font-size:20px;font-weight:700;letter-spacing:.04em}
.chip{font-size:11px;padding:2px 9px;border:1px solid var(--line);border-radius:3px;color:var(--dim)}
.chip.live{color:var(--amber);border-color:var(--amber)}
.chip.stale{color:var(--ask);border-color:var(--ask);display:none}
#countdown{margin-left:auto;color:var(--dim);font-size:12px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
gap:1px;background:var(--line);border:1px solid var(--line);margin-bottom:14px}
.stat{background:var(--panel);padding:9px 12px}
.stat b{display:block;font-size:19px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.good{color:var(--bid)!important}.bad{color:var(--ask)!important}.amb{color:var(--amber)!important}
.panel{background:var(--panel);border:1px solid var(--line);padding:12px;margin-bottom:14px}
.panel h2{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);
font-weight:600;margin-bottom:8px}
.row{display:flex;align-items:center;gap:8px;padding:1px 4px;position:relative;
font-variant-numeric:tabular-nums}
.row .bar{position:absolute;right:0;top:2px;bottom:2px;opacity:.13}
.row.a .bar{background:var(--ask)}.row.b .bar{background:var(--bid)}
.row .px{width:44px}.row.a .px{color:var(--ask)}.row.b .px{color:var(--bid)}
.row .qty{margin-left:auto;color:var(--dim)}
.you{font-size:9px;color:#0D1520;background:var(--amber);border-radius:2px;
padding:0 4px;font-weight:700}
.row.ghost{opacity:.85}.row.ghost .px{color:var(--amber)}
.gutter{border-top:1px dashed var(--line);border-bottom:1px dashed var(--line);
text-align:center;color:var(--dim);font-size:10px;padding:3px 0;margin:4px 0}
svg{width:100%;height:70px;display:block}
#log{height:150px;overflow-y:auto;font-size:11.5px;color:var(--dim);white-space:pre-wrap}
.empty{color:var(--dim);font-size:12px;padding:6px 0}
</style></head><body>
<header>
<span id="ticker">--</span>
<span id="mode" class="chip">DRY RUN</span>
<span id="env" class="chip"></span>
<span id="stale" class="chip stale">STALE FEED</span>
<span id="countdown"></span>
</header>
<div class="stats">
<div class="stat"><span class="eyebrow">Mid</span><b id="mid">--</b></div>
<div class="stat"><span class="eyebrow">Spread</span><b id="spread">--</b></div>
<div class="stat"><span class="eyebrow">Position</span><b id="position" class="amb">--</b></div>
<div class="stat"><span class="eyebrow">Session P&amp;L</span><b id="pnl">--</b></div>
<div class="stat"><span class="eyebrow">Fills</span><b id="fills">0</b></div>
</div>
<div class="panel"><h2>Book &middot; your quotes pinned</h2><div id="ladder"></div></div>
<div class="panel"><h2>Mid, this session</h2><svg id="spark"></svg></div>
<div class="panel"><h2>Event log</h2><div id="log">Waiting for first update&hellip;</div></div>
<script>
const $=id=>document.getElementById(id);
const fixed=(v,d=1)=>v==null?"--":(+v).toFixed(d);
function ladderRow(cls,px,qty,maxq,mine){
const bar=qty!=null?`<span class="bar" style="width:${Math.min(100,100*qty/maxq)}%"></span>`:"";
return `<div class="row ${cls}">${bar}<span class="px">${px}c</span>`+
(mine?`<span class="you">YOU ${mine[1]}</span>`:"")+
`<span class="qty">${qty==null?"":Math.round(qty)}</span></div>`;}
function renderLadder(s){
const resting=s.resting||{},book=s.book;
if(!book){$("ladder").innerHTML='<div class="empty">waiting for book</div>';return;}
const maxq=Math.max(1,...book.bids.map(l=>l[1]),...book.asks.map(l=>l[1]));
let h="";const asks=[...book.asks].reverse();
if(resting.ask&&!asks.some(l=>l[0]===resting.ask[0]))
h+=ladderRow("a ghost",resting.ask[0],null,maxq,resting.ask);
for(const [px,q] of asks)h+=ladderRow("a",px,q,maxq,resting.ask&&resting.ask[0]===px?resting.ask:null);
h+=`<div class="gutter">spread ${s.spread_cents}c &middot; micro ${fixed(s.microprice_cents,1)}c</div>`;
for(const [px,q] of book.bids)h+=ladderRow("b",px,q,maxq,resting.bid&&resting.bid[0]===px?resting.bid:null);
if(resting.bid&&!book.bids.some(l=>l[0]===resting.bid[0]))
h+=ladderRow("b ghost",resting.bid[0],null,maxq,resting.bid);
$("ladder").innerHTML=h;}
function renderSpark(history){
if(!history||history.length<2){$("spark").innerHTML="";return;}
const xs=history.map(p=>p[0]),ys=history.map(p=>p[1]);
const x0=Math.min(...xs),x1=Math.max(...xs);
let y0=Math.min(...ys),y1=Math.max(...ys);
if(y1-y0<1){const c=(y0+y1)/2;y0=c-.5;y1=c+.5;}
const W=780,H=70,P=5;
const pts=history.map(p=>`${P+(W-2*P)*(p[0]-x0)/Math.max(1,x1-x0)},${H-P-(H-2*P)*(p[1]-y0)/(y1-y0)}`).join(" ");
$("spark").setAttribute("viewBox",`0 0 ${W} ${H}`);
$("spark").innerHTML=`<polyline points="${pts}" fill="none" stroke="#E8A33D" stroke-width="1.4"/>`+
`<text x="${W-P}" y="12" fill="#6C7F94" font-size="10" text-anchor="end">${fixed(y1,1)}c</text>`+
`<text x="${W-P}" y="${H-2}" fill="#6C7F94" font-size="10" text-anchor="end">${fixed(y0,1)}c</text>`;}
async function refresh(){
try{
const s=await (await fetch("/state")).json();
if(!s.ticker)return;
$("ticker").textContent=s.ticker;
$("env").textContent=s.env||"";
$("mode").textContent=s.live?"LIVE":"DRY RUN";
$("mode").className="chip"+(s.live?" live":"");
$("stale").style.display=(s.served_at-s.ts>6)?"inline-block":"none";
const left=Math.max(0,(s.stop_ts||0)-s.served_at);
$("countdown").textContent=isFinite(left)?"stops in "+Math.floor(left/60)+"m "+Math.floor(left%60)+"s":"";
$("mid").textContent=fixed(s.mid_cents,1)+"c";
$("spread").textContent=(s.spread_cents==null?"--":s.spread_cents)+"c";
$("position").textContent=(s.position>0?"+":"")+fixed(s.position,0);
const p=$("pnl");p.textContent="$"+fixed(s.pnl_dollars,2);
p.className=s.pnl_dollars>0?"good":s.pnl_dollars<0?"bad":"";
$("fills").textContent=s.fill_count||0;
renderLadder(s);renderSpark(s.mid_history);
if(s.log&&s.log.length){const el=$("log");el.textContent=s.log.join("\n");el.scrollTop=el.scrollHeight;}
}catch(e){$("stale").style.display="inline-block";}}
refresh();setInterval(refresh,1000);
</script></body></html>
"""
