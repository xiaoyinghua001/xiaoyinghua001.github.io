const http = require("node:http");
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const root = __dirname;
const port = Number(process.env.PORT || 4173);
const uploadsDir = path.join(os.tmpdir(), "timelapse-tool-uploads");
const defaultOutputDir = path.join(os.homedir(), "Downloads");
const jobs = new Map();

function sendJson(res, status, body) {
  res.writeHead(status, {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-File-Name",
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
  });
  res.end(JSON.stringify(body));
}

function safeName(name) {
  return (name || "video")
    .replace(/\.[^.]+$/, "")
    .replace(/[^\p{L}\p{N}._ -]+/gu, "")
    .trim()
    .slice(0, 80) || "video";
}

function contentType(filePath) {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".css")) return "text/css; charset=utf-8";
  if (filePath.endsWith(".js")) return "text/javascript; charset=utf-8";
  return "application/octet-stream";
}

function parseMultipart(buffer, boundary) {
  const boundaryBuffer = Buffer.from(`--${boundary}`);
  const fields = {};
  let file = null;
  let cursor = 0;

  while (cursor < buffer.length) {
    const boundaryStart = buffer.indexOf(boundaryBuffer, cursor);
    if (boundaryStart === -1) break;

    const partStart = boundaryStart + boundaryBuffer.length;
    if (buffer[partStart] === 45 && buffer[partStart + 1] === 45) break;

    let headerStart = partStart;
    if (buffer[headerStart] === 13 && buffer[headerStart + 1] === 10) {
      headerStart += 2;
    }

    const headerEnd = buffer.indexOf(Buffer.from("\r\n\r\n"), headerStart);
    if (headerEnd === -1) break;

    const nextBoundary = buffer.indexOf(boundaryBuffer, headerEnd + 4);
    if (nextBoundary === -1) break;

    const rawHeaders = buffer.slice(headerStart, headerEnd).toString("utf8");
    let dataEnd = nextBoundary;
    if (buffer[dataEnd - 2] === 13 && buffer[dataEnd - 1] === 10) {
      dataEnd -= 2;
    }

    const disposition = rawHeaders.match(/content-disposition:[^\r\n]+/i)?.[0] || "";
    const name = disposition.match(/name="([^"]+)"/)?.[1];
    const filename = disposition.match(/filename="([^"]*)"/)?.[1];
    if (!name) {
      cursor = nextBoundary;
      continue;
    }

    const data = buffer.slice(headerEnd + 4, dataEnd);
    if (filename) {
      file = { field: name, filename, data };
    } else {
      fields[name] = data.toString("utf8");
    }

    cursor = nextBoundary;
  }

  return { fields, file };
}

function collectRequest(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

function notifyJob(job) {
  const payload = `data: ${JSON.stringify(job)}\n\n`;
  for (const listener of job.listeners) listener.write(payload);
}

function createJobRecord({ inputPath, outputPath, interval, fps, bitrate, status = "running" }) {
  const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const job = {
    id,
    status,
    progress: 0,
    inputPath,
    outputPath,
    interval,
    fps,
    bitrate,
    error: "",
    listeners: new Set(),
  };

  jobs.set(id, job);
  return job;
}

function runConversion(job) {
  job.status = "running";
  job.progress = 0;
  notifyJob(job);

  const child = spawn("/usr/bin/swift", [
    path.join(root, "timelapse.swift"),
    "--input",
    job.inputPath,
    "--output",
    job.outputPath,
    "--interval",
    String(job.interval),
    "--fps",
    String(job.fps),
    "--bitrate",
    String(job.bitrate),
  ]);

  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    for (const line of chunk.split(/\r?\n/)) {
      const progress = line.match(/^PROGRESS\s+(\d+)/)?.[1];
      if (progress) {
        job.progress = Number(progress);
        notifyJob(job);
      }
      if (line.startsWith("DONE ")) {
        job.outputPath = line.slice(5);
      }
    }
  });

  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    job.error += chunk;
  });

  child.on("close", (code) => {
    job.status = code === 0 ? "done" : "error";
    job.progress = code === 0 ? 100 : job.progress;
    if (code !== 0 && !job.error) job.error = `转换进程退出：${code}`;
    notifyJob(job);
    for (const listener of job.listeners) listener.end();
    fsp.rm(job.inputPath, { force: true }).catch(() => {});
  });

  return job;
}

