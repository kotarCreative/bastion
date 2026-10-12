/* Authentication generation and late exchange checks. Run with node tests/test_spa_auth_races.js. */
"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Node{constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.attributes={};this.dataset={};this.hidden=false;this.value="";this.textContent=""}get firstChild(){return this.children[0]}get lastChild(){return this.children.at(-1)}appendChild(n){this.children.push(n);if(this.tagName==="select"&&this.children.length===1)this.value=n.value;return n}prepend(n){this.children.unshift(n)}removeChild(n){this.children.splice(this.children.indexOf(n),1)}setAttribute(k,v){this.attributes[k]=v}addEventListener(k,f){this.listeners[k]=f}fire(k){if(this.listeners[k])this.listeners[k]({preventDefault(){},currentTarget:this,target:this})}scrollIntoView(){}}
const nodes=new Map();for(const m of fs.readFileSync("templates/spa/index.html","utf8").matchAll(/id="([^"]+)"/g)){const n=new Node("div");n.id=m[1];nodes.set(n.id,n)}
const tabs=[...nodes.values()].filter(n=>n.id.startsWith("tab-")),panels=[...nodes.values()].filter(n=>n.id.startsWith("panel-"));tabs.forEach(n=>n.dataset.panel=n.id.slice(4));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}};
const calls=[];let listener;let clerkSession;let exchangeGate=null;const manualGates=new Map();let repoGate=null;
const repo={repo_id:"r1",owner_name:"alice",name:"one",branch:"main",git_url:"https://bastion.test/alice/one.git",views:["owner"]};
const clerk={get session(){return clerkSession},get isSignedIn(){return Boolean(clerkSession)},load:()=>Promise.resolve(),addListener:f=>{listener=f},mountUserButton(){},mountSignIn(){},openUserProfile(){},signOut(){clerkSession=null;if(listener)listener({session:null});return Promise.resolve()}};
function response(status,body){return{status,json:async()=>body}}
const context={document:{getElementById:id=>nodes.get(id),createElement:t=>new Node(t),querySelectorAll:s=>s===".tab"?tabs:panels,head:{appendChild(n){n.fire("load")}}},window:{location:{protocol:"https:",host:"bastion.test",href:"https://bastion.test/"},Clerk:clerk,scrollTo(){},setTimeout,clearTimeout,crypto:{randomUUID:()=>"uuid"}},URL,TextEncoder,btoa:s=>Buffer.from(s,"binary").toString("base64"),Intl,Date,
fetch:async(path,opts={})=>{calls.push({path,opts});const method=opts.method||"GET";
 if(path==="/api/v1/auth/config")return response(200,{enabled:true,mode:"host",frontend_api_origin:"https://clerk.test",publishable_key:"pk_test"});
 if(path==="/api/v1/auth/clerk"){if(exchangeGate)return exchangeGate.promise;return response(200,{mode:"host",access_token:"host-late",account:{account_id:"acct",owner_name:"alice"}})}
 if(path==="/api/v1/session"){const token=(opts.headers||{}).Authorization||"";if(manualGates.has(token))return manualGates.get(token).promise;return response(200,{mode:"host",account:{account_id:"acct",owner_name:"alice"}})}
 if(path==="/api/v1/repos")return response(200,{repos:[repo]});
 if(path==="/api/v1/repos/r1/auth/session"){if(repoGate)return repoGate.promise;return response(200,{access_token:"ui-late",session:{repo,principal:{id:"owner",kind:"user",emails:["owner@example.test"]},view_id:"owner",team_ids:[],role_ids:[],descriptor:{projected_ref:"refs/heads/main",generation:1,projection_epoch:1,capabilities:{read:[]},restrictions:{read:[]}}}})}
 if(path.endsWith("/logout"))return response(200,{revoked:true});
 if(path.includes("/tree?"))return response(200,{projected_tip:"tip",entries:[],next_cursor:null});
 if(path.endsWith("/permissions"))return response(200,{targets:[],account_picker_enabled:false});
 if(path.endsWith("/credentials"))return response(200,{views:[],tokens:[]});
 throw Error("Unexpected "+path+" "+method)
}};
vm.runInNewContext(fs.readFileSync("templates/spa/app.js","utf8"),context);
const settle=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r))};
(async()=>{
 const firstToken=deferred();clerkSession={id:"session-1",user:{id:"user-1"},getToken:()=>firstToken.promise};await settle();
 assert.equal(calls.filter(c=>c.path==="/api/v1/auth/clerk").length,0,"exchange waits for Clerk token");
 clerkSession=null;listener({session:null});firstToken.resolve("jwt-signed-out");await settle();
 assert.equal(calls.filter(c=>c.path==="/api/v1/auth/clerk").length,0,"sign-out invalidates a pending getToken completion");
 assert.equal(nodes.get("host-dashboard").hidden,true,"signed-out exchange cannot resurrect dashboard");

 exchangeGate=deferred();clerkSession={id:"session-2",user:{id:"user-2"},getToken:()=>Promise.resolve("jwt-exchange")};listener({session:clerkSession});await settle();
 const exchange=calls.find(c=>c.path==="/api/v1/auth/clerk");assert.ok(exchange);assert.equal(exchange.opts.headers.Authorization,"Bearer jwt-exchange","Clerk JWT is sent to the unscoped exchange route");
 clerkSession=null;listener({session:null});exchangeGate.resolve(response(200,{mode:"host",access_token:"late-host",account:{account_id:"acct",owner_name:"late"}}));await settle();
 assert.equal(nodes.get("host-dashboard").hidden,true,"late Clerk exchange cannot recreate login");
 assert.ok(calls.some(c=>c.path==="/api/v1/logout"&&c.opts.headers.Authorization==="Bearer late-host"),"late host exchange token is revoked");

 const manualOld=deferred(),manualNew=deferred();manualGates.set("Bearer old-manual",manualOld);manualGates.set("Bearer new-manual",manualNew);
 nodes.get("token").value="old-manual";nodes.get("login-form").fire("submit");await settle();
 nodes.get("token").value="new-manual";nodes.get("login-form").fire("submit");await settle();
 manualOld.resolve(response(200,{mode:"host",account:{account_id:"old",owner_name:"old"}}));await settle();
 assert.ok(calls.some(c=>c.path==="/api/v1/logout"&&c.opts.headers.Authorization==="Bearer old-manual"),"stale manual host credential is revoked");
 assert.equal(nodes.get("host-dashboard").hidden,true,"stale manual response cannot change active sign-in");
 manualNew.resolve(response(200,{mode:"host",account:{account_id:"new",owner_name:"new"}}));await settle();
 assert.equal(nodes.get("identity").textContent,"new","current manual authentication wins");

 repoGate=deferred();const open=nodes.get("repository-list").children[0].children.find(n=>n.textContent==="Open repository");open.fire("click");await settle();
 nodes.get("logout").fire("click");await settle();
 repoGate.resolve(response(200,{access_token:"unused-ui",session:{repo,principal:{id:"owner",kind:"user",emails:[]},view_id:"owner",team_ids:[],role_ids:[],descriptor:{projected_ref:"refs/heads/main",generation:1,projection_epoch:1,capabilities:{read:[]},restrictions:{read:[]}}}}));await settle();
 assert.ok(calls.some(c=>c.path==="/api/v1/repos/r1/logout"&&c.opts.headers.Authorization==="Bearer unused-ui"),"late repository exchange token is revoked after logout");
  assert.equal(nodes.get("workspace").hidden,true,"late repository exchange cannot restore workspace");
  exchangeGate=null;repoGate=null;
  clerkSession={id:"session-3",user:{id:"user-3"},getToken:()=>Promise.resolve("jwt-third")};listener({session:clerkSession});await settle();
  nodes.get("repository-list").children[0].children.find(n=>n.textContent==="Open repository").fire("click");await settle();
  assert.equal(nodes.get("workspace").hidden,false);
  const swappedAt=calls.length;exchangeGate=deferred();
  clerkSession={id:"session-4",user:{id:"user-4"},getToken:()=>Promise.resolve("jwt-fourth")};listener({session:clerkSession});await settle();
  assert.equal(nodes.get("workspace").hidden,true,"direct Clerk account switch clears the previous repository immediately");
  assert.ok(calls.slice(swappedAt).some(c=>c.path==="/api/v1/logout"&&c.opts.headers.Authorization==="Bearer host-late"));
  assert.ok(calls.slice(swappedAt).some(c=>c.path==="/api/v1/repos/r1/logout"&&c.opts.headers.Authorization==="Bearer ui-late"));
  assert.ok(calls.slice(swappedAt).some(c=>c.path==="/api/v1/auth/clerk"&&c.opts.headers.Authorization==="Bearer jwt-fourth"));
  exchangeGate.resolve(response(200,{mode:"host",access_token:"fourth-host",account:{account_id:"fourth",owner_name:"fourth"}}));await settle();
  assert.equal(nodes.get("identity").textContent,"fourth");
  nodes.get("logout").fire("click");await settle();
  const invalid=deferred();manualGates.set("Bearer invalid-manual",invalid);
  nodes.get("token").value="invalid-manual";nodes.get("login-form").fire("submit");await settle();
  invalid.resolve(response(401,{error:"auth-required"}));await settle();
  assert.equal(nodes.get("login-error").hidden,false,"failed manual login gives visible feedback");
  assert.equal(nodes.get("login-error").textContent,"Sign-in failed.");
  console.log("SPA auth race checks passed: Clerk sign-out/getToken/exchange, manual token supersession, late host/UI token revocation.");
})().catch(e=>{console.error(e);process.exitCode=1});
