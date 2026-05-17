const searchForm = document.querySelector("#searchForm");
const searchBlock = document.querySelector("#searchBlock");
const keywordInput = document.querySelector("#keywordInput");
const formHint = document.querySelector("#formHint");
const historyList = document.querySelector("#historyList");

const historyKey = "xhs-focus-search-history";
const maxHistoryItems = 8;

function readHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(historyKey) || "[]");
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
  } catch {
    return [];
  }
}

function writeHistory(items) {
  localStorage.setItem(historyKey, JSON.stringify(items.slice(0, maxHistoryItems)));
}

function buildSearchUrl(keyword) {
  const params = new URLSearchParams({ keyword });
  return `https://www.xiaohongshu.com/search_result?${params.toString()}`;
}

function setHint(message, isError = false) {
  formHint.textContent = message;
  formHint.classList.toggle("is-error", isError);
}

function showHistory() {
  searchBlock.classList.add("is-history-open");
}

function hideHistory() {
  searchBlock.classList.remove("is-history-open");
}

function saveKeyword(keyword) {
  const history = readHistory();
  const deduped = history.filter((item) => item !== keyword);
  writeHistory([keyword, ...deduped]);
}

function deleteKeyword(keyword) {
  const history = readHistory().filter((item) => item !== keyword);
  writeHistory(history);
  renderHistory();
}

function renderHistory() {
  const history = readHistory();
  historyList.textContent = "";

  if (!history.length) {
    const empty = document.createElement("span");
    empty.className = "empty-history";
    empty.textContent = "这里会保留最近的关键词，只存在这台浏览器里。";
    historyList.append(empty);
    return;
  }

  for (const keyword of history) {
    const row = document.createElement("div");
    row.className = "history-row";

    const searchButton = document.createElement("button");
    searchButton.className = "history-query";
    searchButton.type = "button";
    searchButton.title = `搜索 ${keyword}`;
    searchButton.innerHTML = `
      <svg class="history-clock" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="8.5"></circle>
        <path d="M12 7.5v5l3.5 2.1"></path>
      </svg>
      <span></span>
    `;
    searchButton.querySelector("span").textContent = keyword;
    searchButton.addEventListener("click", () => {
      keywordInput.value = keyword;
      submitSearch(keyword);
    });

    const deleteButton = document.createElement("button");
    deleteButton.className = "history-delete";
    deleteButton.type = "button";
    deleteButton.textContent = "删除";
    deleteButton.setAttribute("aria-label", `删除历史记录：${keyword}`);
    deleteButton.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteKeyword(keyword);
      keywordInput.focus();
      showHistory();
    });

    row.append(searchButton, deleteButton);
    historyList.append(row);
  }
}

function submitSearch(rawKeyword) {
  const keyword = rawKeyword.trim().replace(/\s+/g, " ");

  if (!keyword) {
    keywordInput.focus();
    setHint("先输入一个关键词，再出发。", true);
    return;
  }

  saveKeyword(keyword);
  renderHistory();
  setHint("正在打开小红书搜索结果页...");
  window.location.href = buildSearchUrl(keyword);
}

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitSearch(keywordInput.value);
});

keywordInput.addEventListener("input", () => {
  setHint("");
  showHistory();
});

keywordInput.addEventListener("focus", () => {
  showHistory();
});

document.addEventListener("pointerdown", (event) => {
  if (!searchBlock.contains(event.target)) {
    hideHistory();
  }
});

renderHistory();
