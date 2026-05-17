const videoInput = document.querySelector("#videoInput");
const dropzone = document.querySelector("#dropzone");
const preview = document.querySelector("#preview");
const canvas = document.querySelector("#canvas");
const intervalInput = document.querySelector("#intervalInput");
const fpsInput = document.querySelector("#fpsInput");
const qualityInput = document.querySelector("#qualityInput");
const fpsLabel = document.querySelector("#fpsLabel");
const qualityLabel = document.querySelector("#qualityLabel");
const frameEstimate = document.querySelector("#frameEstimate");
const durationEstimate = document.querySelector("#durationEstimate");
const fileMeta = document.querySelector("#fileMeta");
const exportButton = document.querySelector("#exportButton");
const buttonProgress = document.querySelector("#buttonProgress");
const buttonLabel = document.querySelector("#buttonLabel");
const downloadLink = document.querySelector("#downloadLink");
const settingsForm = document.querySelector("#settingsForm");
const supportLine = document.querySelector("#supportLine");

let sourceFile = null;
let sourceUrl = "";
let outputUrl = "";
let exporting = false;

const mimeOptions = [
  { mime: "video/mp4;codecs=avc1.42E01E,mp4a.40.2", extension: "mp4", label: "MP4" },
  { mime: "video/mp4", extension: "mp4", label: "MP4" },
  { mime: "video/webm;codecs=vp9", extension: "webm", label: "WebM" },
  { mime: "video/webm;codecs=vp8", extension: "webm", label: "WebM" },
  { mime: "video/webm", extension: "webm", label: "WebM" },
];

