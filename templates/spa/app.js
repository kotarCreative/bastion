/* Bastion browser application.
 *
 * The access token lives in one module-local variable (page memory only). It
 * is never written to a cookie, URL, DOM node, or telemetry, and is cleared
 * from the input immediately after use. Untrusted server data is rendered
 * through textContent and attribute setters only.
 */
"use strict";

(function () {
  var credential = null;
  var hostCredential = null;
  var hostAccount = null;
  var hostMode = false;
  var repositories = [];
  var hostRun = 0;
  var selectionRun = 0;
  var createRequestId = null;
  var createPending = false;
  var authGeneration = 0;
  var session = null;
  var clerk = null;
  var clerkHadSession = false;
  var clerkExchangePending = false;
  var clerkExchangeRun = 0;
  var clerkUserButtonMounted = false;
  var historyRendered = false;
  var treeState = { stack: [], entries: [], cursor: null, loading: false };
  var fullTree = true;
  var treeRun = 0;
  var invitationRun = 0;
  // Monotonic run tokens: a late response for a superseded preview is
  // discarded so the newest click always wins.
  var previewRun = 0;
  var readmeRun = 0;

  var ELEMENTS = {
    homepage: document.getElementById("homepage"),
    homeNav: document.getElementById("home-nav"),
    sessionNav: document.getElementById("session-nav"),
    identity: document.getElementById("identity"),
    clerkUserButton: document.getElementById("clerk-user-button"),
    manageAccount: document.getElementById("manage-account"),
    logout: document.getElementById("logout"),
    login: document.getElementById("login"),
    loginForm: document.getElementById("login-form"),
    tokenInput: document.getElementById("token"),
    loginError: document.getElementById("login-error"),
    clerkAuth: document.getElementById("clerk-auth"),
    clerkSignIn: document.getElementById("clerk-sign-in"),
    clerkViewForm: document.getElementById("clerk-view-form"),
    clerkView: document.getElementById("clerk-view"),
    sessionNote: document.getElementById("session-note"),
    workspace: document.getElementById("workspace"),
    repoOwner: document.getElementById("repo-owner"),
    repoName: document.getElementById("repo-name"),
    branchName: document.getElementById("branch-name"),
    tipLabel: document.getElementById("tip-label"),
    treeList: document.getElementById("tree-list"),
    treeMore: document.getElementById("tree-more"),
    breadcrumb: document.getElementById("repo-breadcrumb"),
    entryCount: document.getElementById("entry-count"),
    fileViewer: document.getElementById("file-viewer"),
    readmePanel: document.getElementById("readme-panel"),
    readmeName: document.getElementById("readme-name"),
    readmeContent: document.getElementById("readme-content"),
    cloneDialog: document.getElementById("clone-dialog"),
    cloneUrl: document.getElementById("clone-url"),
    cloneAuthorNote: document.getElementById("clone-author-note"),
    hostDashboard: document.getElementById("host-dashboard"),
    repositoryList: document.getElementById("repository-list"),
    hostMessage: document.getElementById("host-message"),
    createForm: document.getElementById("create-repository"),
    createName: document.getElementById("new-repository-name"),
    createError: document.getElementById("create-error"),
    repositoryListNav: document.getElementById("repository-list-nav"),
  };

  function visibleText(value) {
    return String(value == null ? "" : value);
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = visibleText(text);
    return node;
  }

  function textButton(className, text) {
    var node = element("button", className, text);
    node.type = "button";
    return node;
  }

  function request(path, options) {
    options = options || {};
    var route = path;
    var externalAuth = options.skipCredential || path === "/api/v1/auth/clerk";
    var hostRoute = options.host || externalAuth || path === "/api/v1/auth/config" || (path === "/api/v1/session" && !credential);
    var scoped = hostMode && session && credential && !hostRoute && path.indexOf("/api/v1/repos") !== 0;
    var requestRepoId = scoped && session.repo ? session.repo.repo_id : null;
    var requestSelection = selectionRun;
    if (scoped) route = "/api/v1/repos/" + encodeURIComponent(requestRepoId) + path.slice("/api/v1".length);
    var headers = Object.assign({}, options.headers || {});
    var requestCredential = externalAuth ? null : (hostRoute ? hostCredential : (scoped ? credential : (credential && !options.skipCredential ? credential : null)));
    if (requestCredential && !options.skipCredential) {
      headers.Authorization = "Bearer " + requestCredential;
    }
    return fetch(route, {
      method: options.method || "GET",
      headers: headers,
      body: options.body,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    }).then(function (response) {
      if (scoped && (requestSelection !== selectionRun || requestRepoId !== (session && session.repo && session.repo.repo_id) || requestCredential !== credential)) throw new Error("stale-session");
      if (hostRoute && requestCredential && requestCredential !== hostCredential && !options.allowStaleAuthResponse) throw new Error("stale-session");
      if (response.status === 401 && options.allowStaleAuthResponse && requestCredential && requestCredential !== hostCredential) throw new Error("stale-session");
      // Sign-in attempts have their own generation/error handling; an invalid
      // supplied JWT/token must not invalidate a different active session.
      if (response.status === 401 && !options.optionalAuth && !externalAuth) {
        if (requestCredential && requestCredential === hostCredential && (options.host || !scoped)) clearBastionSession();
        else if (requestCredential === credential) {
          if (hostMode) { clearRepositorySession(); if (hostCredential) renderRepositories(); }
          else clearBastionSession();
        }
        throw new Error("auth-required");
      }
      return response.json().then(function (body) {
        if (scoped && (requestSelection !== selectionRun || requestRepoId !== (session && session.repo && session.repo.repo_id) || requestCredential !== credential)) throw new Error("stale-session");
        if (hostRoute && requestCredential && requestCredential !== hostCredential && !options.allowStaleAuthResponse) throw new Error("stale-session");
        return { status: response.status, body: body };
      });
    });
  }

  function formatBytes(value) {
    if (value == null) return "";
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(value < 10240 ? 1 : 0) + " KB";
    return (value / (1024 * 1024)).toFixed(1) + " MB";
  }

  function shortDate(timestamp) {
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric", month: "short", day: "numeric",
    }).format(new Date(timestamp * 1000));
  }

  function initials(value) {
    var words = visibleText(value).trim().split(/\s+/).filter(Boolean);
    return (words[0] ? words[0][0] : "B").toUpperCase();
  }

  function encodePath(text) {
    var raw = new TextEncoder().encode(text);
    var binary = "";
    raw.forEach(function (byte) { binary += String.fromCharCode(byte); });
    return btoa(binary).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  }

  function showPanel(name) {
    document.querySelectorAll(".workspace-panel").forEach(function (panel) {
      panel.hidden = panel.id !== "panel-" + name;
    });
    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.dataset.panel === name ? "true" : "false");
    });
    if (name === "history" && !historyRendered) renderHistory();
  }

  function clearBastionSession() {
    authGeneration += 1;
    hostRun += 1; selectionRun += 1;
    invitationRun += 1;
    previewRun += 1; readmeRun += 1;
    credential = null;
    hostCredential = null;
    hostAccount = null;
    session = null;
    repositories = []; createRequestId = null; createPending = false;
    historyRendered = false;
    treeRun += 1;
    treeState = { stack: [], entries: [], cursor: null, loading: false };
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.login.hidden = false;
    ELEMENTS.homepage.hidden = false;
    ELEMENTS.homeNav.hidden = false;
    ELEMENTS.sessionNav.hidden = true;
    ELEMENTS.workspace.hidden = true;
    ELEMENTS.hostDashboard.hidden = true;
    ELEMENTS.hostMessage.textContent = ""; clearChildren(ELEMENTS.repositoryList); ELEMENTS.createError.textContent = "";
    ELEMENTS.createName.value = "";
    if (ELEMENTS.createForm.querySelector) { var createButton = ELEMENTS.createForm.querySelector("button[type=submit]"); if (createButton) createButton.disabled = false; }
    ELEMENTS.identity.hidden = true;
    ELEMENTS.identity.textContent = "";
    ELEMENTS.repositoryListNav.hidden = true; ELEMENTS.cloneUrl.textContent = ""; ELEMENTS.cloneAuthorNote.textContent = "";
    ELEMENTS.repoOwner.textContent = ""; ELEMENTS.repoName.textContent = ""; ELEMENTS.branchName.textContent = ""; ELEMENTS.tipLabel.textContent = "";
    ELEMENTS.entryCount.textContent = ""; ELEMENTS.readmeName.textContent = "";
    ELEMENTS.sessionNote.textContent = "Account authentication is managed by Clerk.";
    if (ELEMENTS.cloneDialog.open) ELEMENTS.cloneDialog.close();
    ELEMENTS.manageAccount.hidden = true;
    ELEMENTS.logout.hidden = true;
    ELEMENTS.clerkUserButton.hidden = true;
    document.getElementById("tab-permissions").hidden = true;
    ["panel-history", "panel-decision", "panel-credentials", "panel-permissions", "panel-profile"].forEach(function (id) {
      clearChildren(document.getElementById(id));
    });
    clearChildren(ELEMENTS.treeList);
    clearChildren(ELEMENTS.breadcrumb);
    clearChildren(ELEMENTS.fileViewer);
    clearChildren(ELEMENTS.readmeContent);
    ELEMENTS.fileViewer.hidden = true;
    ELEMENTS.readmePanel.hidden = true;
  }

  function clearRepositorySession() {
    selectionRun += 1; invitationRun += 1; treeRun += 1; previewRun += 1; readmeRun += 1;
    credential = null; session = null; historyRendered = false;
    treeState = { stack: [], entries: [], cursor: null, loading: false };
    ELEMENTS.workspace.hidden = true; ELEMENTS.sessionNav.hidden = true;
    ELEMENTS.repositoryListNav.hidden = true; ELEMENTS.cloneUrl.textContent = ""; ELEMENTS.cloneAuthorNote.textContent = "";
    ELEMENTS.repoOwner.textContent = ""; ELEMENTS.repoName.textContent = ""; ELEMENTS.branchName.textContent = ""; ELEMENTS.tipLabel.textContent = "";
    ELEMENTS.entryCount.textContent = ""; ELEMENTS.readmeName.textContent = "";
    ELEMENTS.sessionNote.textContent = "Account authentication is managed by Clerk.";
    document.getElementById("tab-permissions").hidden = true;
    if (ELEMENTS.cloneDialog.open) ELEMENTS.cloneDialog.close();
    ELEMENTS.hostDashboard.hidden = false;
    if (hostCredential) {
      ELEMENTS.sessionNav.hidden = false; ELEMENTS.identity.hidden = false;
      ELEMENTS.identity.textContent = hostAccount ? hostAccount.owner_name : "Account";
      ELEMENTS.logout.hidden = false;
    }
    clearChildren(ELEMENTS.treeList); clearChildren(ELEMENTS.fileViewer); clearChildren(ELEMENTS.readmeContent);
    clearChildren(ELEMENTS.breadcrumb);
    ELEMENTS.fileViewer.hidden = true; ELEMENTS.readmePanel.hidden = true;
    ["panel-history", "panel-decision", "panel-credentials", "panel-permissions", "panel-profile"].forEach(function (id) { clearChildren(document.getElementById(id)); });
  }

  function revokeAndClear(signOutClerk) {
    var hostToken = hostCredential;
    var repoToken = credential;
    var repoId = session && session.repo && session.repo.repo_id;
    if (hostToken) fetch("/api/v1/logout", { method: "POST", headers: { Authorization: "Bearer " + hostToken }, credentials: "omit", cache: "no-store" }).catch(function () {});
    if (repoToken && repoId) fetch("/api/v1/repos/" + encodeURIComponent(repoId) + "/logout", { method: "POST", headers: { Authorization: "Bearer " + repoToken }, credentials: "omit", cache: "no-store" }).catch(function () {});
    else if (repoToken) fetch("/api/v1/logout", { method: "POST", headers: { Authorization: "Bearer " + repoToken }, credentials: "omit", cache: "no-store" }).catch(function () {});
    clearBastionSession();
    if (signOutClerk && clerk) Promise.resolve(clerk.signOut()).catch(function () {});
  }

  function currentClerkIdentity() {
    if (!clerk || !clerk.session) return "";
    return visibleText(clerk.session.id || "") + "|" + visibleText(clerk.session.user && clerk.session.user.id || clerk.user && clerk.user.id || "");
  }

  function revokeLateLogin(result, token, repoId) {
    if (result && result.body && result.body.mode === "host" && result.body.access_token) {
      fetch("/api/v1/logout", { method: "POST", headers: { Authorization: "Bearer " + result.body.access_token }, credentials: "omit", cache: "no-store" }).catch(function () {});
    } else if (result && result.body && result.body.access_token) {
      var path = repoId ? "/api/v1/repos/" + encodeURIComponent(repoId) + "/logout" : "/api/v1/logout";
      fetch(path, { method: "POST", headers: { Authorization: "Bearer " + result.body.access_token }, credentials: "omit", cache: "no-store" }).catch(function () {});
    } else if (token) {
      fetch("/api/v1/logout", { method: "POST", headers: { Authorization: "Bearer " + token }, credentials: "omit", cache: "no-store" }).catch(function () {});
    }
  }

  function renderRepositories() {
    if (!hostCredential) return;
    var run = ++hostRun, selection = selectionRun;
    ELEMENTS.hostDashboard.hidden = false; ELEMENTS.workspace.hidden = true;
    ELEMENTS.homepage.hidden = true; ELEMENTS.login.hidden = true; ELEMENTS.sessionNav.hidden = false;
    ELEMENTS.hostMessage.textContent = "Loading your repositories…"; clearChildren(ELEMENTS.repositoryList);
    request("/api/v1/repos", { host: true }).then(function (result) {
      if (run !== hostRun || selection !== selectionRun || !hostCredential || session) return;
      if (result.status !== 200) throw new Error("repos");
      repositories = result.body.repos || []; ELEMENTS.hostMessage.textContent = repositories.length ? "Choose a repository to continue." : "No repositories yet. Create your first repository.";
      repositories.forEach(function (repo) {
        var card = element("article", "repository-card");
        card.appendChild(element("h3", null, repo.owner_name + " / " + repo.name));
        card.appendChild(element("p", "hint", repo.branch || "main"));
        var open = textButton("primary-button", "Open repository"); open.addEventListener("click", function () { openHostedRepository(repo); });
        card.appendChild(open); ELEMENTS.repositoryList.appendChild(card);
      });
    }).catch(function () { if (run === hostRun && selection === selectionRun && hostCredential && !session) {
      ELEMENTS.hostMessage.textContent = "Repositories are unavailable. ";
      var retry = textButton("quiet-button", "Retry"); retry.addEventListener("click", renderRepositories); ELEMENTS.hostMessage.appendChild(retry);
    } });
  }

  function showViewSelection(repo, viewIds, run) {
    if (run !== selectionRun) return;
    ELEMENTS.hostDashboard.hidden = false; ELEMENTS.workspace.hidden = true; ELEMENTS.sessionNav.hidden = false;
    ELEMENTS.hostMessage.textContent = "Select a view for " + repo.owner_name + " / " + repo.name + ".";
    var form = element("form", "view-selection");
    var select = element("select"); select.id = "repository-view-choice";
    viewIds.forEach(function (viewId) { var option = element("option", null, viewId); option.value = viewId; select.appendChild(option); });
    var label = element("label", null, "Repository view"); label.setAttribute("for", select.id);
    var submit = element("button", "primary-button", "Open selected view"); submit.type = "submit";
    form.appendChild(label); form.appendChild(select); form.appendChild(submit);
    form.addEventListener("submit", function (event) { event.preventDefault(); if (run === selectionRun) openHostedRepository(repo, select.value); });
    clearChildren(ELEMENTS.repositoryList); ELEMENTS.repositoryList.appendChild(form);
  }

  function openHostedRepository(repo, viewId) {
    var old = credential;
    var previousRepoId = session && session.repo && session.repo.repo_id;
    if (old && previousRepoId) fetch("/api/v1/repos/" + encodeURIComponent(previousRepoId) + "/logout", { method: "POST", headers: { Authorization: "Bearer " + old }, cache: "no-store", credentials: "omit" }).catch(function () {});
    clearRepositorySession();
    var run = ++selectionRun;
    var body = {}; if (viewId) body.view_id = viewId;
    request("/api/v1/repos/" + encodeURIComponent(repo.repo_id) + "/auth/session", { host: true, allowStaleAuthResponse: true, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (result) {
        if (run !== selectionRun || !hostCredential) {
          if (result.status === 200 && result.body && result.body.access_token) fetch("/api/v1/repos/" + encodeURIComponent(repo.repo_id) + "/logout", { method: "POST", headers: { Authorization: "Bearer " + result.body.access_token }, cache: "no-store", credentials: "omit" }).catch(function () {});
          return;
        }
        if (result.status === 409 && Array.isArray(result.body.view_ids)) {
          showViewSelection(repo, result.body.view_ids, run);
          return;
        }
        if (result.status !== 200) throw new Error("open");
        if (run !== selectionRun || !hostCredential) {
          fetch("/api/v1/repos/" + encodeURIComponent(repo.repo_id) + "/logout", { method: "POST", headers: { Authorization: "Bearer " + result.body.access_token }, cache: "no-store", credentials: "omit" }).catch(function () {});
          return;
        }
        ELEMENTS.repositoryListNav.hidden = false;
        openSession(result.body.access_token, result.body.session);
      }).catch(function () { if (run === selectionRun) { ELEMENTS.hostDashboard.hidden = false; ELEMENTS.sessionNav.hidden = false; ELEMENTS.hostMessage.textContent = "Repository could not be opened. "; var retry = textButton("quiet-button", "Retry"); retry.addEventListener("click", function () { openHostedRepository(repo, viewId); }); ELEMENTS.hostMessage.appendChild(retry); } });
  }

  function prefixList(prefixes, emptyText) {
    var list = element("ul", "path-list");
    if (!prefixes || prefixes.length === 0) {
      list.appendChild(element("li", null, emptyText || "None"));
      return list;
    }
    prefixes.forEach(function (prefix) {
      list.appendChild(element("li", null, prefix.prefix_display || "/"));
    });
    return list;
  }

  function definitionList(rows) {
    var definition = element("dl", "definition-list");
    rows.forEach(function (row) {
      definition.appendChild(element("dt", null, row[0]));
      definition.appendChild(element("dd", null, row[1]));
    });
    return definition;
  }

  function currentDirectory() {
    return treeState.stack.length ? treeState.stack[treeState.stack.length - 1] : { path_b64: "", path_display: "" };
  }

  function renderBreadcrumb() {
    clearChildren(ELEMENTS.breadcrumb);
    var root = textButton("breadcrumb-button", session.view_id);
    root.addEventListener("click", function () {
      treeState.stack = [];
      loadDirectory(true);
    });
    ELEMENTS.breadcrumb.appendChild(root);
    treeState.stack.forEach(function (entry, index) {
      ELEMENTS.breadcrumb.appendChild(element("span", "breadcrumb-separator", "/"));
      var name = entry.path_display.split("/").pop();
      if (index === treeState.stack.length - 1) {
        ELEMENTS.breadcrumb.appendChild(element("span", "breadcrumb-current", name));
      } else {
        var crumb = textButton("breadcrumb-button", name);
        crumb.addEventListener("click", function () {
          treeState.stack = treeState.stack.slice(0, index + 1);
          loadDirectory(true);
        });
        ELEMENTS.breadcrumb.appendChild(crumb);
      }
    });
  }

  function renderTreeRows() {
    clearChildren(ELEMENTS.treeList);
    if (treeState.entries.length === 0) {
      ELEMENTS.treeList.appendChild(element("div", "tree-empty", "This directory has no visible files."));
      return;
    }
    treeState.entries.forEach(function (entry) {
      var row = element("div", "tree-row");
      var iconSlot = element("span", "entry-icon");
      iconSlot.appendChild(element("span", entry.kind === "tree" ? "file-glyph folder-glyph" : "file-glyph"));
      var name = entry.path_display.split("/").pop();
      var nameButton = textButton("entry-name", name + (entry.kind === "tree" ? "/" : ""));
      nameButton.addEventListener("click", function () {
        if (entry.kind === "tree") {
          treeState.stack.push(entry);
          loadDirectory(true);
        } else {
          openFile(entry);
        }
      });
      row.appendChild(iconSlot);
      row.appendChild(nameButton);
      row.appendChild(element("span", "entry-kind", entry.kind === "tree" ? "Directory" : "File"));
      row.appendChild(element("span", "entry-size", formatBytes(entry.size)));
      ELEMENTS.treeList.appendChild(row);
    });
  }

  function readmeEntry(entries) {
    var names = ["readme.md", "readme.markdown", "readme.txt", "readme"];
    for (var index = 0; index < names.length; index += 1) {
      var match = entries.find(function (entry) {
        return entry.kind === "blob" && entry.path_display.split("/").pop().toLowerCase() === names[index];
      });
      if (match) return match;
    }
    return null;
  }

  function loadDirectory(reset) {
    if (fullTree) { loadFullTree(); return; }
    // A fresh directory load (navigation) supersedes any request in flight:
    // the stale response is discarded by the treeRun guard below. Only a
    // pagination load waits, so pages append to the list in order.
    if (!reset && treeState.loading) return;
    if (reset) {
      treeState.entries = [];
      treeState.cursor = null;
      ELEMENTS.fileViewer.hidden = true;
      ELEMENTS.readmePanel.hidden = true;
      ELEMENTS.treeMore.hidden = true;
      clearChildren(ELEMENTS.treeList);
      ELEMENTS.treeList.appendChild(element("div", "tree-loading", "Loading visible files…"));
    }
    treeState.loading = true;
    var run = ++treeRun;
    var token = credential;
    var directory = currentDirectory();
    var query = "limit=50";
    if (directory.path_b64) query += "&path_b64=" + encodeURIComponent(directory.path_b64);
    if (treeState.cursor) query += "&cursor=" + encodeURIComponent(treeState.cursor);
    request("/api/v1/tree?" + query).then(function (result) {
      if (run !== treeRun || token !== credential) return;
      if (result.status !== 200) throw new Error("tree-unavailable");
      treeState.entries = treeState.entries.concat(result.body.entries);
      treeState.cursor = result.body.next_cursor;
      ELEMENTS.tipLabel.textContent = result.body.projected_tip.slice(0, 8);
      ELEMENTS.entryCount.textContent = treeState.entries.length + (treeState.entries.length === 1 ? " item" : " items");
      ELEMENTS.treeMore.hidden = !treeState.cursor;
      renderBreadcrumb();
      renderTreeRows();
      var readme = readmeEntry(treeState.entries);
      if (readme) loadReadme(readme);
    }).catch(function (error) {
      if (run !== treeRun || token !== credential) return;
      if (error && error.message === "auth-required") return;
      clearChildren(ELEMENTS.treeList);
      ELEMENTS.treeList.appendChild(element("div", "tree-empty", "This repository view is temporarily unavailable."));
      ELEMENTS.treeMore.hidden = true;
    }).then(function () {
      if (run === treeRun && token === credential) treeState.loading = false;
    });
  }

  function loadFullTree() {
    var run = ++treeRun;
    var token = credential;
    var tip = null;
    var count = 0;
    clearChildren(ELEMENTS.treeList);
    clearChildren(ELEMENTS.breadcrumb);
    ELEMENTS.breadcrumb.appendChild(element("span", "breadcrumb-current", session.view_id + " / Full file structure"));
    ELEMENTS.treeMore.hidden = true;
    ELEMENTS.fileViewer.hidden = true;
    ELEMENTS.readmePanel.hidden = true;
    ELEMENTS.entryCount.textContent = "Loading…";
    var status = element("div", "tree-loading", "Loading your visible file structure…");
    ELEMENTS.treeList.appendChild(status);
    var container = element("div", "full-tree");
    ELEMENTS.treeList.appendChild(container);

    function active() { return run === treeRun && token === credential; }
    function directory(path, parent, cursor) {
      if (!active()) return Promise.resolve();
      var query = "limit=100";
      if (path) query += "&path_b64=" + encodeURIComponent(path);
      if (cursor) query += "&cursor=" + encodeURIComponent(cursor);
      return request("/api/v1/tree?" + query).then(function (result) {
        if (!active()) return;
        if (result.status !== 200) throw new Error("tree-unavailable");
        if (tip && tip !== result.body.projected_tip) throw new Error("tree-changed");
        tip = result.body.projected_tip;
        ELEMENTS.tipLabel.textContent = tip.slice(0, 8);
        var chain = Promise.resolve();
        result.body.entries.forEach(function (entry) {
          chain = chain.then(function () {
            if (!active()) return;
            var row = element("div", "tree-row");
            var icon = element("span", "entry-icon");
            icon.appendChild(element("span", entry.kind === "tree" ? "file-glyph folder-glyph" : "file-glyph"));
            var name = entry.path_display.split("/").pop();
            var button = textButton("entry-name", name + (entry.kind === "tree" ? "/" : ""));
            row.appendChild(icon);
            row.appendChild(button);
            row.appendChild(element("span", "entry-kind", entry.kind === "tree" ? "Directory" : "File"));
            row.appendChild(element("span", "entry-size", formatBytes(entry.size)));
            parent.appendChild(row);
            count += 1;
            ELEMENTS.entryCount.textContent = count + " visible items";
            if (entry.kind === "tree") {
              var children = element("div", "tree-children");
              parent.appendChild(children);
              button.setAttribute("aria-expanded", "true");
              button.addEventListener("click", function () {
                children.hidden = !children.hidden;
                button.setAttribute("aria-expanded", String(!children.hidden));
              });
              return directory(entry.path_b64, children, null);
            }
            button.addEventListener("click", function () { openFile(entry); });
            if (!path && readmeEntry([entry])) loadReadme(entry);
          });
        });
        return chain.then(function () {
          if (result.body.next_cursor) return directory(path, parent, result.body.next_cursor);
        });
      });
    }
    directory("", container, null).then(function () {
      if (!active()) return;
      status.hidden = count > 0;
      if (!count) status.textContent = "This view has no visible files.";
    }).catch(function (error) {
      if (!active()) return;
      clearChildren(container);
      ELEMENTS.entryCount.textContent = "";
      status.textContent = error.message === "tree-changed" ? "The repository changed. Refresh the view to reload the structure." : "The file structure is unavailable. Refresh to try again.";
    });
  }

  function blobRequest(entry) {
    return request("/api/v1/blob?path_b64=" + encodeURIComponent(entry.path_b64));
  }

  function openFile(entry) {
    var run = ++previewRun;
    clearChildren(ELEMENTS.fileViewer);
    ELEMENTS.fileViewer.hidden = false;
    var header = element("header");
    header.appendChild(element("span", "document-icon"));
    header.appendChild(element("strong", null, entry.path_display));
    header.appendChild(element("span", "file-meta", formatBytes(entry.size)));
    var close = textButton("icon-button", "×");
    close.setAttribute("aria-label", "Close file preview");
    close.addEventListener("click", function () { ELEMENTS.fileViewer.hidden = true; });
    header.appendChild(close);
    ELEMENTS.fileViewer.appendChild(header);
    ELEMENTS.fileViewer.appendChild(element("div", "tree-loading", "Loading file…"));
    blobRequest(entry).then(function (result) {
      if (run !== previewRun) return;
      while (ELEMENTS.fileViewer.children.length > 1) ELEMENTS.fileViewer.removeChild(ELEMENTS.fileViewer.lastChild);
      if (result.status !== 200) {
        ELEMENTS.fileViewer.appendChild(element("div", "binary-message", "This file cannot be previewed."));
      } else if (result.body.binary) {
        ELEMENTS.fileViewer.appendChild(element("div", "binary-message", "Binary files are available when you clone this repository, but are not shown in the browser."));
      } else {
        ELEMENTS.fileViewer.appendChild(element("pre", "code-preview", result.body.text));
      }
      ELEMENTS.fileViewer.scrollIntoView({ behavior: "smooth", block: "start" });
    }).catch(function () {
      if (run !== previewRun) return;
      while (ELEMENTS.fileViewer.children.length > 1) ELEMENTS.fileViewer.removeChild(ELEMENTS.fileViewer.lastChild);
      ELEMENTS.fileViewer.appendChild(element("div", "binary-message", "This file cannot be previewed."));
    });
  }

  function appendMarkdown(container, text) {
    var lines = visibleText(text).replace(/\r\n/g, "\n").split("\n");
    var index = 0;
    while (index < lines.length) {
      var line = lines[index];
      if (line.indexOf("```") === 0) {
        var code = [];
        index += 1;
        while (index < lines.length && lines[index].indexOf("```") !== 0) {
          code.push(lines[index]);
          index += 1;
        }
        container.appendChild(element("pre", null, code.join("\n")));
      } else if (/^###\s+/.test(line)) {
        container.appendChild(element("h3", null, line.replace(/^###\s+/, "")));
      } else if (/^##\s+/.test(line)) {
        container.appendChild(element("h2", null, line.replace(/^##\s+/, "")));
      } else if (/^#\s+/.test(line)) {
        container.appendChild(element("h1", null, line.replace(/^#\s+/, "")));
      } else if (/^[-*]\s+/.test(line)) {
        var list = element("ul");
        while (index < lines.length && /^[-*]\s+/.test(lines[index])) {
          list.appendChild(element("li", null, lines[index].replace(/^[-*]\s+/, "")));
          index += 1;
        }
        container.appendChild(list);
        continue;
      } else if (line.trim()) {
        var paragraph = [line];
        while (index + 1 < lines.length && lines[index + 1].trim() && !/^(#{1,3}|[-*]\s|```)/.test(lines[index + 1])) {
          index += 1;
          paragraph.push(lines[index]);
        }
        container.appendChild(element("p", null, paragraph.join("\n")));
      }
      index += 1;
    }
  }

  function loadReadme(entry) {
    var run = ++readmeRun;
    ELEMENTS.readmeName.textContent = entry.path_display.split("/").pop();
    clearChildren(ELEMENTS.readmeContent);
    ELEMENTS.readmeContent.appendChild(element("p", "hint", "Loading documentation…"));
    ELEMENTS.readmePanel.hidden = false;
    blobRequest(entry).then(function (result) {
      if (run !== readmeRun) return;
      if (result.status !== 200 || result.body.binary) {
        ELEMENTS.readmePanel.hidden = true;
        return;
      }
      clearChildren(ELEMENTS.readmeContent);
      appendMarkdown(ELEMENTS.readmeContent, result.body.text);
    }).catch(function () {
      if (run !== readmeRun) return;
      ELEMENTS.readmePanel.hidden = true;
    });
  }

  function renderHistory() {
    var version = selectionRun, token = credential, repoId = session && session.repo && session.repo.repo_id;
    function active() { return version === selectionRun && token === credential && (!repoId || (session && session.repo && session.repo.repo_id === repoId)); }
    var panel = document.getElementById("panel-history");
    clearChildren(panel);
    var shell = element("div", "history-shell");
    var heading = element("div", "panel-heading");
    heading.appendChild(element("h2", null, "Visible history"));
    heading.appendChild(element("p", null, "Commits are drawn from your projected repository and contain only history visible to this view."));
    var list = element("div");
    var more = textButton("load-more", "Load more commits");
    var cursor = null;
    shell.appendChild(heading);
    shell.appendChild(list);
    shell.appendChild(more);
    panel.appendChild(shell);

    function load() {
      more.disabled = true;
      var query = "limit=25" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
      request("/api/v1/history?" + query).then(function (result) {
        if (!active()) return;
        if (result.status !== 200) throw new Error("history-unavailable");
        // Mark rendered only after a page succeeds, so a transient failure
        // leaves the tab retryable on the next visit instead of stuck on the
        // error message for the rest of the session.
        historyRendered = true;
        result.body.commits.forEach(function (commit) {
          var row = element("div", "commit");
          row.appendChild(element("span", "commit-avatar", initials(commit.author_name_display)));
          var copy = element("div");
          copy.appendChild(element("p", "commit-message", commit.message_display.split("\n")[0] || "Untitled commit"));
          copy.appendChild(element("span", "commit-meta", commit.author_name_display + " committed on " + shortDate(commit.committed_at)));
          row.appendChild(copy);
          row.appendChild(element("code", null, commit.oid.slice(0, 7)));
          list.appendChild(row);
        });
        cursor = result.body.next_cursor;
        more.hidden = !cursor;
        more.disabled = false;
        if (!list.firstChild) list.appendChild(element("div", "tree-empty", "No visible commits."));
      }).catch(function () {
        if (!active()) return;
        more.hidden = true;
        list.appendChild(element("div", "tree-empty", "History is temporarily unavailable."));
      });
    }
    more.addEventListener("click", load);
    load();
  }

  function renderDecision() {
    var token = credential, version = selectionRun, repoId = session && session.repo && session.repo.repo_id;
    function active() { return token === credential && version === selectionRun && (!repoId || (session && session.repo && session.repo.repo_id === repoId)); }
    var panel = document.getElementById("panel-decision");
    clearChildren(panel);
    var card = element("div", "inspector-card");
    card.appendChild(element("h2", null, "Access inspector"));
    card.appendChild(element("p", "hint", "Evaluate the active policy for a repository-relative path. This explains permission decisions without exposing hidden content."));
    var form = element("form");
    form.setAttribute("autocomplete", "off");
    var pathLabel = element("label", null, "Path");
    pathLabel.setAttribute("for", "decision-path");
    var path = element("input");
    path.id = "decision-path";
    path.placeholder = "src/service/handler.py";
    var capabilityLabel = element("label", null, "Capability");
    capabilityLabel.setAttribute("for", "decision-capability");
    var capability = element("select");
    capability.id = "decision-capability";
    ["read", "create", "modify", "delete", "mode", "symlink", "branch", "merge", "admin", "tag"].forEach(function (name) {
      var option = element("option", null, name);
      option.value = name;
      capability.appendChild(option);
    });
    var submit = element("button", null, "Evaluate access");
    submit.type = "submit";
    var outcome = element("div");
    form.appendChild(pathLabel);
    form.appendChild(path);
    form.appendChild(capabilityLabel);
    form.appendChild(capability);
    form.appendChild(submit);
    card.appendChild(form);
    card.appendChild(outcome);
    panel.appendChild(card);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      clearChildren(outcome);
      var normalized = path.value.replace(/^\/+/, "");
      request("/api/v1/decisions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capability: capability.value, path_b64: encodePath(normalized) }),
      }).then(function (result) {
        if (!active()) return;
        if (result.status !== 200) throw new Error("decision-failed");
        outcome.appendChild(element("p", result.body.allowed ? "verdict-allow" : "verdict-deny", result.body.allowed ? "Allowed" : "Denied"));
        outcome.appendChild(definitionList([
          ["Longest matching grant", result.body.longest_allow ? result.body.longest_allow.prefix_display || "/" : "No match"],
          ["Longest matching restriction", result.body.longest_deny ? result.body.longest_deny.prefix_display || "/" : "No match"],
        ]));
      }).catch(function () {
        if (!active()) return;
        outcome.appendChild(element("p", "error", "The decision could not be evaluated."));
      });
    });
  }

  function renderPermissions(initialBody) {
    var invitationVersion = ++invitationRun;
    var contextToken = credential, contextSelection = selectionRun, contextRepo = session && session.repo && session.repo.repo_id;
    function activeContext() { return contextToken === credential && contextSelection === selectionRun && (!contextRepo || (session && session.repo && session.repo.repo_id === contextRepo)); }
    var panel = document.getElementById("panel-permissions");
    clearChildren(panel);
    var card = element("div", "permission-card");
    card.appendChild(element("h2", null, "Repository permissions"));
    card.appendChild(element("p", "hint", "Make a file or directory private for a collaborator, or restore inherited visibility. Changes publish a new projection epoch."));
    var content = element("div");
    card.appendChild(content);
    panel.appendChild(card);

    function populate(body) {
      if (invitationVersion !== invitationRun) return;
      renderInvitation(card, body.account_picker_enabled, invitationVersion);
      if (!body.targets.length) {
        content.appendChild(element("p", "hint", "This repository has no other collaborators."));
        return;
      }
      var form = element("form");
      var targetLabel = element("label", null, "Collaborator view");
      var target = element("select");
      targetLabel.setAttribute("for", "permission-target");
      target.id = "permission-target";
      body.targets.forEach(function (item) {
        var option = element("option", null, item.principal_id + " / " + item.view_id + (item.can_modify ? " · Contributor" : " · Read-only"));
        option.value = item.view_id;
        target.appendChild(option);
      });
      var pathLabel = element("label", null, "File or directory path");
      var path = element("input");
      pathLabel.setAttribute("for", "permission-path");
      path.id = "permission-path";
      path.placeholder = "docs/private or secrets.txt";
      path.required = true;
      var actionLabel = element("label", null, "Visibility");
      var action = element("select");
      actionLabel.setAttribute("for", "permission-action");
      action.id = "permission-action";
      [["private", "Private for this collaborator"], ["visible", "Restore inherited visibility"]].forEach(function (item) {
        var option = element("option", null, item[1]);
        option.value = item[0];
        action.appendChild(option);
      });
      var submit = element("button", null, "Update permissions");
      submit.type = "submit";
      var outcome = element("p");
      var current = element("div");

      function showCurrent() {
        clearChildren(current);
        var selected = body.targets.find(function (item) { return item.view_id === target.value; });
        current.appendChild(element("h3", null, "Granted read access"));
        current.appendChild(prefixList(selected ? selected.readable_paths : [], "No readable paths"));
        current.appendChild(element("h3", null, "Explicitly private paths"));
        current.appendChild(prefixList(selected ? selected.private_paths : [], "No UI-managed private paths"));
      }
      target.addEventListener("change", showCurrent);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var normalized = path.value.replace(/^\/+|\/+$/g, "");
        if (!normalized) {
          outcome.className = "error";
          outcome.textContent = "Enter a file or directory path.";
          return;
        }
        outcome.className = "hint";
        outcome.textContent = "Publishing permission change…";
        request("/api/v1/permissions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ view_id: target.value, path_b64: encodePath(normalized), private: action.value === "private" }),
        }).then(function (result) {
          if (!activeContext() || invitationVersion !== invitationRun) return;
          if (result.status !== 200) throw new Error("permission-failed");
          // renderPermissions rebuilds the panel (wiping `outcome`), so the
          // confirmation is re-attached to the fresh panel, mirroring the
          // invitation flow.
          renderPermissions();
          var notice = element("p", "ok", "Permissions updated. A new projection is active.");
          notice.setAttribute("role", "status");
          document.getElementById("panel-permissions").prepend(notice);
        }).catch(function () {
          if (!activeContext() || invitationVersion !== invitationRun) return;
          outcome.className = "error";
          outcome.textContent = "The permission change could not be applied.";
        });
      });
      [targetLabel, target, pathLabel, path, actionLabel, action, submit, outcome].forEach(function (node) { form.appendChild(node); });
      content.appendChild(form);
      content.appendChild(current);
      showCurrent();
    }

    if (initialBody) populate(initialBody);
    else request("/api/v1/permissions").then(function (result) {
      if (result.status !== 200) throw new Error("permissions-unavailable");
      populate(result.body);
    }).catch(function () { if (activeContext() && invitationVersion === invitationRun) content.appendChild(element("p", "error", "Permissions are unavailable.")); });
  }

  function renderInvitation(card, pickerEnabled, version) {
    var tokenAtRender = credential, selectionAtRender = selectionRun, repoAtRender = session && session.repo && session.repo.repo_id;
    function activeContext() { return version === invitationRun && tokenAtRender === credential && selectionAtRender === selectionRun && (!repoAtRender || (session && session.repo && session.repo.repo_id === repoAtRender)); }
    var section = element("section", "invitation-section");
    section.appendChild(element("h2", null, "Invite a collaborator"));
    section.appendChild(element("p", "hint", "Collaborators must already have an approved Bastion account. Select an existing account or enter the verified email on that account, then set repository permissions. Share this site's URL with them; no email is sent."));
    var form = element("form");
    var id = element("input");
    id.id = "invite-id";
    id.required = true;
    id.pattern = "[A-Za-z0-9][A-Za-z0-9._-]{0,63}";
    id.maxLength = 64;
    id.placeholder = "demo-collaborator";
    var email = element("input");
    email.id = "invite-email";
    email.type = "email";
    email.required = true;
    var selectedAccount = null;
    var source = element("select");
    source.id = "invite-source";
    if (pickerEnabled) {
      var existingOption = element("option", null, "Select an existing Clerk account");
      existingOption.value = "account";
      source.appendChild(existingOption);
    }
    var emailOption = element("option", null, "Invite by email");
    emailOption.value = "email";
    source.appendChild(emailOption);
    var sourceLabel = element("label", null, "Invite method");
    sourceLabel.setAttribute("for", source.id);
    form.appendChild(sourceLabel);
    form.appendChild(source);
    var picker = element("div", "account-picker");
    var searchLabel = element("label", null, "Find a Clerk account");
    searchLabel.setAttribute("for", "invite-search");
    var search = element("input");
    search.id = "invite-search";
    search.type = "search";
    search.maxLength = 100;
    search.placeholder = "Search by name or email";
    search.setAttribute("autocomplete", "off");
    var searchStatus = element("p", "hint", "Enter at least 2 characters to search.");
    searchStatus.setAttribute("role", "status");
    var results = element("div", "account-results");
    var more = textButton("load-more", "Load more accounts");
    more.hidden = true;
    var selection = element("p", "account-selection");
    selection.setAttribute("role", "status");
    [searchLabel, search, searchStatus, results, more, selection].forEach(function (node) { picker.appendChild(node); });
    form.appendChild(picker);
    var searchVersion = 0;
    var timer = null;
    var nextOffset = null;
    var token = credential;

    function active(run) {
      return version === invitationRun && token === credential && run === searchVersion;
    }
    function searchAccounts(offset, run) {
      if (!active(run)) return;
      var query = search.value.trim();
      searchStatus.textContent = "Searching Clerk accounts…";
      more.disabled = true;
      request("/api/v1/accounts?query=" + encodeURIComponent(query) + "&offset=" + offset).then(function (result) {
        if (!active(run)) return;
        if (result.status !== 200) throw new Error("search-unavailable");
        result.body.accounts.forEach(function (account) {
          var button = textButton("account-option", account.name + " · " + account.email + (account.already_invited ? " · Already invited" : ""));
          button.disabled = account.already_invited;
          button.addEventListener("click", function () {
            selectedAccount = account;
            email.value = account.email;
            selection.textContent = "Selected: " + account.name + " · " + account.email;
            // Always re-derive the identifier from the account just selected.
            // Keeping a previously suggested or hand-typed value would submit
            // one account's identifier with another account's Clerk identity,
            // provisioning the wrong principal.
            id.value = account.email.split("@")[0].replace(/[^A-Za-z0-9._-]/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 64);
          });
          results.appendChild(button);
        });
        nextOffset = result.body.next_offset;
        more.hidden = nextOffset === null;
        searchStatus.textContent = results.children.length ? "Select an account below. Only active accounts with verified email are shown." : "No eligible accounts on this page. Try another search or invite by email.";
      }).catch(function () {
        if (!active(run)) return;
        searchStatus.textContent = "Account search is unavailable. Try again or choose Invite by email.";
        more.hidden = true;
      }).then(function () { if (active(run)) more.disabled = false; });
    }
    search.addEventListener("input", function () {
      var run = ++searchVersion;
      if (timer) window.clearTimeout(timer);
      clearChildren(results);
      more.hidden = true;
      if (search.value.trim().length < 2) {
        searchStatus.textContent = "Enter at least 2 characters to search.";
        return;
      }
      timer = window.setTimeout(function () { searchAccounts(0, run); }, 300);
    });
    more.addEventListener("click", function () {
      if (nextOffset !== null) searchAccounts(nextOffset, ++searchVersion);
    });
    function changeSource() {
      searchVersion += 1;
      if (timer) window.clearTimeout(timer);
      selectedAccount = null;
      selection.textContent = "";
      email.value = "";
      picker.hidden = source.value !== "account";
      email.readOnly = source.value === "account";
      email.required = source.value !== "account";
      clearChildren(results);
      more.hidden = true;
      search.value = "";
      searchStatus.textContent = "Enter at least 2 characters to search.";
    }
    source.addEventListener("change", changeSource);
    changeSource();
    var access = element("select");
    access.id = "invite-access";
    [["read-only", "Read-only — browse files and history"], ["contributor", "Contributor — read, create, edit, and delete allowed files"]].forEach(function (item) {
      var option = element("option", null, item[1]);
      option.value = item[0];
      access.appendChild(option);
    });
    var paths = element("textarea");
    paths.id = "invite-paths";
    paths.required = true;
    paths.rows = 4;
    paths.placeholder = "docs\nsrc/public\nREADME.md";
    [["Collaborator identifier", id], ["Verified email address", email], ["Permission level", access], ["Allowed files or folders (one per line; / for the entire repo)", paths]].forEach(function (item) {
      var label = element("label", null, item[0]);
      label.setAttribute("for", item[1].id);
      form.appendChild(label);
      form.appendChild(item[1]);
    });
    form.appendChild(element("p", "hint", "Only already-approved Bastion accounts can be added. All other paths are hidden. Contributors cannot manage permissions or invite people. Git credentials are issued separately."));
    var submit = element("button", null, "Invite with these permissions");
    submit.type = "submit";
    var outcome = element("p");
    outcome.setAttribute("role", "status");
    form.appendChild(submit);
    form.appendChild(outcome);
    section.appendChild(form);
    card.appendChild(section);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (source.value === "account" && !selectedAccount) {
        outcome.className = "error";
        outcome.textContent = "Search for and select a Clerk account first.";
        return;
      }
      var selectedPaths = paths.value.split(/\r?\n/).map(function (value) { return value.trim(); }).filter(Boolean);
      if (!selectedPaths.length) return;
      submit.disabled = true;
      outcome.className = "hint";
      outcome.textContent = "Creating the permission-scoped invitation…";
      var payload = { principal_id: id.value.trim(), access: access.value, readable_paths: selectedPaths };
      if (source.value === "account") payload.clerk_user_id = selectedAccount.clerk_user_id;
      else payload.email = email.value.trim();
      request("/api/v1/invitations", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (result) {
        if (!activeContext()) return;
        if (result.status !== 201) {
          outcome.className = "error";
          outcome.textContent = result.status === 409 ? "That identifier, email, or Clerk account is already invited. Choose a unique collaborator." : result.body.error === "account-unavailable" ? "The account is unavailable for this repository. Collaborators must already have an approved Bastion account; ask them to contact an administrator." : "Invitation failed. Check the account, identifier, email, and existing repository paths.";
          return;
        }
        renderPermissions();
        var notice = element("p", "ok", "Invited " + result.body.view_id + " with " + result.body.access + " access. Share this site's URL so they can sign in.");
        notice.setAttribute("role", "status");
        document.getElementById("panel-permissions").prepend(notice);
      }).catch(function () {
        if (!activeContext()) return;
        outcome.className = "error";
        outcome.textContent = "The invitation could not be created.";
      }).then(function () { if (activeContext()) submit.disabled = false; });
    });
  }

  function renderCredentials(initialBody) {
    var contextToken = credential, contextSelection = selectionRun, contextRepo = session && session.repo && session.repo.repo_id;
    function activeContext() { return contextToken === credential && contextSelection === selectionRun && (!contextRepo || (session && session.repo && session.repo.repo_id === contextRepo)); }
    var panel = document.getElementById("panel-credentials");
    clearChildren(panel);
    var card = element("div", "permission-card");
    card.appendChild(element("h2", null, "Git credentials"));
    card.appendChild(element("p", "hint", "Create a token to clone, fetch, and push with an unmodified Git client. The secret is shown once; revoke any token you no longer need."));
    var content = element("div");
    card.appendChild(content);
    panel.appendChild(card);

    function populate(body) {
      if (!activeContext()) return;
      var canPush = {};
      body.views.forEach(function (item) { canPush[item.view_id] = item.can_push; });

      var form = element("form");
      form.setAttribute("autocomplete", "off");
      var viewLabel = element("label", null, "Repository view");
      var view = element("select");
      view.id = "credential-view";
      body.views.forEach(function (item) {
        var option = element("option", null, item.view_id + (item.can_push ? "" : " (read-only)"));
        option.value = item.view_id;
        view.appendChild(option);
      });
      viewLabel.setAttribute("for", view.id);
      var accessLabel = element("label", null, "Access");
      var access = element("select");
      access.id = "credential-access";
      var pushOption = null;
      [["fetch", "Read-only — clone and fetch"], ["fetch,push", "Read and write — clone, fetch, and push"]].forEach(function (item) {
        var option = element("option", null, item[1]);
        option.value = item[0];
        if (item[0] === "fetch,push") pushOption = option;
        access.appendChild(option);
      });
      var expiryLabel = element("label", null, "Expires");
      var expiry = element("select");
      expiry.id = "credential-expiry";
      [["7d", "In 7 days"], ["30d", "In 30 days"], ["90d", "In 90 days"]].forEach(function (item) {
        var option = element("option", null, item[1]);
        option.value = item[0];
        expiry.appendChild(option);
      });
      expiry.value = "30d";
      function syncAccess() {
        var writable = Boolean(canPush[view.value]);
        if (pushOption) pushOption.disabled = !writable;
        if (!writable && access.value === "fetch,push") access.value = "fetch";
      }
      view.addEventListener("change", syncAccess);

      var submit = element("button", null, "Create token");
      submit.type = "submit";
      var outcome = element("p");
      outcome.setAttribute("role", "status");
      [["Repository view", view], ["Access", access], ["Expires", expiry]].forEach(function (item) {
        var label = element("label", null, item[0]);
        label.setAttribute("for", item[1].id);
        form.appendChild(label);
        form.appendChild(item[1]);
      });
      form.appendChild(submit);
      form.appendChild(outcome);
      content.appendChild(form);
      syncAccess();

      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var scopes = access.value.split(",");
        submit.disabled = true;
        outcome.className = "hint";
        outcome.textContent = "Creating token…";
        request("/api/v1/credentials", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ view_id: view.value, scopes: scopes, expires_in: expiry.value }),
        }).then(function (result) {
          if (!activeContext()) return;
          if (result.status !== 201) {
            outcome.className = "error";
            outcome.textContent = result.status === 403 ? "That access level is not available for this view." : "The token could not be created.";
            return;
          }
          renderCredentials();
          var created = element("div", "credential-secret");
          created.appendChild(element("p", "ok", "Token created. Copy it now — it is shown only once."));
          var field = element("div", "copy-field");
          field.appendChild(element("code", null, result.body.credential));
          var copy = textButton("copy-credential", "Copy");
          copy.addEventListener("click", function () {
            if (!navigator.clipboard) { copy.textContent = "Copy unavailable"; return; }
            navigator.clipboard.writeText(result.body.credential).then(function () {
              copy.textContent = "Copied";
              window.setTimeout(function () { copy.textContent = "Copy"; }, 1600);
            }, function () { copy.textContent = "Copy failed"; });
          });
          field.appendChild(copy);
          created.appendChild(field);
          document.getElementById("panel-credentials").prepend(created);
        }).catch(function () {
          if (!activeContext()) return;
          outcome.className = "error";
          outcome.textContent = "The token could not be created.";
        }).then(function () { if (activeContext()) submit.disabled = false; });
      });

      var heading = element("h3", null, "Existing tokens");
      content.appendChild(heading);
      if (!body.tokens.length) {
        content.appendChild(element("p", "hint", "No Git tokens yet."));
        return;
      }
      body.tokens.forEach(function (token) {
        var row = element("div", "credential-row");
        row.appendChild(element("span", "credential-id", token.token_id.slice(0, 8) + "…"));
        row.appendChild(element("span", "credential-scope", token.scopes.join(", ")));
        row.appendChild(element("span", "credential-expiry", token.expires_at ? "expires " + shortDate(token.expires_at) : "no expiry"));
        var revoked = Boolean(token.revoked_at);
        var expired = Boolean(token.expires_at && token.expires_at * 1000 <= Date.now());
        row.appendChild(element("span", "credential-state", revoked ? "Revoked" : expired ? "Expired" : "Active"));
        var revoke = textButton("revoke-credential", "Revoke");
        revoke.disabled = revoked;
        revoke.addEventListener("click", function () {
          request("/api/v1/credentials/" + encodeURIComponent(token.token_id), { method: "DELETE" })
            .then(function (result) {
              if (!activeContext()) return;
              if (result.status !== 200) throw new Error("revoke-failed");
              renderCredentials();
            }).catch(function () {
              if (!activeContext()) return;
              row.appendChild(element("p", "error", "The token could not be revoked."));
            });
        });
        row.appendChild(revoke);
        content.appendChild(row);
      });
    }

    if (initialBody) populate(initialBody);
    else request("/api/v1/credentials").then(function (result) {
      if (result.status !== 200) throw new Error("credentials-unavailable");
      populate(result.body);
    }).catch(function () { if (activeContext()) content.appendChild(element("p", "error", "Credentials are unavailable.")); });
  }

  function renderProfile() {
    var panel = document.getElementById("panel-profile");
    clearChildren(panel);
    var hero = element("div", "profile-hero");
    hero.appendChild(element("div", "profile-avatar", initials(session.principal.id)));
    var heading = element("div");
    heading.appendChild(element("h2", null, session.principal.id));
    heading.appendChild(element("p", null, "Bastion " + session.principal.kind + " · " + (session.repo ? session.repo.owner_name + " / " + session.repo.name + " · " : "") + "view " + session.view_id));
    hero.appendChild(heading);
    var account = textButton(null, clerk ? "Manage account" : "Account managed by token");
    account.disabled = !clerk;
    account.addEventListener("click", function () { if (clerk) clerk.openUserProfile(); });
    hero.appendChild(account);
    panel.appendChild(hero);

    var grid = element("div", "profile-grid");
    var identityCard = element("article", "profile-card");
    identityCard.appendChild(element("h3", null, "Identity"));
    identityCard.appendChild(definitionList([
      ["Principal", session.principal.id],
      ["Account type", session.principal.kind],
      ["Teams", session.team_ids.join(", ") || "None"],
      ["Roles", session.role_ids.join(", ") || "None"],
    ]));
    var viewCard = element("article", "profile-card");
    viewCard.appendChild(element("h3", null, "Active repository view"));
    viewCard.appendChild(definitionList([
      ["View", session.view_id],
      ["Branch", ELEMENTS.branchName.textContent],
      ["Generation", session.descriptor.generation],
      ["Projection epoch", session.descriptor.projection_epoch],
    ]));
    var accessCard = element("article", "profile-card profile-card-wide");
    accessCard.appendChild(element("h3", null, "Read access"));
    var columns = element("div", "access-columns");
    var grants = element("div");
    grants.appendChild(element("h4", null, "Visible paths"));
    grants.appendChild(prefixList(session.descriptor.capabilities.read, "No visible paths"));
    var restrictions = element("div");
    restrictions.appendChild(element("h4", null, "Explicit restrictions"));
    restrictions.appendChild(prefixList(session.descriptor.restrictions.read, "No explicit restrictions"));
    columns.appendChild(grants);
    columns.appendChild(restrictions);
    accessCard.appendChild(columns);
    if (session.principal.emails && session.principal.emails.length) {
      var authorCard = element("article", "profile-card profile-card-wide");
      authorCard.appendChild(element("h3", null, "Approved commit author emails"));
      authorCard.appendChild(element("p", null, session.principal.emails.join(", ")));
      grid.appendChild(authorCard);
    }
    grid.appendChild(identityCard);
    grid.appendChild(viewCard);
    grid.appendChild(accessCard);
    panel.appendChild(grid);
  }

  function configureClone() {
    if (session.repo && session.repo.git_url) {
      try { var git = new URL(session.repo.git_url, window.location.href); git.username = session.view_id; ELEMENTS.cloneUrl.textContent = git.toString(); }
      catch (_) { ELEMENTS.cloneUrl.textContent = session.repo.git_url; }
    } else ELEMENTS.cloneUrl.textContent = window.location.protocol + "//" + encodeURIComponent(session.view_id) + "@" + window.location.host + "/" + encodeURIComponent(session.view_id) + ".git";
  }

  function openSession(token, envelope) {
    credential = token;
    session = envelope;
    ELEMENTS.hostDashboard.hidden = true;
    ELEMENTS.homepage.hidden = true; ELEMENTS.login.hidden = true; ELEMENTS.sessionNav.hidden = false;
    ELEMENTS.identity.hidden = false; ELEMENTS.identity.textContent = session.repo ? session.repo.owner_name + " / " + session.repo.name + " · view " + session.view_id : session.principal.id + " / " + session.view_id;
    ELEMENTS.logout.hidden = false;
    historyRendered = false;
    treeState = { stack: [], entries: [], cursor: null, loading: false };
    ELEMENTS.loginError.hidden = true;
    ELEMENTS.login.hidden = true;
    ELEMENTS.homepage.hidden = true;
    ELEMENTS.homeNav.hidden = true;
    ELEMENTS.sessionNav.hidden = false;
    ELEMENTS.workspace.hidden = false;
    ELEMENTS.identity.hidden = false;
    ELEMENTS.identity.textContent = session.repo ? session.repo.owner_name + " / " + session.repo.name + " · view " + session.view_id : session.principal.id + " / " + session.view_id;
    ELEMENTS.manageAccount.hidden = !clerk;
    ELEMENTS.logout.hidden = false;
    ELEMENTS.repoOwner.textContent = session.repo ? session.repo.owner_name : session.principal.id;
    ELEMENTS.repoName.textContent = session.repo ? session.repo.name : session.view_id;
    var refParts = session.descriptor.projected_ref.split("/");
    ELEMENTS.branchName.textContent = refParts[refParts.length - 1] || "main";
    document.getElementById("tab-permissions").hidden = true;
    configureClone();
    ELEMENTS.cloneAuthorNote.textContent = session.principal.emails && session.principal.emails.length ? "New commits must use an approved author email: " + session.principal.emails.join(", ") : "New commits must use an email approved for your account.";
    ELEMENTS.repositoryListNav.hidden = !hostMode;
    if (session.principal && session.principal.emails) ELEMENTS.sessionNote.textContent = "Git commits must use one of your approved author emails: " + session.principal.emails.join(", ");
    renderDecision();
    renderCredentials();
    renderProfile();
    var token = credential, selectedRepo = session.repo && session.repo.repo_id;
    request("/api/v1/permissions").then(function (result) {
      if (token !== credential || selectedRepo !== (session && session.repo && session.repo.repo_id)) return;
      if (result.status === 200) {
        document.getElementById("tab-permissions").hidden = false;
        renderPermissions(result.body);
      }
    }).catch(function () {});
    showPanel("code");
    loadDirectory(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function loadScript(src, attributes) {
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = src;
      script.defer = true;
      script.crossOrigin = "anonymous";
      Object.keys(attributes || {}).forEach(function (name) { script.setAttribute(name, attributes[name]); });
      script.addEventListener("load", resolve);
      script.addEventListener("error", function () { reject(new Error("authentication service failed to load")); });
      document.head.appendChild(script);
    });
  }

  function showClerkError(message) {
    ELEMENTS.loginError.hidden = false;
    ELEMENTS.loginError.textContent = message;
  }

  function mountClerkUserButton() {
    if (!clerk) return;
    if (!clerkUserButtonMounted) {
      clerk.mountUserButton(ELEMENTS.clerkUserButton, { afterSignOutUrl: "/" });
      clerkUserButtonMounted = true;
    }
    ELEMENTS.clerkUserButton.hidden = false;
  }

  function exchangeClerkSession(view) {
    if (!clerk || !clerk.session || clerkExchangePending) return;
    var generation = ++authGeneration;
    var identity = currentClerkIdentity();
    var clerkSession = clerk.session;
    var exchangeRun = ++clerkExchangeRun;
    clerkExchangePending = true;
    ELEMENTS.loginError.hidden = true;
    clerkSession.getToken().then(function (token) {
      if (generation !== authGeneration || identity !== currentClerkIdentity()) throw new Error("stale-auth");
      var payload = {};
      if (view) payload.view_id = view;
      return request("/api/v1/auth/clerk", {
        method: "POST",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        skipCredential: true,
      });
    }).then(function (result) {
      if (generation !== authGeneration || identity !== currentClerkIdentity()) {
        if (result.status === 200) revokeLateLogin(result, null, null);
        return;
      }
      if (result.status === 409 && Array.isArray(result.body.view_ids)) {
        clearChildren(ELEMENTS.clerkView);
        result.body.view_ids.forEach(function (viewId) {
          var option = element("option", null, viewId);
          option.value = visibleText(viewId);
          ELEMENTS.clerkView.appendChild(option);
        });
        ELEMENTS.clerkViewForm.hidden = false;
        ELEMENTS.clerkSignIn.hidden = true;
        return;
      }
      if (result.status !== 200) {
        showClerkError(result.status === 403 ? "Your account has not been approved for this workspace." : "Your sign-in could not be verified.");
        return;
      }
      mountClerkUserButton();
      if (hostMode && result.body.account) {
        hostCredential = result.body.access_token;
        hostAccount = result.body.account;
        ELEMENTS.identity.textContent = result.body.account.owner_name;
        ELEMENTS.identity.hidden = false; ELEMENTS.logout.hidden = false; ELEMENTS.login.hidden = true; ELEMENTS.homepage.hidden = true; ELEMENTS.homeNav.hidden = true; ELEMENTS.sessionNav.hidden = false;
        renderRepositories(); return;
      }
      openSession(result.body.access_token, result.body.session);
    }).catch(function () {
      if (generation === authGeneration && identity === currentClerkIdentity()) showClerkError("Sign-in failed. Please try again.");
    }).then(function () { if (exchangeRun === clerkExchangeRun) clerkExchangePending = false; });
  }

  function configureClerk(config) {
    hostMode = config.mode === "host";
    if (!config.enabled) return Promise.resolve();
    ELEMENTS.clerkAuth.hidden = false;
    ELEMENTS.sessionNote.textContent = "Authentication, recovery, MFA, and account security are managed by Clerk.";
    return loadScript(config.frontend_api_origin + "/npm/@clerk/ui@1/dist/ui.browser.js")
      .then(function () {
        return loadScript(config.frontend_api_origin + "/npm/@clerk/clerk-js@6/dist/clerk.browser.js", { "data-clerk-publishable-key": config.publishable_key });
      })
      .then(function () {
        clerk = window.Clerk;
        return clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor } });
      })
      .then(function () {
        clerkHadSession = Boolean(clerk.session);
        var listenerIdentity = currentClerkIdentity();
        clerk.addListener(function (state) {
          var hasSession = Boolean(state.session);
          var identity = currentClerkIdentity();
          if (clerkHadSession && hasSession && identity !== listenerIdentity) revokeAndClear(false);
          if (hasSession && (!clerkHadSession || identity !== listenerIdentity) && !session) {
            authGeneration += 1;
            clerkExchangePending = false; clerkExchangeRun += 1;
            mountClerkUserButton();
            exchangeClerkSession();
          }
          if (clerkHadSession && !hasSession) {
            clerkExchangePending = false; clerkExchangeRun += 1;
            revokeAndClear(false);
          }
          listenerIdentity = identity;
          clerkHadSession = hasSession;
        });
        if (clerk.isSignedIn) {
          mountClerkUserButton();
          exchangeClerkSession();
        } else {
          clerk.mountSignIn(ELEMENTS.clerkSignIn);
        }
      });
  }

  function authenticate(token) {
    var generation = ++authGeneration;
    request("/api/v1/session", { headers: { Authorization: "Bearer " + token }, skipCredential: true }).then(function (result) {
      if (generation !== authGeneration) { if (result.status === 200) revokeLateLogin(result, token, null); return; }
      ELEMENTS.tokenInput.value = "";
      if (result.status !== 200) {
        showClerkError(result.status === 403 ? "This token cannot open the browser interface." : "Sign-in failed.");
        return;
      }
      if (hostMode && result.body.account) {
        hostCredential = token; hostAccount = result.body.account; ELEMENTS.tokenInput.value = ""; ELEMENTS.identity.textContent = result.body.account.owner_name; ELEMENTS.identity.hidden = false; ELEMENTS.logout.hidden = false; ELEMENTS.login.hidden = true; ELEMENTS.homepage.hidden = true; ELEMENTS.homeNav.hidden = true; ELEMENTS.sessionNav.hidden = false; renderRepositories(); return;
      }
      openSession(token, result.body);
    }).catch(function (error) {
      if (generation !== authGeneration) return;
      ELEMENTS.tokenInput.value = "";
      if (!error || error.message !== "auth-required") showClerkError("Sign-in failed.");
    });
  }

  ELEMENTS.loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var token = ELEMENTS.tokenInput.value.trim();
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.loginError.hidden = true;
    if (token) authenticate(token);
  });
  ELEMENTS.clerkViewForm.addEventListener("submit", function (event) {
    event.preventDefault();
    exchangeClerkSession(ELEMENTS.clerkView.value);
  });
  ELEMENTS.manageAccount.addEventListener("click", function () { if (clerk) clerk.openUserProfile(); });
  ELEMENTS.logout.addEventListener("click", function () {
    revokeAndClear(true);
  });
  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () { showPanel(tab.dataset.panel); });
  });
  document.getElementById("repository-nav").addEventListener("click", function () { showPanel("code"); });
  ELEMENTS.repositoryListNav.addEventListener("click", function () { var repoId = session && session.repo && session.repo.repo_id, token = credential; if (repoId && token) fetch("/api/v1/repos/" + encodeURIComponent(repoId) + "/logout", { method: "POST", headers: { Authorization: "Bearer " + token }, credentials: "omit", cache: "no-store" }).catch(function () {}); clearRepositorySession(); renderRepositories(); });
  ELEMENTS.createForm.addEventListener("submit", function (event) {
    event.preventDefault(); if (!hostCredential || createPending) return;
    var name = ELEMENTS.createName.value.trim();
    if (!createRequestId || createRequestId.name !== name) {
      try {
        var bytes = new Uint8Array(16);
        if (window.crypto && window.crypto.randomUUID) createRequestId = { name: name, id: window.crypto.randomUUID() };
        else if (window.crypto && window.crypto.getRandomValues) {
          window.crypto.getRandomValues(bytes);
          createRequestId = { name: name, id: Array.prototype.map.call(bytes, function (byte) { return byte.toString(16).padStart(2, "0"); }).join("") };
        } else throw new Error("secure-random-unavailable");
      } catch (_) { ELEMENTS.createError.textContent = "Secure request IDs are unavailable in this browser. Use a current browser to create a repository."; return; }
    }
    var selection = selectionRun, creatingHost = hostCredential; createPending = true;
    var createButton = ELEMENTS.createForm.querySelector ? ELEMENTS.createForm.querySelector("button[type=submit]") : null;
    if (createButton) createButton.disabled = true;
    ELEMENTS.createError.textContent = "Creating repository…";
    request("/api/v1/repos", { host: true, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name, request_id: createRequestId.id }) }).then(function (result) {
      if (!hostCredential || hostCredential !== creatingHost) return;
      if (result.status !== 201 && result.status !== 200) throw new Error("create");
      createRequestId = null; ELEMENTS.createName.value = ""; ELEMENTS.createError.textContent = "";
      if (selection === selectionRun && !session && !ELEMENTS.hostDashboard.hidden) renderRepositories();
      else if (hostCredential) repositories.push(result.body.repo);
    }).catch(function () { if (hostCredential === creatingHost && !session && !ELEMENTS.hostDashboard.hidden) ELEMENTS.createError.textContent = "Repository creation failed. Retry safely; the same request will be reused."; })
      .then(function () { if (hostCredential === creatingHost) { createPending = false; if (createButton) createButton.disabled = false; } });
  });
  document.getElementById("profile-nav").addEventListener("click", function () { showPanel("profile"); });
  ELEMENTS.treeMore.addEventListener("click", function () { loadDirectory(false); });
  document.getElementById("tree-mode").addEventListener("click", function (event) {
    fullTree = !fullTree;
    treeRun += 1;
    treeState = { stack: [], entries: [], cursor: null, loading: false };
    event.currentTarget.textContent = fullTree ? "Browse one folder" : "Show full file structure";
    event.currentTarget.setAttribute("aria-pressed", String(fullTree));
    loadDirectory(true);
  });
  document.getElementById("refresh-repo").addEventListener("click", function () {
    request("/api/v1/session").then(function (result) {
      if (result.status === 200) openSession(credential, result.body);
    });
  });
  document.getElementById("clone-action").addEventListener("click", function () { ELEMENTS.cloneDialog.showModal(); });
  document.getElementById("clone-close").addEventListener("click", function () { ELEMENTS.cloneDialog.close(); });
  ELEMENTS.cloneDialog.addEventListener("click", function (event) {
    if (event.target === ELEMENTS.cloneDialog) ELEMENTS.cloneDialog.close();
  });
  document.getElementById("copy-clone").addEventListener("click", function (event) {
    var button = event.currentTarget;
    if (!navigator.clipboard) {
      button.textContent = "Copy unavailable";
      return;
    }
    navigator.clipboard.writeText(ELEMENTS.cloneUrl.textContent).then(function () {
      button.textContent = "Copied";
      window.setTimeout(function () { button.textContent = "Copy"; }, 1600);
    }).catch(function () { button.textContent = "Copy failed"; });
  });

  clearBastionSession();
  request("/api/v1/auth/config", { optionalAuth: true }).then(function (result) {
    if (result.status !== 200) throw new Error("authentication configuration unavailable");
    return configureClerk(result.body);
  }).catch(function () { showClerkError("Authentication is temporarily unavailable."); });
})();
