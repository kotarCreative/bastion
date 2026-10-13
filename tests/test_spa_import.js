/* First-push wizard contracts, credentials, retry identity, and auth races. */
"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
class Node {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.dataset={}; this.attributes={}; this.hidden=false; this.value=""; this.textContent=""; this.disabled=false; }
  get firstChild() { return this.children[0]; }
  appendChild(node) { this.children.push(node); return node; }
  prepend(node) { this.children.unshift(node); }
  removeChild(node) { this.children.splice(this.children.indexOf(node),1); }
  setAttribute(key,value) { this.attributes[key]=value; }
  addEventListener(key,handler) { this.listeners[key]=handler; }
  fire(key) { this.listeners[key]?.({preventDefault(){}, currentTarget:this, target:this}); }
  scrollIntoView() {}
  querySelector(selector) { return selector === "button[type=submit]" ? this.children.find(n=>n.type==="submit") : null; }
}
const nodes = new Map();
for (const match of fs.readFileSync("templates/spa/index.html","utf8").matchAll(/id="([^"]+)"/g)) {
  const node = new Node("div"); node.id=match[1]; nodes.set(node.id,node);
}
const submitButton=new Node("button"); submitButton.type="submit"; nodes.get("import-repository").appendChild(submitButton);
Object.defineProperty(nodes.get("import-status"), "textContent", {
  get() { return this.children.map(n=>n.textContent).join(""); },
  set(value) { this.children=value ? [{textContent:String(value)}] : []; },
});
const tabs=[...nodes.values()].filter(n=>n.id.startsWith("tab-")), panels=[...nodes.values()].filter(n=>n.id.startsWith("panel-"));
tabs.forEach(n=>n.dataset.panel=n.id.slice(4));
const calls=[], operations=new Map(), repos=[], copied=[];
let uuid=0, failReserve=0, failCredential=0, failStatus=0, expireStatus=0, gateReserve=null, gateCredential=null, gateStatus=null, owner="alice", hostToken="host-token";
const existing={repo_id:"r1", name:"existing", owner_name:"alice", branch:"main", git_url:"/alice/existing.git", views:["owner"]};
function deferred() { let resolve; const promise=new Promise(r=>resolve=r); return {promise,resolve}; }
function shape(op) { return {import_id:op.import_id, name:op.name, branch:op.branch, state:op.state, repo:op.repo, git_url:`/${op.owner}/${op.name}.git`, username:"owner"}; }
const ctx={console, URL, TextEncoder, Intl, Date, navigator:{clipboard:{writeText:async value=>{copied.push(value)}}},
  document:{getElementById:id=>nodes.get(id), createElement:tag=>new Node(tag), querySelectorAll:s=>s===".tab"?tabs:panels},
  window:{location:{protocol:"https:",host:"b.test",href:"https://b.test/"},scrollTo(){},setTimeout,clearTimeout,crypto:{randomUUID:()=>"uuid-"+(++uuid)}},
  fetch:async(path, opts={})=>{
    const method=opts.method||"GET", token=opts.headers?.Authorization;
    calls.push({path,opts}); let status=200, body={};
    if(path==="/api/v1/auth/config") body={enabled:false,mode:"host",import_mode:"first-push",import_limits:{pack_bytes:33554432}};
    else if(path==="/api/v1/session") { hostToken=token.slice(7); body={mode:"host",account:{account_id:owner,owner_name:owner}}; }
    else if(path==="/api/v1/repos"&&method==="GET") body={repos:repos.slice()};
    else if(path==="/api/v1/imports"&&method==="GET") body={imports:[...operations.values()].filter(op=>op.owner===owner&&op.state!=="ready").map(shape)};
    else if(path==="/api/v1/imports"&&method==="POST") {
      assert.equal(token,"Bearer "+hostToken);
      const payload=JSON.parse(opts.body); assert.deepEqual(Object.keys(payload).sort(),["branch","name","request_id"]);
      if(gateReserve) await gateReserve.promise;
      let op=operations.get(payload.request_id);
      if(!op) { op={...payload,owner,import_id:"i"+(operations.size+1),state:"awaiting-push",repo:null,issues:0}; operations.set(payload.request_id,op); }
      if(failReserve) { failReserve--; throw Error("lost reservation response"); }
      status=201; body={import:shape(op)};
    } else if(/^\/api\/v1\/imports\/[^/]+\/credentials$/.test(path)) {
      assert.equal(method,"POST"); assert.equal(token,"Bearer "+hostToken); assert.equal(opts.body,"{}");
      const op=[...operations.values()].find(op=>path.includes("/"+op.import_id+"/")); assert.ok(op);
      op.issues++; const secret="bsi1.test."+op.import_id+"-"+op.issues;
      if(gateCredential) await gateCredential.promise;
      if(failCredential) { failCredential--; throw Error("lost credential response"); }
      status=201; body={credential:secret,username:"owner",expires_at:2000000000};
    } else if(/^\/api\/v1\/imports\/[^/]+$/.test(path)) {
      assert.equal(method,"GET"); assert.equal(token,"Bearer "+hostToken);
      const op=[...operations.values()].find(op=>path.endsWith("/"+op.import_id)); assert.ok(op);
      if(gateStatus) await gateStatus.promise;
      if(expireStatus) { expireStatus--; status=401; body={error:"auth-required"}; }
      else if(failStatus) { failStatus--; status=503; body={error:"unavailable"}; }
      else body={import:shape(op)};
    } else if(/^\/api\/v1\/repos\/[^/]+\/auth\/session$/.test(path)) body={access_token:"repo-token",session:{repo:existing,principal:{id:"owner",kind:"user",emails:["alice@example.test"]},view_id:"owner",team_ids:[],role_ids:[],descriptor:{projected_ref:"refs/heads/main",generation:1,projection_epoch:1,capabilities:{read:[]},restrictions:{read:[]}}}};
    else if(path.includes("/tree?")) body={projected_tip:"tip",entries:[],next_cursor:null};
    else if(path.endsWith("/logout")) body={ok:true};
    else throw Error("Unexpected request: "+method+" "+path);
    return {status,json:async()=>body};
  }};