function startJob({ inputPath, outputPath, interval, fps, bitrate }) {
  return runConversion(createJobRecord({ inputPath, outputPath, interval, fps, bitrate }));
}

async function handleCreateJob(req, res) {
  const content = req.headers["content-type"] || "";
  const boundary = content.match(/boundary=(.+)$/)?.[1];
  if (!boundary) return sendJson(res, 400, { error: "缺少 multipart boundary" });

  await fsp.mkdir(uploadsDir, { recursive: true });
  const body = await collectRequest(req);
  const { fields, file } = parseMultipart(body, boundary);
  if (!file) return sendJson(res, 400, { error: "没有收到视频文件" });

  const interval = Math.max(0.1, Number(fields.interval) || 5);
  const fps = 24;
  const bitrate = Math.max(500_000, Math.round((Number(fields.quality) || 4) * 1_000_000));
  const base = safeName(file.filename);
  const inputPath = path.join(uploadsDir, `${Date.now()}-${base}${path.extname(file.filename) || ".mov"}`);
  const requestedOutput = (fields.outputPath || "").trim();
  const browserSave = fields.browserSave === "1";
  const outputPath = browserSave
    ? path.join(uploadsDir, `${Date.now()}-${base}-延时视频.mp4`)
    : requestedOutput || path.join(defaultOutputDir, `${base}-延时视频.mp4`);

  await fsp.writeFile(inputPath, file.data);
  const job = startJob({ inputPath, outputPath, interval, fps, bitrate });
  sendJson(res, 200, { id: job.id, outputPath: job.outputPath });
}

async function handleCreateRawJob(req, res, url) {
  await fsp.mkdir(uploadsDir, { recursive: true });

  const rawName = url.searchParams.get("name") || req.headers["x-file-name"] || "video.mov";
  const fileName = decodeURIComponent(String(rawName));
  const interval = Math.max(0.1, Number(url.searchParams.get("interval")) || 5);
  const fps = 24;
  const bitrate = Math.max(500_000, Math.round((Number(url.searchParams.get("quality")) || 6) * 1_000_000));
  const base = safeName(fileName);
  const extension = path.extname(fileName) || ".mov";
  const browserSave = url.searchParams.get("browserSave") === "1";
  const requestedOutput = (url.searchParams.get("outputPath") || "").trim();
  const inputPath = path.join(uploadsDir, `${Date.now()}-${base}${extension}`);
  const outputPath = browserSave
    ? path.join(uploadsDir, `${Date.now()}-${base}-延时视频.mp4`)
    : requestedOutput || path.join(defaultOutputDir, `${base}-延时视频.mp4`);

  const writer = fs.createWriteStream(inputPath);
  req.pipe(writer);

  req.on("error", () => {
    writer.destroy();
  });

  writer.on("error", (error) => {
    sendJson(res, 500, { error: error.message || "写入临时文件失败" });
  });

  writer.on("finish", () => {
    const job = startJob({ inputPath, outputPath, interval, fps, bitrate });
    sendJson(res, 200, { id: job.id, outputPath: job.outputPath });
  });
}

async function handlePrepareUploadJob(req, res, url) {
  await fsp.mkdir(uploadsDir, { recursive: true });

  const rawName = url.searchParams.get("name") || "video.mov";
  const fileName = decodeURIComponent(String(rawName));
  const interval = Math.max(0.1, Number(url.searchParams.get("interval")) || 5);
  const fps = 24;
  const bitrate = Math.max(500_000, Math.round((Number(url.searchParams.get("quality")) || 6) * 1_000_000));
  const totalBytes = Math.max(0, Number(url.searchParams.get("size")) || 0);
  const base = safeName(fileName);
  const extension = path.extname(fileName) || ".mov";
  const requestedOutput = (url.searchParams.get("outputPath") || "").trim();
  const inputPath = path.join(uploadsDir, `${Date.now()}-${base}${extension}`);
  const outputPath = requestedOutput || path.join(defaultOutputDir, `${base}-延时视频.mp4`);
  const job = createJobRecord({ inputPath, outputPath, interval, fps, bitrate, status: "uploading" });
  job.totalBytes = totalBytes;
  job.receivedBytes = 0;

  sendJson(res, 200, { id: job.id, outputPath: job.outputPath });
}

