// ---- tiny state ----
let quill = null;
let currentPaper = null; // full paper JSON
let currentQuestion = null; // current question object (reference into currentPaper.questions)
let currentInstruction = null; // current instruction object (reference into currentPaper.instructions) - mutually exclusive with currentQuestion
let pendingSelectPaperId = null; // set right after a process job finishes
let jobPollTimer = null;

// ---- inline formula / chemistry / diagram tokens (see src/marks.py) ----
// Stored in the stem/option HTML as <span class="math-token" data-type data-latex>
// and <img class="diagram-token" src="images/diagrams/...">. The span is
// rendered with KaTeX (+ mhchem for chemistry) only for display - it is
// emptied again before saving, so the rendered markup never reaches
// paper.json - and a diagram's relative src is expanded to the paper's
// image route for display and shrunk back on save.
function renderMathTo(el, latex, type) {
  if (typeof katex === "undefined") {
    el.textContent = latex;
    return;
  }
  // Chemistry is written in mhchem syntax; a bare "H2SO4" is wrapped for the
  // user, an explicit \ce{...} is left alone.
  const source = type === "chemistry" && !/\\ce\s*\{/.test(latex) ? `\\ce{${latex}}` : latex;
  try {
    katex.render(source, el, { throwOnError: false, displayMode: false });
  } catch (err) {
    el.textContent = latex;
  }
}

function renderMathTokens(container) {
  container.querySelectorAll("span.math-token").forEach((el) => {
    renderMathTo(el, el.dataset.latex || "", el.dataset.type);
  });
}

function paperAssetPrefix() {
  return `/api/papers/${currentPaper.paper_id}/`;
}

function setEditableHtml(el, html) {
  const prefix = paperAssetPrefix();
  el.innerHTML = (html || "").split('src="images/diagrams/').join(`src="${prefix}images/diagrams/`);
  renderMathTokens(el);
}

function serializeEditable(el) {
  const clone = el.cloneNode(true);
  clone.querySelectorAll("span.math-token").forEach((n) => {
    n.innerHTML = "";
  });
  return clone.innerHTML.split(`${paperAssetPrefix()}images/diagrams/`).join("images/diagrams/");
}

// Independently-debounced saves per editable field (stem, directions,
// passage, explanation, each option) - a question can have several fields
// mid-edit at once, each needs its own timer, and switching questions must
// flush every pending one (not just whichever was last touched).
const pendingSaves = new Map(); // fieldKey -> {timer, flush}

function scheduleFieldSave(fieldKey, flushFn) {
  const existing = pendingSaves.get(fieldKey);
  if (existing) clearTimeout(existing.timer);
  setSaveIndicator("saving");
  const timer = setTimeout(() => {
    pendingSaves.delete(fieldKey);
    flushFn();
  }, 600);
  pendingSaves.set(fieldKey, { timer, flush: flushFn });
}

function flushAllPendingSaves() {
  const inFlight = [];
  for (const { timer, flush } of pendingSaves.values()) {
    clearTimeout(timer);
    inFlight.push(flush());
  }
  pendingSaves.clear();
  return Promise.all(inFlight);
}

// ---- tab switching ----
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  document.body.dataset.activeTab = name;
  // The Paper/Question controls live in the topbar (saves a whole toolbar row)
  // but only make sense while reviewing.
  document.getElementById("topbar-review-controls").hidden = name !== "review";
  if (name === "review") loadPapers();
  if (name === "jobs") loadJobs();
}

// ---- Process tab ----
let variantsById = {}; // id -> {id, label, fields} - looked up when rendering per-variant extra inputs

async function loadVariants() {
  const res = await fetch("/api/variants");
  const variants = await res.json();
  variantsById = Object.fromEntries(variants.map((v) => [v.id, v]));
  const select = document.getElementById("variant-select");
  select.innerHTML = "";
  for (const v of variants) {
    const opt = document.createElement("option");
    opt.value = v.id;
    opt.textContent = v.label;
    select.appendChild(opt);
  }
  renderVariantFields(select.value);
}

// Some variants need extra booklet-specific input (e.g. a page boundary
// that isn't safe to hardcode - see src/variant_ndana_gat.py) - rendered
// here from the variant's own "fields" metadata rather than being
// hardcoded per variant in the form markup.
function renderVariantFields(variantId) {
  const container = document.getElementById("variant-fields");
  container.innerHTML = "";
  const fields = (variantsById[variantId] && variantsById[variantId].fields) || [];
  for (const field of fields) {
    const label = document.createElement("label");
    label.textContent = field.label;
    const input = document.createElement("input");
    input.type = field.type === "number" ? "number" : "text";
    input.min = "1";
    input.name = field.name;
    input.required = !!field.required;
    label.appendChild(input);
    container.appendChild(label);
  }
}

document.getElementById("variant-select").addEventListener("change", (e) => renderVariantFields(e.target.value));

document.getElementById("process-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fileInput = document.getElementById("pdf-input");
  const variantId = document.getElementById("variant-select").value;
  const jobName = document.getElementById("job-name-input").value.trim();
  if (!fileInput.files.length) return;

  const form = new FormData();
  form.append("pdf", fileInput.files[0]);
  form.append("variant_id", variantId);
  if (jobName) form.append("job_name", jobName);
  for (const input of document.querySelectorAll("#variant-fields input")) {
    form.append(input.name, input.value);
  }

  document.getElementById("process-btn").disabled = true;
  document.getElementById("process-error").hidden = true;
  document.getElementById("process-done").hidden = true;
  const statusEl = document.getElementById("process-status");
  const statusText = document.getElementById("process-status-text");
  statusEl.hidden = false;
  statusText.textContent = "Uploading...";

  try {
    const res = await fetch("/api/process", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "failed to start processing");
    pollJob(data.job_id, data.paper_id, statusText, statusEl);
  } catch (err) {
    showProcessError(err.message);
  }
});

function pollJob(jobId, paperId, statusText, statusEl) {
  clearInterval(jobPollTimer);
  jobPollTimer = setInterval(async () => {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (job.status === "running" || job.status === "pending") {
      statusText.textContent = job.progress || job.status;
    } else if (job.status === "done") {
      clearInterval(jobPollTimer);
      statusEl.hidden = true;
      document.getElementById("process-btn").disabled = false;
      pendingSelectPaperId = paperId;
      const doneEl = document.getElementById("process-done");
      doneEl.hidden = false;
      document.getElementById("go-to-review-link").onclick = (e) => {
        e.preventDefault();
        switchTab("review");
      };
    } else if (job.status === "error") {
      clearInterval(jobPollTimer);
      statusEl.hidden = true;
      document.getElementById("process-btn").disabled = false;
      showProcessError(job.error || "processing failed");
    }
  }, 2000);
}

function showProcessError(msg) {
  const el = document.getElementById("process-error");
  el.textContent = msg;
  el.hidden = false;
}

// ---- Review tab ----
async function loadPapers() {
  const res = await fetch("/api/papers");
  const papers = await res.json();
  const select = document.getElementById("paper-select");
  const prevValue = select.value;
  select.innerHTML = "";
  for (const p of papers) {
    const opt = document.createElement("option");
    opt.value = p.paper_id;
    opt.textContent = p.label;
    select.appendChild(opt);
  }

  let toSelect = pendingSelectPaperId || prevValue || (papers[0] && papers[0].paper_id);
  pendingSelectPaperId = null;
  if (toSelect) {
    select.value = toSelect;
    await loadPaper(toSelect);
  } else {
    currentPaper = null;
    currentQuestion = null;
    document.getElementById("question-select").innerHTML = "";
    document.getElementById("review-body").hidden = true;
    document.getElementById("review-empty").hidden = false;
  }
}

document.getElementById("paper-select").addEventListener("change", (e) => loadPaper(e.target.value));

// ---- Jobs tab (rename / delete) ----
async function loadJobs() {
  const res = await fetch("/api/papers");
  const papers = await res.json();
  const body = document.getElementById("jobs-body");
  body.innerHTML = "";
  document.getElementById("jobs-empty").hidden = papers.length > 0;
  document.getElementById("jobs-table").hidden = papers.length === 0;
  for (const p of papers) body.appendChild(renderJobRow(p));
}

function showJobsError(msg) {
  const el = document.getElementById("jobs-error");
  el.textContent = msg || "";
  el.hidden = !msg;
}

function renderJobRow(p) {
  const tr = document.createElement("tr");
  const nameTd = document.createElement("td");
  const nameSpan = document.createElement("span");
  nameSpan.textContent = p.label;
  nameTd.appendChild(nameSpan);

  const cell = (text) => {
    const td = document.createElement("td");
    td.textContent = text;
    return td;
  };
  const when = p.processed_at ? new Date(p.processed_at).toLocaleString() : "";
  const counts = `${p.total_questions} q, ${p.needs_review_count} flagged`;

  const actionsTd = document.createElement("td");
  actionsTd.className = "jobs-actions";
  const renameBtn = document.createElement("button");
  renameBtn.type = "button";
  renameBtn.className = "rename-paper-btn";
  renameBtn.textContent = "Rename";
  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "delete-paper-btn";
  deleteBtn.title = "Delete this job permanently";
  deleteBtn.textContent = "Delete";
  actionsTd.append(renameBtn, deleteBtn);

  renameBtn.addEventListener("click", () => startRename(p, nameTd, nameSpan, actionsTd));
  deleteBtn.addEventListener("click", () => deleteJob(p));

  tr.append(nameTd, cell(p.source_filename || ""), cell(when), cell(counts), actionsTd);
  return tr;
}