vm.runInNewContext(fs.readFileSync("templates/spa/app.js","utf8"),ctx);
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r))};
const reserveCalls=()=>calls.filter(c=>c.path==="/api/v1/imports"&&c.opts.method==="POST");
function fields(name,branch="main") { nodes.get("import-repository-name").value=name; nodes.get("import-branch").value=branch; }
function submit(name,branch="main") { fields(name,branch); nodes.get("import-repository").fire("submit"); }
function opNamed(name) { return [...operations.values()].find(op=>op.name===name); }
function complete(op) { op.state="ready"; op.repo={repo_id:"r-"+op.import_id,name:op.name,owner_name:op.owner,branch:op.branch,git_url:`/${op.owner}/${op.name}.git`,views:["owner"]}; repos.push(op.repo); }
(async()=>{
  await settle(); nodes.get("token").value="host-token"; nodes.get("login-form").fire("submit"); await settle();
  assert.equal(nodes.get("import-repository").hidden,false);
  assert.equal(nodes.has("import-bundle"),false,"no file upload control");
  submit("Bad_Name"); await settle(); assert.equal(reserveCalls().length,0); assert.match(nodes.get("import-error").textContent,/lowercase/);
  submit("bad-branch","bad..branch"); await settle(); assert.equal(reserveCalls().length,0);
  // Lost reservation response reuses the same immutable intent and never creates a seeded repo.
  failReserve=1; submit("first"); await settle(); assert.match(nodes.get("import-error").textContent,/reservation/);
  nodes.get("import-retry").fire("click"); await settle();
  assert.equal(reserveCalls().length,2);
  assert.equal(JSON.parse(reserveCalls()[0].opts.body).request_id,JSON.parse(reserveCalls()[1].opts.body).request_id);
  assert.match(nodes.get("import-commands").textContent,/git remote add bastion https:\/\/b.test\/alice\/first.git/);
  assert.match(nodes.get("import-commands").textContent,/git push bastion main/);
  assert.doesNotMatch(nodes.get("import-commands").textContent,/bsi1|host-token|repo-token/);
  assert.match(nodes.get("import-secret").value,/^bsi1/);
  assert.equal(nodes.get("import-secret").type,"password");
  nodes.get("import-reveal").fire("click"); assert.equal(nodes.get("import-secret").type,"text");
  nodes.get("import-reveal").fire("click"); assert.equal(nodes.get("import-secret").type,"password");
  nodes.get("import-copy").fire("click"); await settle(); assert.equal(copied.at(-1),nodes.get("import-secret").value);
  const originalSecret=nodes.get("import-secret").value;
  nodes.get("import-rotate").fire("click"); await settle(); assert.notEqual(nodes.get("import-secret").value,originalSecret);
  // The browser does not perform a pack upload or complete the repository; Git does.
  assert.equal(calls.some(c=>/\/(bundle|complete)$/.test(c.path)),false);
  assert.equal(calls.some(c=>c.opts.method==="PUT"),false);
  failStatus=1; nodes.get("import-check").fire("click"); await settle(); assert.match(nodes.get("import-error").textContent,/status is unavailable/);
  const count=reserveCalls().length; nodes.get("import-retry").fire("click"); await settle(); assert.equal(reserveCalls().length,count);
  complete(opNamed("first")); nodes.get("import-check").fire("click"); await settle();
  assert.match(nodes.get("import-status").textContent,/Ready:/); assert.equal(nodes.get("import-secret").value,""); assert.equal(nodes.get("import-instructions").hidden,true);
  // Credential response loss rotates within the same operation without another reservation.
  failCredential=1; submit("lost-credential","release/v2"); await settle(); const lost=opNamed("lost-credential");
  assert.equal(lost.issues,1); assert.match(nodes.get("import-error").textContent,/response was lost/);
  nodes.get("import-retry").fire("click"); await settle(); assert.equal(lost.issues,2); assert.match(nodes.get("import-commands").textContent,/git push bastion release\/v2/);
  // A fresh dashboard recovers pending operations, but never recovers raw secrets.
  nodes.get("repository-list-nav").fire("click"); await settle();
  const resume=nodes.get("import-list").children.flatMap(n=>n.children).find(n=>n.textContent==="Resume first push"); assert.ok(resume);
  resume.fire("click"); assert.equal(nodes.get("import-secret").value,""); assert.equal(nodes.get("import-instructions").hidden,false);
  nodes.get("import-rotate").fire("click"); await settle(); assert.match(nodes.get("import-secret").value,/^bsi1/);
  // Editing fields while reservation is pending invalidates callbacks, preserving the edit.
  gateReserve=deferred(); submit("old-intent"); await settle(); nodes.get("import-repository-name").value="new-intent"; nodes.get("import-repository-name").fire("input");
  gateReserve.resolve(); await settle(); gateReserve=null; assert.equal(nodes.get("import-repository-name").value,"new-intent"); assert.equal(nodes.get("import-secret").value,""); assert.equal(submitButton.disabled,false);
  // Opening a repository while an external import completes cannot replace the workspace.
  submit("background"); await settle(); complete(opNamed("background"));
  const open=nodes.get("repository-list").children.flatMap(n=>n.children).find(n=>n.textContent==="Open repository"); assert.ok(open); open.fire("click"); await settle();
  nodes.get("import-check").fire("click"); await settle(); assert.equal(nodes.get("workspace").hidden,false); assert.equal(nodes.get("repo-name").textContent,"existing");
  assert.ok(calls.filter(c=>c.path.startsWith("/api/v1/imports")).every(c=>c.opts.headers.Authorization==="Bearer host-token"));
  assert.ok(calls.filter(c=>c.path.includes("/tree?")).every(c=>c.opts.headers.Authorization==="Bearer repo-token"));
  // Stale credential responses after logout/account switch must never disclose secrets.
  nodes.get("repository-list-nav").fire("click"); await settle();
  gateCredential=deferred(); submit("logout-race"); await settle(); const gate=gateCredential;
  nodes.get("logout").fire("click"); await settle(); assert.equal(nodes.get("import-secret").value,"");
  owner="charlie"; nodes.get("token").value="new-host"; nodes.get("login-form").fire("submit"); await settle();
  const status=nodes.get("import-status").textContent; gate.resolve(); await settle(); gateCredential=null;
  assert.equal(nodes.get("identity").textContent,"charlie"); assert.equal(nodes.get("import-secret").value,""); assert.equal(nodes.get("import-status").textContent,status);
  // Current host expiration clears the credential and form; late 401 does not clear a new login.
  submit("expired"); await settle(); expireStatus=1; nodes.get("import-check").fire("click"); await settle();
  assert.equal(nodes.get("host-dashboard").hidden,true); assert.equal(nodes.get("import-secret").value,""); assert.equal(nodes.get("import-repository-name").value,"");
  assert.equal(ctx.localStorage,undefined); assert.equal(ctx.sessionStorage,undefined);
  console.log("SPA first-push checks passed: native Git instructions, secret rotation, status/retry recovery, pending lists, workspace isolation, and auth races.");
})().catch(error=>{console.error(error);process.exitCode=1});
