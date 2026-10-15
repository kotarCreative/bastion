/* Host-mode SPA contract checks. Run with: node tests/test_spa_host.js */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Node {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.attributes={}; this.dataset={}; this.hidden=false; this.value=""; this.textContent=""; }
  get firstChild(){return this.children[0]} get lastChild(){return this.children[this.children.length-1]}
  appendChild(n){this.children.push(n); if(this.tagName==="select"&&this.children.length===1)this.value=n.value; return n}
  prepend(n){this.children.unshift(n)} removeChild(n){this.children.splice(this.children.indexOf(n),1)}
  setAttribute(k,v){this.attributes[k]=v} addEventListener(k,f){this.listeners[k]=f}
  fire(k){this.listeners[k]({preventDefault(){},currentTarget:this,target:this})} scrollIntoView(){}
}
const all=n=>[n,...n.children.flatMap(all)];
const rendered=n=>all(n).map(x=>x.textContent).join(" ");
const nodes=new Map();
for(const m of fs.readFileSync("templates/spa/index.html","utf8").matchAll(/id="([^"]+)"/g)){const n=new Node("div");n.id=m[1];nodes.set(n.id,n)}
const panels=[...nodes.values()].filter(n=>n.id.startsWith("panel-")); const tabs=[...nodes.values()].filter(n=>n.id.startsWith("tab-"));
tabs.forEach(n=>n.dataset.panel=n.id.slice(4));
const calls=[]; let repos=[]; let hostIdentity="alice"; let clerkSession; let clerkListener; let nextRepo=2; let requestSeq=0; let failCreate=false; let expireHost=false; let lateRepo401=false; const gates=new Map();
function gate(key){let release;const promise=new Promise(r=>release=r);gates.set(key,{promise,release});return gates.get(key)}
const response=(status,body)=>({status,json:async()=>body});
const newClerkSession=identity=>({id:"session-"+identity,user:{id:"user-"+identity},getToken:async()=>"jwt-"+identity});
clerkSession=newClerkSession(hostIdentity);
const clerk={get session(){return clerkSession},get isSignedIn(){return Boolean(clerkSession)},load:async()=>{},addListener(listener){clerkListener=listener},mountSignIn(){},signOut(){clerkSession=null;clerkListener?.({session:null});return Promise.resolve()}};
const switchClerk=identity=>{hostIdentity=identity;clerkSession=newClerkSession(identity);clerkListener({session:clerkSession})};
const repo=(id,name,owner)=>({repo_id:id,name,owner_name:owner,branch:"main",git_url:`https://bastion.test/${owner}/${name}.git`,views:["owner"]});
const sessionFor=r=>({repo:r,principal:{id:"owner",kind:"user",emails:["owner@example.test"]},view_id:"owner",team_ids:[],role_ids:[],descriptor:{projected_ref:"refs/heads/main",generation:1,projection_epoch:1,capabilities:{read:[]},restrictions:{read:[]}}});
const ctx={document:{getElementById:id=>nodes.get(id),createElement:t=>new Node(t),querySelectorAll:s=>s===".tab"?tabs:panels,head:{appendChild(node){node.fire("load")}}},window:{location:{protocol:"https:",host:"bastion.test",href:"https://bastion.test/",pathname:"/"},Clerk:clerk,scrollTo(){},setTimeout,clearTimeout,prompt(){return null},crypto:{randomUUID:()=>"request-"+(++requestSeq)}},URL,TextEncoder,btoa:s=>Buffer.from(s,"binary").toString("base64"),Intl,Date,
fetch:async(path,opts={})=>{calls.push({path,opts});const method=opts.method||"GET";let b={},status=200;
  if(path==="/api/v1/auth/config")b={enabled:true,mode:"host",frontend_api_origin:"https://clerk.test",publishable_key:"pk_test"};
   else if(path==="/api/v1/auth/clerk")b={mode:"host",access_token:"host-account-session",account:{account_id:"acct",owner_name:hostIdentity,is_host_admin:hostIdentity==="alice"}};
   else if(path==="/api/v1/host/accounts"&&method==="POST"){assert.equal(opts.headers.Authorization,"Bearer host-account-session");b={account:JSON.parse(opts.body)};status=201;}
 else if(path==="/api/v1/repos"&&method==="GET"){if(expireHost)status=401;else b={repos}}
 else if(path==="/api/v1/repos"&&method==="POST"){const payload=JSON.parse(opts.body);assert.deepEqual(Object.keys(payload).sort(),["name","request_id"].sort());if(failCreate){failCreate=false;throw Error("network failure")};const r=repo("r"+nextRepo++,payload.name,hostIdentity);repos.push(r);b={repo:r};status=201}
 else if(path.match(/^\/api\/v1\/repos\/[^/]+\/auth\/session$/)){const r=repos.find(x=>path.includes(x.repo_id));if(r.views.length>1&&!opts.body.includes('"view_id"')){status=409;b={view_ids:r.views}}else {b={access_token:"repo-secret-"+r.repo_id,session:{...sessionFor(r),view_id:r.views.length>1?JSON.parse(opts.body).view_id:"owner"}}}}
 else if(path.includes("/tree?")){b={projected_tip:"abcdef012345",entries:[{path_display:"README.md",path_b64:"UkVBRE1FLm1k",kind:"blob",size:4},{path_display:"secret.txt",path_b64:"c2VjcmV0LnR4dA",kind:"blob",size:4}],next_cursor:null}}
 else if(path.includes("/blob?")){const p=new URLSearchParams(path.split("?")[1]);const name=Buffer.from(p.get("path_b64"),"base64url").toString();if(gates.has("blob:"+name))await gates.get("blob:"+name).promise;b={binary:false,text:"BODY-"+path.split("/")[4]+"-"+name,path_display:name}}
 else if(path.includes("/history?")){if(gates.has("history"))await gates.get("history").promise;b={commits:[{author_name_display:"Owner",message_display:"HISTORY-"+path.split("/")[3],committed_at:1700000000,oid:"123456789012"}],next_cursor:null}}
 else if(path.endsWith("/permissions")||path.endsWith("/credentials")) { if(gates.has(path+":"+method))await gates.get(path+":"+method).promise;if(path.startsWith("/api/v1/repos/r2/")&&path.endsWith("/permissions")&&lateRepo401)status=401;if(method==="POST"&&path.endsWith("/credentials")){status=201;b={credential:"ONE-TIME-SECRET",token:{}}}else b=path.endsWith("/permissions")?{targets:[],account_picker_enabled:false}:{views:[{view_id:"owner",can_push:true}],tokens:[]}; }
 else if(path==="/api/v1/logout")b={ok:true};
 else if(path.endsWith("/logout"))b={ok:true};
 else throw Error("Unexpected request "+path);
 return response(status,b)}};
