/* Dependency-free behavioral checks for the browser application's demo flows.
 * Run with: node tests/test_spa.js
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.dataset = {};
    this.hidden = false;
    this.value = "";
    this.textContent = "";
  }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children[this.children.length - 1]; }
  appendChild(child) {
    this.children.push(child);
    if (this.tagName === "select" && this.children.length === 1) this.value = child.value;
    return child;
  }
  prepend(child) { this.children.unshift(child); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  fire(name) { this.listeners[name]({ preventDefault() {}, currentTarget: this, target: this }); }
  scrollIntoView() {}
}

function descendants(node) {
  return [node].concat(node.children.flatMap(descendants));
}
function text(node) {
  return descendants(node).map(item => item.textContent).join(" ");
}
const nodes = new Map();
const shell = fs.readFileSync("templates/spa/index.html", "utf8");
for (const match of shell.matchAll(/id="([^"]+)"/g)) {
  const node = new Node("div");
  node.id = match[1];
  nodes.set(node.id, node);
}
const panels = [...nodes.values()].filter(node => node.id.startsWith("panel-"));
const tabs = [...nodes.values()].filter(node => node.id.startsWith("tab-"));
tabs.forEach(node => { node.dataset.panel = node.id.slice(4); });
const envelope = {
  principal: { id: "owner", kind: "user" }, view_id: "owner", team_ids: [], role_ids: [],
  descriptor: { projected_ref: "refs/heads/main", generation: 1, projection_epoch: 1,
    capabilities: { read: [{ prefix_display: "/" }] }, restrictions: { read: [] } },
};
const entry = (path, kind) => ({ path_display: path, path_b64: Buffer.from(path).toString("base64url"), kind, size: kind === "blob" ? 12 : null });
const blobs = {
  "LICENSE": "LICENSE_TEXT",
  "src/public/app.js": "APP_JS",
  "src/public/other.js": "OTHER_JS",
};
const delays = {};
const calls = [];
  const credentialCalls = [];
const logoutCalls = [];
const tokens = [];
const context = {
  document: {
    getElementById: id => nodes.get(id),
    createElement: tag => new Node(tag),
    querySelectorAll: selector => selector === ".tab" ? tabs : panels,
  },
  window: { location: { protocol: "https:", host: "demo.test" }, scrollTo() {}, setTimeout, clearTimeout },
  TextEncoder, btoa: value => Buffer.from(value, "binary").toString("base64"), Intl, Date,
  fetch: async (path, options) => {
    const method = (options && options.method) || "GET";
    for (const [needle, ms] of Object.entries(delays)) {
      if (path.includes(needle)) { await new Promise(resolve => setTimeout(resolve, ms)); break; }
    }
    calls.push({ path, options });
    let body;
    let status = 200;
    if (path === "/api/v1/auth/config") body = { enabled: false };
    else if (path === "/api/v1/session") body = envelope;
    else if (path === "/api/v1/logout" && method === "POST") { logoutCalls.push(options); body = { revoked: true }; }
    else if (path === "/api/v1/permissions") {
      if (method === "POST") body = { view_id: "writer", private_paths: [] };
      else body = { targets: [{
        view_id: "writer", principal_id: "writer", emails: ["writer@example.test"],
        readable_paths: [], can_modify: false, private_paths: [],
      }], account_picker_enabled: true };
    }
    else if (path.startsWith("/api/v1/accounts?")) {
      const offset = new URLSearchParams(path.split("?")[1]).get("offset");
      body = offset === "0" ? { accounts: [
        { clerk_user_id: "user_owner", name: "Owner", email: "owner@example.test", already_invited: true },
        { clerk_user_id: "user_writer", name: "Writer", email: "writer@example.test", already_invited: false },
      ], next_offset: 20 } : { accounts: [
        { clerk_user_id: "user_other", name: "Other", email: "other@example.test", already_invited: false },
      ], next_offset: null };
    }
    else if (path === "/api/v1/invitations") {
      const payload = JSON.parse(options.body);
      body = { view_id: payload.principal_id, access: payload.access };
      status = 201;
    } else if (path === "/api/v1/credentials" && method === "POST") {
      const payload = JSON.parse(options.body);
      credentialCalls.push(payload);
      body = { credential: "bst1." + "a".repeat(22) + "." + "b".repeat(43),
        token: { view_id: payload.view_id, scopes: payload.scopes, expires_at: 9999999999 } };
      tokens.push({ token_id: "a".repeat(22), view_id: payload.view_id,
        scopes: payload.scopes, created_at: 1700000000, expires_at: 9999999999, revoked_at: null });
      status = 201;
    } else if (path === "/api/v1/credentials") {
      body = { principal_id: "owner", views: [{ view_id: "owner", can_push: true }],
        tokens: tokens.slice() };
    } else if (path.startsWith("/api/v1/credentials/") && method === "DELETE") {
      const id = decodeURIComponent(path.slice("/api/v1/credentials/".length));
      for (const token of tokens) if (token.token_id === id) token.revoked_at = 1700000000;
      body = { revoked: true, token_id: id };
    } else if (path.startsWith("/api/v1/blob?")) {
      const params = new URLSearchParams(path.split("?")[1]);
      const decoded = Buffer.from(params.get("path_b64") || "", "base64url").toString();
      body = { projected_tip: "1234567890", path_b64: params.get("path_b64"),
        path_display: decoded, size: 12, binary: false, text: blobs[decoded] ?? null };
    } else if (path.startsWith("/api/v1/tree?")) {
      const params = new URLSearchParams(path.split("?")[1]);
      const dir = Buffer.from(params.get("path_b64") || "", "base64url").toString();
      const cursor = params.get("cursor");
      body = { projected_tip: "1234567890", entries: [], next_cursor: null };
      if (!dir && !cursor) { body.entries = [entry("src", "tree")]; body.next_cursor = "page2"; }
      else if (!dir) body.entries = [entry("LICENSE", "blob")];
      else if (dir === "src") body.entries = [entry("src/public", "tree")];
      else if (dir === "src/public") body.entries = [entry("src/public/app.js", "blob"), entry("src/public/other.js", "blob")];
    } else throw new Error("Unexpected request: " + path);
    return { status, json: async () => body };
  },
};
vm.runInNewContext(fs.readFileSync("templates/spa/app.js", "utf8"), context);
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

(async () => {
  await settle();
  nodes.get("token").value = "owner-token";
  nodes.get("login-form").fire("submit");
  await settle();
  const tree = nodes.get("tree-list");
  assert.match(text(tree), /app\.js/);
  assert.match(text(tree), /LICENSE/);
  assert.equal(nodes.get("entry-count").textContent, "5 visible items");
  assert.ok(calls.some(call => call.path.includes("cursor=page2")));
  const folder = descendants(tree).find(node => node.textContent === "src/");
  folder.fire("click");
  assert.equal(folder.attributes["aria-expanded"], "false");
  folder.fire("click");
  assert.equal(folder.attributes["aria-expanded"], "true");
  nodes.get("tree-mode").fire("click");
  await settle();
  assert.doesNotMatch(text(tree), /app\.js/);
  assert.equal(nodes.get("tree-more").hidden, false);

  const permissions = nodes.get("panel-permissions");
  const find = id => descendants(permissions).find(node => node.id === id);
  // The panel holds both the invitation form and the visibility form; select
  // the invitation form by its fields rather than by DOM position.
  const inviteForm = () => descendants(permissions).filter(node => node.tagName === "form")
    .find(node => descendants(node).some(child => child.id === "invite-id"));
  find("invite-source").value = "email";
  find("invite-source").fire("change");
  find("invite-id").value = "reader";
  find("invite-email").value = "reader@example.test";
  find("invite-access").value = "read-only";
  find("invite-paths").value = "src/public\nLICENSE";
  inviteForm().fire("submit");
  await settle();
  const invitation = calls.find(call => call.path === "/api/v1/invitations");
  assert.deepEqual(JSON.parse(invitation.options.body), {
    principal_id: "reader", email: "reader@example.test", access: "read-only", readable_paths: ["src/public", "LICENSE"],
  });
  assert.match(text(permissions), /Invited reader with read-only access/);
  assert.equal(invitation.options.headers.Authorization, "Bearer owner-token");

  assert.equal(find("invite-source").value, "account");
  find("invite-id").value = "writer";
  find("invite-access").value = "contributor";
  find("invite-paths").value = "src/public";
  inviteForm().fire("submit");
  await settle();
  assert.equal(calls.filter(call => call.path === "/api/v1/invitations").length, 1);
  assert.match(text(permissions), /select a Clerk account first/);
  find("invite-search").value = "w";
  find("invite-search").fire("input");
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.equal(calls.filter(call => call.path.startsWith("/api/v1/accounts?")).length, 0);
  find("invite-search").value = "wri";
  find("invite-search").fire("input");
  find("invite-search").value = "writer";
  find("invite-search").fire("input");
  await new Promise(resolve => setTimeout(resolve, 350));
  await settle();
  const searches = calls.filter(call => call.path.startsWith("/api/v1/accounts?"));
  assert.equal(searches.length, 1);
  assert.match(searches[0].path, /query=writer/);
  const options = descendants(permissions).filter(node => node.className === "account-option");
  assert.equal(options[0].disabled, true);
  assert.equal(options[1].disabled, false);
  descendants(permissions).find(node => node.textContent === "Load more accounts").fire("click");
  await settle();
  assert.ok(calls.some(call => call.path.includes("offset=20")));
  assert.match(text(permissions), /Other/);
  options[1].fire("click");
  assert.equal(find("invite-email").value, "writer@example.test");
  assert.equal(find("invite-email").readOnly, true);
  inviteForm().fire("submit");
  await settle();
  const pickerInvitation = calls.filter(call => call.path === "/api/v1/invitations")[1];
  assert.deepEqual(JSON.parse(pickerInvitation.options.body), {
    principal_id: "writer", clerk_user_id: "user_writer", access: "contributor", readable_paths: ["src/public"],
  });
  assert.match(text(permissions), /Invited writer with contributor access/);

  // Selecting an account must always re-derive the identifier from that
  // account. A stale identifier left over from an earlier selection or a
  // typed value must never be submitted with this account's Clerk identity,
  // or the invite provisions the wrong principal id.
  const before = calls.filter(call => call.path === "/api/v1/invitations").length;
  find("invite-source").value = "account";
  find("invite-source").fire("change");
  find("invite-access").value = "read-only";
  find("invite-paths").value = "src/public";
  find("invite-id").value = "stale-identifier";
  find("invite-search").value = "writer";
  find("invite-search").fire("input");
  await new Promise(resolve => setTimeout(resolve, 350));
  await settle();
  const reselect = descendants(permissions).filter(node => node.className === "account-option");
  reselect[1].fire("click");
  assert.equal(find("invite-email").value, "writer@example.test");
  assert.equal(find("invite-id").value, "writer");
  inviteForm().fire("submit");
  await settle();
  const invitations = calls.filter(call => call.path === "/api/v1/invitations");
  assert.equal(invitations.length, before + 1);
  assert.deepEqual(JSON.parse(invitations[invitations.length - 1].options.body), {
    principal_id: "writer", clerk_user_id: "user_writer", access: "read-only", readable_paths: ["src/public"],
  });

  // Switching the invite method must also clear the stale selection.
  find("invite-source").value = "email";
  find("invite-source").fire("change");
  assert.equal(find("invite-email").value, "");
  assert.equal(find("invite-email").readOnly, false);

  // A successful visibility change confirms to the user. The confirmation
  // must survive the panel re-render that follows the update.
  const privacyForm = descendants(permissions).filter(node => node.tagName === "form")
    .find(node => descendants(node).some(child => child.id === "permission-path"));
  descendants(permissions).find(node => node.id === "permission-path").value = "src/public";
  privacyForm.fire("submit");
  await settle();
  assert.match(text(permissions), /Permissions updated\. A new projection is active\./);
  assert.ok(calls.some(call => call.path === "/api/v1/permissions" && call.options.method === "POST"));

  // File preview: a slow earlier click must not overwrite a newer one.
  nodes.get("tree-mode").fire("click");                // back to full tree
  await settle();
  nodes.get("tree-mode").fire("click");                // folder mode
  await settle();
  const navigate = async name => {
    const button = descendants(tree).find(node => node.textContent === name);
    button.fire("click");
    await new Promise(resolve => setTimeout(resolve, 20));
    await settle();
  };
  await navigate("src/");
  await navigate("public/");
  delays[Buffer.from("src/public/app.js").toString("base64url")] = 200;
  const slow = descendants(tree).find(node => node.textContent === "app.js");
  slow.fire("click");
  await new Promise(resolve => setTimeout(resolve, 30));
  const fast = descendants(tree).find(node => node.textContent === "other.js");
  fast.fire("click");
  await new Promise(resolve => setTimeout(resolve, 300));
  await settle();
  delete delays[Buffer.from("src/public/app.js").toString("base64url")];
  assert.match(text(nodes.get("file-viewer")), /src\/public\/other\.js/);
  assert.match(text(nodes.get("file-viewer")), /OTHER_JS/);
  assert.doesNotMatch(text(nodes.get("file-viewer")), /APP_JS/);

  // Tree navigation: a fresh directory load must supersede one in flight, so
  // the breadcrumb can never disagree with the listing shown.
  descendants(nodes.get("repo-breadcrumb")).find(node => node.tagName === "button").fire("click");
  await settle();
  delays[Buffer.from("src").toString("base64url")] = 200;
  descendants(tree).find(node => node.textContent === "src/").fire("click");
  await new Promise(resolve => setTimeout(resolve, 40));
  descendants(nodes.get("repo-breadcrumb")).find(node => node.tagName === "button").fire("click");
  await new Promise(resolve => setTimeout(resolve, 400));
  await settle();
  delete delays[Buffer.from("src").toString("base64url")];
  assert.equal(text(nodes.get("repo-breadcrumb")).trim(), "owner");
  assert.doesNotMatch(text(tree), /public/);

  // Credentials: create a Git token, see the secret once, and revoke it.
  const credentials = nodes.get("panel-credentials");
  const credentialForm = descendants(credentials).find(node => node.tagName === "form");
  assert.ok(credentialForm, "credentials form did not render");
  assert.ok(descendants(credentials).some(node => node.id === "credential-view"));
  assert.ok(descendants(credentials).some(node => node.id === "credential-access"));
  assert.ok(descendants(credentials).some(node => node.id === "credential-expiry"));
  descendants(credentials).find(node => node.id === "credential-access").value = "fetch,push";
  descendants(credentials).find(node => node.id === "credential-expiry").value = "7d";
  credentialForm.fire("submit");
  await settle();
  assert.deepEqual(credentialCalls, [{ view_id: "owner", scopes: ["fetch", "push"], expires_in: "7d" }]);
  assert.match(text(credentials), /shown only once/);
  assert.match(text(credentials), /bst1\./);
  assert.match(text(credentials), /fetch, push/);
  const revoke = descendants(credentials).find(node => node.className === "revoke-credential");
  assert.ok(revoke, "token row did not render");
  revoke.fire("click");
  await settle();
  assert.ok(calls.some(call => call.options.method === "DELETE" && call.path.startsWith("/api/v1/credentials/")));
  assert.match(text(credentials), /Revoked/);

  nodes.get("logout").fire("click");
  await settle();
  assert.equal(logoutCalls.length, 1, "single-root UI token is revoked on sign-out");
  assert.equal(logoutCalls[0].headers.Authorization, "Bearer owner-token");

  console.log("SPA checks passed: tree, scoped invitations, Clerk search debounce, pagination, account selection, visibility confirmation, preview and navigation races, Git credentials.");
})().catch(error => { console.error(error); process.exitCode = 1; });