const outputFormat = mimeOptions.find((option) => MediaRecorder.isTypeSupported(option.mime));
const context = canvas.getContext("2d", { alpha: false });

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatSeconds(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 秒";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

function safeBaseName(name) {
  return (name || "video").replace(/\.[^.]+$/, "").replace(/[\\/:*?"<>|]+/g, "").trim() || "video";
}

function getSettings() {
  return {
    interval: Math.max(0.1, Number(intervalInput.value) || 5),
    fps: Math.max(8, Math.min(30, Number(fpsInput.value) || 24)),
    bitrate: Math.max(1, Math.min(12, Number(qualityInput.value) || 7)) * 1_000_000,
  };
}

function estimateFrames() {
  const duration = preview.duration || 0;
  const { interval } = getSettings();
  return duration ? Math.max(1, Math.floor(duration / interval) + 1) : 0;
}

function setStatus(title, detail, progress = null) {
  supportLine.textContent = detail ? `${title}：${detail}` : title;
  if (progress !== null) {
    const value = Math.max(0, Math.min(100, progress));
    buttonProgress.style.transform = `scaleX(${value / 100})`;
  }
}

function updateEstimate() {
  const { fps, bitrate } = getSettings();
  const frames = estimateFrames();
  fpsLabel.textContent = `${fps} fps`;
  qualityLabel.textContent = `${Math.round(bitrate / 1_000_000)} Mbps`;
  frameEstimate.textContent = `${frames} 帧`;
  durationEstimate.textContent = formatSeconds(frames / fps);
  exportButton.disabled = !sourceFile || !outputFormat || exporting;
}

function resetDownload() {
  if (outputUrl) URL.revokeObjectURL(outputUrl);
  outputUrl = "";
  downloadLink.hidden = true;
  downloadLink.removeAttribute("href");
  downloadLink.removeAttribute("download");
}

function loadVideoFile(file) {
  if (!file || !file.type.startsWith("video/")) {
    setStatus("请选择视频文件", "当前文件不是浏览器识别的视频格式", 0);
    return;
  }

  resetDownload();
  if (sourceUrl) URL.revokeObjectURL(sourceUrl);
  sourceFile = file;
  sourceUrl = URL.createObjectURL(file);
  preview.src = sourceUrl;
  preview.load();
  fileMeta.textContent = `${file.name} · ${formatBytes(file.size)}`;
  setStatus("视频已载入", "等待读取视频时长", 0);
  updateEstimate();
}

function seekVideo(time) {
  return new Promise((resolve, reject) => {
    const onSeeked = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("视频读取失败"));
    };
    const cleanup = () => {
      preview.removeEventListener("seeked", onSeeked);
      preview.removeEventListener("error", onError);
    };

    preview.addEventListener("seeked", onSeeked, { once: true });
    preview.addEventListener("error", onError, { once: true });
    preview.currentTime = Math.min(time, Math.max(0, preview.duration - 0.02));
  });
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function exportTimelapse() {
  if (!sourceFile || exporting || !outputFormat) return;

  exporting = true;
  let failed = false;
  resetDownload();
  exportButton.disabled = true;
  exportButton.classList.add("is-exporting");
  exportButton.classList.remove("is-error");
  buttonLabel.textContent = "转换中";
  buttonProgress.style.transform = "scaleX(0)";

  const { interval, fps, bitrate } = getSettings();
  const frames = estimateFrames();
  const chunks = [];

  canvas.width = preview.videoWidth || 1280;
  canvas.height = preview.videoHeight || 720;

  const stream = canvas.captureStream(0);
  const track = stream.getVideoTracks()[0];
  const recorder = new MediaRecorder(stream, {
    mimeType: outputFormat.mime,
    videoBitsPerSecond: bitrate,
  });

  recorder.ondataavailable = (event) => {
    if (event.data.size) chunks.push(event.data);
  };

  const stopped = new Promise((resolve, reject) => {
    recorder.onstop = resolve;
    recorder.onerror = () => reject(new Error("浏览器录制失败"));
  });

  try {
    setStatus("正在准备", `将导出 ${outputFormat.label} 文件`, 1);
    recorder.start();

    for (let index = 0; index < frames; index += 1) {
      const sourceTime = Math.min(index * interval, preview.duration || 0);
      await seekVideo(sourceTime);
      context.drawImage(preview, 0, 0, canvas.width, canvas.height);
      track.requestFrame();

      const progress = Math.round(((index + 1) / frames) * 100);
      setStatus("正在生成延时视频", `正在处理第 ${index + 1} / ${frames} 帧`, progress);
      await wait(1000 / fps);
    }

    recorder.stop();
    await stopped;

    const blob = new Blob(chunks, { type: outputFormat.mime });
    outputUrl = URL.createObjectURL(blob);
    const fileName = `${safeBaseName(sourceFile.name)}-延时视频.${outputFormat.extension}`;

    downloadLink.href = outputUrl;
    downloadLink.download = fileName;
    downloadLink.hidden = false;
    setStatus("转换完成", `已生成 ${outputFormat.label}，点击保存视频下载`, 100);
  } catch (error) {
    failed = true;
    exportButton.classList.remove("is-exporting");
    exportButton.classList.add("is-error");
    buttonLabel.textContent = "转换失败";
    setStatus("转换失败", error.message || "浏览器无法完成这次转换", 0);
  } finally {
    stream.getTracks().forEach((trackItem) => trackItem.stop());
    exporting = false;
    window.setTimeout(() => {
      exportButton.classList.remove("is-exporting", "is-error");
      buttonLabel.textContent = "转换";
      buttonProgress.style.transform = "scaleX(0)";
      updateEstimate();
    }, failed ? 1600 : 280);
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

[intervalInput, fpsInput, qualityInput].forEach((input) => {
  input.addEventListener("input", updateEstimate);
});

settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  exportTimelapse();
});

if (outputFormat) {
  supportLine.textContent = `当前浏览器会导出 ${outputFormat.label} 文件；无需安装软件，视频不会上传。`;
} else {
  supportLine.textContent = "当前浏览器不支持视频录制，请换 Chrome、Edge 或 Safari 新版本。";
}

updateEstimate();