function startRename(p, nameTd, nameSpan, actionsTd) {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "jobs-rename-input";
  input.value = p.label;
  input.maxLength = 200;
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.textContent = "Save";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";

  nameSpan.hidden = true;
  nameTd.append(input);
  const normalButtons = [...actionsTd.children];
  normalButtons.forEach((b) => (b.hidden = true));
  actionsTd.append(saveBtn, cancelBtn);
  input.focus();
  input.select();

  const finish = () => {
    input.remove();
    saveBtn.remove();
    cancelBtn.remove();
    nameSpan.hidden = false;
    normalButtons.forEach((b) => (b.hidden = false));
  };
  const save = async () => {
    const name = input.value.trim();
    if (!name || name === p.label) return finish();
    showJobsError("");
    const res = await fetch(`/api/papers/${p.paper_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_name: name }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showJobsError(data.error || "failed to rename job");
      return;
    }
    await loadJobs();
  };
  saveBtn.addEventListener("click", save);
  cancelBtn.addEventListener("click", finish);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") save();
    if (e.key === "Escape") finish();
  });
}

async function deleteJob(p) {
  if (!confirm(`Delete this job permanently?\n\n${p.label}\n\nThis removes its saved answers, explanations, and images - it cannot be undone.`)) {
    return;
  }
  showJobsError("");
  const res = await fetch(`/api/papers/${p.paper_id}`, { method: "DELETE" });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    showJobsError(data.error || "failed to delete job");
    return;
  }
  if (currentPaper && currentPaper.paper_id === p.paper_id) {
    currentPaper = null;
    currentQuestion = null;
  }
  await loadJobs();
}

// Interleaves each instruction into the question sequence right before the
// first question it applies to (e.g. Q7, I1, Q8, Q9) - the natural reading
// order, instead of listing all instructions separately from questions.
function buildNavSequence(paper) {
  const instByFirstQ = new Map();
  for (const inst of paper.instructions || []) {
    instByFirstQ.set(inst.applies_to[0], inst);
  }
  const seq = [];
  for (const q of paper.questions) {
    const inst = instByFirstQ.get(q.q_number);
    if (inst) seq.push({ type: "instruction", id: inst.instruction_id });
    seq.push({ type: "question", q_number: q.q_number });
  }
  return seq;
}

async function loadPaper(paperId, selectValue) {
  if (!paperId) return;
  const res = await fetch(`/api/papers/${paperId}`);
  currentPaper = await res.json();
  await loadVocabSuggestions(currentPaper.variant);

  const qSelect = document.getElementById("question-select");
  qSelect.innerHTML = "";
  const seq = buildNavSequence(currentPaper);
  for (const item of seq) {
    const opt = document.createElement("option");
    if (item.type === "instruction") {
      opt.value = `I:${item.id}`;
      opt.textContent = `${item.id} (Instructions)`;
      opt.className = "instruction-option";
    } else {
      const q = currentPaper.questions.find((qq) => qq.q_number === item.q_number);
      opt.value = `Q:${item.q_number}`;
      opt.textContent = `Q${item.q_number}${q.needs_review ? " ⚠" : ""}`;
    }
    qSelect.appendChild(opt);
  }
  document.getElementById("review-empty").hidden = true;
  document.getElementById("review-body").hidden = false;
  if (seq.length) renderSelection(selectValue || qSelect.options[0].value);
}

// Every tag/topic ever entered for this paper's variant, so typing a new
// one on any question of any paper of the same variant suggests ones
// already used elsewhere - shared vocabulary across papers of one variant.
async function loadVocabSuggestions(variantId) {
  if (!variantId) return;
  try {
    const res = await fetch(`/api/variants/${variantId}/vocab`);
    const vocab = await res.json();
    fillDatalist("tag-suggestions", vocab.tags || []);
    fillDatalist("topic-suggestions", vocab.topics || []);
  } catch (err) {
    // suggestions are a nicety, not critical - fail quietly
  }
}

function fillDatalist(id, values) {
  const list = document.getElementById(id);
  list.innerHTML = "";
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    list.appendChild(opt);
  }
}

function rememberSuggestion(datalistId, value) {
  const list = document.getElementById(datalistId);
  const exists = Array.from(list.options).some((o) => o.value.toLowerCase() === value.toLowerCase());
  if (!exists) {
    const opt = document.createElement("option");
    opt.value = value;
    list.appendChild(opt);
  }
}

document.getElementById("question-select").addEventListener("change", (e) => renderSelection(e.target.value));

document.getElementById("prev-q-btn").addEventListener("click", () => stepSelection(-1));
document.getElementById("next-q-btn").addEventListener("click", () => stepSelection(1));

function stepSelection(delta) {
  const qSelect = document.getElementById("question-select");
  const idx = qSelect.selectedIndex + delta;
  if (idx >= 0 && idx < qSelect.options.length) {
    qSelect.selectedIndex = idx;
    renderSelection(qSelect.value);
  }
}

// The nav dropdown/prev-next walk a single sequence of "Q:<n>" and
// "I:<id>" values (see buildNavSequence) - dispatch to whichever render
// function the selected entry needs.
function renderSelection(value) {
  if (!value) return;
  const [kind, id] = value.split(":");
  if (kind === "I") {
    renderInstruction(id);
  } else {
    renderQuestion(Number(id));
  }
}

// Quill's built-in "formula" blot renders KaTeX inline (compact, small) -
// override it to render in KaTeX's display mode instead, so a formula
// pasted into an explanation looks exactly like its Scratchpad preview
// (which also renders in display mode), not visibly different/smaller.
function registerDisplayModeFormula() {
  const Embed = Quill.import("blots/embed");
  class DisplayFormula extends Embed {
    static create(value) {
      const node = super.create(value);
      if (typeof value === "string") {
        katex.render(value, node, { throwOnError: false, displayMode: true });
        node.setAttribute("data-value", value);
      }
      return node;
    }
    static value(domNode) {
      return domNode.getAttribute("data-value");
    }
  }
  DisplayFormula.blotName = "formula";
  DisplayFormula.className = "ql-formula";
  DisplayFormula.tagName = "SPAN";
  Quill.register(DisplayFormula, true);
}

function getQuillInstance() {
  if (!quill) {
    registerDisplayModeFormula();
    quill = new Quill("#editor", {
      theme: "snow",
      modules: {
        toolbar: [
          ["bold", "italic", "underline"],
          [{ color: [] }],
          [{ size: ["small", false, "large", "huge"] }],
          [{ font: [] }],
          ["formula", "image"],
          ["clean"],
        ],
      },
    });
    quill.on("text-change", (delta, oldDelta, source) => {
      if (!currentQuestion || source !== "user") return;
      // Same reason as the other editable fields: keep local state in sync
      // immediately so a re-render before the debounced save lands doesn't
      // redraw the editor from a stale value.
      currentQuestion.explanation_html = quill.root.innerHTML;
      scheduleFieldSave("explanation", () =>
        saveQuestionField(currentQuestion.q_number, { explanation_html: quill.root.innerHTML })
      );
    });
    // The toolbar's own "image" button only opens a file picker - also
    // accept an image pasted straight from the clipboard (e.g. copied out
    // of the LaTeX Scratchpad's source image, or from anywhere else),
    // embedding it as a base64 data URL like the toolbar path does.
    quill.root.addEventListener("paste", (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      const imageItem = Array.from(items).find((item) => item.type && item.type.startsWith("image/"));
      if (!imageItem) return;
      e.preventDefault();
      const file = imageItem.getAsFile();
      const reader = new FileReader();
      reader.onload = () => {
        const range = quill.getSelection(true) || { index: quill.getLength() };
        quill.insertEmbed(range.index, "image", reader.result, "user");
        quill.setSelection(range.index + 1);
      };
      reader.readAsDataURL(file);
    });
  }
  return quill;
}

function renderQuestion(qNumber) {
  flushAllPendingSaves();
  currentQuestion = currentPaper.questions.find((q) => q.q_number === qNumber);
  if (!currentQuestion) return;
  currentInstruction = null;
  document.getElementById("question-select").value = `Q:${qNumber}`;
  document.getElementById("pane-question-label").textContent = "Question";
  const numberInput = document.getElementById("question-number-input");
  numberInput.hidden = false;
  numberInput.value = currentQuestion.q_number;
  const typeSelect = document.getElementById("question-type-select");
  typeSelect.hidden = false;
  typeSelect.value = currentQuestion.question_type || "standard";
  document.getElementById("question-body-content").hidden = false;
  document.getElementById("instruction-body").hidden = true;
  document.querySelector(".pane-notes").hidden = false;

  const meta = document.getElementById("review-meta");
  const metaText = document.getElementById("review-meta-text");
  metaText.textContent = currentQuestion.needs_review
    ? `⚠ needs review: ${currentQuestion.review_reason}`
    : `confidence ${currentQuestion.ocr_confidence}`;
  meta.classList.toggle("needs-review", !!currentQuestion.needs_review);
  document.getElementById("review-meta-dismiss-btn").hidden = !currentQuestion.needs_review;
  updateNavOptionWarning(qNumber, currentQuestion.needs_review);

  const img = document.getElementById("question-image");
  img.src = currentQuestion.image ? `/api/papers/${currentPaper.paper_id}/${currentQuestion.image}` : "";

  const passageBlock = document.getElementById("passage-block");
  if (currentQuestion.passage_text_html) {
    passageBlock.hidden = false;
    document.getElementById("passage-label").textContent = currentQuestion.passage_label || "Passage";
    document.getElementById("passage-text").innerHTML = currentQuestion.passage_text_html;
  } else {
    passageBlock.hidden = true;
  }

  const isMatchList = currentQuestion.question_type === "match_the_list" && currentQuestion.table;
  // A match-the-list question can also come without a "Code" answer grid,
  // just ordinary (a)-(d) text answers ("I-D, II-C, ...") - then only the two
  // lists are tabulated and the normal options render below them.
  const isListOnly = isMatchList && !currentQuestion.table.code_table;
  // Unlike match_the_list, a paired_table doesn't replace the stem/options
  // - it's a "Read the following pairs :"-style table embedded inside an
  // otherwise-standard question, so the normal stem and (a)-(d) options
  // both still render; the table is just shown alongside them for a
  // cleaner read than the flattened OCR text alone.
  const isPairedTable = currentQuestion.question_type === "paired_table" && currentQuestion.table;
  const isGridTable = currentQuestion.question_type === "grid_table" && currentQuestion.table && currentQuestion.table.rows;
  const stemEl = document.getElementById("question-stem");
  // the raw OCR'd stem for match-the-list questions is just the jumbled
  // List/Code text the table below already presents cleanly - showing both
  // is confusing, so hide the raw version once we have a real table.
  stemEl.hidden = isMatchList;
  document.getElementById("insert-row").hidden = isMatchList && !isListOnly;
  const afterTableEl = document.getElementById("stem-after-table");
  afterTableEl.hidden = !isGridTable;
  if (isGridTable) setEditableHtml(afterTableEl, currentQuestion.stem_after_table_html);
  closeFormulaPopover();
  closeSymbolPalette();
  setEditableHtml(stemEl, currentQuestion.question_stem_html);

  const optionsBlock = document.getElementById("options-block");
  const tableBlock = document.getElementById("table-block");

  if (isMatchList && !isListOnly) {
    optionsBlock.hidden = true;
    tableBlock.hidden = false;
    tableBlock.innerHTML = renderMatchListTable(currentQuestion.table);
  } else {
    tableBlock.hidden = !isPairedTable && !isListOnly && !isGridTable;
    if (isGridTable) renderGridTable(tableBlock, currentQuestion.table);
    if (isPairedTable) tableBlock.innerHTML = renderPairedTable(currentQuestion.table);
    if (isListOnly) tableBlock.innerHTML = renderMatchListTable(currentQuestion.table);
    optionsBlock.hidden = false;
    optionsBlock.innerHTML = "";
    for (const letter of ["a", "b", "c", "d"]) {
      const card = document.createElement("div");
      card.className = "option-card" + (currentQuestion.user_answer === letter ? " selected" : "");

      const letterSpan = document.createElement("span");
      letterSpan.className = "option-letter";
      letterSpan.textContent = `(${letter})`;

      const textSpan = document.createElement("span");
      textSpan.className = "option-text";
      textSpan.contentEditable = "true";
      setEditableHtml(textSpan, currentQuestion.options[letter]);
      // Editing the text shouldn't also register a click-to-select on the
      // card it lives inside.
      textSpan.addEventListener("mousedown", (e) => e.stopPropagation());
      textSpan.addEventListener("click", (e) => e.stopPropagation());
      textSpan.addEventListener("click", onTokenClick);
      textSpan.addEventListener("input", () => {
        // Keep local state in sync immediately, not just the server - a
        // re-render before the debounced save lands (switching questions
        // and back, or clicking the card to select it as the answer) would
        // otherwise redraw from the stale pre-edit value and the edit
        // would visually vanish, even though it saved fine.
        currentQuestion.options[letter] = serializeEditable(textSpan);
        scheduleFieldSave(`option_${letter}`, () =>
          saveQuestionField(currentQuestion.q_number, { options: { [letter]: serializeEditable(textSpan) } })
        );
      });

      card.appendChild(letterSpan);
      card.appendChild(textSpan);
      card.addEventListener("click", () => selectAnswer(letter));
      optionsBlock.appendChild(card);
    }
  }

  document.getElementById("clear-answer-btn").hidden = !currentQuestion.user_answer;

  const editor = getQuillInstance();
  editor.setContents(editor.clipboard.convert(currentQuestion.explanation_html || ""));
  setSaveIndicator("");

  renderTags();
}

// An instruction ("Directions :") shared by a run of questions gets its
// own entry in the nav sequence (see buildNavSequence) - shown the same
// way a question is (image left, editable text middle), but without
// answer options or the Explanation/Topics/Tags notes pane, none of which
// apply to it.
function renderInstruction(instructionId) {
  flushAllPendingSaves();
  currentInstruction = (currentPaper.instructions || []).find((i) => i.instruction_id === instructionId);
  if (!currentInstruction) return;
  currentQuestion = null;
  document.getElementById("question-select").value = `I:${instructionId}`;
  document.getElementById("pane-question-label").textContent = "Instructions";
  document.getElementById("question-type-select").hidden = true;
  document.getElementById("question-number-input").hidden = true;
  document.getElementById("question-body-content").hidden = true;
  document.getElementById("instruction-body").hidden = false;
  document.querySelector(".pane-notes").hidden = true;

  const applies = currentInstruction.applies_to;
  const range = applies.length > 1 ? `Q${applies[0]}-Q${applies[applies.length - 1]}` : `Q${applies[0]}`;
  const meta = document.getElementById("review-meta");
  document.getElementById("review-meta-text").textContent = `applies to ${range}`;
  meta.classList.remove("needs-review");
  document.getElementById("review-meta-dismiss-btn").hidden = true;

  const img = document.getElementById("question-image");
  img.src = currentInstruction.image ? `/api/papers/${currentPaper.paper_id}/${currentInstruction.image}` : "";

  document.getElementById("instruction-meta").textContent = `Instructions - applies to ${range}`;
  document.getElementById("instruction-text").innerHTML = currentInstruction.text_html || "";
  setSaveIndicator("");
}

document.getElementById("instruction-text").addEventListener("input", (e) => {
  if (!currentInstruction) return;
  currentInstruction.text_html = e.target.innerHTML;
  scheduleFieldSave("instruction_text", () =>
    saveInstructionField(currentInstruction.instruction_id, { text_html: e.target.innerHTML })
  );
});

async function saveInstructionField(instructionId, updates) {
  setSaveIndicator("saving");
  try {
    const res = await fetch(`/api/papers/${currentPaper.paper_id}/instructions/${instructionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    if (!res.ok) throw new Error("save failed");
    setSaveIndicator("saved");
  } catch (err) {
    setSaveIndicator("error saving");
  }
}

function clearAnswer() {
  currentQuestion.user_answer = null;
  renderQuestion(currentQuestion.q_number);
  saveQuestionField(currentQuestion.q_number, { user_answer: null });
}
document.getElementById("clear-answer-btn").addEventListener("click", clearAnswer);

// Keeps the nav dropdown's "⚠" suffix in sync whenever a question's
// needs_review flag changes (dismissed by hand, or flipped again by a
// reprocess) - rather than only ever reflecting whatever it was when the
// paper was first loaded.
function updateNavOptionWarning(qNumber, needsReview) {
  const opt = document.querySelector(`#question-select option[value="Q:${qNumber}"]`);
  if (!opt) return;
  opt.textContent = needsReview ? `Q${qNumber} ⚠` : `Q${qNumber}`;
}

function dismissNeedsReview() {
  if (!currentQuestion) return;
  currentQuestion.needs_review = false;
  currentQuestion.review_reason = "";
  renderQuestion(currentQuestion.q_number);
  saveQuestionField(currentQuestion.q_number, { needs_review: false, review_reason: "" });
}
document.getElementById("review-meta-dismiss-btn").addEventListener("click", dismissNeedsReview);

function renderMatchListTable(table) {
  const list1 = table.list1 || [];
  const list2 = table.list2 || [];
  const rowCount = Math.max(list1.length, list2.length);
  const rows1 = [];
  for (let i = 0; i < rowCount; i++) {
    const item1 = list1[i];
    const item2 = list2[i];
    rows1.push(`<tr>
      <td>${item1 ? item1.label : ""}. <span contenteditable="true" oninput="updateMatchListItem('list1', ${i}, this.innerHTML)">${item1 ? item1.text : ""}</span></td>
      <td>${item2 ? item2.label : ""}. <span contenteditable="true" oninput="updateMatchListItem('list2', ${i}, this.innerHTML)">${item2 ? item2.text : ""}</span></td>
    </tr>`);
  }
  const codeRows = (table.code_table && table.code_table.rows ? table.code_table.rows : [])
    .map(
      (r, ri) =>
        `<tr><td>(${r.option})</td>${r.values
          .map(
            (v, ci) =>
              `<td contenteditable="true" oninput="updateMatchListCode(${ri}, ${ci}, this.textContent)">${
                v === null || v === undefined ? "" : v
              }</td>`
          )
          .join("")}</tr>`
    )
    .join("");
  if (!table.code_table) {
    return `
    <h4>List I / List II</h4>
    <table><thead><tr><th>List I</th><th>List II</th></tr></thead><tbody>${rows1.join("")}</tbody></table>`;
  }
  return `
    <h4>List I / List II</h4>
    <table><thead><tr><th>List I</th><th>List II</th></tr></thead><tbody>${rows1.join("")}</tbody></table>
    <h4>Code</h4>
    <table><thead><tr><th></th><th>A</th><th>B</th><th>C</th><th>D</th></tr></thead><tbody>${codeRows}</tbody></table>
    <div class="options-block">
      ${["a", "b", "c", "d"]
        .map(
          (letter) =>
            `<div class="option-card${currentQuestion.user_answer === letter ? " selected" : ""}" onclick="selectAnswer('${letter}')"><span class="option-letter">(${letter})</span></div>`
        )
        .join("")}
    </div>`;
}

function updateMatchListItem(listKey, index, value) {
  if (!currentQuestion || !currentQuestion.table) return;
  const list = currentQuestion.table[listKey];
  if (!list || !list[index]) return;
  list[index].text = value;
  scheduleFieldSave(`table_${listKey}_${index}`, () => saveQuestionField(currentQuestion.q_number, { table: currentQuestion.table }));
}

function updateMatchListCode(rowIndex, colIndex, value) {
  if (!currentQuestion || !currentQuestion.table || !currentQuestion.table.code_table) return;
  const row = currentQuestion.table.code_table.rows[rowIndex];
  if (!row) return;
  row.values[colIndex] = value.trim();
  scheduleFieldSave(`table_code_${rowIndex}_${colIndex}`, () => saveQuestionField(currentQuestion.q_number, { table: currentQuestion.table }));
}

// A "Read the following pairs :" style table (src/paired_table.py) - two
// labelled columns, one row per Roman numeral, embedded in the stem
// rather than replacing it like match_the_list's table does.
// A ruled table (src/grid_table.py): editable cells, with the merged cells'
// row/column spans kept. `table` = {header, rows: [[{html, rs, cs}, ...], ...]}.
function renderGridTable(container, table) {
  container.innerHTML = "";
  const controls = document.createElement("label");
  controls.className = "grid-table-controls";
  const headerBox = document.createElement("input");
  headerBox.type = "checkbox";
  headerBox.checked = !!table.header;
  headerBox.addEventListener("change", () => {
    table.header = headerBox.checked;
    renderGridTable(container, table);
    scheduleFieldSave("table_header", () => saveQuestionField(currentQuestion.q_number, { table: currentQuestion.table }));
  });
  controls.append(headerBox, " First row is a header");

  const tableEl = document.createElement("table");
  tableEl.className = "grid-table";
  (table.rows || []).forEach((row, r) => {
    const tr = document.createElement("tr");
    row.forEach((cell, c) => {
      const el = document.createElement(table.header && r === 0 ? "th" : "td");
      if (cell.rs > 1) el.rowSpan = cell.rs;
      if (cell.cs > 1) el.colSpan = cell.cs;
      el.contentEditable = "true";
      el.innerHTML = cell.html || "";
      el.addEventListener("input", () => {
        cell.html = el.innerHTML;
        scheduleFieldSave(`table_cell_${r}_${c}`, () =>
          saveQuestionField(currentQuestion.q_number, { table: currentQuestion.table })
        );
      });
      tr.appendChild(el);
    });
    tableEl.appendChild(tr);
  });
  container.append(controls, tableEl);
}

function renderPairedTable(table) {
  const headers = table.headers || ["", ""];
  const rows = table.rows || [];
  const rowsHtml = rows
    .map(
      (r, i) => `<tr>
        <td>${escapeHtml(r.label)}.</td>
        <td contenteditable="true" oninput="updatePairedTableCell(${i}, 'col1', this.innerHTML)">${r.col1 || ""}</td>
        <td contenteditable="true" oninput="updatePairedTableCell(${i}, 'col2', this.innerHTML)">${r.col2 || ""}</td>
      </tr>`
    )
    .join("");
  return `
    <table>
      <thead><tr><th></th><th>${escapeHtml(headers[0] || "")}</th><th>${escapeHtml(headers[1] || "")}</th></tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
}

function updatePairedTableCell(rowIndex, field, value) {
  if (!currentQuestion || !currentQuestion.table || !currentQuestion.table.rows) return;
  const row = currentQuestion.table.rows[rowIndex];
  if (!row) return;
  row[field] = value;
  scheduleFieldSave(`table_row_${rowIndex}_${field}`, () =>
    saveQuestionField(currentQuestion.q_number, { table: currentQuestion.table })
  );
}

function selectAnswer(letter) {
  // Clicking the already-selected option again clears it, rather than
  // being stuck once any option is picked.
  currentQuestion.user_answer = currentQuestion.user_answer === letter ? null : letter;
  renderQuestion(currentQuestion.q_number);
  saveQuestionField(currentQuestion.q_number, { user_answer: currentQuestion.user_answer });
}

// Tags and topics are the same chip-list-with-suggestions widget, just two
// separate fields/vocabularies - a single set of functions parameterized
// by which one, rather than duplicating each for "topic".
const CHIP_FIELDS = {
  tags: { listEl: "tag-list", inputEl: "tag-input", datalistEl: "tag-suggestions" },
  topics: { listEl: "topic-list", inputEl: "topic-input", datalistEl: "topic-suggestions" },
};

function renderChips(field) {
  const { listEl } = CHIP_FIELDS[field];
  const list = document.getElementById(listEl);
  list.innerHTML = "";
  for (const value of currentQuestion[field] || []) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.innerHTML = `${escapeHtml(value)} <button title="remove">×</button>`;
    chip.querySelector("button").addEventListener("click", () => removeChip(field, value));
    list.appendChild(chip);
  }
}

function renderTags() {
  renderChips("tags");
  renderChips("topics");
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

for (const [field, { inputEl, datalistEl }] of Object.entries(CHIP_FIELDS)) {
  document.getElementById(inputEl).addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const value = e.target.value.trim();
      if (!value) return;
      e.target.value = "";
      addChip(field, value);
      rememberSuggestion(datalistEl, value);
    }
  });
}

function addChip(field, value) {
  if (!currentQuestion[field]) currentQuestion[field] = [];
  if (currentQuestion[field].some((v) => v.toLowerCase() === value.toLowerCase())) return;
  currentQuestion[field].push(value);
  renderChips(field);
  saveQuestionField(currentQuestion.q_number, { [field]: currentQuestion[field] });
}

function removeChip(field, value) {
  currentQuestion[field] = (currentQuestion[field] || []).filter((v) => v !== value);
  renderChips(field);
  saveQuestionField(currentQuestion.q_number, { [field]: currentQuestion[field] });
}

// ---- "working on it" notice for the slow rebuilds (reading a table, re-reading a question) ----
// Shown at the top of the Question pane; the content below is made inert while it
// runs so a manual edit made mid-rebuild can't be silently overwritten by the result.
function setQuestionBusy(message) {
  document.getElementById("question-busy").hidden = !message;
  document.getElementById("question-busy-text").textContent = message || "";
  for (const id of ["question-body-content", "instruction-body"]) {
    const el = document.getElementById(id);
    el.inert = !!message;
    el.classList.toggle("busy", !!message);
  }
}

function busyMessageFor(qNumber, questionType) {
  return questionType === "grid_table"
    ? `Q${qNumber}: reading the table - finding its ruling lines and reading every cell. This can take up to a minute...`
    : `Q${qNumber}: re-reading the question from its image...`;
}

// ---- change question number ----
document.getElementById("question-number-input").addEventListener("change", async (e) => {
  const input = e.target;
  const q = currentQuestion;
  if (!q) return;
  const newNumber = Number(input.value);
  if (!Number.isInteger(newNumber) || newNumber === q.q_number) {
    input.value = q.q_number;
    return;
  }
  // Debounced saves already queued are addressed by the OLD number - let
  // them land before it stops existing.
  await flushAllPendingSaves();
  input.disabled = true;
  setSaveIndicator("saving");
  try {
    const res = await fetch(`/api/papers/${currentPaper.paper_id}/questions/${q.q_number}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q_number: newNumber }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "could not renumber the question");
    // Order and instruction ranges change with the number - reload the
    // paper and land on the renumbered question in its new position.
    await loadPaper(currentPaper.paper_id, `Q:${newNumber}`);
    setSaveIndicator("saved");
  } catch (err) {
    input.value = q.q_number;
    setSaveIndicator("error saving");
    alert(err.message);
  } finally {
    input.disabled = false;
  }
});

