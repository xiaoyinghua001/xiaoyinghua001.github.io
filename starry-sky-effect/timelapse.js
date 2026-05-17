const videoInput = document.querySelector("#videoInput");
const dropzone = document.querySelector("#dropzone");
const preview = document.querySelector("#preview");
const intervalInput = document.querySelector("#intervalInput");
const qualityInput = document.querySelector("#qualityInput");
const qualityLabel = document.querySelector("#qualityLabel");
const exportButton = document.querySelector("#exportButton");
const buttonProgress = document.querySelector("#buttonProgress");
const buttonLabel = document.querySelector("#buttonLabel");
const settingsForm = document.querySelector("#settingsForm");
const frameEstimate = document.querySelector("#frameEstimate");
const durationEstimate = document.querySelector("#durationEstimate");
const fileMeta = document.querySelector("#fileMeta");
const statusText = document.querySelector("#statusText");
const statusDetail = document.querySelector("#statusDetail");
const progressBar = document.querySelector("#progressBar");
const successModal = document.querySelector("#successModal");
const successMessage = document.querySelector("#successMessage");
const closeModalButton = document.querySelector("#closeModalButton");

let sourceFile = null;
let sourceUrl = "";
let exporting = false;
let serverReady = false;
let convertedJob = null;

const apiBase = "http://localhost:4173";
const outputFps = 24;

function setStatus(title, detail, progress = null) {
  statusText.textContent = title;
  statusDetail.textContent = detail;
  if (progress !== null) {
    const safeProgress = Math.max(0, Math.min(100, progress));
    progressBar.style.width = `${safeProgress}%`;
    buttonProgress.style.transform = `scaleX(${safeProgress / 100})`;
  }
}

function setButtonExporting(active) {
  exportButton.classList.toggle("is-exporting", active);
  exportButton.classList.remove("is-error");
  buttonLabel.textContent = active ? "转换中" : "转换";
  if (!active) buttonProgress.style.transform = "scaleX(0)";
}

function setButtonError() {
  exportButton.classList.remove("is-exporting");
  exportButton.classList.add("is-error");
  buttonLabel.textContent = "转换失败";
  buttonProgress.style.transform = "scaleX(0)";
}

function showSuccessModal(outputPath) {
  successMessage.textContent = `MP4 视频已保存到：${outputPath}`;
  successModal.hidden = false;
  closeModalButton.focus();
}

function hideSuccessModal() {
  successModal.hidden = true;
}

function chineseError(error, fallback = "操作失败") {
  const message = `${error?.name || ""} ${error?.message || ""}`.toLowerCase();

  if (error?.name === "AbortError" || message.includes("abort")) return "已取消操作";
  if (message.includes("permission") || message.includes("denied")) return "没有获得写入权限，请确认下载文件夹可写";
  if (message.includes("network") || message.includes("failed to fetch")) return "无法连接本地服务，请确认 node server.js 正在运行";
  if (message.includes("timeout") || message.includes("超时")) return "本地服务响应超时，请重新转换";

  return error?.message && !/[a-z]{4,}/i.test(error.message) ? error.message : fallback;
}

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatSeconds(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 秒";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes} 分 ${rest} 秒`;
}

function getSettings() {
  const interval = Math.max(0.1, Number(intervalInput.value) || 5);
  const quality = Math.max(1, Math.min(12, Number(qualityInput.value) || 6));
  return { interval, fps: outputFps, quality };
}

function updateEstimate() {
  const { interval, fps, quality } = getSettings();
  const duration = preview.duration || 0;
  const frames = duration ? Math.max(1, Math.floor(duration / interval) + 1) : 0;
  frameEstimate.textContent = `${frames} 帧`;
  durationEstimate.textContent = formatSeconds(frames / fps);
  qualityLabel.textContent = `${quality} Mbps`;
  exportButton.disabled = !sourceFile || !serverReady || exporting;
}

async function checkServer() {
  try {
    const response = await fetch(`${apiBase}/api/config`, { cache: "no-store" });
    if (!response.ok) throw new Error("服务不可用");
    serverReady = true;
  } catch {
    serverReady = false;
    setStatus("本地服务未启动", "请在此文件夹运行：node server.js", 0);
  }

  updateEstimate();
}

function loadVideoFile(file) {
  if (!file || !file.type.startsWith("video/")) {
    setStatus("请选择视频文件", "当前文件不是浏览器识别的视频格式", 0);
    return;
  }

  if (sourceUrl) URL.revokeObjectURL(sourceUrl);
  sourceFile = file;
  sourceUrl = URL.createObjectURL(file);
  preview.src = sourceUrl;
  preview.load();
  fileMeta.textContent = `${file.name} · ${formatBytes(file.size)}`;
  convertedJob = null;
  setStatus("视频已载入", "等待读取视频时长", 0);
  updateEstimate();
}

