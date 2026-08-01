"use strict";

const MAX_FILES = 1000;
const MAX_PAGE_BYTES = 2 * 1024 * 1024;
const MAX_CONTENT_BYTES = 30 * 1024 * 1024;
const byId = (id) => document.getElementById(id);

const workspaceTabs = [...document.querySelectorAll("[data-workspace]")];
const editWorkspace = byId("edit-workspace");
const importWorkspace = byId("import-workspace");
const librarySummary = byId("library-summary");
const pageSearch = byId("page-search");
const pageList = byId("page-list");
const pageListEmpty = byId("page-list-empty");
const newPageButton = byId("new-page");
const publishForm = byId("publish-form");
const pagePath = byId("path");
const pageContent = byId("content");
const loadFileButton = byId("load-file-button");
const singleFile = byId("single-file");
const composerTitle = byId("composer-title");
const documentKind = byId("document-kind");
const documentState = byId("document-state");
const documentStateWrap = documentState.parentElement;
const editStatus = byId("edit-status");
const savePageButton = byId("save-page");
const openPage = byId("open-page");
const editorPane = byId("editor-pane");
const previewPane = byId("preview-pane");
const previewFrame = byId("preview-frame");
const writeMode = byId("write-mode");
const previewMode = byId("preview-mode");

const importForm = byId("import-form");
const folderInput = byId("folder");
const individualFilesInput = byId("individual-files");
const prefixInput = byId("prefix");
const keepRoot = byId("keep-root");
const overwrite = byId("overwrite");
const importButton = byId("import-button");
const importStatus = byId("import-status");
const manifestFiles = byId("manifest-files");
const manifestEmpty = byId("manifest-empty");
const fileCount = byId("file-count");
const fileSize = byId("file-size");
const folderName = byId("folder-name");

let pages = [];
let currentPath = null;
let currentRevision = "";
let originalContent = "";
let dirty = false;
let settingContent = false;
let previewTimer = 0;
let selectedFiles = [];

const editor = new TinyMDE.Editor({ textarea: pageContent, placeholder: "# Page title" });
new TinyMDE.CommandBar({
  element: "editor-toolbar",
  editor,
  commands: ["bold", "italic", "strikethrough", "|", "code", "|", "h1", "h2", "|", "ul", "ol", "blockquote", "|", "insertLink"],
});

function setStatus(element, message, state = "") {
  element.textContent = message;
  if (state) element.dataset.state = state;
  else delete element.dataset.state;
}

async function responseError(response) {
  try {
    return (await response.json()).error || `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function titleFromMarkdown(content, fallback = "Untitled page") {
  const heading = content.match(/^#\s+(.+?)\s*$/m);
  if (!heading) return fallback;
  return heading[1].replace(/[*_`<>]/g, "").trim() || fallback;
}

function setDirty(value) {
  dirty = value;
  documentStateWrap.dataset.dirty = String(value);
  if (value) documentState.textContent = "Unsaved changes";
  else if (currentPath) documentState.textContent = "Published";
  else documentState.textContent = "Not published";
  savePageButton.textContent = currentPath ? "Save changes" : "Publish new page";
}

function confirmDiscard() {
  return !dirty || window.confirm("Discard the unsaved changes in this page?");
}

function setEditorContent(content) {
  settingContent = true;
  editor.setContent(content);
  originalContent = content;
  settingContent = false;
  setDirty(false);
}

function newPage(content = "", suggestedPath = "") {
  if (!confirmDiscard()) return false;
  currentPath = null;
  currentRevision = "";
  pagePath.disabled = false;
  pagePath.value = suggestedPath;
  documentKind.textContent = "New document";
  composerTitle.textContent = titleFromMarkdown(content);
  openPage.hidden = true;
  setEditorContent(content);
  setStatus(editStatus, "Choose a path, write the page, then publish it.");
  renderPageList();
  pagePath.focus();
  return true;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function renderPageList() {
  const query = pageSearch.value.trim().toLocaleLowerCase();
  const filtered = pages.filter((page) => `${page.title} ${page.path}`.toLocaleLowerCase().includes(query));
  pageList.replaceChildren();
  filtered.forEach((page) => {
    const item = document.createElement("button");
    const title = document.createElement("strong");
    const path = document.createElement("span");
    item.type = "button";
    item.className = "page-item";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(page.path === currentPath));
    title.textContent = page.title;
    path.textContent = page.path;
    item.append(title, path);
    item.addEventListener("click", () => loadPage(page.path));
    pageList.append(item);
  });
  pageListEmpty.hidden = filtered.length > 0;
}