// ---- change question type ----
const TABLE_QUESTION_TYPES = ["match_the_list", "paired_table", "grid_table"];

function tableFitsType(type, table) {
  if (!table) return false;
  if (type === "match_the_list") return "list1" in table && "list2" in table;
  if (type === "paired_table") return "headers" in table && "rows" in table;
  if (type === "grid_table") return "header" in table && "rows" in table;
  return false;
}

document.getElementById("question-type-select").addEventListener("change", async (e) => {
  const select = e.target;
  const q = currentQuestion;
  const newType = select.value;
  if (!q || newType === q.question_type) return;

  const hasTable = q.table && Object.keys(q.table).length > 0;
  if (newType === "grid_table" && !tableFitsType(newType, q.table)) {
    if (
      !confirm(
        "Change this question to a Grid table? The table is detected from the image, and the stem, the text below the table and the options are rebuilt around it - any manual edits to them will be lost."
      )
    ) {
      select.value = q.question_type;
      return;
    }
  } else if (hasTable && !tableFitsType(newType, q.table)) {
    const what = TABLE_QUESTION_TYPES.includes(newType) ? "rebuilt from the image" : "removed";
    if (!confirm(`Change this question's type? Its current table will be ${what} - any manual edits to it will be lost.`)) {
      select.value = q.question_type;
      return;
    }
  }

  flushAllPendingSaves();
  select.disabled = true;
  setSaveIndicator("saving");
  const buildsTable = TABLE_QUESTION_TYPES.includes(newType) && !tableFitsType(newType, q.table);
  if (buildsTable) {
    setQuestionBusy(
      newType === "grid_table"
        ? busyMessageFor(q.q_number, "grid_table")
        : `Q${q.q_number}: building the table from the image...`
    );
  }
  try {
    const res = await fetch(`/api/papers/${currentPaper.paper_id}/questions/${q.q_number}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question_type: newType }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "could not change the question type");
    Object.assign(q, data);
    renderQuestion(q.q_number);
    setSaveIndicator("saved");
  } catch (err) {
    select.value = q.question_type;
    setSaveIndicator("error saving");
    alert(err.message);
  } finally {
    setQuestionBusy(null);
    select.disabled = false;
  }
});

async function saveQuestionField(qNumber, updates) {
  setSaveIndicator("saving");
  try {
    const res = await fetch(`/api/papers/${currentPaper.paper_id}/questions/${qNumber}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    if (!res.ok) throw new Error("save failed");
    setSaveIndicator("saved");
  } catch (err) {
    setSaveIndicator("error saving");
  }
}

function setSaveIndicator(state) {
  // Question mode and instruction mode each have their own indicator
  // element (only one is ever visible at a time) - update both so
  // whichever is showing reflects the current save state.
  for (const id of ["save-indicator", "instruction-save-indicator"]) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.classList.remove("saving", "saved");
    if (state === "saving") {
      el.textContent = "Saving...";
      el.classList.add("saving");
    } else if (state === "saved") {
      el.textContent = "Saved";
      el.classList.add("saved");
    } else if (state === "error saving") {
      el.textContent = "Error saving";
    } else {
      el.textContent = "";
    }
  }
}

// ---- editable question body (stem / passage) ----
// One-time setup: these elements persist across renderQuestion() calls
// (only their innerHTML is replaced), so the listeners only need attaching
// once, not per-render.
function initEditableFields() {
  const bindings = [
    { id: "question-stem", field: "question_stem_html", key: "stem" },
    { id: "passage-text", field: "passage_text_html", key: "passage" },
    { id: "stem-after-table", field: "stem_after_table_html", key: "stem_after" },
  ];
  for (const { id, field, key } of bindings) {
    const el = document.getElementById(id);
    el.contentEditable = "true";
    el.addEventListener("input", () => {
      if (!currentQuestion) return;
      // Keep local state in sync immediately, not just the server - a
      // re-render before the debounced save lands (switching questions and
      // back) would otherwise redraw from the stale pre-edit value and the
      // edit would visually vanish, even though it saved fine.
      currentQuestion[field] = serializeEditable(el);
      scheduleFieldSave(key, () => saveQuestionField(currentQuestion.q_number, { [field]: serializeEditable(el) }));
    });
  }
}

// ---- LaTeX Scratchpad tab ----
// Paste/drop/choose a formula image on the left, optionally crop it down
// to just the formula (a full screenshot - headings, bullet text, "Final
// Answer" lines - confuses the converter, which expects one isolated
// expression), convert it (server-side, via pix2tex) to LaTeX, check the
// rendered result on the right, copy it into an explanation. The image
// never gets attached to any question - this tab is just a one-off
// conversion scratchpad.
const MAX_CROP_CANVAS_WIDTH = 560;

function initLatexScratchpad() {
  const dropzone = document.getElementById("latex-dropzone");
  const canvas = document.getElementById("latex-crop-canvas");
  const ctx = canvas.getContext("2d");
  const hint = document.getElementById("latex-dropzone-hint");
  const cropHint = document.getElementById("latex-crop-hint");
  const fileInput = document.getElementById("latex-file-input");
  const chooseBtn = document.getElementById("latex-choose-btn");
  const resetCropBtn = document.getElementById("latex-reset-crop-btn");
  const convertBtn = document.getElementById("latex-convert-btn");
  const statusEl = document.getElementById("latex-convert-status");
  const errorEl = document.getElementById("latex-convert-error");
  const previewEl = document.getElementById("latex-preview");
  const outputEl = document.getElementById("latex-output");
  const copyBtn = document.getElementById("latex-copy-btn");
  const copyStatusEl = document.getElementById("latex-copy-status");

  const stitchBtn = document.getElementById("latex-stitch-btn");
  const stitchStatusEl = document.getElementById("latex-stitch-status");
  const stitchErrorEl = document.getElementById("latex-stitch-error");
  const stitchLinesEl = document.getElementById("latex-stitch-lines");
  const stitchPreviewWrapEl = document.getElementById("latex-stitch-preview-wrap");
  const stitchPreviewEl = document.getElementById("latex-stitch-preview");
  const stitchOutputEl = document.getElementById("latex-stitch-output");
  const stitchCopyBtn = document.getElementById("latex-stitch-copy-btn");
  const stitchCopyStatusEl = document.getElementById("latex-stitch-copy-status");

  let sourceImage = null; // HTMLImageElement, the full pasted/dropped image
  let displayScale = 1; // canvas pixels per source-image pixel
  let cropRect = null; // {x, y, w, h} in SOURCE-image pixel coordinates
  let dragStart = null; // {x, y} in canvas pixel coordinates
  let stitchLines = []; // [{kind: "text"|"formula", text, latex?, bbox}], SOURCE-image pixel coords

  function setImage(blob) {
    const img = new Image();
    img.onload = () => {
      sourceImage = img;
      displayScale = Math.min(1, MAX_CROP_CANVAS_WIDTH / img.naturalWidth);
      canvas.width = Math.round(img.naturalWidth * displayScale);
      canvas.height = Math.round(img.naturalHeight * displayScale);
      canvas.hidden = false;
      hint.hidden = true;
      cropHint.hidden = false;
      resetCropBtn.hidden = false;
      // Default selection is the whole image, so Convert works immediately
      // even if the user never bothers to crop (e.g. it was already a
      // tight formula crop coming in).
      cropRect = { x: 0, y: 0, w: img.naturalWidth, h: img.naturalHeight };
      redrawCanvas();
      convertBtn.disabled = false;
      errorEl.hidden = true;
      outputEl.value = "";
      previewEl.innerHTML = "";
      copyBtn.disabled = true;
      copyStatusEl.textContent = "";
      stitchBtn.disabled = false;
      stitchErrorEl.hidden = true;
      stitchLines = [];
      stitchLinesEl.innerHTML = "";
      stitchPreviewWrapEl.hidden = true;
    };
    img.src = URL.createObjectURL(blob);
  }

  function redrawCanvas() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(sourceImage, 0, 0, canvas.width, canvas.height);
    if (!cropRect) return;
    const rx = cropRect.x * displayScale;
    const ry = cropRect.y * displayScale;
    const rw = cropRect.w * displayScale;
    const rh = cropRect.h * displayScale;
    const isFullImage = rx === 0 && ry === 0 && rw === canvas.width && rh === canvas.height;
    if (!isFullImage) {
      // Dim everything outside the selection so it's obvious what will
      // actually get sent to the converter.
      ctx.fillStyle = "rgba(0,0,0,0.45)";
      ctx.fillRect(0, 0, canvas.width, ry);
      ctx.fillRect(0, ry + rh, canvas.width, canvas.height - (ry + rh));
      ctx.fillRect(0, ry, rx, rh);
      ctx.fillRect(rx + rw, ry, canvas.width - (rx + rw), rh);
    }
    ctx.strokeStyle = "#2563eb";
    ctx.lineWidth = 2;
    ctx.strokeRect(rx, ry, rw, rh);
  }

  function canvasPoint(e) {
    const rect = canvas.getBoundingClientRect();
    // getBoundingClientRect() reflects any CSS scaling (e.g. max-width:
    // 100% shrinking the canvas on a narrow pane) - map back to the
    // canvas's own pixel coordinate space, not just the CSS pixel offset.
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: Math.max(0, Math.min(canvas.width, (e.clientX - rect.left) * scaleX)),
      y: Math.max(0, Math.min(canvas.height, (e.clientY - rect.top) * scaleY)),
    };
  }

  canvas.addEventListener("mousedown", (e) => {
    e.preventDefault();
    dragStart = canvasPoint(e);
  });
  canvas.addEventListener("mousemove", (e) => {
    if (!dragStart || !sourceImage) return;
    const cur = canvasPoint(e);
    const x0 = Math.min(dragStart.x, cur.x);
    const y0 = Math.min(dragStart.y, cur.y);
    const x1 = Math.max(dragStart.x, cur.x);
    const y1 = Math.max(dragStart.y, cur.y);
    cropRect = {
      x: x0 / displayScale,
      y: y0 / displayScale,
      w: (x1 - x0) / displayScale,
      h: (y1 - y0) / displayScale,
    };
    redrawCanvas();
  });
  window.addEventListener("mouseup", () => {
    if (!dragStart) return;
    dragStart = null;
    // A near-zero-size drag (or a plain click) isn't a real selection -
    // fall back to the whole image rather than sending pix2tex a sliver.
    if (cropRect && (cropRect.w * displayScale < 8 || cropRect.h * displayScale < 8) && sourceImage) {
      cropRect = { x: 0, y: 0, w: sourceImage.naturalWidth, h: sourceImage.naturalHeight };
      redrawCanvas();
    }
  });

  resetCropBtn.addEventListener("click", () => {
    if (!sourceImage) return;
    cropRect = { x: 0, y: 0, w: sourceImage.naturalWidth, h: sourceImage.naturalHeight };
    redrawCanvas();
  });

  dropzone.addEventListener("click", () => dropzone.focus());

  dropzone.addEventListener("paste", (e) => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    const imageItem = Array.from(items).find((item) => item.type && item.type.startsWith("image/"));
    if (!imageItem) return;
    e.preventDefault();
    setImage(imageItem.getAsFile());
  });

  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file && file.type.startsWith("image/")) setImage(file);
  });

  chooseBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) setImage(fileInput.files[0]);
    fileInput.value = "";
  });

  convertBtn.addEventListener("click", async () => {
    if (!sourceImage || !cropRect) return;
    convertBtn.disabled = true;
    statusEl.hidden = false;
    errorEl.hidden = true;
    const cropCanvas = document.createElement("canvas");
    cropCanvas.width = Math.round(cropRect.w);
    cropCanvas.height = Math.round(cropRect.h);
    cropCanvas
      .getContext("2d")
      .drawImage(sourceImage, cropRect.x, cropRect.y, cropRect.w, cropRect.h, 0, 0, cropCanvas.width, cropCanvas.height);
    try {
      const blob = await new Promise((resolve) => cropCanvas.toBlob(resolve, "image/png"));
      const form = new FormData();
      form.append("image", blob, "formula.png");
      const res = await fetch("/api/latex/convert", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "conversion failed");
      outputEl.value = data.latex;
      copyBtn.disabled = false;
      renderLatexPreview(data.latex);
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    } finally {
      statusEl.hidden = true;
      convertBtn.disabled = false;
    }
  });

  function renderLatexPreview(latex) {
    try {
      katex.render(latex, previewEl, { throwOnError: true, displayMode: true });
    } catch (err) {
      previewEl.innerHTML = `<span class="katex-error">Could not render: ${escapeHtml(err.message)}</span>`;
    }
  }

  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(outputEl.value);
      copyStatusEl.textContent = "Copied";
      setTimeout(() => (copyStatusEl.textContent = ""), 1500);
    } catch (err) {
      outputEl.select();
      document.execCommand("copy");
      copyStatusEl.textContent = "Copied";
      setTimeout(() => (copyStatusEl.textContent = ""), 1500);
    }
  });

  // The conversion isn't always valid LaTeX as-is (pix2tex can emit
  // unbalanced braces/\left-\right on complex expressions) - let the user
  // hand-fix the source here and see it re-render live, rather than only
  // being able to copy out a broken result.
  let editRenderTimer = null;
  outputEl.addEventListener("input", () => {
    copyBtn.disabled = !outputEl.value.trim();
    clearTimeout(editRenderTimer);
    editRenderTimer = setTimeout(() => renderLatexPreview(outputEl.value), 300);
  });

  // ---- Auto-detect & Stitch (whole screenshot) ----
  // Splits the FULL pasted image (not the crop selection above - this mode
  // is for when you don't want to crop) into text/formula lines server-side
  // (heuristic, see the hint text) and stitches them into one HTML block,
  // same ql-formula span format the explanation editor already understands.
  function sourceImageToBlob() {
    const c = document.createElement("canvas");
    c.width = sourceImage.naturalWidth;
    c.height = sourceImage.naturalHeight;
    c.getContext("2d").drawImage(sourceImage, 0, 0);
    return new Promise((resolve) => c.toBlob(resolve, "image/png"));
  }

  function cropSourceImageToBlob(bbox) {
    const [x0, y0, x1, y1] = bbox;
    const w = Math.max(1, Math.round(x1 - x0));
    const h = Math.max(1, Math.round(y1 - y0));
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    c.getContext("2d").drawImage(sourceImage, x0, y0, w, h, 0, 0, w, h);
    return new Promise((resolve) => c.toBlob(resolve, "image/png"));
  }

  function buildStitchHtml() {
    return stitchLines
      .map((line) =>
        line.kind === "formula" && line.latex
          ? `<p><span class="ql-formula" data-value="${escapeHtml(line.latex)}"> </span></p>`
          : `<p>${escapeHtml(line.text)}</p>`
      )
      .join("");
  }

  function renderStitchPreview(html) {
    stitchPreviewEl.innerHTML = html;
    stitchPreviewEl.querySelectorAll(".ql-formula[data-value]").forEach((span) => {
      try {
        katex.render(span.getAttribute("data-value"), span, { throwOnError: false, displayMode: true });
      } catch (err) {
        // leave the raw span text as a fallback
      }
    });
  }

  function refreshStitchOutput() {
    const html = buildStitchHtml();
    stitchOutputEl.value = html;
    renderStitchPreview(html);
    stitchCopyBtn.disabled = !html.trim();
    stitchPreviewWrapEl.hidden = false;
  }

  function renderStitchLines() {
    stitchLinesEl.innerHTML = "";
    stitchLines.forEach((line, i) => {
      const row = document.createElement("div");
      row.className = "latex-stitch-line";

      const badge = document.createElement("span");
      badge.className = `latex-stitch-badge ${line.kind}`;
      badge.textContent = line.kind === "formula" ? "Formula" : "Text";

      const content = document.createElement("span");
      content.className = "latex-stitch-line-content";
      content.textContent = line.kind === "formula" ? line.latex || line.text : line.text;

      const toggleBtn = document.createElement("button");
      toggleBtn.type = "button";
      toggleBtn.className = "latex-stitch-toggle";
      toggleBtn.textContent = line.kind === "formula" ? "Mark as text" : "Mark as formula";
      toggleBtn.addEventListener("click", () => toggleStitchLine(i, toggleBtn));

      row.append(badge, content, toggleBtn);
      stitchLinesEl.appendChild(row);
    });
    refreshStitchOutput();
  }

  async function toggleStitchLine(index, toggleBtn) {
    const line = stitchLines[index];
    if (line.kind === "formula") {
      // Falling back to text just reuses the OCR text already fetched -
      // no re-conversion needed.
      line.kind = "text";
      renderStitchLines();
      return;
    }
    if (line.latex) {
      line.kind = "formula";
      renderStitchLines();
      return;
    }
    // Forcing text -> formula for a line that's never been converted needs
    // an on-demand pix2tex call, cropped to just that line's region.
    toggleBtn.disabled = true;
    toggleBtn.textContent = "Converting...";
    try {
      const blob = await cropSourceImageToBlob(line.bbox);
      const form = new FormData();
      form.append("image", blob, "line.png");
      const res = await fetch("/api/latex/convert", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "conversion failed");
      line.latex = data.latex;
      line.kind = "formula";
      renderStitchLines();
    } catch (err) {
      alert(err.message);
      toggleBtn.disabled = false;
      toggleBtn.textContent = "Mark as formula";
    }
  }

  stitchBtn.addEventListener("click", async () => {
    if (!sourceImage) return;
    stitchBtn.disabled = true;
    stitchStatusEl.hidden = false;
    stitchErrorEl.hidden = true;
    try {
      const blob = await sourceImageToBlob();
      const form = new FormData();
      form.append("image", blob, "screenshot.png");
      const res = await fetch("/api/latex/analyze_screenshot", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "analysis failed");
      stitchLines = data.lines;
      renderStitchLines();
    } catch (err) {
      stitchErrorEl.textContent = err.message;
      stitchErrorEl.hidden = false;
    } finally {
      stitchStatusEl.hidden = true;
      stitchBtn.disabled = false;
    }
  });

  let stitchEditTimer = null;
  stitchOutputEl.addEventListener("input", () => {
    stitchCopyBtn.disabled = !stitchOutputEl.value.trim();
    clearTimeout(stitchEditTimer);
    stitchEditTimer = setTimeout(() => renderStitchPreview(stitchOutputEl.value), 300);
  });

  stitchCopyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(stitchOutputEl.value);
      stitchCopyStatusEl.textContent = "Copied";
      setTimeout(() => (stitchCopyStatusEl.textContent = ""), 1500);
    } catch (err) {
      stitchOutputEl.select();
      document.execCommand("copy");
      stitchCopyStatusEl.textContent = "Copied";
      setTimeout(() => (stitchCopyStatusEl.textContent = ""), 1500);
    }
  });
}

