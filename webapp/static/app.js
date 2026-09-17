// ---- tiny state ----
let quill = null;
let currentPaper = null; // full paper JSON
let currentQuestion = null; // current question object (reference into currentPaper.questions)
let pendingSelectPaperId = null; // set right after a process job finishes
let jobPollTimer = null;

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
  for (const { timer, flush } of pendingSaves.values()) {
    clearTimeout(timer);
    flush();
  }
  pendingSaves.clear();
}

// ---- tab switching ----
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "review") loadPapers();
}

// ---- Process tab ----
async function loadVariants() {
  const res = await fetch("/api/variants");
  const variants = await res.json();
  const select = document.getElementById("variant-select");
  select.innerHTML = "";
  for (const v of variants) {
    const opt = document.createElement("option");
    opt.value = v.id;
    opt.textContent = v.label;
    select.appendChild(opt);
  }
}

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
    const when = p.processed_at ? new Date(p.processed_at).toLocaleString() : "";
    opt.textContent = `${p.label} (${p.total_questions} q, ${p.needs_review_count} flagged) - ${when}`;
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

document.getElementById("delete-paper-btn").addEventListener("click", async () => {
  const select = document.getElementById("paper-select");
  const paperId = select.value;
  if (!paperId) return;
  const label = select.options[select.selectedIndex] ? select.options[select.selectedIndex].textContent : paperId;
  if (!confirm(`Delete this job permanently?\n\n${label}\n\nThis removes its saved answers, explanations, and images - it cannot be undone.`)) {
    return;
  }
  const res = await fetch(`/api/papers/${paperId}`, { method: "DELETE" });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    alert(data.error || "failed to delete job");
    return;
  }
  if (currentPaper && currentPaper.paper_id === paperId) {
    currentPaper = null;
    currentQuestion = null;
  }
  pendingSelectPaperId = null;
  select.value = "";
  await loadPapers();
});