async function refreshLibrary(selectedPath = currentPath) {
  try {
    const response = await fetch("/api/pages", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(await responseError(response));
    pages = (await response.json()).pages;
    librarySummary.textContent = `${pages.length.toLocaleString()} ${pages.length === 1 ? "page" : "pages"} on the content volume`;
    currentPath = selectedPath;
    renderPageList();
  } catch (error) {
    librarySummary.textContent = "Content volume unavailable";
    setStatus(editStatus, `Page library failed: ${error.message}`, "error");
  }
}

async function loadPage(path) {
  if (path === currentPath || !confirmDiscard()) return;
  setStatus(editStatus, `Loading ${path}…`);
  try {
    const response = await fetch(`/api/page?path=${encodeURIComponent(path)}`, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(await responseError(response));
    const page = await response.json();
    currentPath = page.path;
    currentRevision = page.revision;
    pagePath.value = page.path;
    pagePath.disabled = true;
    documentKind.textContent = "Published document";
    composerTitle.textContent = page.title;
    openPage.href = page.url;
    openPage.hidden = false;
    setEditorContent(page.content);
    setStatus(editStatus, `Loaded ${page.path}.`);
    renderPageList();
  } catch (error) {
    setStatus(editStatus, `Load failed: ${error.message}`, "error");
  }
}

function previewDocument(html) {
  return `<!doctype html>
<html><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: https:; style-src 'unsafe-inline'">
<base target="_blank"><style>
:root{color-scheme:light dark;font:16px/1.65 system-ui,sans-serif;background:#fff;color:#142033}
body{margin:0;padding:2rem clamp(1rem,5vw,4rem)}article{max-width:54rem;margin:auto}h1,h2,h3{line-height:1.2;letter-spacing:-.025em}h1{font-size:2.4rem}h2{margin-top:2rem;border-bottom:1px solid #d5dfeb;padding-bottom:.35rem}a{color:#075f80}pre,code{font-family:ui-monospace,monospace;background:#edf2f7}code{padding:.12em .3em;border-radius:.25rem}pre{overflow:auto;padding:1rem;border-radius:.5rem}pre code{padding:0}blockquote{margin-left:0;border-left:3px solid #075f80;padding-left:1rem;color:#526176}table{border-collapse:collapse;width:100%}th,td{border:1px solid #d5dfeb;padding:.5rem;text-align:left}img{max-width:100%}
@media(prefers-color-scheme:dark){:root{background:#0e1a2b;color:#e8eff7}h2,th,td{border-color:#263a54}a{color:#63cbe8}pre,code{background:#14243a}blockquote{border-color:#63cbe8;color:#a8b6c8}}
</style></head><body><article>${html}</article></body></html>`;
}

async function refreshPreview() {
  setStatus(editStatus, "Rendering preview…");
  try {
    const response = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ content: editor.getContent() }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    previewFrame.srcdoc = previewDocument((await response.json()).html);
    setStatus(editStatus, dirty ? "Previewing unsaved changes." : "Preview is current.");
  } catch (error) {
    setStatus(editStatus, `Preview failed: ${error.message}`, "error");
  }
}

function setEditorMode(mode) {
  const previewing = mode === "preview";
  editorPane.hidden = previewing;
  previewPane.hidden = !previewing;
  writeMode.setAttribute("aria-selected", String(!previewing));
  previewMode.setAttribute("aria-selected", String(previewing));
  if (previewing) refreshPreview();
}

function schedulePreview() {
  if (previewPane.hidden) return;
  window.clearTimeout(previewTimer);
  previewTimer = window.setTimeout(refreshPreview, 450);
}

editor.addEventListener("change", (event) => {
  if (settingContent) return;
  composerTitle.textContent = titleFromMarkdown(event.content, currentPath ? composerTitle.textContent : "Untitled page");
  setDirty(event.content !== originalContent);
  schedulePreview();
});

publishForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = editor.getContent();
  if (!pagePath.value.trim() || !content.trim()) {
    setStatus(editStatus, "A page path and Markdown content are required.", "error");
    return;
  }
  savePageButton.disabled = true;
  setStatus(editStatus, "Saving and rebuilding topics and search…");
  try {
    const response = await fetch("/api/pages", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ path: pagePath.value, content, revision: currentRevision }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const result = await response.json();
    currentPath = result.saved;
    currentRevision = result.revision;
    originalContent = content;
    pagePath.value = result.saved;
    pagePath.disabled = true;
    documentKind.textContent = "Published document";
    openPage.href = result.url;
    openPage.hidden = false;
    setDirty(false);
    setStatus(editStatus, "Published. Topics and search are current.", "success");
    await refreshLibrary(result.saved);
  } catch (error) {
    setStatus(editStatus, `Save stopped: ${error.message}`, "error");
  } finally {
    savePageButton.disabled = false;
  }
});