// ---- collapsible Image/Question panes (Review tab) ----
// Lets the Explanation pane get more room on demand - collapsing a pane
// just shrinks it to a narrow strip; the other panes' flex-grow ratios
// reclaim the freed width automatically, no layout math needed here.
function initPaneCollapse() {
  // Scoped to buttons that actually declare a target pane - the pane
  // header also holds other same-styled buttons (e.g. "Edit image") that
  // aren't collapse toggles.
  document.querySelectorAll(".pane-collapse-btn[data-pane]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const pane = document.getElementById(btn.dataset.pane);
      const collapsed = pane.classList.toggle("collapsed");
      btn.innerHTML = collapsed ? "&plus;" : "&minus;";
      btn.title = collapsed ? "Expand" : "Collapse";
    });
  });
}

// ---- Edit image: redraw a question's crop region(s) from the full page(s) ----
// Opens the question's source page in a modal; the user drags one or more
// rectangles marking the true area(s), Save re-crops server-side
// (src/pipeline.py's crop_and_stack_regions, the same stacking logic the
// pipeline itself uses) and overwrites the question's image. A question or
// instruction can run onto another page (or column): every region carries its
// own page, prev/next page buttons move between pages, and the regions are
// stacked into one image in the order they were drawn.
//
// For a question, the user can also mark formula / chemistry / diagram areas
// inside those regions (src/marks.py): a formula is read into editable LaTeX
// with pix2tex as soon as it is drawn; chemistry is typed in mhchem syntax
// (pix2tex is available as a draft); a diagram is kept as a picture. Saving
// then rebuilds the stem and options with each mark placed inline.
function initImageEditor() {
  const overlay = document.getElementById("image-editor-overlay");
  const canvas = document.getElementById("image-editor-canvas");
  const ctx = canvas.getContext("2d");
  const closeBtn = document.getElementById("image-editor-close-btn");
  const cancelBtn = document.getElementById("image-editor-cancel-btn");
  const saveBtn = document.getElementById("image-editor-save-btn");
  const clearBtn = document.getElementById("image-editor-clear-btn");
  const regionListEl = document.getElementById("image-editor-region-list");
  const previewEl = document.getElementById("image-editor-preview");
  const statusEl = document.getElementById("image-editor-status");
  const errorEl = document.getElementById("image-editor-error");
  const editBtn = document.getElementById("edit-image-btn");
  const modeEl = document.getElementById("image-editor-mode");
  const marksSection = document.getElementById("image-editor-marks-section");
  const markListEl = document.getElementById("image-editor-mark-list");
  const prevPageBtn = document.getElementById("image-editor-prev-page");
  const nextPageBtn = document.getElementById("image-editor-next-page");
  const pageLabelEl = document.getElementById("image-editor-page-label");
  const allPagesBox = document.getElementById("image-editor-all-pages");

  const MAX_W = 900;
  const MAX_H = 640;
  const MODE_STYLE = {
    crop: { color: "#2563eb", tag: "", name: "Region" },
    formula: { color: "#7c3aed", tag: "F", name: "Formula" },
    chemistry: { color: "#0d9488", tag: "C", name: "Chemistry" },
    diagram: { color: "#d97706", tag: "D", name: "Diagram" },
  };

  const pageImages = new Map(); // page number -> loaded HTMLImageElement
  let pageList = []; // the pages prev/next walk, ascending
  let currentPage = null;
  let targetPage = null; // the question's own page - where the editor starts if nothing is stored
  let displayScale = 1;
  let regions = []; // [{page,x,y,w,h}], SOURCE-page pixel coords, in draw (= stacking) order
  let marks = []; // [{id,type,page,x,y,w,h,latex,busy,error}], same coords
  let markSeq = 0;
  let hadMarks = false; // the question already had marks when the editor opened
  let mode = "crop";
  let dragStart = null; // {x,y} in canvas pixel coordinates

  // Works on whichever of currentQuestion/currentInstruction is active, so
  // the one "Edit image" tool serves both (an instruction has no `page`
  // field of its own - it's inferred from the first question it applies
  // to, since that's the page its Directions header actually sits on).
  // Only a question supports marks.
  function getEditTarget() {
    if (currentInstruction) {
      const firstQ = currentPaper.questions.find((q) => q.q_number === currentInstruction.applies_to[0]);
      return {
        page: firstQ ? firstQ.page : null,
        imageRegions: currentInstruction.image_regions,
        supportsMarks: false,
        recropUrl: `/api/papers/${currentPaper.paper_id}/instructions/${currentInstruction.instruction_id}/recrop`,
        apply: (data) => {
          currentInstruction.image = data.image;
          currentInstruction.image_regions = data.image_regions;
        },
      };
    }
    if (currentQuestion) {
      return {
        page: currentQuestion.page,
        imageRegions: currentQuestion.image_regions,
        supportsMarks: true,
        recropUrl: `/api/papers/${currentPaper.paper_id}/questions/${currentQuestion.q_number}/recrop`,
        apply: (data) => {
          // marks rebuild the stem/options too, so take the whole question
          Object.assign(currentQuestion, data);
          renderQuestion(currentQuestion.q_number);
        },
      };
    }
    return null;
  }

  // Stored regions come in two shapes: {regions: [{page, box}], marks} and, for
  // a job edited before question text could span pages, {page, boxes, marks}.
  function normalizeStored(ir) {
    if (!ir) return { regions: [], marks: [] };
    if (Array.isArray(ir.regions)) return { regions: ir.regions, marks: ir.marks || [] };
    return {
      regions: (ir.boxes || []).map((box) => ({ page: ir.page, box })),
      marks: (ir.marks || []).map((m) => ({ ...m, page: m.page ?? ir.page })),
    };
  }

  function pageImageUrl(page) {
    const pageNum = String(page).padStart(3, "0");
    return `/api/papers/${currentPaper.paper_id}/page_images/page_${pageNum}.png`;
  }

  function loadPageImage(page) {
    if (pageImages.has(page)) return Promise.resolve(pageImages.get(page));
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        pageImages.set(page, img);
        resolve(img);
      };
      img.onerror = () => reject(new Error(`Could not load the image of page ${page}.`));
      img.src = pageImageUrl(page);
    });
  }

  // The pages the prev/next buttons walk: the pages the pipeline read as
  // content when it recorded them (so a bilingual booklet's Hindi pages and
  // the rough-work pages are skipped), else every page image; "Show all pages"
  // widens it, and any page already holding a region or mark is always in it.
  function computePageList() {
    const available = currentPaper.available_pages || [];
    const content = currentPaper.content_pages;
    const base = allPagesBox.checked || !content || !content.length ? available : content;
    // the page being looked at stays reachable even if "Show all pages" was just unticked
    const used = [targetPage, currentPage, ...regions.map((r) => r.page), ...marks.map((m) => m.page)];
    pageList = [...new Set([...base, ...used])].filter((p) => Number.isInteger(p)).sort((a, b) => a - b);
  }

  function updatePageNav() {
    const idx = pageList.indexOf(currentPage);
    prevPageBtn.disabled = idx <= 0;
    nextPageBtn.disabled = idx === -1 || idx >= pageList.length - 1;
    const here = regions.filter((r) => r.page === currentPage).length;
    pageLabelEl.textContent = `Page ${currentPage}` + (here ? ` (${here} region${here > 1 ? "s" : ""} here)` : "");
  }

  async function showPage(page) {
    try {
      const img = await loadPageImage(page);
      currentPage = page;
      displayScale = Math.min(1, MAX_W / img.naturalWidth, MAX_H / img.naturalHeight);
      canvas.width = Math.round(img.naturalWidth * displayScale);
      canvas.height = Math.round(img.naturalHeight * displayScale);
      redraw();
      updatePageNav();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
    }
  }

  function stepPage(delta) {
    const idx = pageList.indexOf(currentPage);
    const next = pageList[idx + delta];
    if (next !== undefined) showPage(next);
  }
  prevPageBtn.addEventListener("click", () => stepPage(-1));
  nextPageBtn.addEventListener("click", () => stepPage(1));
  allPagesBox.addEventListener("change", () => {
    computePageList();
    updatePageNav();
  });

  function setMode(newMode) {
    mode = newMode;
    canvas.style.cursor = "crosshair";
    for (const radio of modeEl.querySelectorAll('input[name="ie-mode"]')) radio.checked = radio.value === mode;
  }
  modeEl.addEventListener("change", (e) => {
    if (e.target && e.target.name === "ie-mode") mode = e.target.value;
  });

  async function openEditor() {
    const target = getEditTarget();
    if (!target || !target.page) return;
    errorEl.hidden = true;
    statusEl.textContent = "";
    regions = [];
    marks = [];
    markSeq = 0;
    hadMarks = false;
    targetPage = target.page;
    allPagesBox.checked = false;
    setMode("crop");
    modeEl.hidden = !target.supportsMarks;
    marksSection.hidden = !target.supportsMarks;

    const stored = normalizeStored(target.imageRegions);
    regions = stored.regions.map((r) => {
      const [x0, y0, x1, y1] = r.box;
      return { page: r.page, x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    });
    if (target.supportsMarks) {
      marks = stored.marks.map((m) => {
        const [x0, y0, x1, y1] = m.box;
        const n = parseInt(String(m.id).replace(/\D/g, ""), 10);
        if (Number.isFinite(n)) markSeq = Math.max(markSeq, n);
        return { id: m.id, type: m.type, page: m.page, x: x0, y: y0, w: x1 - x0, h: y1 - y0, latex: m.latex || "", busy: false, error: "" };
      });
      hadMarks = marks.length > 0;
    }

    computePageList();
    const startPage = regions.length ? regions[0].page : targetPage;
    try {
      // every page holding a region or mark must be loaded up front: the
      // preview and the mark thumbnails draw from them
      await Promise.all([...new Set([startPage, ...regions.map((r) => r.page), ...marks.map((m) => m.page)])].map(loadPageImage));
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
      overlay.hidden = false;
      return;
    }
    await showPage(startPage);
    renderRegionList();
    renderMarkList();
    overlay.hidden = false;
  }

  function closeEditor() {
    overlay.hidden = true;
  }

  function drawBox(r, label, color) {
    const rx = r.x * displayScale;
    const ry = r.y * displayScale;
    const rw = r.w * displayScale;
    const rh = r.h * displayScale;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(rx, ry, rw, rh);
    ctx.font = "11px sans-serif";
    const text = String(label);
    const tagW = Math.max(20, ctx.measureText(text).width + 12);
    ctx.fillStyle = color;
    ctx.fillRect(rx, ry, tagW, 16);
    ctx.fillStyle = "white";
    ctx.fillText(text, rx + 6, ry + 12);
  }

  function markLabel(m) {
    const same = marks.filter((x) => x.type === m.type);
    return `${MODE_STYLE[m.type].tag}${same.indexOf(m) + 1}`;
  }

  function redraw(dragRect) {
    const img = pageImages.get(currentPage);
    if (!img) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    // numbered by draw order across ALL pages - that is the stacking order
    regions.forEach((r, i) => {
      if (r.page === currentPage) drawBox(r, i + 1, MODE_STYLE.crop.color);
    });
    marks.forEach((m) => {
      if (m.page === currentPage) drawBox(m, markLabel(m), MODE_STYLE[m.type].color);
    });
    if (dragRect) {
      const label = mode === "crop" ? regions.length + 1 : `${MODE_STYLE[mode].tag}+`;
      drawBox(dragRect, label, mode === "crop" ? "#059669" : MODE_STYLE[mode].color);
    }
  }

  function canvasPoint(e) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return {
      x: Math.max(0, Math.min(canvas.width, (e.clientX - rect.left) * scaleX)),
      y: Math.max(0, Math.min(canvas.height, (e.clientY - rect.top) * scaleY)),
    };
  }

  canvas.addEventListener("mousedown", (e) => {
    e.preventDefault();
    dragStart = canvasPoint(e);
  });
  canvas.addEventListener("mousemove", (e) => {
    if (!dragStart || !pageImages.get(currentPage)) return;
    const cur = canvasPoint(e);
    const x0 = Math.min(dragStart.x, cur.x);
    const y0 = Math.min(dragStart.y, cur.y);
    const x1 = Math.max(dragStart.x, cur.x);
    const y1 = Math.max(dragStart.y, cur.y);
    redraw({ x: x0 / displayScale, y: y0 / displayScale, w: (x1 - x0) / displayScale, h: (y1 - y0) / displayScale });
  });
  window.addEventListener("mouseup", (e) => {
    if (!dragStart || !pageImages.get(currentPage) || overlay.hidden) {
      dragStart = null;
      return;
    }
    const cur = canvasPoint(e);
    const x0 = Math.min(dragStart.x, cur.x);
    const y0 = Math.min(dragStart.y, cur.y);
    const x1 = Math.max(dragStart.x, cur.x);
    const y1 = Math.max(dragStart.y, cur.y);
    dragStart = null;
    if (x1 - x0 < 6 || y1 - y0 < 6) {
      redraw(); // an accidental click/tiny drag - not a real region
      return;
    }
    const rect = { page: currentPage, x: x0 / displayScale, y: y0 / displayScale, w: (x1 - x0) / displayScale, h: (y1 - y0) / displayScale };
    if (mode === "crop") {
      regions.push(rect);
      redraw();
      updatePageNav();
      renderRegionList();
      renderMarkList();
    } else {
      addMark(mode, rect);
    }
  });

  // a mark belongs to a region on its own page
  function insideSomeRegion(r) {
    const cx = r.x + r.w / 2;
    const cy = r.y + r.h / 2;
    return regions.some((g) => g.page === r.page && cx >= g.x && cx <= g.x + g.w && cy >= g.y && cy <= g.y + g.h);
  }

  function addMark(type, rect) {
    if (!insideSomeRegion(rect)) {
      errorEl.textContent = "Draw the question's area on this page first (Question area), then mark formulas, chemistry and diagrams inside it.";
      errorEl.hidden = false;
      redraw();
      return;
    }
    errorEl.hidden = true;
    const mark = { id: `m${++markSeq}`, type, ...rect, latex: "", busy: false, error: "" };
    marks.push(mark);
    redraw();
    renderMarkList();
    // A formula is read straight away; chemistry is typed by the user (the
    // pix2tex draft is one click away), a diagram needs no reading.
    if (type === "formula") convertMark(mark);
  }

  async function convertMark(mark) {
    mark.busy = true;
    mark.error = "";
    renderMarkList();
    try {
      const res = await fetch(`/api/papers/${currentPaper.paper_id}/latex_region`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ page: mark.page, box: [mark.x, mark.y, mark.x + mark.w, mark.y + mark.h] }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "could not read the formula");
      mark.latex = data.latex;
    } catch (err) {
      mark.error = err.message;
    } finally {
      mark.busy = false;
      renderMarkList();
    }
  }

  function renderRegionList() {
    regionListEl.innerHTML = "";
    regions.forEach((r, i) => {
      const row = document.createElement("div");
      row.className = "image-editor-region-row";
      const label = document.createElement("span");
      label.textContent = `Region ${i + 1} `;
      const pageBtn = document.createElement("button");
      pageBtn.type = "button";
      pageBtn.className = "page-chip";
      pageBtn.textContent = `page ${r.page}`;
      pageBtn.title = "Go to this page";
      pageBtn.addEventListener("click", () => showPage(r.page));
      label.appendChild(pageBtn);
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        regions.splice(i, 1);
        redraw();
        updatePageNav();
        renderRegionList();
        renderMarkList();
      });
      row.append(label, removeBtn);
      regionListEl.appendChild(row);
    });
    updateSaveState();
    renderPreview();
  }

  function thumbnailFor(m) {
    const c = document.createElement("canvas");
    const scale = Math.min(1, 240 / Math.max(1, m.w), 90 / Math.max(1, m.h));
    c.width = Math.max(1, Math.round(m.w * scale));
    c.height = Math.max(1, Math.round(m.h * scale));
    const img = pageImages.get(m.page);
    if (img) c.getContext("2d").drawImage(img, m.x, m.y, m.w, m.h, 0, 0, c.width, c.height);
    c.className = "mark-thumb";
    return c;
  }

  function renderMarkList() {
    markListEl.innerHTML = "";
    if (!marks.length) {
      const empty = document.createElement("div");
      empty.className = "image-editor-preview-empty";
      empty.textContent = "None yet - pick Formula, Chemistry or Diagram above and draw on the page.";
      markListEl.appendChild(empty);
    }
    marks.forEach((m) => {
      const row = document.createElement("div");
      row.className = "mark-row";
      row.style.borderLeftColor = MODE_STYLE[m.type].color;

      const head = document.createElement("div");
      head.className = "mark-row-head";
      const title = document.createElement("span");
      title.textContent = `${MODE_STYLE[m.type].name} ${markLabel(m)} `;
      const pageBtn = document.createElement("button");
      pageBtn.type = "button";
      pageBtn.className = "page-chip";
      pageBtn.textContent = `page ${m.page}`;
      pageBtn.title = "Go to this page";
      pageBtn.addEventListener("click", () => showPage(m.page));
      title.appendChild(pageBtn);
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        marks.splice(marks.indexOf(m), 1);
        redraw();
        renderMarkList();
      });
      head.append(title, removeBtn);
      row.append(head, thumbnailFor(m));

      if (!insideSomeRegion(m)) {
        const warn = document.createElement("div");
        warn.className = "error";
        warn.textContent = "Outside every question area on its page - move or remove it.";
        row.appendChild(warn);
      }

      if (m.type !== "diagram") {
        const textarea = document.createElement("textarea");
        textarea.className = "mark-latex";
        textarea.rows = 2;
        textarea.value = m.latex;
        textarea.placeholder =
          m.type === "chemistry" ? "e.g. \\ce{H2SO4 + 2NaOH -> Na2SO4 + 2H2O}" : m.busy ? "Reading the formula..." : "LaTeX";
        textarea.disabled = m.busy;
        const preview = document.createElement("div");
        preview.className = "mark-preview";
        const refresh = () => {
          preview.innerHTML = "";
          if (m.latex.trim()) renderMathTo(preview, m.latex, m.type);
        };
        textarea.addEventListener("input", () => {
          m.latex = textarea.value;
          refresh();
          updateSaveState();
        });
        refresh();
        row.append(textarea, preview);

        if (m.busy) {
          const status = document.createElement("div");
          status.className = "save-indicator saving";
          status.textContent = "Reading the formula (the first one loads the model and can take a minute)...";
          row.appendChild(status);
        }
        if (m.error) {
          const err = document.createElement("div");
          err.className = "error";
          err.textContent = m.error;
          row.appendChild(err);
        }
        if (m.type === "chemistry" || m.error) {
          const readBtn = document.createElement("button");
          readBtn.type = "button";
          readBtn.className = "secondary-btn";
          readBtn.textContent = m.type === "chemistry" ? "Read with pix2tex (draft)" : "Try again";
          readBtn.disabled = m.busy;
          readBtn.addEventListener("click", () => convertMark(m));
          row.appendChild(readBtn);
        }
      }
      markListEl.appendChild(row);
    });
    updateSaveState();
  }

  function updateSaveState() {
    const marksReady = marks.every(
      (m) => !m.busy && insideSomeRegion(m) && (m.type === "diagram" || m.latex.trim().length > 0)
    );
    saveBtn.disabled = regions.length === 0 || !marksReady;
  }

  function renderPreview() {
    if (!regions.length || regions.some((r) => !pageImages.get(r.page))) {
      previewEl.innerHTML = `<div class="image-editor-preview-empty">Draw at least one region to preview.</div>`;
      return;
    }
    const gap = 6;
    const crops = regions.map((r) => ({ w: Math.max(1, Math.round(r.w)), h: Math.max(1, Math.round(r.h)), r }));
    const width = Math.max(...crops.map((c) => c.w));
    const height = crops.reduce((sum, c) => sum + c.h, 0) + gap * (crops.length - 1);
    const c = document.createElement("canvas");
    c.width = width;
    c.height = height;
    const cctx = c.getContext("2d");
    cctx.fillStyle = "white";
    cctx.fillRect(0, 0, width, height);
    let y = 0;
    for (const crop of crops) {
      cctx.drawImage(pageImages.get(crop.r.page), crop.r.x, crop.r.y, crop.w, crop.h, 0, y, crop.w, crop.h);
      y += crop.h + gap;
    }
    previewEl.innerHTML = "";
    const img = document.createElement("img");
    img.src = c.toDataURL("image/png");
    previewEl.appendChild(img);
  }

  clearBtn.addEventListener("click", () => {
    regions = [];
    marks = [];
    redraw();
    updatePageNav();
    renderRegionList();
    renderMarkList();
  });

  closeBtn.addEventListener("click", closeEditor);
  cancelBtn.addEventListener("click", closeEditor);
  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) closeEditor();
  });

  saveBtn.addEventListener("click", async () => {
    const target = getEditTarget();
    if (!regions.length || !target) return;
    const rebuildsText = target.supportsMarks && (marks.length > 0 || hadMarks);
    if (
      rebuildsText &&
      !confirm(
        "Saving formula/diagram marks rebuilds this question's stem and options from the image - any manual edits to that text will be replaced. Continue?"
      )
    ) {
      return;
    }
    saveBtn.disabled = true;
    statusEl.textContent = rebuildsText ? "Saving and rebuilding the text..." : "Saving...";
    errorEl.hidden = true;
    const payload = { regions: regions.map((r) => ({ page: r.page, box: [r.x, r.y, r.x + r.w, r.y + r.h] })) };
    if (target.supportsMarks) {
      payload.marks = marks.map((m) => ({
        id: m.id,
        type: m.type,
        page: m.page,
        box: [m.x, m.y, m.x + m.w, m.y + m.h],
        ...(m.type === "diagram" ? {} : { latex: m.latex.trim() }),
      }));
    }
    try {
      const res = await fetch(target.recropUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "recrop failed");
      target.apply(data);
      document.getElementById("question-image").src = `/api/papers/${currentPaper.paper_id}/${data.image}?t=${Date.now()}`;
      statusEl.textContent = "Saved";
      closeEditor();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.hidden = false;
      statusEl.textContent = "";
    } finally {
      updateSaveState();
    }
  });

  editBtn.addEventListener("click", openEditor);
}

