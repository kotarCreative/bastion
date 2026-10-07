/* Bastion permission-demo application.
 *
 * Credential handling: the access token lives in one module-local variable
 * (page memory).  It is never written to any browser storage API, cookie,
 * URL, the DOM, or telemetry, and it is cleared from the input immediately
 * after use.  Reloading the page discards it.  All untrusted server data is
 * rendered through DOM text APIs (textContent), never HTML interpolation,
 * and displayed values are never turned into URLs without component-safe
 * encoding.
 */
"use strict";

(function () {
  var credential = null; // Bastion token, page memory only
  var session = null;
  var clerk = null;
  var clerkHadSession = false;
  var clerkExchangePending = false;
  var clerkUserButtonMounted = false;

  var ELEMENTS = {
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
  };

  function visibleText(value) {
    // Lossy presentation text; control characters were already escaped by
    // the server, so textContent renders them inertly.
    return String(value == null ? "" : value);
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = visibleText(text);
    }
    return node;
  }

  function request(path, options) {
    options = options || {};
    var headers = options.headers || {};
    if (credential && !options.skipCredential) {
      headers["Authorization"] = "Bearer " + credential;
    }
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    }).then(function (response) {
      if (response.status === 401 && !options.optionalAuth) {
        clearBastionSession();
        throw new Error("auth-required");
      }
      return response.json().then(function (body) {
        return { status: response.status, body: body };
      });
    });
  }

  function showPanel(name) {
    var panels = ["summary", "decision", "tree", "history", "permissions", "clone"];
    panels.forEach(function (panel) {
      document.getElementById("panel-" + panel).hidden = panel !== name;
      var tab = document.getElementById("tab-" + panel);
      tab.setAttribute("aria-selected", panel === name ? "true" : "false");
    });
  }

  function clearBastionSession() {
    credential = null;
    session = null;
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.login.hidden = false;
    document.getElementById("homepage").hidden = false;
    document.getElementById("home-nav").hidden = false;
    ELEMENTS.workspace.hidden = true;
    ELEMENTS.identity.hidden = true;
    ELEMENTS.identity.textContent = "";
    ELEMENTS.manageAccount.hidden = true;
    ELEMENTS.logout.hidden = true;
    clearChildren(document.getElementById("panel-summary"));
    clearChildren(document.getElementById("panel-decision"));
    clearChildren(document.getElementById("panel-tree"));
    clearChildren(document.getElementById("panel-history"));
    clearChildren(document.getElementById("panel-permissions"));
    clearChildren(document.getElementById("panel-clone"));
    document.getElementById("tab-permissions").hidden = true;
  }

  function prefixList(prefixes) {
    var list = element("ul");
    prefixes.forEach(function (prefix) {
      var item = element("li");
      item.textContent = visibleText(prefix.prefix_display);
      list.appendChild(item);
    });
    return list;
  }

  function capabilityTable(map, title) {
    var section = element("section");
    section.appendChild(element("h2", null, title));
    var table = element("table");
    var head = element("tr");
    head.appendChild(element("th", null, "capability"));
    head.appendChild(element("th", null, "prefixes"));
    table.appendChild(head);
    Object.keys(map).forEach(function (capability) {
      var row = element("tr");
      row.appendChild(element("td", null, capability));
      var cell = element("td");
      if (map[capability].length === 0) {
        cell.textContent = "(none)";
      } else {
        cell.appendChild(prefixList(map[capability]));
      }
      row.appendChild(cell);
      table.appendChild(row);
    });
    section.appendChild(table);
    return section;
  }

  function renderSummary() {
    var panel = document.getElementById("panel-summary");
    clearChildren(panel);
    var heading = element("h2", null, "View " + session.view_id);
    panel.appendChild(heading);
    var definition = element("dl");
    function row(term, value) {
      definition.appendChild(element("dt", null, term));
      definition.appendChild(element("dd", null, value));
    }
    row("principal", session.principal.id + " (" + session.principal.kind + ")");
    row("descriptor", session.descriptor.id);
    row("descriptor generation", session.descriptor.generation);
    row("projection epoch", session.descriptor.projection_epoch);
    row("projected ref", session.descriptor.projected_ref);
    row("teams", session.team_ids.join(", ") || "(none)");
    row("bindings", session.binding_ids.join(", ") || "(none)");
    row("roles", session.role_ids.join(", ") || "(none)");
    panel.appendChild(definition);
    panel.appendChild(element("p", "hint",
      "Downloads already fetched by a client stay readable until the client "
      + "discards them; a policy change starts a new projection epoch for "
      + "future operations."));
    panel.appendChild(
      capabilityTable(session.descriptor.capabilities, "Allowed prefixes")
    );
    panel.appendChild(
      capabilityTable(session.descriptor.restrictions, "Restricted prefixes")
    );
  }

  function renderDecision() {
    var panel = document.getElementById("panel-decision");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Decision inspector"));
    var form = element("form");
    form.setAttribute("autocomplete", "off");
    var label = element("label", null, "Path (repository-relative)");
    var input = element("input");
    input.type = "text";
    input.id = "decision-path";
    label.setAttribute("for", "decision-path");
    var capabilityLabel = element("label", null, "Capability");
    var select = element("select");
    select.id = "decision-capability";
    capabilityLabel.setAttribute("for", "decision-capability");
    ["read", "create", "modify", "delete", "mode", "symlink", "branch", "merge", "admin", "tag"]
      .forEach(function (capability) {
        var option = element("option", null, capability);
        option.value = capability;
        select.appendChild(option);
      });
    var submit = element("button", null, "Evaluate");
    submit.type = "submit";
    var outcome = element("div");
    form.appendChild(label);
    form.appendChild(input);
    form.appendChild(capabilityLabel);
    form.appendChild(select);
    form.appendChild(submit);
    panel.appendChild(form);
    panel.appendChild(outcome);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      clearChildren(outcome);
      var path = input.value.replace(/^\/+/, "");
      var raw = new TextEncoder().encode(path);
      var binary = "";
      raw.forEach(function (byte) {
        binary += String.fromCharCode(byte);
      });
      var encoded = btoa(binary).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
      request("/api/v1/decisions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capability: select.value, path_b64: encoded }),
      }).then(function (result) {
        if (result.status !== 200) {
          outcome.appendChild(element("p", "error", "Decision evaluation failed."));
          return;
        }
        var verdict = element("p", result.body.allowed ? "verdict-allow" : "verdict-deny",
          (result.body.allowed ? "allowed" : "denied"));
        outcome.appendChild(verdict);
        var definition = element("dl");
        function matchRow(term, match) {
          definition.appendChild(element("dt", null, term));
          definition.appendChild(element("dd", null,
            match ? match.prefix_display : "(no match)"));
        }
        matchRow("longest allow", result.body.longest_allow);
        matchRow("longest deny", result.body.longest_deny);
        outcome.appendChild(definition);
      }).catch(function () {
        clearChildren(outcome);
        outcome.appendChild(element("p", "error", "Decision evaluation failed."));
      });
    });
  }

  function renderTree() {
    var panel = document.getElementById("panel-tree");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Projected tree"));
    var breadcrumb = element("p", "breadcrumb");
    var list = element("ul", "tree-list");
    var moreButton = element("button", null, "Load more");
    var state = { prefix: "", cursor: null };

    function encodeBytes(text) {
      var raw = new TextEncoder().encode(text);
      var binary = "";
      raw.forEach(function (byte) {
        binary += String.fromCharCode(byte);
      });
      return btoa(binary).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
    }

    function open(entry) {
      if (entry.kind !== "tree") {
        return;
      }
      state.prefix = entry.path_b64;
      state.cursor = null;
      breadcrumb.textContent = entry.path_display;
      clearChildren(list);
      load();
    }

    function load() {
      var query = "limit=50";
      if (state.prefix) {
        query += "&path_b64=" + encodeURIComponent(state.prefix);
      }
      if (state.cursor) {
        query += "&cursor=" + encodeURIComponent(state.cursor);
      }
      request("/api/v1/tree?" + query).then(function (result) {
        if (result.status !== 200) {
          list.appendChild(element("li", "error", "Tree is unavailable."));
          return;
        }
        result.body.entries.forEach(function (entry) {
          var item = element("li");
          item.textContent = entry.path_display + (entry.kind === "tree" ? "/" : "");
          if (entry.kind === "tree") {
            item.setAttribute("role", "button");
            item.tabIndex = 0;
            item.addEventListener("click", function () { open(entry); });
            item.addEventListener("keydown", function (event) {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                open(entry);
              }
            });
          }
          list.appendChild(item);
        });
        state.cursor = result.body.next_cursor;
        moreButton.hidden = !result.body.next_cursor;
      }).catch(function () {
        list.appendChild(element("li", "error", "Tree is unavailable."));
      });
    }

    moreButton.addEventListener("click", load);
    panel.appendChild(breadcrumb);
    panel.appendChild(list);
    panel.appendChild(moreButton);
    load();
  }

  function renderHistory() {
    var panel = document.getElementById("panel-history");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Visible history"));
    var list = element("div");
    var moreButton = element("button", null, "Load more");
    var cursor = null;
    function load() {
      var query = "limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
      request("/api/v1/history?" + query).then(function (result) {
        if (result.status !== 200) {
          list.appendChild(element("p", "error", "History is unavailable."));
          return;
        }
        result.body.commits.forEach(function (commit) {
          var entry = element("div", "commit");
          entry.appendChild(element("code", null, commit.oid));
          entry.appendChild(element("div", null, commit.message_display));
          entry.appendChild(element("div", "breadcrumb",
            commit.author_name_display + " <" + commit.author_email_display + "> "
            + new Date(commit.committed_at * 1000).toISOString()));
          list.appendChild(entry);
        });
        cursor = result.body.next_cursor;
        moreButton.hidden = !result.body.next_cursor;
      }).catch(function () {
        list.appendChild(element("p", "error", "History is unavailable."));
      });
    }
    moreButton.addEventListener("click", load);
    panel.appendChild(list);
    panel.appendChild(moreButton);
    load();
  }

  function renderClone() {
    var panel = document.getElementById("panel-clone");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Clone and credentials"));
    panel.appendChild(element("p", "hint",
      "The remote URL never contains your token. Configure Git once per host "
      + "so credentials are asked interactively and stored by a non-plaintext "
      + "helper."));
    var commands = [
      "git config credential.useHttpPath true",
      "git clone https://<host>/" + session.view_id + ".git",
    ];
    commands.forEach(function (line) {
      panel.appendChild(element("pre", null, line));
    });
    panel.appendChild(element("p", "hint",
      "When Git prompts for a password, paste your Bastion token. Two views on "
      + "the same host use different credentials; useHttpPath keeps them apart."));
  }

  function encodePath(text) {
    var raw = new TextEncoder().encode(text);
    var binary = "";
    raw.forEach(function (byte) {
      binary += String.fromCharCode(byte);
    });
    return btoa(binary).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  }

  function renderPermissions(initialBody) {
    var panel = document.getElementById("panel-permissions");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Repository permissions"));
    panel.appendChild(element("p", "hint",
      "Choose a collaborator and a repository-relative file or folder. Private paths "
      + "are removed from that collaborator's next projected repository epoch."));
    var content = element("div");
    panel.appendChild(content);

    function populate(body) {
      if (body.targets.length === 0) {
        content.appendChild(element("p", "hint", "This repository has no other collaborators."));
        return;
      }
      var form = element("form");
      var targetLabel = element("label", null, "Collaborator view");
      var target = element("select");
      target.id = "permission-target";
      targetLabel.setAttribute("for", target.id);
      body.targets.forEach(function (item) {
        var option = element("option", null,
          item.principal_id + " / " + item.view_id);
        option.value = item.view_id;
        target.appendChild(option);
      });
      var pathLabel = element("label", null, "File or folder path");
      var path = element("input");
      path.id = "permission-path";
      path.type = "text";
      path.placeholder = "docs/private or secrets.txt";
      path.required = true;
      pathLabel.setAttribute("for", path.id);
      var actionLabel = element("label", null, "Visibility");
      var action = element("select");
      action.id = "permission-action";
      actionLabel.setAttribute("for", action.id);
      var makePrivate = element("option", null, "Private for this collaborator");
      makePrivate.value = "private";
      action.appendChild(makePrivate);
      var makeVisible = element("option", null, "Restore inherited visibility");
      makeVisible.value = "visible";
      action.appendChild(makeVisible);
      var submit = element("button", null, "Update permissions");
      submit.type = "submit";
      var outcome = element("p");
      var current = element("div");

      function showCurrent() {
        clearChildren(current);
        var selected = body.targets.filter(function (item) {
          return item.view_id === target.value;
        })[0];
        current.appendChild(element("h3", null, "Private paths"));
        if (!selected || selected.private_paths.length === 0) {
          current.appendChild(element("p", "hint", "No UI-managed private paths."));
          return;
        }
        current.appendChild(prefixList(selected.private_paths));
      }

      target.addEventListener("change", showCurrent);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        outcome.className = "hint";
        var trimmed = path.value.replace(/^\/+|\/+$/g, "");
        if (!trimmed) {
          outcome.className = "error";
          outcome.textContent = "Enter a file or folder path (the repository root cannot be made private).";
          return;
        }
        outcome.textContent = "Applying permission change…";
        request("/api/v1/permissions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            view_id: target.value,
            path_b64: encodePath(trimmed),
            private: action.value === "private",
          }),
        }).then(function (update) {
          if (update.status !== 200) {
            outcome.className = "error";
            outcome.textContent = update.status === 403
              ? "You no longer have permission to manage this repository."
              : "The permission change could not be applied. Check the path and try again.";
            return;
          }
          outcome.className = "ok";
          outcome.textContent = "Permissions updated.";
          renderPermissions();
        }).catch(function () {
          outcome.className = "error";
          outcome.textContent = "The permission change could not be applied.";
        });
      });
      form.appendChild(targetLabel);
      form.appendChild(target);
      form.appendChild(pathLabel);
      form.appendChild(path);
      form.appendChild(actionLabel);
      form.appendChild(action);
      form.appendChild(submit);
      form.appendChild(outcome);
      content.appendChild(form);
      content.appendChild(current);
      showCurrent();
    }

    if (initialBody) {
      populate(initialBody);
    } else {
      request("/api/v1/permissions").then(function (result) {
        if (result.status !== 200) {
          content.appendChild(element("p", "error", "Permissions are unavailable."));
          return;
        }
        populate(result.body);
      }).catch(function () {
        content.appendChild(element("p", "error", "Permissions are unavailable."));
      });
    }
  }

  function openSession(token, envelope) {
    credential = token;
    session = envelope;
    ELEMENTS.loginError.hidden = true;
    ELEMENTS.login.hidden = true;
    document.getElementById("homepage").hidden = true;
    document.getElementById("home-nav").hidden = true;
    ELEMENTS.workspace.hidden = false;
    ELEMENTS.identity.hidden = false;
    ELEMENTS.identity.textContent = session.principal.id + " / " + session.view_id;
    ELEMENTS.manageAccount.hidden = !clerk;
    ELEMENTS.logout.hidden = false;
    document.getElementById("tab-permissions").hidden = true;
    renderSummary();
    renderDecision();
    renderTree();
    renderHistory();
    request("/api/v1/permissions").then(function (result) {
      if (result.status === 200) {
        document.getElementById("tab-permissions").hidden = false;
        renderPermissions(result.body);
      }
    });
    renderClone();
    showPanel("summary");
  }

  function loadScript(src, attributes) {
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = src;
      script.defer = true;
      script.crossOrigin = "anonymous";
      Object.keys(attributes || {}).forEach(function (name) {
        script.setAttribute(name, attributes[name]);
      });
      script.addEventListener("load", resolve);
      script.addEventListener("error", function () {
        reject(new Error("authentication service failed to load"));
      });
      document.head.appendChild(script);
    });
  }

  function showClerkError(message) {
    ELEMENTS.loginError.hidden = false;
    ELEMENTS.loginError.textContent = message;
  }

  function mountClerkUserButton() {
    if (!clerk || clerkUserButtonMounted) {
      return;
    }
    clerk.mountUserButton(ELEMENTS.clerkUserButton, { afterSignOutUrl: "/" });
    clerkUserButtonMounted = true;
    ELEMENTS.clerkUserButton.hidden = false;
  }

  function exchangeClerkSession(view) {
    if (!clerk || !clerk.session || clerkExchangePending) {
      return;
    }
    clerkExchangePending = true;
    ELEMENTS.loginError.hidden = true;
    clerk.session.getToken().then(function (token) {
      var payload = {};
      if (view) {
        payload.view_id = view;
      }
      return request("/api/v1/auth/clerk", {
        method: "POST",
        headers: {
          "Authorization": "Bearer " + token,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
        optionalAuth: true,
        skipCredential: true,
      });
    }).then(function (result) {
      if (result.status === 409 && Array.isArray(result.body.view_ids)) {
        clearChildren(ELEMENTS.clerkView);
        result.body.view_ids.forEach(function (viewId) {
          var option = document.createElement("option");
          option.value = visibleText(viewId);
          option.textContent = visibleText(viewId);
          ELEMENTS.clerkView.appendChild(option);
        });
        ELEMENTS.clerkViewForm.hidden = false;
        ELEMENTS.clerkSignIn.hidden = true;
        return;
      }
      if (result.status !== 200) {
        showClerkError(result.status === 403
          ? "Your Clerk account has not been invited to this repository."
          : "Clerk sign-in could not be verified.");
        return;
      }
      mountClerkUserButton();
      openSession(result.body.access_token, result.body.session);
    }).catch(function () {
      showClerkError("Clerk sign-in failed. Please try again.");
    }).then(function () {
      clerkExchangePending = false;
    });
  }

  function configureClerk(config) {
    if (!config.enabled) {
      return Promise.resolve();
    }
    ELEMENTS.clerkAuth.hidden = false;
    ELEMENTS.sessionNote.textContent =
      "Authentication, recovery, MFA, and account security are managed by Clerk.";
    return loadScript(config.frontend_api_origin + "/npm/@clerk/ui@1/dist/ui.browser.js")
      .then(function () {
        return loadScript(
          config.frontend_api_origin + "/npm/@clerk/clerk-js@6/dist/clerk.browser.js",
          { "data-clerk-publishable-key": config.publishable_key }
        );
      })
      .then(function () {
        clerk = window.Clerk;
        return clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor } });
      })
      .then(function () {
        clerkHadSession = Boolean(clerk.session);
        clerk.addListener(function (state) {
          var hasSession = Boolean(state.session);
          if (!clerkHadSession && hasSession && !session) {
            mountClerkUserButton();
            exchangeClerkSession();
          }
          if (clerkHadSession && !hasSession && credential) {
            request("/api/v1/logout", { method: "POST", optionalAuth: true })
              .then(clearBastionSession, clearBastionSession);
          }
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
    request("/api/v1/session", { headers: { Authorization: "Bearer " + token } })
      .then(function (result) {
        ELEMENTS.tokenInput.value = "";
        if (result.status !== 200) {
          ELEMENTS.loginError.hidden = false;
          ELEMENTS.loginError.textContent =
            result.status === 403 ? "This token cannot open the demo interface." : "Sign-in failed.";
          return;
        }
        openSession(token, result.body);
      })
      .catch(function (error) {
        ELEMENTS.tokenInput.value = "";
        if (error && error.message === "auth-required") {
          return;
        }
        ELEMENTS.loginError.hidden = false;
        ELEMENTS.loginError.textContent = "Sign-in failed.";
      });
  }

  ELEMENTS.loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var token = ELEMENTS.tokenInput.value.trim();
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.loginError.hidden = true;
    if (!token) {
      return;
    }
    authenticate(token);
  });

  ELEMENTS.clerkViewForm.addEventListener("submit", function (event) {
    event.preventDefault();
    exchangeClerkSession(ELEMENTS.clerkView.value);
  });

  ELEMENTS.manageAccount.addEventListener("click", function () {
    if (clerk) {
      clerk.openUserProfile();
    }
  });

  ELEMENTS.logout.addEventListener("click", function () {
    request("/api/v1/logout", { method: "POST", optionalAuth: true })
      .then(function () {
        clearBastionSession();
        return clerk ? clerk.signOut() : null;
      }, function () {
        clearBastionSession();
        return clerk ? clerk.signOut() : null;
      });
  });

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      showPanel(tab.id.replace("tab-", ""));
    });
  });

  // The Bastion credential starts empty; Clerk may restore its own session.
  clearBastionSession();
  request("/api/v1/auth/config", { optionalAuth: true })
    .then(function (result) {
      if (result.status !== 200) {
        throw new Error("authentication configuration unavailable");
      }
      return configureClerk(result.body);
    })
    .catch(function () {
      showClerkError("Authentication is temporarily unavailable.");
    });
})();
