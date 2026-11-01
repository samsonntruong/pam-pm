(function () {
  "use strict";

  var boardView = document.getElementById("view-board");
  if (!boardView) return;

  var project = boardView.getAttribute("data-project") || "default";
  var editable = boardView.getAttribute("data-editable") === "1";
  var statusEl = document.getElementById("board-status");
  var errorEl = document.getElementById("board-error");
  var filterEl = document.getElementById("sprint-filter");
  var drawer = document.getElementById("item-drawer");
  var drawerTitle = document.getElementById("drawer-title");
  var drawerBody = document.getElementById("drawer-body");
  var backdrop = document.getElementById("drawer-backdrop");
  var boardCache = null;
  var refreshTimer = null;
  var dragId = null;

  var BOARD_OPS = {
    create: 1,
    update: 1,
    move: 1,
    add_link: 1,
    set_sprint: 1,
    create_sprint: 1,
    close_sprint: 1,
    comment: 1
  };

  function esc(value) {
    var map = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    };
    return String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      function (ch) { return map[ch]; }
    );
  }

  function showError(message) {
    errorEl.innerHTML = "";
    var pill = document.createElement("span");
    pill.className = "pill pill-fail";
    pill.textContent = message;
    errorEl.appendChild(pill);
  }

  function clearError() {
    errorEl.innerHTML = "";
  }

  function failMessage(resp, data) {
    if (data && (data.message || data.hint)) {
      return (data.message || "") + (data.hint ? " " + data.hint : "");
    }
    return "Sorry, that did not work. Check your connection and try again.";
  }

  function friendlyError(err) {
    var msg = err && err.message ? String(err.message) : "";
    if (
      !msg ||
      /status \d+/.test(msg) ||
      /failed to fetch|networkerror|load failed|json|unexpected token|syntaxerror/i.test(msg)
    ) {
      return "Sorry, that did not work. Check your connection and try again.";
    }
    return msg;
  }

  function setPending(el, pending, busyText) {
    if (!el) return;
    if (pending) {
      if (el.dataset.orig === undefined) el.dataset.orig = el.textContent;
      el.disabled = true;
      if (busyText) el.textContent = busyText;
      el.setAttribute("aria-busy", "true");
    } else {
      el.disabled = false;
      if (el.dataset.orig !== undefined) el.textContent = el.dataset.orig;
      el.removeAttribute("aria-busy");
    }
  }

  function showPending(message) {
    errorEl.innerHTML = "";
    var pill = document.createElement("span");
    pill.className = "pill";
    pill.textContent = message;
    errorEl.appendChild(pill);
  }

  function pendingLine(id, message) {
    var box = document.getElementById(id);
    if (!box) return;
    box.innerHTML = "";
    var pill = document.createElement("span");
    pill.className = "pill";
    pill.textContent = message;
    box.appendChild(pill);
  }

  function api(path, options) {
    options = options || {};
    var url =
      path +
      (path.indexOf("?") >= 0 ? "&" : "?") +
      "project=" +
      encodeURIComponent(project);
    var opts = { headers: { "Content-Type": "application/json" } };
    if (options.method) opts.method = options.method;
    if (options.body !== undefined) opts.body = JSON.stringify(options.body);
    return fetch(url, opts).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok || !data.ok) throw new Error(failMessage(resp, data));
        return data;
      });
    });
  }

  function apiForm(path, formData) {
    var url = path + "?project=" + encodeURIComponent(project);
    return fetch(url, { method: "POST", body: formData }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok || !data.ok) throw new Error(failMessage(resp, data));
        return data;
      });
    });
  }

  function formObject(form) {
    var out = {};
    new FormData(form).forEach(function (value, key) {
      if (typeof value === "string") out[key] = value;
    });
    return out;
  }

  /* Theme lives in base.html so every page shares one toggle. */

  /* View tabs: Board, Backlog, Planning. */
  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-view]"));
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(function (other) {
        other.classList.remove("tab-active");
        other.setAttribute("aria-selected", "false");
        var panel = document.getElementById(other.getAttribute("data-view"));
        if (panel) panel.hidden = true;
      });
      tab.classList.add("tab-active");
      tab.setAttribute("aria-selected", "true");
      var current = document.getElementById(tab.getAttribute("data-view"));
      if (current) current.hidden = false;
    });
  });

  /* Sprint filter for the kanban columns. */
  function applyFilter() {
    var val = filterEl ? filterEl.value : "";
    Array.prototype.forEach.call(
      document.querySelectorAll("#kanban .kanban-card"),
      function (card) {
        var sprint = card.getAttribute("data-sprint") || "";
        var show =
          !val || (val === "__none__" ? !sprint : sprint === val);
        card.style.display = show ? "" : "none";
      }
    );
  }
  if (filterEl) filterEl.addEventListener("change", applyFilter);

  /* Refresh views from server HTML so the server stays the source of truth. */
  function scheduleRefresh() {
    if (refreshTimer) return;
    refreshTimer = setTimeout(function () {
      refreshTimer = null;
      refreshBoard();
    }, 800);
  }

  function refreshBoard() {
    fetch("/board?project=" + encodeURIComponent(project), {
      headers: { Accept: "text/html" }
    })
      .then(function (resp) {
        return resp.text();
      })
      .then(function (markup) {
        var doc = new DOMParser().parseFromString(markup, "text/html");
        ["kanban", "view-backlog", "view-planning", "links-card"].forEach(
          function (id) {
            var fresh = doc.getElementById(id);
            var live = document.getElementById(id);
            if (fresh && live) live.innerHTML = fresh.innerHTML;
          }
        );
        var freshTab = doc.getElementById("tab-backlog");
        var liveTab = document.getElementById("tab-backlog");
        if (freshTab && liveTab) liveTab.textContent = freshTab.textContent;
        applyFilter();
      })
      .catch(function () {
        /* keep the current view when refresh fails */
      });
  }

  function moveItem(id, state, trigger) {
    clearError();
    showPending("Saving your move...");
    setPending(trigger, true);
    api("/api/board/items/" + encodeURIComponent(id) + "/move", {
      method: "POST",
      body: { state: state }
    })
      .then(function () {
        clearError();
        scheduleRefresh();
      })
      .catch(function (err) {
        showError(friendlyError(err));
      })
      .finally(function () {
        setPending(trigger, false);
      });
  }

  /* Drag and drop between columns (Move to menu is the alternative). */
  document.addEventListener("dragstart", function (evt) {
    var card = evt.target && evt.target.closest
      ? evt.target.closest(".kanban-card")
      : null;
    if (!card || !editable) return;
    dragId = card.getAttribute("data-id");
    try {
      evt.dataTransfer.setData("text/plain", dragId);
      evt.dataTransfer.effectAllowed = "move";
    } catch (err) {
      /* clipboard types are best effort */
    }
    card.classList.add("dragging");
  });

  document.addEventListener("dragend", function () {
    dragId = null;
    Array.prototype.forEach.call(
      document.querySelectorAll(".kanban-card.dragging"),
      function (card) {
        card.classList.remove("dragging");
      }
    );
    Array.prototype.forEach.call(
      document.querySelectorAll(".kanban-drop.drop-hint"),
      function (zone) {
        zone.classList.remove("drop-hint");
      }
    );
  });

  document.addEventListener("dragover", function (evt) {
    var zone = evt.target && evt.target.closest
      ? evt.target.closest(".kanban-drop")
      : null;
    if (!zone || !editable || !dragId) return;
    evt.preventDefault();
    try {
      evt.dataTransfer.dropEffect = "move";
    } catch (err) {
      /* drop effect is best effort */
    }
    zone.classList.add("drop-hint");
  });

  document.addEventListener("drop", function (evt) {
    var zone = evt.target && evt.target.closest
      ? evt.target.closest(".kanban-drop")
      : null;
    if (!zone || !editable || !dragId) return;
    evt.preventDefault();
    zone.classList.remove("drop-hint");
    moveItem(dragId, zone.getAttribute("data-state") || "", null);
  });

  document.addEventListener("click", function (evt) {
    var mover = evt.target && evt.target.closest
      ? evt.target.closest("[data-move]")
      : null;
    if (mover && editable) {
      evt.preventDefault();
      moveItem(
        mover.getAttribute("data-id") || "",
        mover.getAttribute("data-move") || "",
        mover
      );
      return;
    }
    var opener = evt.target && evt.target.closest
      ? evt.target.closest("[data-open]")
      : null;
    if (opener) {
      openDrawer(opener.getAttribute("data-open") || "");
      return;
    }
    var closer = evt.target && evt.target.closest
      ? evt.target.closest("[data-close-sprint]")
      : null;
    if (closer && editable) {
      closeSprint(closer.getAttribute("data-close-sprint") || "", closer);
    }
  });

  document.addEventListener("change", function (evt) {
    var sel = evt.target && evt.target.closest
      ? evt.target.closest("[data-sprint-for]")
      : null;
    if (!sel || !editable || sel.disabled) return;
    clearError();
    showPending("Saving your change...");
    sel.disabled = true;
    api(
      "/api/board/items/" + encodeURIComponent(sel.getAttribute("data-sprint-for")) + "/sprint",
      { method: "POST", body: { iteration_id: sel.value } }
    )
      .then(function () {
        clearError();
        scheduleRefresh();
      })
      .catch(function (err) {
        showError(friendlyError(err));
      })
      .finally(function () {
        sel.disabled = false;
      });
  });

  function closeSprint(id, trigger) {
    clearError();
    showPending("Closing the sprint...");
    setPending(trigger, true, "Working...");
    api("/api/board/sprints/" + encodeURIComponent(id) + "/close", {
      method: "POST",
      body: {}
    })
      .then(function () {
        clearError();
        scheduleRefresh();
      })
      .catch(function (err) {
        showError(friendlyError(err));
      })
      .finally(function () {
        setPending(trigger, false);
      });
  }

  /* Item drawer: full fields, edit form, links, comments. */
  function getBoardData() {
    if (boardCache) return Promise.resolve(boardCache);
    return api("/api/board").then(function (data) {
      boardCache = data;
      return data;
    });
  }

  function stateOptions(states, current) {
    return states
      .map(function (st) {
        return (
          '<option value="' +
          esc(st) +
          '"' +
          (st === current ? " selected" : "") +
          ">" +
          esc(st) +
          "</option>"
        );
      })
      .join("");
  }

  function sprintOptions(iterations, current) {
    var out = '<option value="">No sprint</option>';
    out += iterations
      .map(function (entry) {
        return (
          '<option value="' +
          esc(entry.id) +
          '"' +
          (entry.id === current ? " selected" : "") +
          ">" +
          esc(entry.name) +
          "</option>"
        );
      })
      .join("");
    return out;
  }

  function renderDrawer(item, links, comments, meta) {
    var parts = [];
    parts.push(
      '<p class="muted"># ' +
        esc(item.id) +
        " &middot; " +
        esc(item.state) +
        (item.changed ? " &middot; changed " + esc(item.changed) : "") +
        "</p>"
    );
    if (item.external_url) {
      parts.push(
        '<p><a href="' + esc(item.external_url) + '">Open in ' + esc(meta.backend_label || "Azure DevOps") + '</a></p>'
      );
    }
    if (item.description) {
      parts.push("<h3>Description</h3><p>" + esc(item.description) + "</p>");
    }
    parts.push('<dl class="drawer-fields">');
    [
      ["Owner", item.owner],
      ["Estimate", item.estimate],
      ["Remaining", item.remaining],
      ["Priority", item.priority],
      ["Tags", item.tags_text],
      ["Due date", item.due_date],
      ["Sprint", item.sprint]
    ].forEach(function (row) {
      if (row[1] !== "" && row[1] !== null && row[1] !== undefined) {
        parts.push(
          "<div><dt>" + esc(row[0]) + "</dt><dd>" + esc(row[1]) + "</dd></div>"
        );
      }
    });
    parts.push("</dl>");

    if (meta.editable) {
      parts.push(
        '<h3>Edit</h3><form id="drawer-edit-form" data-id="' +
          esc(item.id) +
          '">' +
          '<div class="form-grid">' +
          '<label>Title <input type="text" name="title" value="' +
          esc(item.title) +
          '" maxlength="200"></label>' +
          '<label>Owner <input type="text" name="owner" value="' +
          esc(item.owner) +
          '"></label>' +
          "<label>State <select name=\"state\">" +
          stateOptions(meta.states || [], item.state) +
          "</select></label>" +
          "<label>Sprint <select name=\"iteration_id\">" +
          sprintOptions(meta.iterations || [], item.iteration_id) +
          "</select></label>" +
          '<label>Estimate <input type="text" name="estimate" value="' +
          esc(item.estimate) +
          '"></label>' +
          '<label>Remaining <input type="text" name="remaining" value="' +
          esc(item.remaining) +
          '"></label>' +
          '<label>Priority <input type="text" name="priority" value="' +
          esc(item.priority) +
          '"></label>' +
          '<label>Tags <input type="text" name="tags" value="' +
          esc(item.tags_text) +
          '"></label>' +
          '<label>Due date <input type="date" name="due_date" value="' +
          esc(item.due_date) +
          '"></label>' +
          "</div>" +
          '<label>Description <textarea name="description" rows="3">' +
          esc(item.description) +
          "</textarea></label>" +
          '<div class="form-actions"><button type="submit">Save changes</button></div>' +
          '<div class="result" id="drawer-edit-result" aria-live="polite"></div>' +
          "</form>"
      );
    }

    parts.push("<h3>Dependency links (" + links.length + ")</h3>");
    if (links.length) {
      parts.push("<ul>");
      links.forEach(function (link) {
        parts.push(
          "<li>" + esc(link.label) + ' <span class="muted">(' + esc(link.rel) + ")</span></li>"
        );
      });
      parts.push("</ul>");
    } else {
      parts.push('<p class="muted">No links on this item yet.</p>');
    }
    if (meta.editable && meta.capabilities && meta.capabilities.dependency_links) {
      parts.push(
        '<form id="drawer-link-form" data-id="' +
          esc(item.id) +
          '" class="inline-form">' +
          '<label>Target item id <input type="text" name="target_id" required autocomplete="off"></label>' +
          '<label>Relation <select name="rel">' +
          '<option value="System.LinkTypes.Dependency-Forward">This blocks target</option>' +
          '<option value="System.LinkTypes.Dependency-Reverse">This waits on target</option>' +
          "</select></label>" +
          "<button type=\"submit\">Add link</button>" +
          '<div class="result" id="drawer-link-result" aria-live="polite"></div>' +
          "</form>"
      );
    }

    parts.push("<h3>Comments (" + comments.length + ")</h3>");
    if (comments.length) {
      parts.push("<ul>");
      comments.forEach(function (entry) {
        parts.push(
          "<li>" +
            esc(entry.text) +
            ' <span class="muted">' +
            esc(entry.author || "unknown") +
            "</span></li>"
        );
      });
      parts.push("</ul>");
    } else {
      parts.push('<p class="muted">No comments yet.</p>');
    }
    if (meta.editable && meta.capabilities && meta.capabilities.comments) {
      parts.push(
        '<form id="drawer-comment-form" data-id="' +
          esc(item.id) +
          '">' +
          '<label>Add comment <textarea name="text" rows="2" required></textarea></label>' +
          '<div class="form-actions"><button type="submit">Add comment</button></div>' +
          '<div class="result" id="drawer-comment-result" aria-live="polite"></div>' +
          "</form>"
      );
    }
    return parts.join("");
  }

  function openDrawer(id) {
    drawer.hidden = false;
    backdrop.hidden = false;
    drawerTitle.textContent = "Item #" + id;
    drawerBody.innerHTML = '<p class="muted">Loading item...</p>';
    Promise.all([
      api("/api/board/items/" + encodeURIComponent(id)),
      getBoardData()
    ])
      .then(function (results) {
        var detail = results[0];
        var meta = results[1];
        drawerTitle.textContent = "#" + detail.item.id + " " + detail.item.title;
        drawerBody.innerHTML = renderDrawer(
          detail.item,
          detail.links || [],
          detail.comments || [],
          meta
        );
      })
      .catch(function (err) {
        drawerBody.innerHTML = "";
        var pill = document.createElement("span");
        pill.className = "pill pill-fail";
        pill.textContent = friendlyError(err);
        drawerBody.appendChild(pill);
      });
  }

  function closeDrawer() {
    drawer.hidden = true;
    backdrop.hidden = true;
  }

  document.getElementById("drawer-close").addEventListener("click", closeDrawer);
  backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape" && !drawer.hidden) closeDrawer();
  });

  function resultLine(id, ok, message) {
    var box = document.getElementById(id);
    if (!box) return;
    box.innerHTML = "";
    var pill = document.createElement("span");
    pill.className = ok ? "pill pill-ok" : "pill pill-fail";
    pill.textContent = message;
    box.appendChild(pill);
  }

  /* Panels: new item and CSV import. */
  var newPanel = document.getElementById("new-item-panel");
  var newBtn = document.getElementById("new-item-btn");
  var newCancel = document.getElementById("new-item-cancel");
  if (newBtn && newPanel) {
    newBtn.addEventListener("click", function () {
      newPanel.hidden = !newPanel.hidden;
    });
  }
  if (newCancel && newPanel) {
    newCancel.addEventListener("click", function () {
      newPanel.hidden = true;
    });
  }
  var importPanel = document.getElementById("import-panel");
  var importBtn = document.getElementById("import-btn");
  var importCancel = document.getElementById("import-cancel");
  if (importBtn && importPanel) {
    importBtn.addEventListener("click", function () {
      importPanel.hidden = !importPanel.hidden;
    });
  }
  if (importCancel && importPanel) {
    importCancel.addEventListener("click", function () {
      importPanel.hidden = true;
    });
  }

  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.id) return;
    if (form.id === "new-item-form") {
      evt.preventDefault();
      var newSubmit = form.querySelector('button[type="submit"]');
      setPending(newSubmit, true, "Saving...");
      pendingLine("new-item-result", "Saving your item...");
      api("/api/board/items", { method: "POST", body: formObject(form) })
        .then(function (data) {
          resultLine("new-item-result", true, "Created item " + data.item.id + ".");
          form.reset();
          boardCache = null;
          scheduleRefresh();
        })
        .catch(function (err) {
          resultLine("new-item-result", false, friendlyError(err));
        })
        .finally(function () {
          setPending(newSubmit, false);
        });
    } else if (form.id === "import-form") {
      evt.preventDefault();
      var importSubmit = form.querySelector('button[type="submit"]');
      setPending(importSubmit, true, "Importing...");
      pendingLine("import-result", "Importing your file...");
      var fileInput = form.querySelector('input[type="file"]');
      var pasted = form.querySelector('textarea[name="csv"]').value || "";
      var done = function (data) {
        var createdWord = data.created === 1 ? "item" : "items";
        resultLine(
          "import-result",
          true,
          "Imported " + data.created + " " + createdWord + ", skipped " + data.skipped + "."
        );
        boardCache = null;
        scheduleRefresh();
      };
      var failed = function (err) {
        resultLine("import-result", false, friendlyError(err));
      };
      var task;
      if (fileInput && fileInput.files && fileInput.files.length) {
        task = apiForm("/api/board/import", new FormData(form));
      } else {
        task = api("/api/board/import", {
          method: "POST",
          body: { csv: pasted }
        });
      }
      task.then(done).catch(failed).finally(function () {
        setPending(importSubmit, false);
      });
    } else if (form.id === "sprint-form") {
      evt.preventDefault();
      var sprintSubmit = form.querySelector('button[type="submit"]');
      setPending(sprintSubmit, true, "Saving...");
      pendingLine("sprint-result", "Creating your sprint...");
      api("/api/board/sprints", { method: "POST", body: formObject(form) })
        .then(function (data) {
          resultLine("sprint-result", true, "Created sprint " + data.sprint.name + ".");
          form.reset();
          boardCache = null;
          scheduleRefresh();
        })
        .catch(function (err) {
          resultLine("sprint-result", false, friendlyError(err));
        })
        .finally(function () {
          setPending(sprintSubmit, false);
        });
    } else if (form.id === "drawer-edit-form") {
      evt.preventDefault();
      var editSubmit = form.querySelector('button[type="submit"]');
      setPending(editSubmit, true, "Saving...");
      pendingLine("drawer-edit-result", "Saving your changes...");
      api("/api/board/items/" + encodeURIComponent(form.getAttribute("data-id")), {
        method: "PATCH",
        body: formObject(form)
      })
        .then(function (data) {
          resultLine("drawer-edit-result", true, "Saved item " + data.item.id + ".");
          boardCache = null;
          scheduleRefresh();
          openDrawer(data.item.id);
        })
        .catch(function (err) {
          resultLine("drawer-edit-result", false, friendlyError(err));
        })
        .finally(function () {
          setPending(editSubmit, false);
        });
    } else if (form.id === "drawer-link-form") {
      evt.preventDefault();
      var linkSubmit = form.querySelector('button[type="submit"]');
      setPending(linkSubmit, true, "Saving...");
      pendingLine("drawer-link-result", "Adding your link...");
      var fields = formObject(form);
      api("/api/board/links", {
        method: "POST",
        body: {
          source_id: form.getAttribute("data-id"),
          target_id: fields.target_id,
          rel: fields.rel
        }
      })
        .then(function () {
          boardCache = null;
          scheduleRefresh();
          openDrawer(form.getAttribute("data-id"));
        })
        .catch(function (err) {
          resultLine("drawer-link-result", false, friendlyError(err));
        })
        .finally(function () {
          setPending(linkSubmit, false);
        });
    } else if (form.id === "drawer-comment-form") {
      evt.preventDefault();
      var commentSubmit = form.querySelector('button[type="submit"]');
      setPending(commentSubmit, true, "Saving...");
      pendingLine("drawer-comment-result", "Adding your comment...");
      api(
        "/api/board/items/" + encodeURIComponent(form.getAttribute("data-id")) + "/comments",
        { method: "POST", body: formObject(form) }
      )
        .then(function () {
          boardCache = null;
          scheduleRefresh();
          openDrawer(form.getAttribute("data-id"));
        })
        .catch(function (err) {
          resultLine("drawer-comment-result", false, friendlyError(err));
        })
        .finally(function () {
          setPending(commentSubmit, false);
        });
    }
  });

  /* Live refresh: board writes arrive over the event stream. */
  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }
  try {
    var source = new EventSource("/api/activity/stream?live=1");
    source.onopen = function () {
      setStatus("Stream: live");
    };
    source.onerror = function () {
      setStatus("Stream: retrying");
    };
    source.onmessage = function (evt) {
      try {
        var data = JSON.parse(evt.data);
        if (data && BOARD_OPS[data.operation]) {
          boardCache = null;
          scheduleRefresh();
        }
      } catch (err) {
        /* keep the current view on parse errors */
      }
    };
  } catch (err) {
    setStatus("Stream: off");
  }
})();