// ---- inline formula editing: click a formula/chemistry token to edit its
// LaTeX, or insert a new one at the cursor of the stem / an option ----
// A token is a non-editable <span class="math-token"> (see renderMathTokens),
// so the browser already deletes it as one unit with Backspace/Delete; this
// adds the click-to-edit popover and the insert buttons. Every change is
// pushed through the field's normal "input" handler so it is saved exactly
// like typed text.
let formulaPopoverState = null; // {mode: "edit", token, container} | {mode: "insert", container, range}
let lastCaret = null; // {container, range}: the last selection inside the stem or an option

function editableFieldOf(node) {
  const el = node && (node.nodeType === Node.TEXT_NODE ? node.parentElement : node);
  const field = el && el.closest ? el.closest("#question-stem, .option-text") : null;
  return field && field.isContentEditable ? field : null;
}

function onTokenClick(e) {
  const token = e.target.closest ? e.target.closest("span.math-token") : null;
  if (!token) return;
  e.preventDefault();
  e.stopPropagation();
  openFormulaPopover({ mode: "edit", token, container: editableFieldOf(token) });
}

function closeFormulaPopover() {
  formulaPopoverState = null;
  const pop = document.getElementById("formula-popover");
  if (pop) pop.hidden = true;
}

function refreshFormulaPreview() {
  const preview = document.getElementById("formula-preview");
  const latex = document.getElementById("formula-latex").value;
  preview.innerHTML = "";
  if (latex.trim()) renderMathTo(preview, latex, document.getElementById("formula-type").value);
  document.getElementById("formula-apply-btn").disabled = !latex.trim();
}