async function loadPaper(paperId) {
  if (!paperId) return;
  const res = await fetch(`/api/papers/${paperId}`);
  currentPaper = await res.json();
  await loadVocabSuggestions(currentPaper.variant);

  const qSelect = document.getElementById("question-select");
  qSelect.innerHTML = "";
  for (const q of currentPaper.questions) {
    const opt = document.createElement("option");
    opt.value = q.q_number;
    opt.textContent = `Q${q.q_number}${q.needs_review ? " ⚠" : ""}`;
    qSelect.appendChild(opt);
  }
  document.getElementById("review-empty").hidden = true;
  document.getElementById("review-body").hidden = false;
  renderQuestion(currentPaper.questions[0].q_number);
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

document.getElementById("question-select").addEventListener("change", (e) => renderQuestion(Number(e.target.value)));

document.getElementById("prev-q-btn").addEventListener("click", () => stepQuestion(-1));
document.getElementById("next-q-btn").addEventListener("click", () => stepQuestion(1));

function stepQuestion(delta) {
  const qSelect = document.getElementById("question-select");
  const idx = qSelect.selectedIndex + delta;
  if (idx >= 0 && idx < qSelect.options.length) {
    qSelect.selectedIndex = idx;
    renderQuestion(Number(qSelect.value));
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
  document.getElementById("question-select").value = String(qNumber);

  const meta = document.getElementById("review-meta");
  meta.textContent = currentQuestion.needs_review ? `⚠ needs review: ${currentQuestion.review_reason}` : `confidence ${currentQuestion.ocr_confidence}`;
  meta.classList.toggle("needs-review", !!currentQuestion.needs_review);

  const img = document.getElementById("question-image");
  img.src = currentQuestion.image ? `/api/papers/${currentPaper.paper_id}/${currentQuestion.image}` : "";

  const directionsEl = document.getElementById("section-directions");
  directionsEl.hidden = !currentQuestion.section_directions_html;
  directionsEl.innerHTML = currentQuestion.section_directions_html || "";

  const passageBlock = document.getElementById("passage-block");
  if (currentQuestion.passage_text_html) {
    passageBlock.hidden = false;
    document.getElementById("passage-label").textContent = currentQuestion.passage_label || "Passage";
    document.getElementById("passage-text").innerHTML = currentQuestion.passage_text_html;
  } else {
    passageBlock.hidden = true;
  }

  const isMatchList = currentQuestion.question_type === "match_the_list" && currentQuestion.table;
  const stemEl = document.getElementById("question-stem");
  // the raw OCR'd stem for match-the-list questions is just the jumbled
  // List/Code text the table below already presents cleanly - showing both
  // is confusing, so hide the raw version once we have a real table.
  stemEl.hidden = isMatchList;
  stemEl.innerHTML = currentQuestion.question_stem_html || "";

  const optionsBlock = document.getElementById("options-block");
  const tableBlock = document.getElementById("table-block");

  if (isMatchList) {
    optionsBlock.hidden = true;
    tableBlock.hidden = false;
    tableBlock.innerHTML = renderMatchListTable(currentQuestion.table);
  } else {
    tableBlock.hidden = true;
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
      textSpan.innerHTML = currentQuestion.options[letter] || "";
      // Editing the text shouldn't also register a click-to-select on the
      // card it lives inside.
      textSpan.addEventListener("mousedown", (e) => e.stopPropagation());
      textSpan.addEventListener("click", (e) => e.stopPropagation());
      textSpan.addEventListener("input", () => {
        // Keep local state in sync immediately, not just the server - a
        // re-render before the debounced save lands (switching questions
        // and back, or clicking the card to select it as the answer) would
        // otherwise redraw from the stale pre-edit value and the edit
        // would visually vanish, even though it saved fine.
        currentQuestion.options[letter] = textSpan.innerHTML;
        scheduleFieldSave(`option_${letter}`, () =>
          saveQuestionField(currentQuestion.q_number, { options: { [letter]: textSpan.innerHTML } })
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

function clearAnswer() {
  currentQuestion.user_answer = null;
  renderQuestion(currentQuestion.q_number);
  saveQuestionField(currentQuestion.q_number, { user_answer: null });
}
document.getElementById("clear-answer-btn").addEventListener("click", clearAnswer);

function renderMatchListTable(table) {
  const list1 = table.list1 || [];
  const list2 = table.list2 || [];
  const rows1 = list1
    .map((item, i) => `<tr><td>${item.label}. ${item.text}</td><td>${list2[i] ? list2[i].label + ". " + list2[i].text : ""}</td></tr>`)
    .join("");
  const codeRows = (table.code_table && table.code_table.rows ? table.code_table.rows : [])
    .map((r) => `<tr><td>(${r.option})</td>${r.values.map((v) => `<td>${v === null || v === undefined ? "?" : v}</td>`).join("")}</tr>`)
    .join("");
  return `
    <h4>List I / List II</h4>
    <table><thead><tr><th>List I</th><th>List II</th></tr></thead><tbody>${rows1}</tbody></table>
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
  const el = document.getElementById("save-indicator");
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

// ---- editable question body (stem / directions / passage) ----
// One-time setup: these elements persist across renderQuestion() calls
// (only their innerHTML is replaced), so the listeners only need attaching
// once, not per-render.
function initEditableFields() {
  const bindings = [
    { id: "question-stem", field: "question_stem_html", key: "stem" },
    { id: "section-directions", field: "section_directions_html", key: "directions" },
    { id: "passage-text", field: "passage_text_html", key: "passage" },
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
      currentQuestion[field] = el.innerHTML;
      scheduleFieldSave(key, () => saveQuestionField(currentQuestion.q_number, { [field]: el.innerHTML }));
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

  let sourceImage = null; // HTMLImageElement, the full pasted/dropped image
  let displayScale = 1; // canvas pixels per source-image pixel
  let cropRect = null; // {x, y, w, h} in SOURCE-image pixel coordinates
  let dragStart = null; // {x, y} in canvas pixel coordinates

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
}

// ---- init ----
loadVariants();
initEditableFields();
initLatexScratchpad();