function watchJob(id) {
  return new Promise((resolve, reject) => {
    const events = new EventSource(`${apiBase}/api/jobs/${id}/events`);

    events.onmessage = (event) => {
      const job = JSON.parse(event.data);
      const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));

      if (job.status === "running") {
        setStatus("正在生成 MP4", `转换进度 ${progress}%`, 22 + progress * 0.76);
      } else if (job.status === "done") {
        setStatus("保存成功", `已保存到 ${job.outputPath}`, 100);
      }

      if (job.status === "done") {
        events.close();
        resolve(job);
      }

      if (job.status === "error") {
        events.close();
        reject(new Error(job.error || "导出失败"));
      }
    };

    events.onerror = () => {
      events.close();
      reject(new Error("进度连接中断"));
    };
  });
}

function createJob() {
  return new Promise((resolve, reject) => {
    const { interval, quality } = getSettings();
    const params = new URLSearchParams({
      interval: String(interval),
      quality: String(quality),
      browserSave: "0",
      name: sourceFile.name,
      _: String(Date.now()),
    });
    const request = new XMLHttpRequest();
    let waitingTimer = null;
    let waitingSeconds = 0;
    request.open("POST", `${apiBase}/api/jobs/raw?${params.toString()}`);
    request.responseType = "json";
    request.timeout = 10 * 60 * 1000;

    request.upload.onprogress = (event) => {
      if (!event.lengthComputable) {
        setStatus("正在上传视频", "正在传输原视频到本地服务", 8);
        return;
      }

      const uploadProgress = Math.round((event.loaded / event.total) * 100);
      setStatus("正在上传视频", `上传进度 ${uploadProgress}%`, 3 + uploadProgress * 0.17);
    };

    request.upload.onload = () => {
      setStatus("正在写入临时文件", "上传完成，等待本地服务确认", 20);
      let progress = 20;
      waitingTimer = window.setInterval(() => {
        waitingSeconds += 1;
        progress = Math.min(28, progress + 1);
        setStatus(
          "正在写入临时文件",
          waitingSeconds >= 30 ? "仍在确认临时文件，请稍等" : "本地服务正在确认视频文件",
          progress,
        );
      }, 700);
    };

    request.onload = () => {
      if (waitingTimer) window.clearInterval(waitingTimer);
      const payload = request.response || {};
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
      } else {
        reject(new Error(payload.error || "创建导出任务失败"));
      }
    };

    request.onerror = () => {
      if (waitingTimer) window.clearInterval(waitingTimer);
      reject(new Error("上传到本地服务失败"));
    };

    request.ontimeout = () => {
      if (waitingTimer) window.clearInterval(waitingTimer);
      reject(new Error("上传超时，请重新转换"));
    };

    request.onabort = () => {
      if (waitingTimer) window.clearInterval(waitingTimer);
      reject(new Error("上传已取消"));
    };

    request.send(sourceFile);
  });
}

async function exportTimelapse() {
  if (!sourceFile || exporting) return;

  exporting = true;
  let failed = false;
  setButtonExporting(true);
  updateEstimate();

  try {
    if (!serverReady) throw new Error("本地服务未启动");

    setStatus("正在上传视频", "正在传输原视频到本地服务", 3);
    const payload = await createJob();
    setStatus("正在生成 MP4", "macOS 原生编码器正在处理", 28);
    const job = await watchJob(payload.id);
    convertedJob = job;
    setStatus("保存成功", `已保存到 ${job.outputPath}`, 100);
    showSuccessModal(job.outputPath);
  } catch (error) {
    failed = true;
    setStatus(chineseError(error, "导出失败"), "请确认本地服务正在运行", 0);
    setButtonError();
  } finally {
    exporting = false;
    window.setTimeout(() => setButtonExporting(false), failed ? 1600 : 280);
    updateEstimate();
  }
}

videoInput.addEventListener("change", (event) => {
  loadVideoFile(event.target.files?.[0]);
});

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("is-dragging");
});

dropzone.addEventListener("dragleave", () => {
  dropzone.classList.remove("is-dragging");
});

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("is-dragging");
  loadVideoFile(event.dataTransfer.files?.[0]);
});

preview.addEventListener("loadedmetadata", () => {
  setStatus("视频已载入", `原视频时长 ${formatSeconds(preview.duration)}`, 0);
  updateEstimate();
});

[intervalInput, qualityInput].forEach((input) => {
  input.addEventListener("input", () => {
    convertedJob = null;
    updateEstimate();
  });
});

settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  exportTimelapse();
});

closeModalButton.addEventListener("click", hideSuccessModal);
successModal.addEventListener("click", (event) => {
  if (event.target === successModal) hideSuccessModal();
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !successModal.hidden) hideSuccessModal();
});

updateEstimate();
checkServer();
