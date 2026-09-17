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
  }
}

document.getElementById("paper-select").addEventListener("change", (e) => loadPaper(e.target.value));

async function loadPaper(paperId) {
  if (!paperId) return;
  const res = await fetch(`/api/papers/${paperId}`);
  currentPaper = await res.json();

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

function getQuillInstance() {
  if (!quill) {
    quill = new Quill("#editor", {
      theme: "snow",
      modules: {
        toolbar: [
          ["bold", "italic", "underline"],
          [{ color: [] }],
          [{ size: ["small", false, "large", "huge"] }],
          [{ font: [] }],
          ["clean"],
        ],
      },
    });
    quill.on("text-change", (delta, oldDelta, source) => {
      if (!currentQuestion || source !== "user") return;
      scheduleFieldSave("explanation", () =>
        saveQuestionField(currentQuestion.q_number, { explanation_html: quill.root.innerHTML })
      );
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

  document.getElementById("section-directions").innerHTML = currentQuestion.section_directions_html || "";

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

  const editor = getQuillInstance();
  editor.setContents(editor.clipboard.convert(currentQuestion.explanation_html || ""));
  setSaveIndicator("");

  renderTags();
}

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
  currentQuestion.user_answer = letter;
  document.querySelectorAll(".option-card").forEach((el) => el.classList.remove("selected"));
  renderQuestion(currentQuestion.q_number);
  saveQuestionField(currentQuestion.q_number, { user_answer: letter });
}

function renderTags() {
  const list = document.getElementById("tag-list");
  list.innerHTML = "";
  for (const tag of currentQuestion.tags || []) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.innerHTML = `${escapeHtml(tag)} <button title="remove">×</button>`;
    chip.querySelector("button").addEventListener("click", () => removeTag(tag));
    list.appendChild(chip);
  }
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

document.getElementById("tag-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    const value = e.target.value.trim();
    if (!value) return;
    e.target.value = "";
    addTag(value);
  }
});

function addTag(tag) {
  if (!currentQuestion.tags) currentQuestion.tags = [];
  if (currentQuestion.tags.includes(tag)) return;
  currentQuestion.tags.push(tag);
  renderTags();
  saveQuestionField(currentQuestion.q_number, { tags: currentQuestion.tags });
}

function removeTag(tag) {
  currentQuestion.tags = (currentQuestion.tags || []).filter((t) => t !== tag);
  renderTags();
  saveQuestionField(currentQuestion.q_number, { tags: currentQuestion.tags });
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
      scheduleFieldSave(key, () => saveQuestionField(currentQuestion.q_number, { [field]: el.innerHTML }));
    });
  }
}

// ---- init ----
loadVariants();
initEditableFields();