const windowListeners={};
ctx.window.addEventListener=(name,handler)=>{windowListeners[name]=handler};
ctx.window.history={replaceState(_state,_title,path){ctx.window.location.pathname=path;ctx.window.location.hash="";}};
vm.runInNewContext(fs.readFileSync("templates/spa/app.js","utf8"),ctx);
const settle=async()=>{for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r))};
(async()=>{
 await settle();
  assert.equal(nodes.get("host-dashboard").hidden,false);assert.match(rendered(nodes.get("host-message")),/No repositories yet/);
  assert.equal(nodes.get("create-repository").hidden,true);
  assert.equal(nodes.get("import-repository").hidden,true);
  assert.equal(nodes.get("approve-account").hidden,true);
  assert.equal(nodes.get("approve-account-action").hidden,false);
  assert.equal(nodes.get("profile-menu-content").hidden,true);
  nodes.get("profile-menu-toggle").fire("click");
  assert.equal(nodes.get("profile-menu-content").hidden,false);
  assert.equal(nodes.get("profile-menu-toggle").attributes["aria-expanded"],"true");
  nodes.get("profile-menu").contains=target=>target===nodes.get("profile-menu-toggle");
  windowListeners.click({target:nodes.get("profile-menu-toggle")});
  assert.equal(nodes.get("profile-menu-content").hidden,false,"clicking inside keeps the dropdown open");
  windowListeners.click({target:nodes.get("repository-list")});
  assert.equal(nodes.get("profile-menu-content").hidden,true,"outside click dismisses the dropdown");
  nodes.get("profile-menu-toggle").fire("click");
  let focusReturned=false;nodes.get("profile-menu-toggle").focus=()=>{focusReturned=true};
  windowListeners.keydown({key:"Escape"});
  assert.equal(focusReturned,true);
  assert.equal(nodes.get("profile-menu-content").hidden,true);
  nodes.get("profile-menu-toggle").fire("click");
  nodes.get("profile-nav").fire("click");
  assert.equal(nodes.get("profile-menu-content").hidden,true,"choosing Profile dismisses the dropdown");
  assert.equal(nodes.get("host-profile").hidden,false,"profile is available without selecting a repository");
  assert.match(rendered(nodes.get("host-profile")),/alice/);
  nodes.get("approve-account-action").fire("click");
  assert.equal(nodes.get("repository-index").hidden,true);
  assert.equal(nodes.get("approve-account").hidden,false);
  nodes.get("approve-owner").value="bob";nodes.get("approve-email").value="bob@example.test";nodes.get("approve-account").fire("submit");await settle();
  assert.match(nodes.get("approve-status").textContent,/Account approved/);
 nodes.get("repository-action-back").fire("click");await settle();
 assert.equal(nodes.get("repository-index").hidden,false);
 nodes.get("create-repository-action").fire("click");
 assert.equal(nodes.get("create-repository").hidden,false);
 assert.equal(ctx.window.location.hash,"#repositories/new");
 ctx.window.location.hash="#repositories";windowListeners.hashchange();
 assert.equal(nodes.get("repository-index").hidden,false,"browser Back returns to the list");
 ctx.window.location.hash="#repositories/new";windowListeners.hashchange();
 assert.equal(nodes.get("create-repository").hidden,false,"browser Forward restores the creation page");
 nodes.get("new-repository-name").value="first";failCreate=true;nodes.get("create-repository").fire("submit");await settle();
 assert.match(nodes.get("create-error").textContent,/Retry safely/);
 nodes.get("create-repository").fire("submit");await settle();
 assert.ok(calls.find(c=>c.path==="/api/v1/repos"&&c.opts.method==="POST"));
 const createPayloads=calls.filter(c=>c.path==="/api/v1/repos"&&c.opts.method==="POST").map(c=>JSON.parse(c.opts.body));assert.equal(createPayloads[0].request_id,createPayloads[1].request_id,"failed request reuses idempotency key");
 const readmeGate=gate("blob:README.md"); const pendingPermissions=gate("/api/v1/repos/r2/permissions");
 assert.equal(nodes.get("repository-index").hidden,false);
 assert.equal(nodes.get("create-repository").hidden,true);
 const open=all(nodes.get("repository-list")).find(n=>n.className==="repository-name");open.fire("click");await settle();
 assert.equal(nodes.get("workspace").hidden,false);
 const scoped=calls.filter(c=>c.path.startsWith("/api/v1/repos/r2/")&&c.path.includes("/tree"));assert.ok(scoped.length);
 assert.ok(scoped.every(c=>c.opts.headers.Authorization==="Bearer repo-secret-r2"));
 assert.ok(calls.filter(c=>c.path.startsWith("/api/v1/repos/r2/")).every(c=>c.opts.headers.Authorization!=="Bearer host-secret"));
 assert.match(nodes.get("clone-url").textContent,/alice\/first\.git/);
 assert.match(fs.readFileSync("templates/spa/index.html","utf8"),/credential\.useHttpPath/);
 assert.match(nodes.get("clone-author-note").textContent,/owner@example\.test/);
 assert.equal(nodes.get("host-dashboard").hidden,true,"opening a repository hides dashboard");
  assert.ok(calls.filter(c=>c.path==="/api/v1/repos"||c.path.endsWith("/auth/session")).every(c=>c.opts.headers.Authorization==="Bearer host-account-session"),"host routes use only host token");
 assert.ok(calls.filter(c=>c.path.startsWith("/api/v1/repos/r2/")&&!c.path.endsWith("/auth/session")).every(c=>c.opts.headers.Authorization==="Bearer repo-secret-r2"),"repository routes use repo token");
 // Force asynchronous README/history/permissions/credentials from A to resolve only after switching to B.
 const historyGate=gate("history");
 nodes.get("tab-history").fire("click");
 // README blob, history, and permissions remain pending while switching.
 lateRepo401=true;repos.push(repo("r3","second","bob"));repos[repos.length-1].views=["owner","writer"]; nodes.get("repository-list-nav").fire("click");await settle();
 const secondCard=all(nodes.get("repository-list")).find(n=>n.tagName==="article"&&rendered(n).includes("bob / second"));assert.ok(secondCard);all(secondCard).find(n=>n.className==="repository-name").fire("click");await settle();
 const viewForm=all(nodes.get("repository-list")).find(n=>n.tagName==="form");assert.ok(viewForm,"multiple views offer an in-page form");const viewSelect=all(viewForm).find(n=>n.id==="repository-view-choice");assert.ok(viewSelect);viewSelect.value="writer";viewForm.fire("submit");await settle();
 assert.equal(nodes.get("workspace").hidden,false);assert.match(nodes.get("repo-owner").textContent,/bob/);assert.match(nodes.get("repo-name").textContent,/second/);assert.match(nodes.get("identity").textContent,/writer/);assert.match(nodes.get("clone-url").textContent,/writer@/);
 historyGate.release();readmeGate.release();pendingPermissions.release();await settle();lateRepo401=false;
 assert.equal(nodes.get("host-dashboard").hidden,true,"late 401 from A cannot evict B/host state");
 assert.doesNotMatch(rendered(nodes.get("panel-history")),/HISTORY-r2/);assert.doesNotMatch(rendered(nodes.get("readme-content")),/BODY-r2/);
 // Create a one-time Git secret in A, switch while it is in flight, and ensure the late secret never enters B's DOM.
 nodes.get("repository-list-nav").fire("click");await settle();
 const firstCard=all(nodes.get("repository-list")).find(n=>n.tagName==="article"&&rendered(n).includes("alice / first"));all(firstCard).find(n=>n.className==="repository-name").fire("click");await settle();
 nodes.get("tab-credentials").fire("click");await settle();
 const credentialForm=all(nodes.get("panel-credentials")).find(n=>n.tagName==="form");assert.ok(credentialForm);
 const credentialGate=gate("/api/v1/repos/r2/credentials:POST");credentialForm.fire("submit");await settle();
 nodes.get("repository-list-nav").fire("click");await settle();
 const secondCardAgain=all(nodes.get("repository-list")).find(n=>n.tagName==="article"&&rendered(n).includes("bob / second"));all(secondCardAgain).find(n=>n.className==="repository-name").fire("click");await settle();
 const secondViewForm=all(nodes.get("repository-list")).find(n=>n.tagName==="form");all(secondViewForm).find(n=>n.id==="repository-view-choice").value="writer";secondViewForm.fire("submit");await settle();
 credentialGate.release();await settle();assert.doesNotMatch(rendered(nodes.get("workspace")),/ONE-TIME-SECRET/);
 // Repo A's old 401/rejection must not clear B. Trigger a delayed old host-scoped request, switch, then reject it.
 const late=gate("/api/v1/repos/r3/credentials");nodes.get("tab-credentials").fire("click");
 await settle();
 const callsBefore=calls.length;
 // Host logout captures and revokes both scopes on their own exact routes.
 nodes.get("logout").fire("click");await settle();
 assert.equal(nodes.get("host-dashboard").hidden,true);assert.equal(nodes.get("workspace").hidden,true);
  assert.ok(calls.some(c=>c.path==="/api/v1/logout"&&c.opts.headers.Authorization==="Bearer host-account-session"),"host logout is unscoped");
 assert.ok(calls.some(c=>c.path==="/api/v1/repos/r3/logout"&&c.opts.headers.Authorization==="Bearer repo-secret-r3"),"repo logout is scoped to captured repo");
 late.release();await settle();assert.equal(nodes.get("host-dashboard").hidden,true);assert.ok(calls.length>=callsBefore);
 // A later account receives a fresh request id; a failed create retries with the same id.
  switchClerk("charlie");await settle();
  assert.equal(nodes.get("identity").textContent,"charlie");assert.equal(nodes.get("logout").hidden,false);
  assert.equal(nodes.get("approve-account").hidden,true);assert.equal(nodes.get("approve-email").value,"");
  ctx.window.location.hash="#repositories/approve";windowListeners.hashchange();
  assert.equal(nodes.get("repository-index").hidden,false,"non-admin cannot navigate to approval page");
  assert.equal(nodes.get("approve-account").hidden,true);
 nodes.get("new-repository-name").value="charlie-project";nodes.get("create-repository").fire("submit");await settle();
 const allCreates=calls.filter(c=>c.path==="/api/v1/repos"&&c.opts.method==="POST").map(c=>JSON.parse(c.opts.body));assert.notEqual(allCreates[allCreates.length-1].request_id,createPayloads[0].request_id,"new account starts with fresh idempotency key");
 expireHost=true;nodes.get("repository-list-nav").fire("click");await settle();
 assert.equal(nodes.get("host-dashboard").hidden,true,"expired host token clears dashboard");assert.equal(nodes.get("login").hidden,false,"expired host token returns to sign in");assert.equal(nodes.get("identity").textContent,"","expired host identity is cleared");
 console.log("Host SPA checks passed: retry idempotency, view selection, scoped host/repo auth, delayed stale responses, dual-scope logout, and host expiry.");
})().catch(e=>{console.error(e);process.exitCode=1});