async function handleUploadJob(req, res, id) {
  const job = jobs.get(id);
  if (!job) return sendJson(res, 404, { error: "任务不存在" });
  if (job.status !== "uploading") return sendJson(res, 409, { error: "任务状态不允许上传" });

  const writer = fs.createWriteStream(job.inputPath);
  let responded = false;

  const fail = (error) => {
    if (responded) return;
    responded = true;
    job.status = "error";
    job.error = error.message || "写入临时文件失败";
    notifyJob(job);
    sendJson(res, 500, { error: job.error });
  };

  req.on("data", (chunk) => {
    job.receivedBytes += chunk.length;
    if (job.totalBytes > 0) {
      job.progress = Math.min(100, Math.round((job.receivedBytes / job.totalBytes) * 100));
      notifyJob(job);
    }
  });

  req.on("error", (error) => {
    writer.destroy();
    fail(error);
  });

  writer.on("error", fail);

  writer.on("finish", () => {
    if (responded) return;
    responded = true;
    job.progress = 100;
    notifyJob(job);
    sendJson(res, 200, { id: job.id, outputPath: job.outputPath });
    runConversion(job);
  });

  req.pipe(writer);
}

function handleEvents(req, res, id) {
  const job = jobs.get(id);
  if (!job) return sendJson(res, 404, { error: "任务不存在" });

  res.writeHead(200, {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
  });

  job.listeners.add(res);
  res.write(`data: ${JSON.stringify(job)}\n\n`);
  req.on("close", () => job.listeners.delete(res));
}

function handleJobFile(req, res, id) {
  const job = jobs.get(id);
  if (!job) return sendJson(res, 404, { error: "任务不存在" });
  if (job.status !== "done") return sendJson(res, 409, { error: "任务尚未完成" });

  const stream = fs.createReadStream(job.outputPath);
  stream.on("error", () => {
    sendJson(res, 404, { error: "导出文件不存在" });
  });

  res.writeHead(200, {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
    "Content-Type": "video/mp4",
    "Content-Disposition": `attachment; filename="${encodeURIComponent(path.basename(job.outputPath))}"`,
  });
  stream.pipe(res);
}

async function serveStatic(req, res) {
  const url = new URL(req.url, `http://localhost:${port}`);
  const pathname = url.pathname === "/timelapse/" || url.pathname === "/timelapse" ? "/timelapse.html" : url.pathname;
  let filePath = path.join(root, decodeURIComponent(pathname === "/" ? "/index.html" : pathname));
  if (!filePath.startsWith(root)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }

  try {
    const data = await fsp.readFile(filePath);
    res.writeHead(200, {
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "no-store",
      "Content-Type": contentType(filePath),
    });
    res.end(data);
  } catch {
    res.writeHead(404);
    res.end("Not found");
  }
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "OPTIONS") {
      res.writeHead(204, {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-File-Name",
        "Access-Control-Max-Age": "86400",
        "Cache-Control": "no-store",
      });
      return res.end();
    }

    const url = new URL(req.url, `http://localhost:${port}`);
    if (req.method === "GET" && url.pathname === "/api/config") {
      return sendJson(res, 200, { defaultOutputDir });
    }

    if (req.method === "POST" && url.pathname === "/api/jobs") {
      return handleCreateJob(req, res);
    }

    if (req.method === "POST" && url.pathname === "/api/jobs/raw") {
      return handleCreateRawJob(req, res, url);
    }

    if ((req.method === "GET" || req.method === "POST") && url.pathname === "/api/jobs/prepare") {
      return handlePrepareUploadJob(req, res, url);
    }

    if (req.method === "POST" && url.pathname.startsWith("/api/jobs/") && url.pathname.endsWith("/upload")) {
      const id = url.pathname.split("/")[3];
      return handleUploadJob(req, res, id);
    }

    if (req.method === "GET" && url.pathname.startsWith("/api/jobs/") && url.pathname.endsWith("/events")) {
      const id = url.pathname.split("/")[3];
      return handleEvents(req, res, id);
    }

    if (req.method === "GET" && url.pathname.startsWith("/api/jobs/") && url.pathname.endsWith("/file")) {
      const id = url.pathname.split("/")[3];
      return handleJobFile(req, res, id);
    }

    return serveStatic(req, res);
  } catch (error) {
    sendJson(res, 500, { error: error.message || "服务器错误" });
  }
});

server.listen(port, () => {
  console.log(`延时视频工具已启动：http://localhost:${port}/`);
});