newPageButton.addEventListener("click", () => newPage());
pageSearch.addEventListener("input", renderPageList);
writeMode.addEventListener("click", () => setEditorMode("write"));
previewMode.addEventListener("click", () => setEditorMode("preview"));
loadFileButton.addEventListener("click", () => singleFile.click());
singleFile.addEventListener("change", async () => {
  const file = singleFile.files[0];
  if (!file || !newPage()) return;
  const content = await file.text();
  pagePath.value = file.name;
  setEditorContent(content);
  composerTitle.textContent = titleFromMarkdown(content);
  setDirty(true);
  setStatus(editStatus, `Loaded ${file.name} as a new draft.`);
  singleFile.value = "";
});

workspaceTabs.forEach((tab) => tab.addEventListener("click", () => {
  const name = tab.dataset.workspace;
  if (name === "import" && !confirmDiscard()) return;
  workspaceTabs.forEach((candidate) => candidate.setAttribute("aria-selected", String(candidate === tab)));
  editWorkspace.hidden = name !== "edit";
  importWorkspace.hidden = name !== "import";
  history.replaceState(null, "", name === "import" ? "#import" : "#edit");
}));

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "s" && !editWorkspace.hidden) {
    event.preventDefault();
    publishForm.requestSubmit();
  }
});
window.addEventListener("beforeunload", (event) => {
  if (!dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

function sourceParts(file) {
  return (file.webkitRelativePath || file.name).split("/").filter(Boolean);
}

function importPath(file) {
  const parts = sourceParts(file);
  const relative = keepRoot.checked || parts.length === 1 ? parts : parts.slice(1);
  const prefix = prefixInput.value.trim().replace(/^\/+|\/+$/g, "");
  return [...(prefix ? [prefix] : []), ...relative].join("/");
}

function renderManifest(preserveStatus = false) {
  const total = selectedFiles.reduce((sum, file) => sum + file.size, 0);
  fileCount.textContent = selectedFiles.length.toLocaleString();
  fileSize.textContent = formatBytes(total);
  const firstParts = selectedFiles.length ? sourceParts(selectedFiles[0]) : [];
  folderName.textContent = selectedFiles.length ? (firstParts.length > 1 ? firstParts[0] : "Selected files") : "—";
  manifestFiles.replaceChildren();
  manifestEmpty.hidden = selectedFiles.length > 0;
  manifestFiles.hidden = selectedFiles.length === 0;
  selectedFiles.slice(0, 10).forEach((file) => {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = importPath(file);
    item.append(code);
    manifestFiles.append(item);
  });
  if (selectedFiles.length > 10) {
    const item = document.createElement("li");
    item.textContent = `…and ${selectedFiles.length - 10} more`;
    manifestFiles.append(item);
  }
  const tooMany = selectedFiles.length > MAX_FILES;
  const tooLarge = total > MAX_CONTENT_BYTES;
  const oversized = selectedFiles.find((file) => file.size > MAX_PAGE_BYTES);
  importButton.disabled = !selectedFiles.length || tooMany || tooLarge || Boolean(oversized);
  if (tooMany) setStatus(importStatus, `Select no more than ${MAX_FILES.toLocaleString()} Markdown pages.`, "error");
  else if (tooLarge) setStatus(importStatus, "The selected Markdown exceeds the 30 MiB content limit.", "error");
  else if (oversized) setStatus(importStatus, `${oversized.name} exceeds the 2 MiB page limit.`, "error");
  else if (!preserveStatus) setStatus(importStatus, selectedFiles.length ? "Ready to import. Existing pages remain protected by default." : "");
}

function selectImportFiles(fileList) {
  selectedFiles = [...fileList].filter((file) => file.name.toLocaleLowerCase().endsWith(".md") && !sourceParts(file).some((part) => part.startsWith(".")));
  renderManifest();
}

folderInput.addEventListener("change", () => selectImportFiles(folderInput.files));
individualFilesInput.addEventListener("change", () => selectImportFiles(individualFilesInput.files));
prefixInput.addEventListener("input", () => renderManifest());
keepRoot.addEventListener("change", () => renderManifest());

importForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (importButton.disabled) return;
  importButton.disabled = true;
  setStatus(importStatus, `Reading ${selectedFiles.length.toLocaleString()} pages…`);
  try {
    const importedPages = await Promise.all(selectedFiles.map(async (file) => ({ path: importPath(file), content: await file.text() })));
    setStatus(importStatus, "Uploading, validating, and rebuilding once…");
    const response = await fetch("/api/pages/import", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ pages: importedPages, overwrite: overwrite.checked }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const result = await response.json();
    setStatus(importStatus, `Imported ${result.count.toLocaleString()} pages. Topics and search are current.`, "success");
    await refreshLibrary();
  } catch (error) {
    setStatus(importStatus, `Import stopped: ${error.message}`, "error");
  } finally {
    renderManifest(true);
  }
});

refreshLibrary();
newPage();
if (location.hash === "#import") byId("import-tab").click();