function openFormulaPopover(state, initialType) {
  if (!state.container) return;
  formulaPopoverState = state;
  const pop = document.getElementById("formula-popover");
  const typeEl = document.getElementById("formula-type");
  const latexEl = document.getElementById("formula-latex");
  const editing = state.mode === "edit";
  typeEl.value = editing ? state.token.dataset.type || "formula" : initialType || "formula";
  latexEl.value = editing ? state.token.dataset.latex || "" : "";
  latexEl.placeholder = typeEl.value === "chemistry" ? "e.g. \\ce{H2SO4 + 2NaOH -> Na2SO4 + 2H2O}" : "LaTeX, e.g. \\frac{a}{b}";
  document.getElementById("formula-delete-btn").hidden = !editing;
  pop.hidden = false;
  refreshFormulaPreview();

  let anchor = editing ? state.token.getBoundingClientRect() : state.range.getBoundingClientRect();
  if (!anchor || (anchor.width === 0 && anchor.height === 0)) anchor = state.container.getBoundingClientRect();
  const w = pop.offsetWidth;
  const h = pop.offsetHeight;
  const left = Math.min(Math.max(8, anchor.left), Math.max(8, window.innerWidth - w - 8));
  let top = anchor.bottom + 8;
  if (top + h > window.innerHeight - 8) top = Math.max(8, anchor.top - h - 8);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  latexEl.focus();
  latexEl.select();
}

// An inline edit must also reach the stored mark, or the Edit-image modal
// would show (and a rebuild would restore) the LaTeX from before the edit.
function syncMarkFromToken(token, oldLatex, oldType, latex, type) {
  const marks = (currentQuestion && currentQuestion.image_regions && currentQuestion.image_regions.marks) || [];
  const mark = token.dataset.mark
    ? marks.find((m) => m.id === token.dataset.mark)
    : marks.find((m) => m.type !== "diagram" && m.type === oldType && m.latex === oldLatex);
  if (!mark) return;
  mark.latex = latex;
  mark.type = type;
  saveQuestionField(currentQuestion.q_number, { marks_update: { [mark.id]: { latex, type } } });
}

function insertFormulaToken(state, latex, type) {
  const container = state.container;
  const token = document.createElement("span");
  token.className = "math-token";
  token.dataset.type = type;
  token.dataset.latex = latex;
  token.setAttribute("contenteditable", "false");
  renderMathTo(token, latex, type);

  let range = state.range;
  if (!range || !container.contains(range.startContainer)) {
    range = document.createRange();
    range.selectNodeContents(container);
    range.collapse(false);
  }
  range.deleteContents();
  range.insertNode(token);
  // keep somewhere to type after the token (a non-editable span at the very
  // end of a field otherwise leaves no caret position)
  let after = token.nextSibling;
  if (!after || after.nodeType !== Node.TEXT_NODE || !/^[\s\u00a0]/.test(after.textContent)) {
    after = document.createTextNode("\u00a0");
    token.after(after);
  }
  container.focus();
  const caret = document.createRange();
  caret.setStart(after, Math.min(1, after.length));
  caret.collapse(true);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(caret);
  container.dispatchEvent(new Event("input", { bubbles: true }));
}

function applyFormulaPopover() {
  const state = formulaPopoverState;
  if (!state) return;
  const latex = document.getElementById("formula-latex").value.trim();
  const type = document.getElementById("formula-type").value;
  if (!latex) return;
  if (state.mode === "edit") {
    const token = state.token;
    const oldLatex = token.dataset.latex || "";
    const oldType = token.dataset.type;
    token.dataset.latex = latex;
    token.dataset.type = type;
    token.innerHTML = "";
    renderMathTo(token, latex, type);
    syncMarkFromToken(token, oldLatex, oldType, latex, type);
    state.container.dispatchEvent(new Event("input", { bubbles: true }));
  } else {
    insertFormulaToken(state, latex, type);
  }
  closeFormulaPopover();
}

function deleteFormulaFromPopover() {
  const state = formulaPopoverState;
  if (!state || state.mode !== "edit") return;
  const container = state.container;
  state.token.remove();
  container.dispatchEvent(new Event("input", { bubbles: true }));
  closeFormulaPopover();
}

function openInsertFormula(type) {
  let container = null;
  let range = null;
  if (lastCaret && lastCaret.container.isConnected && !lastCaret.container.closest("[hidden]")) {
    ({ container, range } = lastCaret);
  } else {
    const stem = document.getElementById("question-stem");
    container = stem && !stem.hidden ? stem : document.querySelector("#options-block .option-text");
    if (container) {
      range = document.createRange();
      range.selectNodeContents(container);
      range.collapse(false);
    }
  }
  if (container) openFormulaPopover({ mode: "insert", container, range }, type);
}

function initFormulaEditor() {
  document.getElementById("question-stem").addEventListener("click", onTokenClick);

  document.addEventListener("selectionchange", () => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return;
    const container = editableFieldOf(selection.anchorNode);
    if (container) lastCaret = { container, range: selection.getRangeAt(0).cloneRange() };
  });

  // mousedown + preventDefault so pressing an insert button doesn't blur
  // the field and lose the cursor position it inserts at
  for (const [id, type] of [["insert-formula-btn", "formula"], ["insert-chemistry-btn", "chemistry"]]) {
    const btn = document.getElementById(id);
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    btn.addEventListener("click", () => openInsertFormula(type));
  }

  const latexEl = document.getElementById("formula-latex");
  latexEl.addEventListener("input", refreshFormulaPreview);
  document.getElementById("formula-type").addEventListener("change", refreshFormulaPreview);
  latexEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) applyFormulaPopover();
    if (e.key === "Escape") closeFormulaPopover();
  });
  document.getElementById("formula-apply-btn").addEventListener("click", applyFormulaPopover);
  document.getElementById("formula-cancel-btn").addEventListener("click", closeFormulaPopover);
  document.getElementById("formula-delete-btn").addEventListener("click", deleteFormulaFromPopover);
  document.addEventListener("mousedown", (e) => {
    const pop = document.getElementById("formula-popover");
    if (pop.hidden || pop.contains(e.target)) return;
    if (e.target.closest && (e.target.closest("span.math-token") || e.target.closest("#insert-row"))) return;
    closeFormulaPopover();
  });
}

// ---- symbol palette: Greek letters and maths symbols, inserted as plain Unicode ----
// The OCR model is English-only and can't produce these (it drops or swaps them
// for lookalikes), so this is the quick way to put them back. They are ordinary
// text characters - no LaTeX - so they save and export like any other text.
const SYMBOL_GROUPS = [
  ["Greek", [["α", "alpha"], ["β", "beta"], ["γ", "gamma"], ["δ", "delta"], ["ε", "epsilon"], ["ζ", "zeta"], ["η", "eta"], ["θ", "theta"], ["λ", "lambda"], ["μ", "mu"], ["ν", "nu"], ["ξ", "xi"], ["π", "pi"], ["ρ", "rho"], ["σ", "sigma"], ["τ", "tau"], ["φ", "phi"], ["χ", "chi"], ["ψ", "psi"], ["ω", "omega"], ["Γ", "Gamma"], ["Δ", "Delta"], ["Θ", "Theta"], ["Λ", "Lambda"], ["Ξ", "Xi"], ["Π", "Pi"], ["Σ", "Sigma"], ["Φ", "Phi"], ["Ψ", "Psi"], ["Ω", "Omega"]]],
  ["Relations", [["≤", "less than or equal"], ["≥", "greater than or equal"], ["≠", "not equal"], ["≈", "approximately"], ["≡", "identical / congruent"], ["∝", "proportional"], ["≪", "much less than"], ["≫", "much greater than"], ["<", "less than"], [">", "greater than"], ["∈", "element of"], ["∉", "not an element of"], ["⊂", "subset"], ["⊆", "subset or equal"], ["∪", "union"], ["∩", "intersection"], ["∅", "empty set"]]],
  ["Operators", [["±", "plus or minus"], ["∓", "minus or plus"], ["×", "times"], ["÷", "divide"], ["−", "minus"], ["·", "dot"], ["√", "square root"], ["∛", "cube root"], ["∑", "sum"], ["∏", "product"], ["∫", "integral"], ["∂", "partial"], ["∇", "nabla"], ["∞", "infinity"], ["∴", "therefore"], ["∵", "because"], ["∀", "for all"], ["∃", "there exists"]]],
  ["Arrows & logic", [["→", "right arrow"], ["←", "left arrow"], ["↔", "both ways"], ["⇒", "implies"], ["⇐", "implied by"], ["⇔", "if and only if"], ["↑", "up arrow"], ["↓", "down arrow"], ["⊥", "perpendicular"], ["∥", "parallel"], ["∠", "angle"], ["△", "triangle"]]],
  ["Scripts", [["⁰", "superscript 0"], ["¹", "superscript 1"], ["²", "squared"], ["³", "cubed"], ["⁴", "superscript 4"], ["⁵", "superscript 5"], ["⁶", "superscript 6"], ["⁷", "superscript 7"], ["⁸", "superscript 8"], ["⁹", "superscript 9"], ["⁺", "superscript plus"], ["⁻", "superscript minus"], ["ⁿ", "superscript n"], ["₀", "subscript 0"], ["₁", "subscript 1"], ["₂", "subscript 2"], ["₃", "subscript 3"], ["₄", "subscript 4"], ["₅", "subscript 5"], ["₆", "subscript 6"], ["₇", "subscript 7"], ["₈", "subscript 8"], ["₉", "subscript 9"], ["ₓ", "subscript x"]]],
  ["Units & misc", [["°", "degree"], ["′", "prime / minutes"], ["″", "double prime / seconds"], ["℃", "degrees Celsius"], ["Å", "angstrom"], ["µ", "micro"], ["Ω", "ohm"], ["%", "percent"], ["‰", "per mille"], ["½", "one half"], ["⅓", "one third"], ["¼", "one quarter"], ["¾", "three quarters"], ["‖", "norm / parallel"], ["•", "bullet"]]],
];
const SYMBOL_FIELDS = "#question-stem, .option-text, #stem-after-table, #passage-text, #table-block td, #table-block th";
const RECENT_SYMBOLS_KEY = "qp.recentSymbols";
let lastSymbolCaret = null; // {field, range}: the last cursor position inside any question-text field

function symbolFieldOf(node) {
  const el = node && (node.nodeType === Node.TEXT_NODE ? node.parentElement : node);
  const field = el && el.closest ? el.closest(SYMBOL_FIELDS) : null;
  return field && field.isContentEditable ? field : null;
}

function loadRecentSymbols() {
  try {
    const list = JSON.parse(localStorage.getItem(RECENT_SYMBOLS_KEY) || "[]");
    return Array.isArray(list) ? list.filter((s) => typeof s === "string").slice(0, 12) : [];
  } catch (err) {
    return [];
  }
}

function rememberSymbol(symbol) {
  const recent = [symbol, ...loadRecentSymbols().filter((s) => s !== symbol)].slice(0, 12);
  try {
    localStorage.setItem(RECENT_SYMBOLS_KEY, JSON.stringify(recent));
  } catch (err) {
    /* storage unavailable - the palette just doesn't remember */
  }
}

function insertSymbol(symbol) {
  const selection = window.getSelection();
  let field = symbolFieldOf(document.activeElement);
  if (!field || !selection.rangeCount || !field.contains(selection.anchorNode)) {
    // the field lost focus (e.g. the palette's own search/scroll) - go back to where the cursor was
    if (lastSymbolCaret && lastSymbolCaret.field.isConnected && lastSymbolCaret.field.contains(lastSymbolCaret.range.startContainer)) {
      field = lastSymbolCaret.field;
      field.focus();
      selection.removeAllRanges();
      selection.addRange(lastSymbolCaret.range);
    } else {
      field = document.getElementById("question-stem");
      if (!field || field.hidden) return;
      field.focus();
      const range = document.createRange();
      range.selectNodeContents(field);
      range.collapse(false);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }
  // insertText keeps the browser's undo history and fires the field's normal
  // "input" event, so the symbol saves exactly like typed text
  document.execCommand("insertText", false, symbol);
  rememberSymbol(symbol);
}

function closeSymbolPalette() {
  const palette = document.getElementById("symbol-palette");
  if (palette) palette.hidden = true;
}

function buildSymbolPalette() {
  const palette = document.getElementById("symbol-palette");
  palette.innerHTML = "";
  const groups = [];
  const recent = loadRecentSymbols();
  if (recent.length) groups.push(["Recent", recent.map((s) => [s, "recently used"])]);
  groups.push(...SYMBOL_GROUPS);
  for (const [title, symbols] of groups) {
    const heading = document.createElement("div");
    heading.className = "symbol-group-title";
    heading.textContent = title;
    const grid = document.createElement("div");
    grid.className = "symbol-grid";
    for (const [symbol, name] of symbols) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = symbol;
      btn.title = `${symbol}  ${name}`;
      // mousedown + preventDefault: keep the cursor in the field it inserts into
      btn.addEventListener("mousedown", (e) => e.preventDefault());
      btn.addEventListener("click", () => insertSymbol(symbol));
      grid.appendChild(btn);
    }
    palette.append(heading, grid);
  }
}

function toggleSymbolPalette() {
  const palette = document.getElementById("symbol-palette");
  if (!palette.hidden) {
    closeSymbolPalette();
    return;
  }
  closeFormulaPopover();
  buildSymbolPalette();
  palette.hidden = false;
  const anchor = document.getElementById("insert-symbol-btn").getBoundingClientRect();
  const w = palette.offsetWidth;
  const h = palette.offsetHeight;
  palette.style.left = `${Math.min(Math.max(8, anchor.left), Math.max(8, window.innerWidth - w - 8))}px`;
  let top = anchor.bottom + 6;
  if (top + h > window.innerHeight - 8) top = Math.max(8, window.innerHeight - h - 8);
  palette.style.top = `${top}px`;
}

function initSymbolPalette() {
  const btn = document.getElementById("insert-symbol-btn");
  btn.addEventListener("mousedown", (e) => e.preventDefault());
  btn.addEventListener("click", toggleSymbolPalette);

  document.addEventListener("selectionchange", () => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return;
    const field = symbolFieldOf(selection.anchorNode);
    if (field) lastSymbolCaret = { field, range: selection.getRangeAt(0).cloneRange() };
  });
  document.addEventListener("mousedown", (e) => {
    const palette = document.getElementById("symbol-palette");
    if (palette.hidden || palette.contains(e.target) || btn.contains(e.target)) return;
    closeSymbolPalette();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSymbolPalette();
  });
}

// ---- floating Bold/Underline toolbar for editable question-body text ----
// Select some text inside the stem/directions/passage/options (all
// contenteditable, see initEditableFields()) and a small toolbar pops up
// next to the selection - execCommand runs directly on the browser's own
// contenteditable selection, which is what the "input" listeners already
// on these fields pick up to save (same path as any other edit there).
function initFormatToolbar() {
  const toolbar = document.getElementById("format-toolbar");
  const buttons = toolbar.querySelectorAll("button[data-cmd]");
  const questionPane = document.getElementById("pane-question");

  function isWithinEditableField(node) {
    if (!node) return false;
    const el = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    const editable = el && el.closest && el.closest('[contenteditable="true"]');
    return !!(editable && questionPane.contains(editable));
  }

  function updateActiveStates() {
    buttons.forEach((btn) => {
      let active = false;
      try {
        active = document.queryCommandState(btn.dataset.cmd);
      } catch (err) {
        active = false;
      }
      btn.classList.toggle("active", active);
    });
  }

  function hide() {
    toolbar.hidden = true;
  }

  document.addEventListener("selectionchange", () => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0 || !isWithinEditableField(selection.anchorNode)) {
      hide();
      return;
    }
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) {
      hide();
      return;
    }
    toolbar.hidden = false;
    updateActiveStates();
    const top = rect.top - toolbar.offsetHeight - 8;
    const left = rect.left + rect.width / 2 - toolbar.offsetWidth / 2;
    toolbar.style.top = `${Math.max(4, top)}px`;
    toolbar.style.left = `${Math.max(4, left)}px`;
  });

  buttons.forEach((btn) => {
    // mousedown (not click) + preventDefault - a click would blur the
    // field first and lose the selection execCommand needs to act on.
    btn.addEventListener("mousedown", (e) => {
      e.preventDefault();
      document.execCommand(btn.dataset.cmd);
      updateActiveStates();
    });
  });

  document.addEventListener("mousedown", (e) => {
    if (!toolbar.contains(e.target) && !isWithinEditableField(e.target)) hide();
  });
}

// ---- Reprocess: re-run OCR + extraction on the current image ----
// Works on whichever of currentQuestion/currentInstruction is active
// (same idea as getEditTarget() inside initImageEditor) - typically used
// right after "Edit image" fixes a bad crop, so stale text from the
// original pipeline run doesn't linger. Overwrites immediately per
// question's stem/options/table (or an instruction's text) with a fresh
// OCR read - no diff preview, so it confirms first since any manual
// corrections to that text are lost.
function initReprocess() {
  const btn = document.getElementById("reprocess-btn");
  const originalLabel = btn.textContent;

  btn.addEventListener("click", async () => {
    if (!currentQuestion && !currentInstruction) return;
    const warning = currentInstruction
      ? "Reprocess this instruction from its current image? This overwrites its text with a fresh OCR read - any manual corrections will be lost."
      : "Reprocess this question from its current image? This overwrites its stem/options (or table) with a fresh OCR read - any manual corrections will be lost.";
    if (!confirm(warning)) return;

    btn.disabled = true;
    btn.textContent = "Reprocessing...";
    setQuestionBusy(
      currentInstruction
        ? "Re-reading the instruction from its image..."
        : busyMessageFor(currentQuestion.q_number, currentQuestion.question_type)
    );
    try {
      const url = currentInstruction
        ? `/api/papers/${currentPaper.paper_id}/instructions/${currentInstruction.instruction_id}/reprocess`
        : `/api/papers/${currentPaper.paper_id}/questions/${currentQuestion.q_number}/reprocess`;
      const res = await fetch(url, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "reprocess failed");

      if (currentInstruction) {
        const idx = (currentPaper.instructions || []).findIndex((i) => i.instruction_id === data.instruction_id);
        if (idx !== -1) currentPaper.instructions[idx] = data;
        renderInstruction(data.instruction_id);
      } else {
        const idx = currentPaper.questions.findIndex((q) => q.q_number === data.q_number);
        if (idx !== -1) currentPaper.questions[idx] = data;
        renderQuestion(data.q_number);
      }
    } catch (err) {
      alert(err.message);
    } finally {
      setQuestionBusy(null);
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  });
}

// ---- init ----
loadVariants();
initEditableFields();
initLatexScratchpad();
initPaneCollapse();
initImageEditor();
initFormatToolbar();
initFormulaEditor();
initSymbolPalette();
initReprocess();
