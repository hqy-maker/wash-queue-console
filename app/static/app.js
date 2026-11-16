const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".heic"]);
const VIDEO_EXTS = new Set([".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"]);

const STATUS_META = {
  draft: { label: "草稿", cls: "pill-muted" },
  drafting: { label: "草稿", cls: "pill-muted" },
  queued: { label: "排队中", cls: "pill-orange" },
  processing: { label: "处理中", cls: "pill-blue" },
  recheck_requested: { label: "待重检", cls: "pill-orange" },
  needs_confirmation: { label: "待确认", cls: "pill-orange" },
  ready_on_canvas: { label: "已就绪", cls: "pill-ready" },
  prompt_update_requested: { label: "待更新提示词", cls: "pill-purple" },
  run_requested: { label: "待运行", cls: "pill-purple" },
  draft_ready: { label: "已出样片", cls: "pill-green" },
  feedback_queued: { label: "待修改", cls: "pill-orange" },
  final_requested: { label: "转正片中", cls: "pill-purple" },
  final_ready: { label: "已过条", cls: "pill-passed" },
  final_done: { label: "已过条", cls: "pill-passed" },
  error: { label: "出错", cls: "pill-red" },
};

function isFinalInProgress(shot) {
  if (!shot) return false;
  if (typeof shot.isFinalInProgress === "boolean") return shot.isFinalInProgress;
  const st = shot.status;
  const pendingAction = shot.jimengMeta?.pendingAction || "";
  return Boolean(
    st === "final_requested" ||
      (st === "processing" && shot.processingTaskType === "final_requested") ||
      (["queued", "processing", "needs_confirmation"].includes(st) && pendingAction === "promote_1080p")
  );
}

function isPassedFinal(shot) {
  if (!shot) return false;
  if (typeof shot.isPassedFinal === "boolean") return shot.isPassedFinal;
  return ["final_ready", "final_done"].includes(shot.status);
}

function isOutdatedFinal(shot) {
  if (!shot || isPassedFinal(shot) || isFinalInProgress(shot)) return false;
  return Boolean(shot.hasOutdatedFinal);
}

function guardPassedFinalAction(
  shot,
  { actionName = "修改", consequenceText = "", okText = "", onProceed = null } = {}
) {
  if (typeof onProceed !== "function") return;
  if (!isPassedFinal(shot)) {
    return onProceed();
  }
  const finTime = shot.finalVideo?.downloadedAt || shot.lastGeneratedAt || shot.updatedAt || "已归档";
  openConfirmModal({
    title: `本镜头已定版（1080P 已过条），确定要${actionName}吗？`,
    message:
      `「${shot.id}」已完成 1080P 正片定版。${consequenceText ? consequenceText + "\n\n" : ""}` +
      "确认后将解锁本镜修改，后续可继续生成新版 480P 样片并重转 1080P 正片（原 1080P 归档视频仍会保留用于对照）。",
    meta: `镜头：${shot.id} · 当前定版时间：${finTime}`,
    okText: okText || `确认解锁并${actionName}`,
    cancelText: "取消（保持定版）",
    danger: true,
    onConfirm: onProceed,
  });
}

const ERROR_CODE_LABELS = {
  model_conflict: "参数冲突",
  stalled: "卡住中断",
  interrupted: "进程中断",
  timeout: "运行超时",
  rate_limit: "配额/限流",
  network_error: "网络中断",
  auth_expired: "登录失效",
  credits_insufficient: "积分不足",
  moderation: "内容拦截",
  asset_missing: "素材缺失",
  unknown_error: "执行异常",
};

function getShotErrorTag(shot) {
  if (!shot || shot.status !== "error") return "";
  if ((shot.errorTag || "").trim()) return shot.errorTag.trim();
  return ERROR_CODE_LABELS[shot.errorCode || ""] || "执行异常";
}

function getDisplayStatusMeta(shot) {
  if (!shot) return { label: "", cls: "pill-muted" };
  if (shot.status === "error") {
    const errTag = getShotErrorTag(shot);
    return { label: errTag ? `出错 · ${errTag}` : "出错", cls: "pill-red" };
  }
  if (isFinalInProgress(shot)) {
    if (shot.status === "needs_confirmation") {
      return { label: "正片·待确认", cls: "pill-orange" };
    }
    return { label: "转正片中", cls: "pill-purple" };
  }
  if (isPassedFinal(shot)) {
    return STATUS_META.final_ready;
  }
  return STATUS_META[shot.status] || { label: shot.status, cls: "pill-muted" };
}

const DEFAULT_SCENES = ["通用", "第08场", "第10场", "第75场"];
const WHITEOUT_TAGS = ["涂脸", "涂帽子", "涂衣服"];

const state = {
  data: null,
  view: "builder", // "builder" | "detail"
  selectedShotId: null,
  selectedSceneFolder: null,
  editingFolderName: null,
  creatingFolder: false,
  draggingShotId: null,
  expandedFolders: new Set(),
  seenFolders: new Set(),
  selectedSceneTab: "通用",
  eventSeq: 0,
  mentionCleanups: [],
  builderPromptEditor: null,
  detailPromptEditor: null,
  fbCommentEditor: null,
  historyModal: {
    open: false,
    shotId: null,
    selectedVersionId: null,
  },
  confirmDialog: {
    open: false,
    onConfirm: null,
  },
  renameModal: {
    open: false,
    shotId: null,
    downloadVerAfter: null,
  },
  exportConfigModal: {
    open: false,
    focusScene: "",
  },
  runtimeModal: {
    open: false,
    shotId: null,
  },
  runtimeByShot: {},
  runtimeBusy: false,
  runtimePollTimer: null,
  shotMoreOpen: false,
  shotChatMenuOpen: false,
  shotChatBusy: false,
  canvasPicker: {
    open: false,
    query: "",
    creating: false,
    newName: "",
    createMode: "current_ws",
    submitting: false,
    refreshing: false,
    authStarting: false,
  },
  platformAuthPollTimer: null,
  aiStatusOpen: false,
  agentStatus: null,
  aiActionPending: false,
  serverUnreachable: false,
  updateMenuOpen: false,
  updateInfo: null,
  updateActionPending: false,
  updateApplying: false,
  builder: {
    shotId: "",
    shotIdTouched: false,
    title: "",
    scene: "",
    sceneTouched: false,
    userPrompt: "",
    userPromptTokens: "",
    videoItem: null,
    imageItems: [],
    whiteoutParts: [],
    whiteoutTarget: "",
  },
  detailDraft: {
    shotId: null,
    lastSyncedPrompt: "",
    editedPrompt: "",
    editedPromptTokens: "",
    comment: "",
    commentTokens: "",
    attachments: [], // { id, file, url, kind, name }
    whiteoutParts: [],
    whiteoutTarget: "",
    compareSource: false,
    previewVersion: "auto", // "auto" | "draft" | "final"
  },
};

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function getExt(name) {
  const idx = String(name || "").lastIndexOf(".");
  return idx >= 0 ? name.slice(idx).toLowerCase() : "";
}

function isVideoFile(file) {
  return (file.type || "").startsWith("video/") || VIDEO_EXTS.has(getExt(file.name));
}

function isImageFile(file) {
  return (file.type || "").startsWith("image/") || IMAGE_EXTS.has(getExt(file.name));
}

function uid() {
  return Math.random().toString(36).slice(2, 10);
}

function stripExt(name) {
  return String(name || "").replace(/\.[^.]+$/, "");
}

function mediaDisplayName(obj, fallback = "") {
  if (!obj) return fallback;
  if (obj.filename) return obj.filename;
  if (obj.filePath) {
    const parts = String(obj.filePath).split(/[\\/]/);
    return parts[parts.length - 1] || obj.label || fallback;
  }
  return obj.label || fallback;
}

function sanitizeDownloadStem(raw, shotId = "") {
  let txt = String(raw || "").trim();
  if (!txt) return "";
  txt = txt.replace(/\.(mp4|mov|mkv|webm)$/i, "").trim();
  if (shotId) {
    const escaped = String(shotId).trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    txt = txt.replace(new RegExp(`^${escaped}[\\s_\\-]+`, "i"), "").trim();
  }
  txt = txt.replace(/[\s_\-]*(?:1080p|480p|正片|样片)$/i, "").trim();
  return txt
    .replace(/[\\/:*?"<>|\x00-\x1f\s]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^[._-]+|[._-]+$/g, "")
    .slice(0, 80);
}

function buildClientDownloadFilename(shot, kind = "1080p", customTitle = null) {
  const sid = String(shot?.id || "shot").trim() || "shot";
  const cleanKind = String(kind).toLowerCase() === "final" || String(kind).toLowerCase() === "1080p" ? "1080p" : "480p";
  if (customTitle === null) {
    if (cleanKind === "1080p" && shot?.downloadName1080p) return shot.downloadName1080p;
    if (cleanKind === "480p" && shot?.downloadName480p) return shot.downloadName480p;
  }
  const rawTitle = customTitle === null ? shot?.title || "" : customTitle;
  const stem = sanitizeDownloadStem(rawTitle, sid);
  return stem ? `${sid}_${stem}_${cleanKind}.mp4` : `${sid}_${cleanKind}.mp4`;
}

function shotMediaSubtitle(s) {
  if ((s?.title || "").trim()) {
    return String(s.title).trim();
  }
  if (s.sourceVideo?.mediaUrl || s.sourceVideo?.filePath) {
    return mediaDisplayName(s.sourceVideo, "原片视频");
  }
  if (s.compImage) return "仅图参考";
  return "";
}

function showToast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.add("hidden"), 2800);
}

async function apiJson(url, opts = {}) {
  const res = await fetch(url, opts);
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    throw new Error(data?.detail || `HTTP ${res.status}`);
  }
  return data;
}

function suggestNextShotId(shots) {
  let maxNum = 0;
  for (const s of shots || []) {
    const m = /^S(\d+)$/i.exec(String(s.id || "").trim());
    if (m) maxNum = Math.max(maxNum, Number(m[1]));
  }
  return `S${String(maxNum + 1).padStart(2, "0")}`;
}

function getAllScenes(data) {
  const seen = new Set(DEFAULT_SCENES);
  for (const sc of data?.scenes || []) {
    if (sc) seen.add(sc);
  }
  for (const p of data?.presets || []) {
    if (p.scene) seen.add(p.scene);
  }
  for (const s of data?.shots || []) {
    if (s.scene) seen.add(s.scene);
  }
  return Array.from(seen).sort((a, b) => {
    if (a === "通用") return -1;
    if (b === "通用") return 1;
    return a.localeCompare(b, "zh-Hans-CN", { numeric: true });
  });
}

function getPresetTabs(presets, data = state.data, activeScene = state.builder.scene) {
  const scenes = new Set(["通用"]);
  for (const sc of data?.scenes || []) {
    if (sc && sc.trim()) scenes.add(sc.trim());
  }
  for (const p of presets || []) {
    if (p.scene && p.scene.trim()) scenes.add(p.scene.trim());
  }
  for (const s of data?.shots || []) {
    if (s.scene && s.scene.trim()) scenes.add(s.scene.trim());
  }
  const activeTrim = (activeScene || "").trim();
  if (activeTrim && activeTrim !== "未分场") scenes.add(activeTrim);

  const rest = Array.from(scenes)
    .filter((s) => s !== "通用" && s !== activeTrim)
    .sort((a, b) => a.localeCompare(b, "zh-Hans-CN", { numeric: true }));

  const out = [];
  if (activeTrim && activeTrim !== "未分场") out.push(activeTrim);
  if (!out.includes("通用")) out.push("通用");
  return [...out, ...rest];
}

function getBuilderItemPresetId(it) {
  return String(it?.presetId || it?.originalPresetId || "").trim();
}

function withMediaVersion(url, obj) {
  const raw = String(url || "").trim();
  if (!raw || raw.startsWith("blob:") || raw.startsWith("data:")) return raw;
  const ver = obj?.whiteoutVersion || obj?.meta?.bytes || "";
  if (!ver) return raw;
  return `${raw}${raw.includes("?") ? "&" : "?"}v=${encodeURIComponent(String(ver))}`;
}

function buildOrderedBuilderAssets() {
  const items = [];
  state.builder.imageItems.forEach((img, i) => {
    const slotTag = `图片${i + 1}`;
    const hasPreset = Boolean(getBuilderItemPresetId(img));
    const rawUrl = img.url || "";
    const rawOrigUrl = img.originalUrl || rawUrl;
    items.push({
      id: img.id,
      slotTag,
      mentionToken: slotTag,
      insertText: hasPreset ? `${slotTag}（${img.label}）` : slotTag,
      mediaType: "image",
      rawUrl,
      rawOrigUrl,
      hasOrigDiff: Boolean(rawOrigUrl && rawOrigUrl !== rawUrl),
      url: withMediaVersion(rawUrl, img),
      originalUrl: withMediaVersion(rawOrigUrl, {}),
      whiteoutApplied: Boolean(img.whiteoutApplied),
      whiteoutParts: img.whiteoutParts || [],
      label: img.label,
      subtitle: img.kind === "preset" ? img.scene || "角色库" : hasPreset ? "已涂白角色" : "上传图片",
      meta: img.meta,
      editableIndex: i,
    });
  });
  if (state.builder.videoItem) {
    const idx = state.builder.imageItems.length + 1;
    const slotTag = `视频${idx}`;
    const v = state.builder.videoItem;
    items.push({
      id: v.id,
      slotTag,
      mentionToken: slotTag,
      insertText: slotTag,
      mediaType: "video",
      url: v.url,
      label: v.name,
      subtitle: v.meta?.suggestedDuration ? `${v.meta.suggestedDuration}s` : "原片视频",
      meta: v.meta,
      editableIndex: -1,
    });
  }
  return items;
}

function buildOrderedShotAssets(shot) {
  if (!shot) return [];
  const items = [];
  const imgs = [
    ...(shot.compImage ? [{ ...shot.compImage, _isComp: true }] : []),
    ...(shot.charRefs || []),
  ];
  imgs.forEach((img, i) => {
    const slotTag = `图片${i + 1}`;
    const label = img.label || (img._isComp ? "构图图" : `参考图${i + 1}`);
    const isPreset = img.kind === "preset" || Boolean(img.presetId);
    const rawUrl = img.mediaUrl || "";
    const rawOrigUrl = img.originalMediaUrl || rawUrl;
    items.push({
      id: img.slotId || (img._isComp ? "comp_0" : `char_${i}`),
      slotTag,
      mentionToken: slotTag,
      insertText: isPreset ? `${slotTag}（${label}）` : slotTag,
      mediaType: "image",
      rawUrl,
      rawOrigUrl,
      hasOrigDiff: Boolean(rawOrigUrl && rawOrigUrl !== rawUrl),
      url: withMediaVersion(rawUrl, img),
      originalUrl: rawOrigUrl,
      whiteoutApplied: Boolean(img.whiteoutApplied),
      whiteoutParts: img.whiteoutParts || [],
      label,
      meta: img.meta || null,
    });
  });
  if (shot.sourceVideo && shot.sourceVideo.mediaUrl) {
    const idx = imgs.length + 1;
    const slotTag = `视频${idx}`;
    items.push({
      id: "source_video",
      slotTag,
      mentionToken: slotTag,
      insertText: slotTag,
      mediaType: "video",
      url: shot.sourceVideo.mediaUrl,
      label: mediaDisplayName(shot.sourceVideo, "原片视频"),
      meta: shot.sourceVideo.meta || null,
    });
  }
  return items;
}

function buildFeedbackAttachAssets() {
  let imgIdx = 0;
  let vidIdx = 0;
  return (state.detailDraft.attachments || []).map((att) => {
    const isVid = att.kind === "video";
    if (isVid) vidIdx += 1;
    else imgIdx += 1;
    const slotTag = isVid ? `新视频${vidIdx}` : `新图${imgIdx}`;
    return {
      id: `fb_${att.id}`,
      slotTag,
      mentionToken: slotTag,
      insertText: slotTag,
      mediaType: isVid ? "video" : "image",
      url: att.url,
      label: att.name,
      subtitle: "新上传参考",
      isFeedbackAttachment: true,
    };
  });
}

function buildDetailMentionAssets(shot) {
  return [...buildOrderedShotAssets(shot), ...buildFeedbackAttachAssets()];
}

function renderAssetStripHtml(
  items,
  { editable = false, emptyText = "上传视频、图片或点选上方角色后，素材小图标会显示在这里", hideWhenEmpty = false } = {}
) {
  if (!items.length) {
    if (hideWhenEmpty) {
      return `<div class="asset-strip-wrap hidden"></div>`;
    }
    return `<div class="asset-strip-wrap"><div class="asset-strip-empty">${escapeHtml(emptyText)}</div></div>`;
  }
  const imageCount = items.filter((it) => it.mediaType === "image").length;

  const renderCard = (item) => {
    const isVideo = item.mediaType === "video";
    const issues = item.meta?.issues || [];
    const warnDot = issues.length
      ? `<span class="asset-warn-dot" title="${escapeHtml(issues.join("；"))}"></span>`
      : "";
    const woSummary = (item.whiteoutParts || []).filter(Boolean).join("·");
    const maskedBadge = !isVideo && item.whiteoutApplied
      ? `<span class="asset-masked-badge" title="${escapeHtml((woSummary ? `已涂白（${woSummary}）` : "已涂白") + " · 当前预览即为涂白图，点击可补涂或查看对比")}">已涂白</span>`
      : "";
    const hasOrigDiff = !isVideo && (item.hasOrigDiff ?? Boolean(item.originalUrl && item.originalUrl !== item.url));
    const mediaHtml = isVideo
      ? `<video src="${escapeHtml(item.url)}" muted playsinline preload="metadata"></video><div class="asset-video-play">▶</div>`
      : `<img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.label)}" />`;
    const caption = `${item.slotTag} · ${item.label}${!isVideo && item.whiteoutApplied ? "（已涂白）" : ""}`;
    let actionsHtml = "";
    if (editable) {
      if (isVideo) {
        actionsHtml = `
          <div class="asset-thumb-actions">
            <span class="pill pill-muted" style="font-size:10px;padding:0 5px;">${escapeHtml(
              item.meta?.suggestedDuration ? `${item.meta.suggestedDuration}s` : "原片"
            )}</span>
            <button class="mini-btn danger" type="button" data-remove-video="1" title="移除视频">×</button>
          </div>`;
      } else {
        const i = item.editableIndex;
        actionsHtml = `
          <div class="asset-thumb-actions">
            <button class="mini-btn" type="button" data-move-img="${i}" data-dir="-1" ${i === 0 ? "disabled" : ""} title="前移">←</button>
            <button class="mini-btn" type="button" data-move-img="${i}" data-dir="1" ${i >= imageCount - 1 ? "disabled" : ""} title="后移">→</button>
            <button class="mini-btn danger" type="button" data-remove-img="${i}" title="移除">×</button>
          </div>`;
      }
    }
    const compareBtnHtml = hasOrigDiff
      ? `<button type="button" class="mini-link-btn" data-zoom-orig-url="${escapeHtml(item.originalUrl)}" data-zoom-caption="${escapeHtml(`${item.slotTag} · ${item.label}（未涂白原图）`)}" title="点击查看未涂白原图进行对比">原图对比</button>`
      : "";
    const footerMetaHtml =
      compareBtnHtml || actionsHtml
        ? `<div class="asset-thumb-sub-actions">${compareBtnHtml || "<span></span>"}${actionsHtml || ""}</div>`
        : "";
    const paintAttrs = isVideo
      ? ""
      : `data-paint-slot="${escapeHtml(item.slotTag)}" data-paint-id="${escapeHtml(item.id)}" data-paint-orig="${escapeHtml(item.originalUrl || item.url)}" data-paint-mode="${editable ? "builder" : "detail"}"`;
    return `
      <div class="asset-thumb-card">
        <div class="asset-thumb-media ${isVideo ? "" : "is-paintable"}"
          data-zoom-url="${escapeHtml(item.url)}"
          data-zoom-kind="${isVideo ? "video" : "image"}"
          data-zoom-caption="${escapeHtml(caption)}"
          ${paintAttrs}
          title="${isVideo ? `点击放大查看 ${escapeHtml(caption)}` : `点击涂白 · ${escapeHtml(caption)}`}">
          ${mediaHtml}
          <span class="asset-slot-badge ${isVideo ? "is-video" : ""}">${escapeHtml(item.slotTag)}</span>
          ${maskedBadge}
          ${warnDot}
        </div>
        <div class="asset-thumb-footer">
          <div class="asset-thumb-label" title="${escapeHtml(item.label)}">${escapeHtml(item.label)}</div>
          ${footerMetaHtml}
        </div>
      </div>`;
  };

  return `<div class="asset-strip-wrap"><div class="asset-thumb-strip">${items.map(renderCard).join("")}</div></div>`;
}

function buildWhiteoutTargetOptionsHtml(imageItems, selectedTarget) {
  if (!imageItems.length) {
    return `<option value="">请先添加图片素材</option>`;
  }
  const placeholder =
    imageItems.length === 1
      ? `默认：${imageItems[0].slotTag} · ${imageItems[0].label}`
      : "自动识别对象";
  const opts = imageItems.map((it) => {
    const sel = selectedTarget === it.slotTag ? "selected" : "";
    return `<option value="${escapeHtml(it.slotTag)}" ${sel}>${escapeHtml(it.slotTag)} · ${escapeHtml(it.label)}</option>`;
  });
  return `<option value="">${escapeHtml(placeholder)}</option>${opts.join("")}`;
}

function renderWhiteoutBarHtml({ prefix, parts = [], targetSlot = "", imageItems = [] }) {
  const selected = new Set(parts || []);
  const pillHtml = WHITEOUT_TAGS.map((tag) => {
    const active = selected.has(tag);
    return `<button type="button" class="whiteout-pill ${active ? "active" : ""}" data-${prefix}-wo-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`;
  }).join("");
  return `
    <div class="whiteout-bar" id="${prefix}WhiteoutBar">
      <div class="whiteout-left">
        <span class="whiteout-label">快捷涂白</span>
        <div class="whiteout-pills">${pillHtml}</div>
      </div>
      <div class="whiteout-target-wrap">
        <span>对象</span>
        <select id="${prefix}WhiteoutTarget" class="whiteout-target-select" ${imageItems.length ? "" : "disabled"}>
          ${buildWhiteoutTargetOptionsHtml(imageItems, targetSlot)}
        </select>
      </div>
    </div>`;
}

function bindWhiteoutBarEvents({ prefix, getParts, setParts, setTarget }) {
  const attr = `data-${prefix}-wo-tag`;
  document.querySelectorAll(`[${attr}]`).forEach((btn) => {
    btn.addEventListener("click", () => {
      const tag = btn.getAttribute(attr);
      if (!tag) return;
      const next = new Set(getParts() || []);
      if (next.has(tag)) {
        next.delete(tag);
        btn.classList.remove("active");
      } else {
        next.add(tag);
        btn.classList.add("active");
      }
      setParts(Array.from(next));
    });
  });

  const sel = document.getElementById(`${prefix}WhiteoutTarget`);
  sel?.addEventListener("change", () => {
    setTarget(sel.value || "");
  });
}

/* Mirror div calculation for @ mention popup placement */
const MENTION_START = "\uFFF0";
const MENTION_END = "\uFFF1";
const MENTION_TOKEN_RE = /\uFFF0([^\uFFF1|]*)\|([^\uFFF1]*)\uFFF1/g;
const PLAIN_MENTION_RE = /@((?:新?(?:图片?|视频))\d+)/g;

function clearMentionCleanups() {
  for (const fn of state.mentionCleanups || []) {
    try {
      fn();
    } catch {
      // ignore cleanup error
    }
  }
  state.mentionCleanups = [];
  state.builderPromptEditor = null;
  state.detailPromptEditor = null;
  state.fbCommentEditor = null;
}

function tokensFromPlainText(plainText, items) {
  const src = String(plainText || "");
  if (!src) return "";
  const byTag = new Map((items || []).map((it) => [it.slotTag, it]));
  return src.replace(PLAIN_MENTION_RE, (full, tag) => {
    const item = byTag.get(tag);
    if (!item) return full;
    return `${MENTION_START}${item.id}|${item.slotTag}${MENTION_END}`;
  });
}

function syncTokensWithAssets(tokenText, plainText, items) {
  const rawTokens = tokenText ? String(tokenText) : tokensFromPlainText(plainText, items);
  if (!rawTokens) return { tokens: "", plainText: "" };
  const byId = new Map((items || []).map((it) => [it.id, it]));
  const updatedTokens = rawTokens.replace(MENTION_TOKEN_RE, (_m, id, fallbackTag) => {
    const live = byId.get(id);
    const nextTag = live ? live.slotTag : fallbackTag;
    return `${MENTION_START}${id}|${nextTag}${MENTION_END}`;
  });
  const updatedPlain = updatedTokens.replace(MENTION_TOKEN_RE, (_m, id, tag) => {
    const live = byId.get(id);
    return `@${live ? live.slotTag : tag}`;
  });
  return { tokens: updatedTokens, plainText: updatedPlain };
}

function updateMentionChipEl(chip, item, fallbackTag) {
  const tag = item ? item.slotTag : fallbackTag;
  const isVid = item ? item.mediaType === "video" : /^新?视频/.test(tag);
  const isNew = item ? Boolean(item.isFeedbackAttachment) : /^新/.test(tag);
  const isMissing = !item;

  chip.dataset.slotTag = tag;
  chip.className = [
    "mention-chip",
    isVid ? "is-video" : "",
    isNew ? "is-new-att" : "",
    isMissing ? "is-missing" : "",
  ]
    .filter(Boolean)
    .join(" ");

  chip.title = item
    ? `${tag}${item.label ? ` · ${item.label}` : ""}（双击可放大预览）`
    : `${tag}（素材已移除）`;

  const desiredThumbUrl = isVid ? "" : item?.url || "";
  const existingImg = chip.querySelector("img.mention-chip-thumb");
  const existingVideoThumb = chip.querySelector(".mention-chip-video-thumb");
  const existingText = chip.querySelector(".mention-chip-text");

  if (isVid && !existingVideoThumb) {
    chip.innerHTML = `<span class="mention-chip-video-thumb">▶</span><span class="mention-chip-text">${escapeHtml(tag)}</span>`;
  } else if (!isVid && desiredThumbUrl && existingImg) {
    if (existingImg.dataset.thumbUrl !== desiredThumbUrl) {
      existingImg.src = desiredThumbUrl;
      existingImg.dataset.thumbUrl = desiredThumbUrl;
    }
    if (existingText && existingText.textContent !== tag) {
      existingText.textContent = tag;
    }
  } else if (!isVid && desiredThumbUrl) {
    chip.innerHTML = `<img class="mention-chip-thumb" data-thumb-url="${escapeHtml(desiredThumbUrl)}" src="${escapeHtml(desiredThumbUrl)}" alt="" /><span class="mention-chip-text">${escapeHtml(tag)}</span>`;
  } else {
    chip.innerHTML = `<span class="mention-chip-text">${escapeHtml(tag)}${isMissing ? " (已移除)" : ""}</span>`;
  }
}

function buildMentionChipEl(assetId, slotTag, item) {
  const chip = document.createElement("span");
  chip.setAttribute("contenteditable", "false");
  chip.dataset.mentionChip = "1";
  chip.dataset.assetId = assetId;
  updateMentionChipEl(chip, item, slotTag);
  return chip;
}

function renderTokensToEditor(editorEl, tokenText, items) {
  editorEl.innerHTML = "";
  if (!tokenText) return;
  const byId = new Map((items || []).map((it) => [it.id, it]));
  const lines = String(tokenText).split("\n");
  for (const line of lines) {
    const lineDiv = document.createElement("div");
    if (!line) {
      lineDiv.appendChild(document.createElement("br"));
      editorEl.appendChild(lineDiv);
      continue;
    }
    let lastPos = 0;
    MENTION_TOKEN_RE.lastIndex = 0;
    let m;
    while ((m = MENTION_TOKEN_RE.exec(line)) !== null) {
      if (m.index > lastPos) {
        lineDiv.appendChild(document.createTextNode(line.slice(lastPos, m.index)));
      }
      const assetId = m[1];
      const fallbackTag = m[2];
      const liveItem = byId.get(assetId) || null;
      lineDiv.appendChild(buildMentionChipEl(assetId, fallbackTag, liveItem));
      lastPos = m.index + m[0].length;
    }
    if (lastPos < line.length) {
      lineDiv.appendChild(document.createTextNode(line.slice(lastPos)));
    }
    editorEl.appendChild(lineDiv);
  }
}

function serializeMentionEditor(editorEl, byId) {
  const lines = [];
  let curT = "";
  let curP = "";
  let hasInLine = false;

  function flushLine() {
    lines.push({ t: curT, p: curP });
    curT = "";
    curP = "";
    hasInLine = false;
  }

  function pushText(raw) {
    const clean = String(raw || "").replace(/\u200B/g, "");
    if (!clean) return;
    const parts = clean.split("\n");
    parts.forEach((part, idx) => {
      if (idx > 0) flushLine();
      if (part) {
        curT += part;
        curP += part;
        hasInLine = true;
      }
    });
  }

  function walk(node) {
    if (!node) return;
    if (node.nodeType === Node.TEXT_NODE) {
      pushText(node.nodeValue);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;

    if (node.dataset?.mentionChip === "1") {
      const id = node.dataset.assetId || "";
      const live = byId.get(id);
      const tag = live ? live.slotTag : node.dataset.slotTag || "";
      curT += `${MENTION_START}${id}|${tag}${MENTION_END}`;
      curP += `@${tag}`;
      hasInLine = true;
      return;
    }

    if (node.tagName === "BR") {
      const parent = node.parentNode;
      const parentIsRoot = parent === editorEl;
      const isSole =
        parent &&
        Array.from(parent.childNodes).every(
          (c) => c === node || (c.nodeType === Node.TEXT_NODE && !c.nodeValue.replace(/\u200B/g, ""))
        );
      if (parentIsRoot && isSole) return;
      if (!parentIsRoot && isSole) return;
      if (!node.nextSibling && parent && (parent.tagName === "DIV" || parent.tagName === "P")) return;
      flushLine();
      return;
    }

    const isBlock = node.tagName === "DIV" || node.tagName === "P" || node.tagName === "LI";
    if (isBlock) {
      if (hasInLine) flushLine();
      for (const ch of node.childNodes) walk(ch);
      flushLine();
      return;
    }

    for (const ch of node.childNodes) walk(ch);
  }

  for (const ch of Array.from(editorEl.childNodes)) {
    walk(ch);
  }
  if (hasInLine) flushLine();

  return {
    tokens: lines.map((l) => l.t).join("\n"),
    plainText: lines.map((l) => l.p).join("\n"),
  };
}

function getRangeViewportCoords(textNode, offset, editorEl) {
  try {
    const r = document.createRange();
    r.setStart(textNode, offset);
    r.collapse(true);
    const rect = r.getBoundingClientRect();
    if (rect && (rect.height > 0 || rect.top > 0 || rect.left > 0)) {
      return {
        left: Math.min(Math.max(12, rect.left), window.innerWidth - 280),
        top: Math.min(Math.max(12, rect.bottom + 4), window.innerHeight - 220),
        rawTop: rect.top,
      };
    }
  } catch {
    // fallback below
  }
  const edRect = editorEl.getBoundingClientRect();
  return {
    left: Math.min(Math.max(12, edRect.left + 14), window.innerWidth - 280),
    top: Math.min(Math.max(12, edRect.top + 34), window.innerHeight - 220),
    rawTop: edRect.top + 12,
  };
}

function initRichMentionEditor({ editorEl, initialTokens = "", initialPlainText = "", getItemsFn, onChange }) {
  if (!editorEl) return null;

  const initItems = getItemsFn();
  const initialSynced = syncTokensWithAssets(initialTokens, initialPlainText, initItems);
  renderTokensToEditor(editorEl, initialSynced.tokens, initItems);
  if (onChange) onChange(initialSynced);

  const popover = document.createElement("div");
  popover.className = "mention-popover hidden";
  document.body.appendChild(popover);

  let matches = [];
  let activeIndex = 0;
  let mentionTarget = null; // { textNode, start, end }
  let isOpen = false;
  let isComposing = false;

  function emitSync() {
    const items = getItemsFn();
    const byId = new Map(items.map((it) => [it.id, it]));
    const serialized = serializeMentionEditor(editorEl, byId);
    if (onChange) onChange(serialized);
    return serialized;
  }

  function closeMenu() {
    isOpen = false;
    matches = [];
    mentionTarget = null;
    popover.classList.add("hidden");
  }

  function applySelection(item) {
    if (!item || !mentionTarget || !mentionTarget.textNode.isConnected) return;
    const { textNode, start, end } = mentionTarget;
    const full = textNode.nodeValue || "";
    const prefix = full.slice(0, start);
    const suffix = full.slice(end);
    const needSpace = suffix.length === 0 || !/^[\s\u200B，。！？、；：）》\]）】]/.test(suffix);

    const parent = textNode.parentNode;
    const chip = buildMentionChipEl(item.id, item.slotTag, item);
    const afterTextNode = document.createTextNode((needSpace ? " " : "") + suffix);

    if (prefix) {
      textNode.nodeValue = prefix;
      parent.insertBefore(chip, textNode.nextSibling);
      parent.insertBefore(afterTextNode, chip.nextSibling);
    } else {
      parent.insertBefore(chip, textNode);
      parent.insertBefore(afterTextNode, textNode);
      textNode.remove();
    }

    const sel = window.getSelection();
    if (sel) {
      const nextRange = document.createRange();
      nextRange.setStart(afterTextNode, needSpace ? 1 : 0);
      nextRange.collapse(true);
      sel.removeAllRanges();
      sel.addRange(nextRange);
    }

    editorEl.focus();
    closeMenu();
    emitSync();
  }

  function renderMenu() {
    if (!matches.length || !mentionTarget) {
      closeMenu();
      return;
    }
    popover.innerHTML = matches
      .map((item, idx) => {
        const thumb =
          item.mediaType === "video"
            ? `<div class="mention-thumb-video">▶</div>`
            : `<img class="mention-thumb" src="${escapeHtml(item.url)}" alt="" />`;
        return `
          <button type="button" class="mention-item ${idx === activeIndex ? "active" : ""}" data-mention-idx="${idx}">
            ${thumb}
            <div style="min-width:0;flex:1;">
              <div class="mention-token">@${escapeHtml(item.mentionToken)}</div>
              <div class="mention-label">${escapeHtml(item.label)}${item.isFeedbackAttachment ? " · 新上传" : ""}</div>
            </div>
          </button>
        `;
      })
      .join("");

    const coords = getRangeViewportCoords(mentionTarget.textNode, mentionTarget.start, editorEl);
    popover.classList.remove("hidden");
    const popRect = popover.getBoundingClientRect();
    let top = coords.top;
    if (top + popRect.height > window.innerHeight - 12 && coords.rawTop - popRect.height - 8 > 10) {
      top = coords.rawTop - popRect.height - 6;
    }
    popover.style.left = `${coords.left}px`;
    popover.style.top = `${top}px`;

    popover.querySelectorAll(".mention-item").forEach((btn) => {
      btn.addEventListener("mousedown", (e) => {
        e.preventDefault();
        const idx = Number(btn.dataset.mentionIdx);
        applySelection(matches[idx]);
      });
    });
  }

  function evaluateTrigger() {
    if (isComposing) return;
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || !sel.isCollapsed || !editorEl.contains(sel.anchorNode)) {
      closeMenu();
      return;
    }
    let node = sel.anchorNode;
    let offset = sel.anchorOffset;
    if (node.nodeType === Node.ELEMENT_NODE) {
      if (offset > 0 && node.childNodes[offset - 1]?.nodeType === Node.TEXT_NODE) {
        node = node.childNodes[offset - 1];
        offset = node.nodeValue.length;
      } else {
        closeMenu();
        return;
      }
    }
    if (node.nodeType !== Node.TEXT_NODE) {
      closeMenu();
      return;
    }

    const before = node.nodeValue.slice(0, offset);
    const m = /@([^\s@\n\u200B]*)$/.exec(before);
    if (!m) {
      closeMenu();
      return;
    }
    const items = getItemsFn();
    if (!items.length) {
      closeMenu();
      return;
    }
    const query = (m[1] || "").trim().toLowerCase();
    matches = query
      ? items.filter(
          (it) =>
            it.mentionToken.toLowerCase().includes(query) ||
            it.label.toLowerCase().includes(query) ||
            String(it.subtitle || "").toLowerCase().includes(query)
        )
      : items;
    if (!matches.length) {
      closeMenu();
      return;
    }
    mentionTarget = { textNode: node, start: offset - m[0].length, end: offset };
    activeIndex = 0;
    isOpen = true;
    renderMenu();
  }

  function normalizeEmptyDom() {
    if (editorEl.querySelector('[data-mention-chip="1"]')) return;
    const cleanText = (editorEl.textContent || "").replace(/\u200B/g, "");
    if (!cleanText && editorEl.children.length <= 1) {
      editorEl.innerHTML = "";
    }
  }

  function onCompositionStart() {
    isComposing = true;
  }

  function onCompositionEnd() {
    isComposing = false;
    normalizeEmptyDom();
    emitSync();
    evaluateTrigger();
  }

  function onInput() {
    if (!isComposing) normalizeEmptyDom();
    emitSync();
    evaluateTrigger();
  }

  function onPaste(e) {
    e.preventDefault();
    const raw = (e.clipboardData?.getData("text/plain") || "").replace(/\r\n?/g, "\n");
    if (!raw) return;
    const items = getItemsFn();
    const byTag = new Map(items.map((it) => [it.slotTag, it]));
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    range.deleteContents();

    const frag = document.createDocumentFragment();
    const lines = raw.split("\n");
    let lastInserted = null;
    lines.forEach((line, lIdx) => {
      if (lIdx > 0) {
        const br = document.createElement("br");
        frag.appendChild(br);
        lastInserted = br;
      }
      let pos = 0;
      PLAIN_MENTION_RE.lastIndex = 0;
      let m;
      while ((m = PLAIN_MENTION_RE.exec(line)) !== null) {
        if (m.index > pos) {
          const tn = document.createTextNode(line.slice(pos, m.index));
          frag.appendChild(tn);
          lastInserted = tn;
        }
        const matchedItem = byTag.get(m[1]);
        if (matchedItem) {
          const chip = buildMentionChipEl(matchedItem.id, matchedItem.slotTag, matchedItem);
          frag.appendChild(chip);
          lastInserted = chip;
        } else {
          const tn = document.createTextNode(m[0]);
          frag.appendChild(tn);
          lastInserted = tn;
        }
        pos = m.index + m[0].length;
      }
      if (pos < line.length) {
        const tn = document.createTextNode(line.slice(pos));
        frag.appendChild(tn);
        lastInserted = tn;
      }
    });

    range.insertNode(frag);
    if (lastInserted) {
      const nr = document.createRange();
      if (lastInserted.nodeType === Node.TEXT_NODE) {
        nr.setStart(lastInserted, lastInserted.nodeValue.length);
      } else {
        nr.setStartAfter(lastInserted);
      }
      nr.collapse(true);
      sel.removeAllRanges();
      sel.addRange(nr);
    }
    emitSync();
    closeMenu();
  }

  function onDblClick(e) {
    const chip = e.target.closest('[data-mention-chip="1"]');
    if (!chip) return;
    const item = getItemsFn().find((it) => it.id === chip.dataset.assetId);
    if (item?.url) {
      openLightbox(
        item.url,
        item.mediaType === "video" ? "video" : "image",
        `${item.slotTag}${item.label ? ` · ${item.label}` : ""}`
      );
    }
  }

  function onKeydown(e) {
    if (!isOpen) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = (activeIndex + 1) % matches.length;
      renderMenu();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = (activeIndex - 1 + matches.length) % matches.length;
      renderMenu();
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applySelection(matches[activeIndex]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeMenu();
    }
  }

  function onBlur() {
    setTimeout(closeMenu, 140);
  }

  function syncAssets(nextItems) {
    const items = nextItems || getItemsFn();
    const byId = new Map(items.map((it) => [it.id, it]));
    editorEl.querySelectorAll('[data-mention-chip="1"]').forEach((chip) => {
      const id = chip.dataset.assetId || "";
      const live = byId.get(id) || null;
      updateMentionChipEl(chip, live, chip.dataset.slotTag || "");
    });
    const serialized = serializeMentionEditor(editorEl, byId);
    if (onChange) onChange(serialized);
    if (isOpen) evaluateTrigger();
    return serialized;
  }

  editorEl.addEventListener("compositionstart", onCompositionStart);
  editorEl.addEventListener("compositionend", onCompositionEnd);
  editorEl.addEventListener("input", onInput);
  editorEl.addEventListener("paste", onPaste);
  editorEl.addEventListener("dblclick", onDblClick);
  editorEl.addEventListener("keydown", onKeydown);
  editorEl.addEventListener("blur", onBlur);

  const destroy = () => {
    editorEl.removeEventListener("compositionstart", onCompositionStart);
    editorEl.removeEventListener("compositionend", onCompositionEnd);
    editorEl.removeEventListener("input", onInput);
    editorEl.removeEventListener("paste", onPaste);
    editorEl.removeEventListener("dblclick", onDblClick);
    editorEl.removeEventListener("keydown", onKeydown);
    editorEl.removeEventListener("blur", onBlur);
    popover.remove();
  };

  state.mentionCleanups.push(destroy);
  return { syncAssets, closeMenu, destroy };
}

/* Lightbox & Interactive Whiteout Paint Canvas */
const paintState = {
  openToken: 0,
  isPaint: false,
  mode: "detail", // "builder" | "detail"
  shotId: "",
  slotTag: "",
  slotId: "",
  caption: "",
  origUrl: "",
  curUrl: "",
  width: 0,
  height: 0,
  viewScale: 1,
  zoomMode: "fit", // "fit" | "1" | "2"
  tool: "white", // "white" | "erase" | "pan"
  brushSize: 44,
  strokes: [],
  activeStroke: null,
  isDrawing: false,
  isPanning: false,
  panStart: null,
  spacePressed: false,
  comparingOriginal: false,
  wasResetToOriginal: false,
  underlayCanvas: null,
  initialCanvas: null,
  workCanvas: null,
  erasePattern: null,
};

function loadImageElement(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("图片加载失败"));
    img.src = url;
  });
}

function getBrushImageDiameter() {
  const maxDim = Math.max(paintState.width || 1000, paintState.height || 1000);
  return Math.max(4, Math.round((paintState.brushSize * maxDim) / 1000));
}

function applyStrokeStyle(ctx, stroke) {
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.lineWidth = stroke.diameter;
  if (stroke.tool === "erase" && paintState.erasePattern) {
    ctx.strokeStyle = paintState.erasePattern;
    ctx.fillStyle = paintState.erasePattern;
  } else {
    ctx.strokeStyle = "#FFFFFF";
    ctx.fillStyle = "#FFFFFF";
  }
  return ctx;
}

function drawStrokeOnWork(stroke) {
  if (!paintState.workCanvas || !stroke?.points?.length) return;
  const ctx = paintState.workCanvas.getContext("2d");
  applyStrokeStyle(ctx, stroke);
  const pts = stroke.points;
  if (pts.length === 1) {
    ctx.beginPath();
    ctx.arc(pts[0].x, pts[0].y, stroke.diameter / 2, 0, Math.PI * 2);
    ctx.fill();
  } else {
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i += 1) {
      ctx.lineTo(pts[i].x, pts[i].y);
    }
    ctx.stroke();
  }
  ctx.restore();
}

function syncPaintDisplayCanvas() {
  const displayCanvas = document.getElementById("paintDisplayCanvas");
  if (!displayCanvas || !paintState.workCanvas) return;
  const srcCanvas = paintState.comparingOriginal ? paintState.underlayCanvas : paintState.workCanvas;
  const dctx = displayCanvas.getContext("2d");
  dctx.clearRect(0, 0, displayCanvas.width, displayCanvas.height);
  dctx.drawImage(srcCanvas, 0, 0);
}

function rebuildPaintWorkCanvas() {
  if (!paintState.workCanvas || !paintState.initialCanvas) return;
  const wctx = paintState.workCanvas.getContext("2d");
  wctx.clearRect(0, 0, paintState.width, paintState.height);
  wctx.drawImage(paintState.initialCanvas, 0, 0);
  for (const s of paintState.strokes) {
    drawStrokeOnWork(s);
  }
  syncPaintDisplayCanvas();
  updatePaintToolbarUi();
}

function updatePaintViewportLayout() {
  if (!paintState.isPaint || !paintState.width || !paintState.height) return;
  const viewport = document.getElementById("paintViewport");
  const displayCanvas = document.getElementById("paintDisplayCanvas");
  if (!viewport || !displayCanvas) return;

  const availW = Math.max(viewport.clientWidth - 36, 280);
  const availH = Math.max(viewport.clientHeight - 36, 220);
  let scale = 1;
  if (paintState.zoomMode === "fit") {
    scale = Math.min(availW / paintState.width, availH / paintState.height, 2);
  } else {
    scale = Number(paintState.zoomMode) || 1;
  }
  paintState.viewScale = scale;
  const cssW = Math.max(1, Math.round(paintState.width * scale));
  const cssH = Math.max(1, Math.round(paintState.height * scale));
  displayCanvas.style.width = `${cssW}px`;
  displayCanvas.style.height = `${cssH}px`;

  viewport.style.justifyContent = cssW > availW ? "flex-start" : "center";
  viewport.style.alignItems = cssH > availH ? "flex-start" : "center";
}

function updatePaintToolbarUi() {
  document.querySelectorAll("[data-paint-tool]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.paintTool === paintState.tool);
  });
  document.querySelectorAll("[data-paint-zoom]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.paintZoom === String(paintState.zoomMode));
  });
  const undoBtn = document.getElementById("paintUndoBtn");
  if (undoBtn) undoBtn.disabled = paintState.strokes.length === 0;
  const sizeInput = document.getElementById("paintSizeRange");
  const sizeVal = document.getElementById("paintSizeVal");
  if (sizeInput) sizeInput.value = String(paintState.brushSize);
  if (sizeVal) sizeVal.textContent = String(paintState.brushSize);
  const stage = document.getElementById("paintStage");
  if (stage) {
    const panningMode = paintState.tool === "pan" || paintState.spacePressed;
    stage.style.cursor = panningMode ? (paintState.isPanning ? "grabbing" : "grab") : "crosshair";
  }
}

function updatePaintCursorRing(clientX, clientY, visible = true) {
  const ring = document.getElementById("paintCursorRing");
  if (!ring) return;
  if (!visible || !paintState.isPaint || paintState.tool === "pan" || paintState.spacePressed) {
    ring.classList.add("hidden");
    return;
  }
  const diaCss = Math.max(6, Math.round(getBrushImageDiameter() * paintState.viewScale));
  ring.style.left = `${clientX}px`;
  ring.style.top = `${clientY}px`;
  ring.style.width = `${diaCss}px`;
  ring.style.height = `${diaCss}px`;
  ring.style.borderColor = paintState.tool === "erase" ? "#34c759" : "#ffffff";
  ring.classList.remove("hidden");
}

function getImageCoordsFromClient(clientX, clientY) {
  const displayCanvas = document.getElementById("paintDisplayCanvas");
  const rect = displayCanvas.getBoundingClientRect();
  return {
    x: ((clientX - rect.left) / Math.max(rect.width, 1)) * paintState.width,
    y: ((clientY - rect.top) / Math.max(rect.height, 1)) * paintState.height,
  };
}

async function openImagePaintModal({ url, origUrl, caption, mode, slotTag, slotId, shotId }) {
  if (!url) return;
  const modal = document.getElementById("lightboxModal");
  const inner = document.getElementById("lightboxInner");
  const toolbar = document.getElementById("paintToolbar");
  const viewport = document.getElementById("paintViewport");
  const imgEl = document.getElementById("lightboxImg");
  const vidEl = document.getElementById("lightboxVideo");
  const capEl = document.getElementById("lightboxCaption");
  const closeImgBtn = document.getElementById("closeLightboxBtn");
  const saveLocalBtn = document.getElementById("paintSaveLocalBtn");
  const savePrimaryBtn = document.getElementById("paintSavePrimaryBtn");

  vidEl.pause();
  vidEl.removeAttribute("src");
  imgEl.removeAttribute("src");
  vidEl.hidden = true;
  imgEl.hidden = true;
  closeImgBtn.classList.add("hidden");
  capEl.classList.add("hidden");

  inner.classList.add("is-paint");
  toolbar.classList.remove("hidden");
  viewport.classList.remove("hidden");

  const token = ++paintState.openToken;
  paintState.isPaint = true;
  paintState.mode = mode || "detail";
  paintState.shotId = shotId || "";
  paintState.slotTag = slotTag || "";
  paintState.slotId = slotId || "";
  paintState.caption = caption || "";
  paintState.curUrl = url;
  paintState.origUrl = origUrl || url;
  paintState.tool = "white";
  paintState.zoomMode = "fit";
  paintState.strokes = [];
  paintState.activeStroke = null;
  paintState.isDrawing = false;
  paintState.isPanning = false;
  paintState.comparingOriginal = false;
  paintState.wasResetToOriginal = false;

  document.getElementById("paintCaption").textContent = caption || "涂白画板";
  document.getElementById("paintDims").textContent = "加载中…";

  if (paintState.mode === "detail") {
    saveLocalBtn.classList.remove("hidden");
    savePrimaryBtn.textContent = "保存并同步画布";
  } else {
    saveLocalBtn.classList.add("hidden");
    savePrimaryBtn.textContent = "应用涂白";
  }
  updatePaintToolbarUi();
  modal.classList.remove("hidden");

  try {
    const [curImg, origImg] = await Promise.all([
      loadImageElement(url),
      loadImageElement(origUrl || url).catch(() => null),
    ]);
    if (token !== paintState.openToken || !paintState.isPaint) return;

    const w = curImg.naturalWidth || curImg.width;
    const h = curImg.naturalHeight || curImg.height;
    paintState.width = w;
    paintState.height = h;
    document.getElementById("paintDims").textContent = `${w} × ${h}`;

    const mkCanvas = () => {
      const c = document.createElement("canvas");
      c.width = w;
      c.height = h;
      return c;
    };
    const underlay = mkCanvas();
    const uctx = underlay.getContext("2d");
    if (origImg && origImg.naturalWidth) {
      uctx.drawImage(origImg, 0, 0, w, h);
    } else {
      uctx.drawImage(curImg, 0, 0, w, h);
    }

    const initial = mkCanvas();
    initial.getContext("2d").drawImage(curImg, 0, 0, w, h);

    const work = mkCanvas();
    work.getContext("2d").drawImage(initial, 0, 0);

    const displayCanvas = document.getElementById("paintDisplayCanvas");
    displayCanvas.width = w;
    displayCanvas.height = h;

    paintState.underlayCanvas = underlay;
    paintState.initialCanvas = initial;
    paintState.workCanvas = work;
    paintState.erasePattern = work.getContext("2d").createPattern(underlay, "no-repeat");

    syncPaintDisplayCanvas();
    requestAnimationFrame(() => {
      if (token === paintState.openToken && paintState.isPaint) {
        updatePaintViewportLayout();
        updatePaintToolbarUi();
      }
    });
  } catch (err) {
    if (token === paintState.openToken) {
      showToast(`打开画板失败: ${err.message}`);
      closeLightbox();
    }
  }
}

async function savePaintedMask(syncToCanvas) {
  if (!paintState.isPaint || !paintState.workCanvas) return;
  const isReset = paintState.wasResetToOriginal && paintState.strokes.length === 0;

  const doSave = async () => {
    const saveLocalBtn = document.getElementById("paintSaveLocalBtn");
    const savePrimaryBtn = document.getElementById("paintSavePrimaryBtn");
    saveLocalBtn.disabled = true;
    savePrimaryBtn.disabled = true;
    const prevLabel = savePrimaryBtn.textContent;
    savePrimaryBtn.textContent = "保存中…";

    try {
      const blob = await new Promise((resolve) =>
        paintState.workCanvas.toBlob((b) => resolve(b), "image/png")
      );
      if (!blob) throw new Error("画布导出 PNG 失败");

      if (paintState.mode === "builder") {
        const item = state.builder.imageItems.find((x) => x.id === paintState.slotId);
        if (!item) throw new Error("找不到要更新的素材图片");
        if (!item.originalUrl) item.originalUrl = item.url;
        const presetId = getBuilderItemPresetId(item);
        if (presetId && !item.originalPresetId) {
          item.originalPresetKind = "preset";
          item.originalPresetId = presetId;
        }
        if (isReset) {
          if (item.url?.startsWith("blob:") && item.url !== item.originalUrl) {
            URL.revokeObjectURL(item.url);
          }
          item.url = item.originalUrl;
          if (item.originalFile) item.file = item.originalFile;
          if (item.originalPresetKind === "preset" || item.originalPresetId) {
            item.kind = "preset";
            item.presetId = item.originalPresetId;
          }
          item.whiteoutApplied = false;
          showToast("已还原为原图");
        } else {
          if (!item.originalFile && item.file) item.originalFile = item.file;
          const baseName = String(item.label || "image").replace(/\.[^.]+$/, "");
          const maskFile = new File([blob], `${baseName}_mask.png`, { type: "image/png" });
          if (item.url?.startsWith("blob:") && item.url !== item.originalUrl) {
            URL.revokeObjectURL(item.url);
          }
          item.file = maskFile;
          item.url = URL.createObjectURL(maskFile);
          item.kind = "upload";
          item.presetId = "";
          item.whiteoutApplied = true;
          preflightFileInBackground(maskFile, (meta) => {
            item.meta = meta;
            renderBuilderAssetStripOnly();
          });
          showToast("已应用手涂涂白");
        }
        closeLightbox();
        renderBuilderView();
        return;
      }

      const fd = new FormData();
      fd.append("slot_tag", paintState.slotTag || "");
      fd.append("slot_id", paintState.slotId || "");
      fd.append("sync_to_canvas", syncToCanvas ? "true" : "false");
      fd.append("is_reset", isReset ? "true" : "false");
      fd.append("file", blob, `${paintState.slotTag || "mask"}.png`);

      await apiJson(`/api/shots/${encodeURIComponent(paintState.shotId)}/mask-image`, {
        method: "POST",
        body: fd,
      });
      const shotRef = (state.data?.shots || []).find((s) => s.id === paintState.shotId);
      const platLabel = getPlatformLabel(shotRef?.platform);
      showToast(
        isReset
          ? "已还原原图"
          : syncToCanvas
          ? `已保存涂白图，并已通知 Agent 同步 ${platLabel} 画布`
          : "已保存涂白图"
      );
      closeLightbox();
      await refreshState();
    } catch (err) {
      showToast(`保存失败: ${err.message}`);
    } finally {
      saveLocalBtn.disabled = false;
      savePrimaryBtn.disabled = false;
      savePrimaryBtn.textContent = prevLabel;
    }
  };

  if (paintState.mode !== "builder") {
    const shotRef = (state.data?.shots || []).find((s) => s.id === paintState.shotId);
    if (isPassedFinal(shotRef)) {
      const actionName = isReset ? "还原原图" : syncToCanvas ? "保存涂白并同步画布" : "保存涂白修改";
      const consequenceText = syncToCanvas
        ? "确认保存涂白图后将解锁本镜并通知 Agent 替换画布对应参考图。"
        : "确认保存涂白图后将解锁本镜素材状态，便于后续继续重新生成。";
      guardPassedFinalAction(shotRef, {
        actionName,
        consequenceText,
        okText: `确认解锁并${actionName}`,
        onProceed: doSave,
      });
      return;
    }
  }
  await doSave();
}

function bindPaintModalEvents() {
  document.querySelectorAll("[data-paint-tool]").forEach((btn) => {
    btn.addEventListener("click", () => {
      paintState.tool = btn.dataset.paintTool || "white";
      updatePaintToolbarUi();
    });
  });

  document.querySelectorAll("[data-paint-zoom]").forEach((btn) => {
    btn.addEventListener("click", () => {
      paintState.zoomMode = btn.dataset.paintZoom || "fit";
      updatePaintViewportLayout();
      updatePaintToolbarUi();
    });
  });

  const sizeRange = document.getElementById("paintSizeRange");
  sizeRange?.addEventListener("input", () => {
    paintState.brushSize = Number(sizeRange.value) || 44;
    updatePaintToolbarUi();
  });

  document.getElementById("paintUndoBtn")?.addEventListener("click", () => {
    if (!paintState.strokes.length) return;
    paintState.strokes.pop();
    rebuildPaintWorkCanvas();
  });

  const compareBtn = document.getElementById("paintCompareBtn");
  const startCompare = () => {
    if (!paintState.isPaint) return;
    paintState.comparingOriginal = true;
    syncPaintDisplayCanvas();
  };
  const endCompare = () => {
    if (!paintState.comparingOriginal) return;
    paintState.comparingOriginal = false;
    syncPaintDisplayCanvas();
  };
  compareBtn?.addEventListener("pointerdown", startCompare);
  compareBtn?.addEventListener("pointerup", endCompare);
  compareBtn?.addEventListener("pointerleave", endCompare);
  compareBtn?.addEventListener("pointercancel", endCompare);

  document.getElementById("paintResetBtn")?.addEventListener("click", () => {
    if (!paintState.isPaint || !paintState.underlayCanvas || !paintState.initialCanvas) return;
    paintState.initialCanvas.getContext("2d").drawImage(paintState.underlayCanvas, 0, 0);
    paintState.strokes = [];
    paintState.wasResetToOriginal = true;
    rebuildPaintWorkCanvas();
    showToast("画板已还原为原图（点保存后生效）");
  });

  document.getElementById("paintSaveLocalBtn")?.addEventListener("click", () => savePaintedMask(false));
  document.getElementById("paintSavePrimaryBtn")?.addEventListener("click", () =>
    savePaintedMask(paintState.mode === "detail")
  );
  document.getElementById("closePaintBtn")?.addEventListener("click", closeLightbox);

  const stage = document.getElementById("paintStage");
  const viewport = document.getElementById("paintViewport");

  stage?.addEventListener("pointerdown", (e) => {
    if (!paintState.isPaint || !paintState.workCanvas) return;
    e.preventDefault();
    try {
      stage.setPointerCapture(e.pointerId);
    } catch {
      // Ignore capture errors
    }

    if (paintState.tool === "pan" || paintState.spacePressed || e.button === 1 || e.altKey) {
      paintState.isPanning = true;
      paintState.panStart = {
        x: e.clientX,
        y: e.clientY,
        scrollLeft: viewport.scrollLeft,
        scrollTop: viewport.scrollTop,
      };
      updatePaintToolbarUi();
      return;
    }

    if (e.button !== 0) return;
    const pt = getImageCoordsFromClient(e.clientX, e.clientY);
    const stroke = {
      tool: paintState.tool,
      diameter: getBrushImageDiameter(),
      points: [pt],
    };
    paintState.isDrawing = true;
    paintState.activeStroke = stroke;

    const wctx = paintState.workCanvas.getContext("2d");
    applyStrokeStyle(wctx, stroke);
    wctx.beginPath();
    wctx.arc(pt.x, pt.y, stroke.diameter / 2, 0, Math.PI * 2);
    wctx.fill();
    wctx.restore();
    syncPaintDisplayCanvas();
    updatePaintCursorRing(e.clientX, e.clientY, true);
  });

  stage?.addEventListener("pointermove", (e) => {
    if (!paintState.isPaint) return;
    updatePaintCursorRing(e.clientX, e.clientY, true);

    if (paintState.isPanning && paintState.panStart) {
      viewport.scrollLeft = paintState.panStart.scrollLeft - (e.clientX - paintState.panStart.x);
      viewport.scrollTop = paintState.panStart.scrollTop - (e.clientY - paintState.panStart.y);
      return;
    }
    if (!paintState.isDrawing || !paintState.activeStroke || !paintState.workCanvas) return;

    const pt = getImageCoordsFromClient(e.clientX, e.clientY);
    const pts = paintState.activeStroke.points;
    const prev = pts[pts.length - 1];
    pts.push(pt);

    const wctx = paintState.workCanvas.getContext("2d");
    applyStrokeStyle(wctx, paintState.activeStroke);
    wctx.beginPath();
    wctx.moveTo(prev.x, prev.y);
    wctx.lineTo(pt.x, pt.y);
    wctx.stroke();
    wctx.restore();
    syncPaintDisplayCanvas();
  });

  const finishStroke = () => {
    if (paintState.isPanning) {
      paintState.isPanning = false;
      paintState.panStart = null;
      updatePaintToolbarUi();
    }
    if (paintState.isDrawing && paintState.activeStroke) {
      paintState.strokes.push(paintState.activeStroke);
      paintState.wasResetToOriginal = false;
      paintState.activeStroke = null;
      paintState.isDrawing = false;
      updatePaintToolbarUi();
    }
  };

  stage?.addEventListener("pointerup", finishStroke);
  stage?.addEventListener("pointercancel", finishStroke);
  stage?.addEventListener("pointerleave", () => updatePaintCursorRing(0, 0, false));
  window.addEventListener("resize", () => {
    if (paintState.isPaint) updatePaintViewportLayout();
  });
}

function openLightbox(url, kind, caption) {
  if (!url) return;
  const modal = document.getElementById("lightboxModal");
  const inner = document.getElementById("lightboxInner");
  const toolbar = document.getElementById("paintToolbar");
  const viewport = document.getElementById("paintViewport");
  const imgEl = document.getElementById("lightboxImg");
  const vidEl = document.getElementById("lightboxVideo");
  const capEl = document.getElementById("lightboxCaption");
  const closeImgBtn = document.getElementById("closeLightboxBtn");

  paintState.openToken += 1;
  paintState.isPaint = false;
  inner.classList.remove("is-paint");
  toolbar.classList.add("hidden");
  viewport.classList.add("hidden");
  closeImgBtn.classList.remove("hidden");
  capEl.classList.remove("hidden");

  vidEl.pause();
  vidEl.removeAttribute("src");
  imgEl.removeAttribute("src");

  if (kind === "video") {
    imgEl.hidden = true;
    vidEl.hidden = false;
    vidEl.src = url;
    vidEl.play().catch(() => {});
  } else {
    vidEl.hidden = true;
    imgEl.hidden = false;
    imgEl.src = url;
  }
  capEl.textContent = caption || "";
  modal.classList.remove("hidden");
}

function closeLightbox() {
  const modal = document.getElementById("lightboxModal");
  const inner = document.getElementById("lightboxInner");
  const toolbar = document.getElementById("paintToolbar");
  const viewport = document.getElementById("paintViewport");
  const imgEl = document.getElementById("lightboxImg");
  const vidEl = document.getElementById("lightboxVideo");
  const ring = document.getElementById("paintCursorRing");

  paintState.openToken += 1;
  paintState.isPaint = false;
  paintState.isDrawing = false;
  paintState.isPanning = false;
  paintState.activeStroke = null;
  paintState.strokes = [];
  paintState.comparingOriginal = false;
  paintState.spacePressed = false;
  paintState.underlayCanvas = null;
  paintState.initialCanvas = null;
  paintState.workCanvas = null;
  paintState.erasePattern = null;

  inner.classList.remove("is-paint");
  toolbar.classList.add("hidden");
  viewport.classList.add("hidden");
  ring?.classList.add("hidden");
  document.getElementById("closeLightboxBtn")?.classList.remove("hidden");
  document.getElementById("lightboxCaption")?.classList.remove("hidden");

  vidEl.pause();
  vidEl.removeAttribute("src");
  imgEl.removeAttribute("src");
  modal.classList.add("hidden");
}

/* Topbar & Sidebar */
function renderSceneDatalist() {
  const dl = document.getElementById("sceneDatalist");
  if (!dl || !state.data) return;
  dl.innerHTML = getAllScenes(state.data)
    .map((sc) => `<option value="${escapeHtml(sc)}"></option>`)
    .join("");
}

function getShotFolderName(shot) {
  return (shot?.scene || "").trim() || "未分场";
}

function groupShotsByScene(shots, customScenes = []) {
  const groups = new Map();
  for (const rawSc of customScenes || []) {
    const sc = String(rawSc || "").trim();
    if (sc && sc !== "未分场" && !groups.has(sc)) {
      groups.set(sc, []);
    }
  }
  for (const shot of shots) {
    const folder = getShotFolderName(shot);
    if (!groups.has(folder)) groups.set(folder, []);
    groups.get(folder).push(shot);
  }
  const names = Array.from(groups.keys()).sort((a, b) => {
    if (a === "未分场" && b !== "未分场") return 1;
    if (b === "未分场" && a !== "未分场") return -1;
    return a.localeCompare(b, "zh-Hans-CN", { numeric: true });
  });
  return names.map((name) => ({ name, shots: groups.get(name) }));
}

function syncBuilderSceneSelection(sceneFolder) {
  if (state.builder.sceneTouched) return;
  const normalized = sceneFolder && sceneFolder !== "未分场" ? sceneFolder.trim() : "";
  state.builder.scene = normalized;
  state.selectedSceneTab = normalized || "通用";
}

function normalizePlatform(raw) {
  const clean = String(raw || "").trim().toLowerCase();
  if (clean === "jimeng" || clean === "dreamina" || clean === "即梦" || clean === "即梦官网") {
    return "jimeng";
  }
  return "libtv";
}

function getPlatformLabel(platform, short = false) {
  return normalizePlatform(platform) === "jimeng" ? (short ? "即梦" : "即梦官网") : "LibTV";
}

function buildFallbackCanvasUrl(uuid, platform) {
  const plat = normalizePlatform(platform);
  const uid = String(uuid || "").trim();
  if (plat === "jimeng") {
    return uid ? `https://jimeng.jianying.com/ai-tool/ai-canvas/${uid}` : "https://jimeng.jianying.com/ai-tool/home";
  }
  return uid ? `https://www.liblib.tv/canvas?projectId=${uid}` : "https://www.liblib.tv/";
}

function getActiveCanvasContext() {
  const d = state.data;
  if (!d) return { platform: "libtv", uuid: "", name: "未选择画布", url: "", shot: null };
  const shot = state.view === "detail" ? (d.shots || []).find((s) => s.id === state.selectedShotId) : null;
  if (shot) {
    const plat = normalizePlatform(shot.platform || d.defaultPlatform);
    const uid = String(shot.projectUuid || "").trim();
    return {
      platform: plat,
      uuid: uid,
      name: shot.projectName || uid || (plat === "jimeng" ? "未选择即梦画布" : "未选择 LibTV 画布"),
      url: shot.canvasUrl || buildFallbackCanvasUrl(uid, plat),
      shot,
    };
  }
  const plat = normalizePlatform(d.defaultPlatform);
  const pdef = d.platformDefaults?.[plat] || {};
  const uid = String(pdef.projectUuid ?? d.defaultProjectUuid ?? "").trim();
  const fallbackName = plat === "jimeng" ? "未选择即梦画布" : "未选择 LibTV 画布";
  return {
    platform: plat,
    uuid: uid,
    name: pdef.projectName || d.defaultProjectName || uid || fallbackName,
    url: pdef.canvasUrl || buildFallbackCanvasUrl(uid, plat),
    shot: null,
  };
}

function getAllCanvasOptions(platformOverride = null) {
  const d = state.data;
  if (!d) return [];
  const active = getActiveCanvasContext();
  const plat = normalizePlatform(platformOverride || active.platform);
  const byUuid = new Map();
  const listSource = d.projectsByPlatform?.[plat] || (plat === normalizePlatform(d.defaultPlatform) ? d.projects : []) || [];
  for (const p of listSource) {
    if (!p?.uuid) continue;
    byUuid.set(p.uuid, {
      platform: plat,
      uuid: p.uuid,
      name: p.name || p.uuid,
      rawName: p.rawName || p.name || "",
      workspaceName: p.workspaceName || "",
      workspaceId: p.workspaceId ?? null,
      url: p.url || buildFallbackCanvasUrl(p.uuid, plat),
    });
  }
  for (const s of d.shots || []) {
    if (normalizePlatform(s.platform) !== plat) continue;
    const uid = (s.projectUuid || "").trim();
    if (!uid || byUuid.has(uid)) continue;
    byUuid.set(uid, {
      platform: plat,
      uuid: uid,
      name: s.projectName || uid,
      rawName: s.projectName || "",
      workspaceName: "",
      workspaceId: null,
      url: s.canvasUrl || buildFallbackCanvasUrl(uid, plat),
    });
  }
  const pdef = d.platformDefaults?.[plat] || {};
  const defUuid = String(pdef.projectUuid || (plat === normalizePlatform(d.defaultPlatform) ? d.defaultProjectUuid : "") || "").trim();
  if (defUuid && !byUuid.has(defUuid)) {
    byUuid.set(defUuid, {
      platform: plat,
      uuid: defUuid,
      name: pdef.projectName || defUuid,
      rawName: pdef.projectName || "",
      workspaceName: "",
      workspaceId: pdef.workspaceId || null,
      url: pdef.canvasUrl || buildFallbackCanvasUrl(defUuid, plat),
    });
  }
  const list = Array.from(byUuid.values());
  list.sort((a, b) => {
    if (a.uuid === active.uuid && b.uuid !== active.uuid) return -1;
    if (b.uuid === active.uuid && a.uuid !== active.uuid) return 1;
    if (a.uuid === defUuid && b.uuid !== defUuid) return -1;
    if (b.uuid === defUuid && a.uuid !== defUuid) return 1;
    return 0;
  });
  return list;
}

function stopPlatformAuthPoll() {
  if (state.platformAuthPollTimer) {
    clearInterval(state.platformAuthPollTimer);
    state.platformAuthPollTimer = null;
  }
}

function startPlatformAuthPoll(platOverride) {
  const plat = normalizePlatform(platOverride);
  const statusUrl = plat === "jimeng" ? "/api/jimeng/auth/status" : "/api/libtv/auth/status";
  const statusKey = plat === "jimeng" ? "jimengStatus" : "libtvStatus";
  const platLabel = getPlatformLabel(plat);
  stopPlatformAuthPoll();
  state.platformAuthPollTimer = setInterval(async () => {
    try {
      const res = await apiJson(statusUrl);
      const authObj = res[statusKey];
      if (state.data && authObj) {
        state.data[statusKey] = authObj;
        if (Array.isArray(res.projects) && res.projects.length) {
          state.data.projectsByPlatform = state.data.projectsByPlatform || {};
          state.data.projectsByPlatform[plat] = res.projects;
          if (normalizePlatform(state.data.defaultPlatform) === plat) {
            state.data.projects = res.projects;
          }
        }
      }
      if (authObj?.authenticated) {
        stopPlatformAuthPoll();
        showToast(`已成功连接 ${platLabel} 账号`);
        await refreshState({ preserveEditing: true });
        if (state.canvasPicker.open) renderCanvasPopover();
      } else if (!authObj?.loginPending) {
        stopPlatformAuthPoll();
        if (state.canvasPicker.open) renderCanvasPopover();
      }
    } catch {
      // ignore transient poll error
    }
  }, 2500);
}

function closeCanvasPopover() {
  stopPlatformAuthPoll();
  if (!state.canvasPicker.open) return;
  state.canvasPicker.open = false;
  state.canvasPicker.creating = false;
  const pop = document.getElementById("canvasPopover");
  const btn = document.getElementById("canvasPickerBtn");
  pop?.classList.add("hidden");
  btn?.classList.remove("is-open");
  btn?.setAttribute("aria-expanded", "false");
}

async function applyPlatformSwitch(targetPlatform, { openPopover = false } = {}) {
  const plat = normalizePlatform(targetPlatform);
  const active = getActiveCanvasContext();
  if (active.shot) {
    const picked = (state.data?.projectsByPlatform?.[plat] || []).find(Boolean);
    const pdef = state.data?.platformDefaults?.[plat] || {};
    const targetUuid = pdef.projectUuid || picked?.uuid || "";
    const targetName =
      pdef.projectName || picked?.name || (plat === "jimeng" ? "未选择即梦画布" : "未选择 LibTV 画布");
    const targetUrl = pdef.canvasUrl || picked?.url || buildFallbackCanvasUrl(targetUuid, plat);
    await apiJson(`/api/shots/${encodeURIComponent(active.shot.id)}/project`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform: plat,
        projectUuid: targetUuid,
        projectName: targetName,
        canvasUrl: targetUrl,
        workspaceId: pdef.workspaceId ?? "",
        setAsDefault: true,
      }),
    });
  }
  const cfg = await apiJson("/api/config/platform", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ platform: plat }),
  });
  await refreshState({ preserveEditing: true });
  const platLabel = getPlatformLabel(plat);
  showToast(`已切换目标平台为「${platLabel}」`);
  const platAuth = plat === "jimeng" ? cfg.jimengStatus : cfg.libtvStatus;
  if (
    openPopover ||
    (platAuth && !platAuth.authenticated) ||
    !getActiveCanvasContext().uuid
  ) {
    state.canvasPicker.open = true;
    state.canvasPicker.creating = false;
    state.canvasPicker.query = "";
    renderCanvasPopover();
    if (platAuth?.loginPending) startPlatformAuthPoll(plat);
  } else if (state.canvasPicker.open) {
    renderCanvasPopover();
  }
}

async function switchPlatform(targetPlatform, opts = {}) {
  const nextPlat = normalizePlatform(targetPlatform);
  const active = getActiveCanvasContext();
  if (active.platform === nextPlat && !opts.force) {
    if (opts.openPopover) {
      state.canvasPicker.open = true;
      renderCanvasPopover();
    }
    return;
  }
  if (active.shot && (active.shot.libtvSampleNodeKey || active.shot.draftVideo || active.shot.finalVideo)) {
    const curLabel = getPlatformLabel(active.platform);
    const nextLabel = getPlatformLabel(nextPlat);
    openConfirmModal({
      title: `将「${active.shot.id}」切换到${nextLabel}？`,
      message: `当前镜头已在 ${curLabel} 上关联画布或生成视频，是否将本镜头的目标平台改设为${nextLabel}？\n\n若只想为新镜头使用${nextLabel}而不影响「${active.shot.id}」，请点取消后点击左侧「+ 新建镜头」再切换平台。`,
      okText: `切为${nextLabel}`,
      cancelText: "取消",
      onConfirm: () => applyPlatformSwitch(nextPlat, opts),
    });
    return;
  }
  try {
    await applyPlatformSwitch(nextPlat, opts);
  } catch (err) {
    showToast(err.message);
  }
}

function renderCanvasPopover() {
  const pop = document.getElementById("canvasPopover");
  const btn = document.getElementById("canvasPickerBtn");
  if (!pop || !btn) return;
  if (!state.canvasPicker.open) {
    pop.classList.add("hidden");
    btn.classList.remove("is-open");
    btn.setAttribute("aria-expanded", "false");
    return;
  }

  pop.classList.remove("hidden");
  btn.classList.add("is-open");
  btn.setAttribute("aria-expanded", "true");

  const active = getActiveCanvasContext();
  const activePlat = active.platform;
  const platLabel = getPlatformLabel(activePlat);
  const jimengSt = state.data?.jimengStatus || {};
  const libtvSt = state.data?.libtvStatus || { installed: true, authenticated: true };
  const authSt = activePlat === "jimeng" ? jimengSt : libtvSt;
  const needAuth = activePlat === "jimeng" ? !jimengSt.authenticated : Boolean(state.data?.libtvStatus && !libtvSt.authenticated);

  const tabsHtml = `
    <div class="canvas-popover-tabs" role="tablist" aria-label="目标平台">
      <button type="button" class="canvas-popover-tab ${activePlat === "libtv" ? "active" : ""}" data-popover-platform="libtv">LibTV 画布</button>
      <button type="button" class="canvas-popover-tab ${activePlat === "jimeng" ? "active" : ""}" data-popover-platform="jimeng">即梦官网画布</button>
    </div>
  `;

  if (needAuth) {
    if (authSt.loginPending) startPlatformAuthPoll(activePlat);
    const loginUrl = activePlat === "jimeng" ? "/api/jimeng/auth/login" : "/api/libtv/auth/login";
    const statusUrl = activePlat === "jimeng" ? "/api/jimeng/auth/status" : "/api/libtv/auth/status";
    const statusKey = activePlat === "jimeng" ? "jimengStatus" : "libtvStatus";
    const cliName = activePlat === "jimeng" ? "dreamina-canvas" : "libtv";
    const codeHtml = authSt.userCode
      ? `<div class="jimeng-auth-code"><span>网页授权验证码</span><strong>${escapeHtml(authSt.userCode)}</strong></div>`
      : "";
    const verifyLinkHtml = authSt.verificationUrl
      ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(authSt.verificationUrl)}" target="_blank" rel="noopener">打开授权页 ↗</a>`
      : "";
    const acctHint = authSt.installed === false
      ? `本机尚未检测到 ${cliName} CLI，请先双击安装脚本完成环境配置。`
      : authSt.detail || `连接你的${platLabel}账号后，即可读取与新建画布。`;

    pop.innerHTML = `
      ${tabsHtml}
      <div class="jimeng-auth-box">
        <div class="jimeng-auth-head">
          <span>${escapeHtml(platLabel)} 授权</span>
          <span class="pill ${authSt.loginPending ? "pill-blue" : "pill-orange"}">${authSt.loginPending ? "等待网页确认" : "未登录"}</span>
        </div>
        <div class="jimeng-auth-desc">${escapeHtml(acctHint)}</div>
        ${codeHtml}
        <div class="jimeng-auth-actions">
          <button id="platformAuthLoginBtn" class="btn btn-primary btn-sm" type="button" ${state.canvasPicker.authStarting || authSt.installed === false ? "disabled" : ""}>
            ${state.canvasPicker.authStarting ? "获取授权链接…" : authSt.loginPending ? "重新获取链接" : `登录授权${escapeHtml(platLabel)}`}
          </button>
          ${verifyLinkHtml}
          <button id="platformAuthRefreshBtn" class="btn btn-secondary btn-sm" type="button">↻ 检查状态</button>
        </div>
      </div>
    `;

    pop.querySelectorAll("[data-popover-platform]").forEach((tBtn) => {
      tBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        switchPlatform(tBtn.dataset.popoverPlatform, { openPopover: true });
      });
    });

    document.getElementById("platformAuthLoginBtn")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      state.canvasPicker.authStarting = true;
      renderCanvasPopover();
      try {
        const res = await apiJson(loginUrl, { method: "POST" });
        if (state.data && res[statusKey]) {
          state.data[statusKey] = res[statusKey];
        }
        if (res.authenticated) {
          stopPlatformAuthPoll();
          showToast(`已连接 ${platLabel} 账号`);
          await refreshState({ preserveEditing: true });
        } else if (res.verificationUrl) {
          window.open(res.verificationUrl, "_blank", "noopener");
          showToast(`已打开 ${platLabel} 授权页，请在浏览器确认登录`);
          startPlatformAuthPoll(activePlat);
        }
      } catch (err) {
        showToast(err.message);
      } finally {
        state.canvasPicker.authStarting = false;
        renderCanvasPopover();
      }
    });

    document.getElementById("platformAuthRefreshBtn")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        const res = await apiJson(statusUrl);
        const stObj = res[statusKey];
        if (state.data && stObj) {
          state.data[statusKey] = stObj;
          if (Array.isArray(res.projects)) {
            state.data.projectsByPlatform = state.data.projectsByPlatform || {};
            state.data.projectsByPlatform[activePlat] = res.projects;
          }
        }
        if (stObj?.authenticated) {
          stopPlatformAuthPoll();
          showToast(`${platLabel} 已授权连接`);
          await refreshState({ preserveEditing: true });
        } else {
          showToast(stObj?.detail || `尚未检测到 ${platLabel} 登录态`);
          renderCanvasPopover();
        }
      } catch (err) {
        showToast(err.message);
      }
    });
    return;
  }

  const allOptions = getAllCanvasOptions(activePlat);
  const q = (state.canvasPicker.query || "").trim().toLowerCase();
  const filtered = q
    ? allOptions.filter(
        (p) =>
          (p.name || "").toLowerCase().includes(q) ||
          (p.workspaceName || "").toLowerCase().includes(q) ||
          (p.uuid || "").toLowerCase().includes(q)
      )
    : allOptions;

  const listHtml = filtered.length
    ? filtered
        .map((p) => {
          const isCurrent = p.uuid === active.uuid;
          const subText = [
            p.workspaceName && p.workspaceName !== p.rawName ? `空间：${p.workspaceName}` : "",
            p.uuid.slice(0, 10) + "…",
          ]
            .filter(Boolean)
            .join(" · ");
          return `
            <div class="canvas-option-row">
              <button type="button" class="canvas-option-btn ${isCurrent ? "active" : ""}" data-pick-canvas="${escapeHtml(p.uuid)}">
                <span class="canvas-option-check">${isCurrent ? "✓" : ""}</span>
                <span class="canvas-option-body">
                  <span class="canvas-option-title">${escapeHtml(p.name)}</span>
                  <span class="canvas-option-sub">${escapeHtml(subText)}</span>
                </span>
              </button>
              <a class="canvas-option-ext" href="${escapeHtml(p.url)}" target="_blank" rel="noopener" title="在浏览器打开该 ${escapeHtml(platLabel)} 画布">↗</a>
            </div>
          `;
        })
        .join("")
    : `<div class="canvas-popover-empty">未找到匹配的 ${escapeHtml(platLabel)} 画布</div>`;

  const curWsId = state.data?.platformDefaults?.libtv?.workspaceId || state.data?.defaultWorkspaceId || "";
  const curWsLabel = state.data?.platformDefaults?.libtv?.projectName || state.data?.defaultProjectName || "当前工作区";
  const curCeiling = Number(state.data?.jimengCreditCeiling ?? 0) || 0;
  const curAutoQa = Boolean(state.data?.autoQaAfterRender);
  const jimengAccountBarHtml =
    activePlat === "jimeng" && jimengSt.authenticated
      ? `
        <div class="jimeng-account-bar">
          <div class="jimeng-account-top">
            <span>已连接：${escapeHtml(jimengSt.accountName || "即梦账号")}${jimengSt.teamName ? ` · ${escapeHtml(jimengSt.teamName)}` : ""}</span>
            <span>${jimengSt.credits != null ? `积分 ${escapeHtml(String(jimengSt.credits))}` : ""}</span>
          </div>
          <div class="jimeng-ceiling-row" title="设置单镜积分自动确认上限：报价不超过上限时自动执行 node confirm/run，超限或设为 0 时停在待确认询问">
            <span class="jimeng-ceiling-label">单镜自动确认上限</span>
            <div class="jimeng-ceiling-ctrl">
              <input id="jimengCreditCeilingInput" type="number" min="0" max="100000" step="50" value="${curCeiling}" />
              <span class="jimeng-ceiling-unit">积分</span>
              <button id="saveJimengCeilingBtn" class="btn btn-secondary btn-sm" type="button">保存</button>
            </div>
          </div>
          <label class="jimeng-autoqa-row" title="关闭（默认极速）：480P/1080P 由后台 Python 直接下载并同步就绪，省去 AI 看片耗时与费用；开启：下载后再唤起轻量 AI 自检写评语">
            <input id="autoQaAfterRenderToggle" type="checkbox" ${curAutoQa ? "checked" : ""} />
            <span>出片后自动 AI 看片自检（关闭=极速直出）</span>
          </label>
        </div>
      `
      : "";

  const createModeSelectHtml =
    activePlat === "libtv"
      ? `
        <select id="newCanvasModeSelect" title="选择新建位置">
          ${curWsId ? `<option value="current_ws" ${state.canvasPicker.createMode === "current_ws" ? "selected" : ""}>创建到当前项目（${escapeHtml(curWsLabel)}）</option>` : ""}
          <option value="new_ws" ${state.canvasPicker.createMode === "new_ws" ? "selected" : ""}>新建同名项目 + 画布</option>
          <option value="root" ${state.canvasPicker.createMode === "root" ? "selected" : ""}>独立画布（LibTV 根目录）</option>
        </select>
      `
      : "";

  const footerHtml = state.canvasPicker.creating
    ? `
      <form id="canvasCreateForm" class="canvas-create-form">
        <input id="newCanvasNameInput" type="text" placeholder="输入新画布名称（如：第75场）" value="${escapeHtml(state.canvasPicker.newName || "")}" required />
        ${createModeSelectHtml}
        <div class="canvas-create-actions">
          <button id="cancelCreateCanvasBtn" class="btn btn-secondary btn-sm" type="button">取消</button>
          <button id="submitCreateCanvasBtn" class="btn btn-primary btn-sm" type="submit" ${state.canvasPicker.submitting ? "disabled" : ""}>${state.canvasPicker.submitting ? "创建中…" : "创建并切换"}</button>
        </div>
      </form>
    `
    : `<button id="openCreateCanvasBtn" class="btn btn-primary btn-sm" type="button" style="width:100%;">+ 新建 ${escapeHtml(platLabel)} 画布</button>`;

  pop.innerHTML = `
    ${tabsHtml}
    ${jimengAccountBarHtml}
    <div class="canvas-popover-head">
      <input id="canvasSearchInput" class="canvas-search-input" type="text" placeholder="搜索全部${escapeHtml(platLabel)}画布…" value="${escapeHtml(state.canvasPicker.query || "")}" />
      <button id="refreshProjectsBtn" class="btn btn-secondary btn-sm" type="button" title="从${escapeHtml(platLabel)}刷新画布列表" ${state.canvasPicker.refreshing ? "disabled" : ""}>${state.canvasPicker.refreshing ? "…" : "↻"}</button>
    </div>
    <div class="canvas-option-list">${listHtml}</div>
    <div class="canvas-popover-foot">${footerHtml}</div>
  `;

  pop.querySelectorAll("[data-popover-platform]").forEach((tBtn) => {
    tBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      switchPlatform(tBtn.dataset.popoverPlatform, { openPopover: true });
    });
  });

  const ceilingInp = document.getElementById("jimengCreditCeilingInput");
  const saveCeilingBtn = document.getElementById("saveJimengCeilingBtn");
  const submitCreditCeiling = async () => {
    const rawVal = Number(ceilingInp?.value ?? 0);
    const cleanVal = Number.isFinite(rawVal) ? Math.max(0, Math.min(100000, Math.round(rawVal))) : 0;
    try {
      const res = await apiJson("/api/config/jimeng-credit-ceiling", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ creditCeiling: cleanVal }),
      });
      if (state.data) state.data.jimengCreditCeiling = res.jimengCreditCeiling ?? cleanVal;
      if (ceilingInp) ceilingInp.value = String(res.jimengCreditCeiling ?? cleanVal);
      showToast(
        (res.jimengCreditCeiling ?? cleanVal) > 0
          ? `已开启即梦单镜 ${res.jimengCreditCeiling ?? cleanVal} 积分以内自动确认放行`
          : "已关闭即梦自动确认（每次扣费停下询问）"
      );
    } catch (err) {
      showToast(err.message);
    }
  };
  ceilingInp?.addEventListener("click", (e) => e.stopPropagation());
  ceilingInp?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      submitCreditCeiling();
    }
  });
  saveCeilingBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    submitCreditCeiling();
  });

  const autoQaToggle = document.getElementById("autoQaAfterRenderToggle");
  autoQaToggle?.addEventListener("click", (e) => e.stopPropagation());
  autoQaToggle?.addEventListener("change", async (e) => {
    e.stopPropagation();
    const enabled = Boolean(autoQaToggle.checked);
    try {
      const res = await apiJson("/api/config/auto-qa", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ autoQaAfterRender: enabled }),
      });
      if (state.data) state.data.autoQaAfterRender = Boolean(res.autoQaAfterRender);
      showToast(enabled ? "已开启：出片下载后自动唤醒轻量 AI 看片自检" : "已切换为极速直出：480P/1080P 下载完直接就绪");
    } catch (err) {
      autoQaToggle.checked = !enabled;
      showToast(err.message);
    }
  });

  const searchInp = document.getElementById("canvasSearchInput");
  searchInp?.addEventListener("input", () => {
    state.canvasPicker.query = searchInp.value;
    const caretPos = searchInp.selectionStart ?? searchInp.value.length;
    renderCanvasPopover();
    const nextInp = document.getElementById("canvasSearchInput");
    if (nextInp) {
      nextInp.focus();
      nextInp.setSelectionRange(caretPos, caretPos);
    }
  });

  document.getElementById("refreshProjectsBtn")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    state.canvasPicker.refreshing = true;
    renderCanvasPopover();
    try {
      const res = await apiJson(`/api/projects?platform=${encodeURIComponent(activePlat)}&refresh=1`);
      if (state.data && Array.isArray(res.projects)) {
        state.data.projectsByPlatform = state.data.projectsByPlatform || {};
        state.data.projectsByPlatform[activePlat] = res.projects;
        state.data.projects = res.projects;
      }
      if (res.authRequired) {
        if (state.data?.jimengStatus) state.data.jimengStatus.authenticated = false;
        showToast("即梦官网登录已失效，请重新授权");
      } else {
        showToast(`已刷新 ${platLabel} 画布列表（共 ${res.projects?.length || 0} 张）`);
      }
    } catch (err) {
      showToast(err.message);
    } finally {
      state.canvasPicker.refreshing = false;
      renderCanvasPopover();
    }
  });

  pop.querySelectorAll("[data-pick-canvas]").forEach((optBtn) => {
    optBtn.addEventListener("click", async () => {
      const targetUuid = optBtn.dataset.pickCanvas;
      const picked = allOptions.find((x) => x.uuid === targetUuid);
      if (!picked) return;
      try {
        if (active.shot) {
          await apiJson(`/api/shots/${encodeURIComponent(active.shot.id)}/project`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              platform: activePlat,
              projectUuid: picked.uuid,
              projectName: picked.name,
              canvasUrl: picked.url,
              workspaceId: picked.workspaceId ?? "",
              setAsDefault: true,
            }),
          });
          showToast(`已将 ${active.shot.id} 画布切换为「${picked.name}」`);
        } else {
          await apiJson("/api/config/default-project", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              platform: activePlat,
              projectUuid: picked.uuid,
              projectName: picked.name,
              canvasUrl: picked.url,
              workspaceId: picked.workspaceId ?? "",
            }),
          });
          showToast(`已切换当前默认${platLabel}画布为「${picked.name}」`);
        }
        closeCanvasPopover();
        await refreshState();
      } catch (err) {
        showToast(err.message);
      }
    });
  });

  document.getElementById("openCreateCanvasBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    state.canvasPicker.creating = true;
    state.canvasPicker.createMode = curWsId ? "current_ws" : "new_ws";
    renderCanvasPopover();
    document.getElementById("newCanvasNameInput")?.focus();
  });

  document.getElementById("cancelCreateCanvasBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    state.canvasPicker.creating = false;
    state.canvasPicker.newName = "";
    renderCanvasPopover();
  });

  const newNameInp = document.getElementById("newCanvasNameInput");
  newNameInp?.addEventListener("input", () => {
    state.canvasPicker.newName = newNameInp.value;
  });
  const modeSel = document.getElementById("newCanvasModeSelect");
  modeSel?.addEventListener("change", () => {
    state.canvasPicker.createMode = modeSel.value;
  });

  document.getElementById("canvasCreateForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = (state.canvasPicker.newName || "").trim();
    if (!name) {
      showToast("请输入画布名称");
      newNameInp?.focus();
      return;
    }
    const mode = state.canvasPicker.createMode || (curWsId ? "current_ws" : "root");
    const payload =
      activePlat === "jimeng"
        ? { name, platform: "jimeng", setAsDefault: true }
        : {
            name,
            platform: "libtv",
            createWorkspace: mode === "new_ws",
            workspaceId: mode === "current_ws" && curWsId ? String(curWsId) : "0",
            setAsDefault: true,
          };
    state.canvasPicker.submitting = true;
    renderCanvasPopover();
    try {
      const res = await apiJson("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const created = res.project;
      if (created?.uuid) {
        if (state.data) {
          state.data.projectsByPlatform = state.data.projectsByPlatform || {};
          const prev = state.data.projectsByPlatform[activePlat] || [];
          state.data.projectsByPlatform[activePlat] = [created, ...prev.filter((x) => x.uuid !== created.uuid)];
          state.data.projects = state.data.projectsByPlatform[activePlat];
        }
        if (active.shot) {
          await apiJson(`/api/shots/${encodeURIComponent(active.shot.id)}/project`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              platform: activePlat,
              projectUuid: created.uuid,
              projectName: created.name,
              canvasUrl: created.url,
              workspaceId: created.workspaceId ?? "",
              setAsDefault: true,
            }),
          });
        }
      }
      state.canvasPicker.newName = "";
      state.canvasPicker.creating = false;
      closeCanvasPopover();
      showToast(`已新建并切换到${platLabel}画布「${created?.name || name}」`);
      await refreshState();
    } catch (err) {
      showToast(err.message);
      state.canvasPicker.submitting = false;
      renderCanvasPopover();
    }
  });
}

function closeAiStatusPopover() {
  if (!state.aiStatusOpen) return;
  state.aiStatusOpen = false;
  document.getElementById("aiStatusPopover")?.classList.add("hidden");
  const btn = document.getElementById("aiStatusBtn");
  btn?.classList.remove("is-open");
  btn?.setAttribute("aria-expanded", "false");
}

function closeShotChatMenu() {
  if (!state.shotChatMenuOpen) return;
  state.shotChatMenuOpen = false;
  document.getElementById("shotChatMenu")?.classList.add("hidden");
  const btn = document.getElementById("shotChatMenuToggleBtn");
  btn?.classList.remove("is-open");
  btn?.setAttribute("aria-expanded", "false");
}

async function copyTextToClipboard(text) {
  const value = String(text || "");
  if (!value) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Fallback to execCommand copy below
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

async function openShotForegroundChat(shotId, mode = "auto", note = "", triggerBtn = null) {
  if (!shotId || state.shotChatBusy) return;
  closeShotChatMenu();
  state.shotChatBusy = true;
  const prevText = triggerBtn ? triggerBtn.textContent : "";
  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.textContent = "唤起窗口中…";
  }
  try {
    const res = await apiJson(`/api/shots/${encodeURIComponent(shotId)}/open-chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, note }),
    });
    if (res.mode === "resume") {
      const shortId = res.conversationId ? `${res.conversationId.slice(0, 8)}…` : "";
      showToast(`已打开终端前台窗口并续接原对话${shortId ? `（${shortId}）` : ""}`);
    } else {
      showToast("已打开终端独立前台对话，并带入本镜完整上下文");
    }
  } catch (err) {
    showToast(err.message || "无法打开前台对话窗口");
  } finally {
    state.shotChatBusy = false;
    if (triggerBtn && triggerBtn.isConnected) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = prevText;
    }
  }
}

async function handoffShotToDesktopApp(shotId, note = "", triggerBtn = null) {
  if (!shotId || state.shotChatBusy) return;
  closeShotChatMenu();
  state.shotChatBusy = true;
  const prevText = triggerBtn ? triggerBtn.textContent : "";
  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.textContent = "准备交接…";
  }
  try {
    const ctxRes = await apiJson(`/api/shots/${encodeURIComponent(shotId)}/handoff-context`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note }),
    });
    const copied = await copyTextToClipboard(ctxRes.contextText || "");
    try {
      await apiJson("/api/system/open-antigravity", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: "antigravity" }),
      });
      showToast(
        copied
          ? "已复制本镜完整上下文并唤起 Antigravity，请直接 Cmd+V 开聊"
          : "已唤起 Antigravity 桌面端（若剪贴板受限可手动粘贴上下文）"
      );
    } catch (err) {
      showToast(copied ? `已复制上下文到剪贴板，但唤起 Antigravity 失败：${err.message}` : err.message);
    }
  } catch (err) {
    showToast(err.message || "准备交接上下文失败");
  } finally {
    state.shotChatBusy = false;
    if (triggerBtn && triggerBtn.isConnected) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = prevText;
    }
  }
}

async function launchForegroundAppWindow(target) {
  if (state.aiActionPending) return;
  state.aiActionPending = true;
  renderAiStatusPopover();
  try {
    await apiJson("/api/system/open-antigravity", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    });
    showToast(target === "terminal_agy" ? "已打开独立终端前台 agy 对话" : "已唤起 Antigravity 桌面端窗口");
  } catch (err) {
    showToast(err.message || "唤起前台窗口失败");
  } finally {
    state.aiActionPending = false;
    renderAiStatusPill();
  }
}

function getAgentStatusMeta() {
  if (state.serverUnreachable) {
    return {
      state: "offline",
      label: "服务未连接",
      detail: "无法连接到本地控制台服务（127.0.0.1:8787）；请双击桌面「洗片控制台.app」启动后点下方刷新。",
      bridgeOnline: false,
      bridgePaused: false,
      maxWorkers: 4,
      busy: false,
      activeShotIds: [],
      pendingCount: 0,
    };
  }
  return (
    state.agentStatus ||
    state.data?.agentStatus || {
      state: "offline",
      label: "AI 检查中…",
      detail: "正在检查后台 agy 并发桥接状态…",
      bridgeOnline: false,
      bridgePaused: false,
      maxWorkers: 4,
      busy: false,
      activeShotIds: [],
      pendingCount: 0,
    }
  );
}

function renderAiStatusPill() {
  const btn = document.getElementById("aiStatusBtn");
  const labelEl = document.getElementById("aiStatusLabel");
  if (!btn || !labelEl) return;
  const st = getAgentStatusMeta();
  const stateCls =
    st.state === "busy"
      ? "is-busy"
      : st.state === "bridge_ready"
      ? "is-bridge-ready"
      : st.state === "paused"
      ? "is-paused"
      : "is-offline";
  btn.className = `ai-status-pill ${stateCls}${state.aiStatusOpen ? " is-open" : ""}`;
  labelEl.textContent = st.label || "AI 状态";
  if (state.aiStatusOpen) {
    renderAiStatusPopover();
  }
}

async function runAiBridgeAction(endpoint, successMessage) {
  state.aiActionPending = true;
  renderAiStatusPopover();
  try {
    const res = await apiJson(endpoint, { method: "POST" });
    state.serverUnreachable = false;
    if (res.agentStatus) state.agentStatus = res.agentStatus;
    showToast(successMessage);
    await refreshAgentStatus();
  } catch (err) {
    showToast(err.message);
  } finally {
    state.aiActionPending = false;
    renderAiStatusPill();
  }
}

function renderAiStatusPopover() {
  const pop = document.getElementById("aiStatusPopover");
  const btn = document.getElementById("aiStatusBtn");
  if (!pop || !btn) return;
  if (!state.aiStatusOpen) {
    pop.classList.add("hidden");
    btn.classList.remove("is-open");
    btn.setAttribute("aria-expanded", "false");
    return;
  }

  pop.classList.remove("hidden");
  btn.classList.add("is-open");
  btn.setAttribute("aria-expanded", "true");

  const st = getAgentStatusMeta();
  const badgeCls =
    st.state === "busy"
      ? "pill-blue"
      : st.state === "bridge_ready"
      ? "pill-green"
      : st.state === "paused"
      ? "pill-orange"
      : "pill-red";

  const activeText = st.activeShotIds?.length ? st.activeShotIds.join("、") : "空闲中";
  const convShort = st.lastConversationId ? `${st.lastConversationId.slice(0, 12)}…` : "尚无记录";
  const pendingAttr = state.aiActionPending ? "disabled" : "";
  const isBridgeActive = st.bridgeOnline && !st.bridgePaused;

  let actionsHtml = `<button id="refreshAiStatusBtn" data-ai-action="refresh" class="btn btn-secondary btn-sm" type="button" ${pendingAttr}>刷新状态</button>`;
  if (!state.serverUnreachable) {
    if (isBridgeActive) {
      actionsHtml += `
        <button id="toggleBridgeBtn" data-ai-action="stop" class="btn btn-secondary btn-sm" type="button" ${pendingAttr}>${state.aiActionPending ? "处理中…" : "暂停后台桥接"}</button>
        <button id="reconnectBridgeBtn" data-ai-action="restart" class="btn btn-primary btn-sm" type="button" ${pendingAttr}>${state.aiActionPending ? "重连中…" : "一键重连"}</button>
      `;
    } else {
      actionsHtml += `
        <button id="toggleBridgeBtn" data-ai-action="start" class="btn btn-primary btn-sm" type="button" ${pendingAttr}>${state.aiActionPending ? "重连中…" : "一键重连（启动桥接）"}</button>
      `;
    }
  }

  const launchHtml = !state.serverUnreachable
    ? `
      <div class="ai-popover-launch">
        <button id="openTerminalAgyBtn" data-ai-action="open-terminal-agy" class="btn btn-secondary btn-sm" type="button" ${pendingAttr} title="在新终端窗口启动独立前台 agy 对话">💻 终端新开 agy</button>
        <button id="openAntigravityAppBtn" data-ai-action="open-antigravity" class="btn btn-secondary btn-sm" type="button" ${pendingAttr} title="唤起 Antigravity 桌面客户端">🚀 打开 Antigravity</button>
      </div>
    `
    : "";

  const nextHtml = `
    <div class="ai-popover-head">
      <span class="ai-popover-title">Antigravity 并发桥接与前台接管</span>
      <span class="pill ${badgeCls}">${escapeHtml(st.label)}</span>
    </div>
    <div class="ai-popover-desc">${escapeHtml(st.detail || "")}</div>
    <div class="ai-popover-meta">
      <div class="ai-popover-row"><span>本地 agy 桥接</span><strong>${state.serverUnreachable ? "服务未连接" : st.agyInstalled === false ? "未安装 agy CLI" : st.bridgePaused ? "已暂停" : st.bridgeOnline ? `运行中 (PID ${st.bridgePid || "-"})` : "未运行"}</strong></div>
      <div class="ai-popover-row"><span>并发调度能力</span><strong>${st.maxWorkers || 4} 路独立会话（互不串线）</strong></div>
      <div class="ai-popover-row"><span>处理中 / 排队</span><strong>${escapeHtml(activeText)} / ${st.pendingCount || 0} 待办</strong></div>
      <div class="ai-popover-row"><span>最近完成会话</span><strong>${escapeHtml(convShort)}${st.lastRunAt ? ` · ${escapeHtml(st.lastRunAt.slice(11))}` : ""}</strong></div>
    </div>
    ${st.lastError ? `<div class="ai-popover-err">最近异常：${escapeHtml(st.lastError)}</div>` : ""}
    <div class="ai-popover-actions">${actionsHtml}</div>
    ${launchHtml}
  `;

  if (pop.dataset.renderedHtml !== nextHtml) {
    pop.innerHTML = nextHtml;
    pop.dataset.renderedHtml = nextHtml;
  }
}

async function refreshAgentStatus() {
  try {
    const res = await apiJson("/api/agent/status");
    state.serverUnreachable = false;
    state.agentStatus = res;
    if (state.data) state.data.agentStatus = res;
    renderAiStatusPill();
    if (state.view === "detail" && state.selectedShotId) {
      const curShot = (state.data?.shots || []).find((s) => s.id === state.selectedShotId);
      if (curShot && isShotInQueueOrProcessing(curShot)) {
        refreshShotRuntimeBannerDom(curShot);
      }
    }
  } catch {
    state.serverUnreachable = true;
    renderAiStatusPill();
  }
}

const ACTIONABLE_QUEUE_STATUSES = new Set([
  "queued",
  "feedback_queued",
  "recheck_requested",
  "prompt_update_requested",
  "run_requested",
  "final_requested",
]);

function isShotInQueueOrProcessing(shot) {
  if (!shot) return false;
  return shot.status === "processing" || ACTIONABLE_QUEUE_STATUSES.has(shot.status);
}

function formatDurationZh(totalSec) {
  const sec = Math.max(0, Math.floor(Number(totalSec) || 0));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m} 分 ${s} 秒` : `${m} 分`;
}

function getShotRuntimeSnapshot(shot) {
  if (!shot) return null;
  const cached = state.runtimeByShot?.[shot.id];
  const cachedValid = cached && cached.status === shot.status;
  if (cachedValid) return cached;

  const summaryMap = state.agentStatus?.shotWorkers || state.data?.agentStatus?.shotWorkers || {};
  const w = summaryMap[shot.id];
  const st = shot.status || "";
  const isProc = st === "processing";
  const isWait = ACTIONABLE_QUEUE_STATUSES.has(st);
  const agyMeta = getAgentStatusMeta();
  let health = isProc ? "running" : isWait ? "queued" : "idle";
  let healthLabel = isProc ? "🟢 Agent 正在后台处理中" : isWait ? "⏳ 正在排队等待接单" : "当前未在运行";
  let diagnosis = shot.agentSummary || "";
  const elapsedSec = w?.elapsedSec ?? 0;
  const idleSec = w?.idleSec ?? 0;
  if (isProc) {
    if (!agyMeta.bridgeOnline && !state.serverUnreachable) {
      health = "stalled";
      healthLabel = "🔴 后台桥接未运行";
      diagnosis = "镜头状态为「处理中」，但后台桥接未在线；建议点「↻ 刷新重试」。";
    } else if (idleSec >= 180) {
      health = "stalled";
      healthLabel = `🟠 已 ${idleSec} 秒无新动作（疑似卡住）`;
    } else if (idleSec >= 60) {
      health = "slow";
      healthLabel = `🟡 等待平台/工具响应中（${idleSec} 秒前更新）`;
    }
  } else if (isWait && agyMeta.bridgePaused) {
    health = "paused";
    healthLabel = "⏸ 后台桥接已暂停";
  }
  return {
    shotId: shot.id,
    status: st,
    isProcessing: isProc,
    isWaiting: isWait,
    taskType: shot.processingTaskType || (isWait ? st : ""),
    taskLabel: STATUS_META[shot.processingTaskType || st]?.label || shot.processingTaskType || st,
    health,
    healthLabel,
    diagnosis,
    elapsedSec,
    idleSec,
    phase: w?.phase || (isProc ? "后台执行中" : isWait ? "排队待领单" : ""),
    lastNote: w?.lastNote || shot.agentSummary || "",
    stepCount: w?.stepCount || 0,
    agyPid: w?.agyPid || null,
    agyPidAlive: w ? true : null,
    bridgeOnline: Boolean(agyMeta.bridgeOnline),
    bridgePaused: Boolean(agyMeta.bridgePaused),
    bridgePid: agyMeta.bridgePid || null,
    conversationId: shot.cliConversationId || "",
    recentSteps: [],
    recentLogs: [],
  };
}

async function fetchShotRuntimeStatus(shotId, { showToastOnResult = false } = {}) {
  if (!shotId) return null;
  try {
    const res = await apiJson(`/api/shots/${encodeURIComponent(shotId)}/agent-status`);
    if (res.runtime) {
      state.runtimeByShot[shotId] = res.runtime;
    }
    const latestShot = res.shot;
    if (latestShot && state.data?.shots) {
      const idx = state.data.shots.findIndex((s) => s.id === latestShot.id);
      if (idx >= 0) {
        const prevStatus = state.data.shots[idx].status;
        state.data.shots[idx] = latestShot;
        if (prevStatus !== latestShot.status && state.view === "detail" && state.selectedShotId === shotId) {
          renderActiveView();
          if (state.runtimeModal.open && state.runtimeModal.shotId === shotId) {
            renderRuntimeModalBody();
          }
          if (showToastOnResult) {
            showToast(`镜头状态已更新为「${getDisplayStatusMeta(latestShot).label}」`);
          }
          return res.runtime;
        }
      }
    }
    if (state.view === "detail" && state.selectedShotId === shotId && latestShot) {
      refreshShotRuntimeBannerDom(latestShot);
    }
    if (state.runtimeModal.open && state.runtimeModal.shotId === shotId) {
      renderRuntimeModalBody();
    }
    if (showToastOnResult && res.runtime) {
      const rt = res.runtime;
      showToast(
        rt.health === "stalled" || rt.health === "offline"
          ? `${rt.healthLabel}，建议点「↻ 强制刷新重试」`
          : `${rt.healthLabel}（${rt.phase || rt.taskLabel} · 已耗时 ${formatDurationZh(rt.elapsedSec)}）`
      );
    }
    return res.runtime || null;
  } catch (err) {
    if (showToastOnResult) {
      showToast(err.message || "查询 Agent 状态失败");
    }
    return null;
  }
}

async function retryShotProcessing(shotId, mode = "retry") {
  if (!shotId || state.runtimeBusy) return;
  state.runtimeBusy = true;
  if (state.runtimeModal.open) renderRuntimeModalBody();
  try {
    const res = await apiJson(`/api/shots/${encodeURIComponent(shotId)}/retry-processing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (res.runtime) {
      state.runtimeByShot[shotId] = res.runtime;
    }
    const toastMsg =
      mode === "restore"
        ? "已中止当前后台任务并退回稳态"
        : mode === "recheck"
        ? "已中止旧任务并转入自检重检队列"
        : "已中止卡住任务并重新入队重试，等待空闲 Worker 接单";
    showToast(toastMsg);
    if (mode === "restore") {
      closeShotRuntimeModal();
    }
    await refreshState();
    if (state.runtimeModal.open && state.runtimeModal.shotId === shotId) {
      renderRuntimeModalBody();
    }
  } catch (err) {
    showToast(err.message || "重试失败");
  } finally {
    state.runtimeBusy = false;
    if (state.runtimeModal.open) renderRuntimeModalBody();
  }
}

function buildRuntimeBannerInnerHtml(shot, rt) {
  const health = rt?.health || "queued";
  const isStalled = health === "stalled" || health === "offline";
  const isProc = Boolean(rt?.isProcessing || shot.status === "processing");
  const timerBits = [];
  if (isProc) {
    timerBits.push(`已耗时 ${formatDurationZh(rt?.elapsedSec || 0)}`);
    timerBits.push(`${rt?.idleSec || 0} 秒前有动作`);
    if (rt?.agyPid) timerBits.push(`PID ${rt.agyPid}`);
  } else if (rt?.taskLabel) {
    timerBits.push(`待执行：${rt.taskLabel}`);
  }
  const timerText = timerBits.join(" · ");
  const retryCls = isStalled ? "btn-primary" : "btn-secondary";
  const retryLabel = isProc ? "↻ 卡住刷新重试" : "↻ 刷新重试";

  return `
    <div class="runtime-banner-top">
      <div class="runtime-banner-left">
        <span class="runtime-health-label">${escapeHtml(rt?.healthLabel || "⏳ 正在处理中")}</span>
        ${timerText ? `<span class="runtime-timer-pill">${escapeHtml(timerText)}</span>` : ""}
      </div>
      <div class="runtime-banner-actions">
        <button id="runtimeBannerDetailBtn" class="btn btn-secondary btn-xs" type="button">📋 步骤与进程</button>
        <button id="runtimeBannerRefreshBtn" class="btn btn-secondary btn-xs" type="button">↻ 刷新状态</button>
        <button id="runtimeBannerRetryBtn" class="btn ${retryCls} btn-xs" type="button" ${state.runtimeBusy ? "disabled" : ""} title="立刻中止卡住的进程并把当前任务重新入队执行">${retryLabel}</button>
      </div>
    </div>
    <div class="runtime-banner-detail">
      ${rt?.phase ? `<span class="runtime-banner-phase">当前步骤：${escapeHtml(rt.phase)}</span>` : ""}
      <span>${escapeHtml(rt?.lastNote || rt?.diagnosis || shot.agentSummary || "")}</span>
    </div>
  `;
}

function renderShotRuntimeBannerHtml(shot) {
  if (!isShotInQueueOrProcessing(shot)) return "";
  const rt = getShotRuntimeSnapshot(shot);
  const healthCls = rt?.health ? `is-${rt.health}` : "is-running";
  return `<section id="shotRuntimeBanner" class="runtime-status-banner ${healthCls}" aria-label="Agent 实时处理状态">${buildRuntimeBannerInnerHtml(shot, rt)}</section>`;
}

function refreshShotRuntimeBannerDom(shot) {
  const banner = document.getElementById("shotRuntimeBanner");
  const rt = getShotRuntimeSnapshot(shot);
  if (banner && rt) {
    banner.className = `runtime-status-banner is-${rt.health || "running"}`;
    banner.innerHTML = buildRuntimeBannerInnerHtml(shot, rt);
  }
  const topBtn = document.getElementById("openShotRuntimeBtn");
  if (topBtn && rt) {
    const isStalled = rt.health === "stalled" || rt.health === "offline";
    topBtn.className = `btn ${isStalled ? "btn-warning" : "btn-secondary"} btn-sm`;
    topBtn.textContent = isStalled ? "⚠ 卡住·重试" : "📡 处理状态";
  }
}

function openShotRuntimeModal(shotId) {
  if (!shotId) return;
  closeShotMoreMenu();
  closeShotChatMenu();
  state.runtimeModal = { open: true, shotId };
  renderRuntimeModalBody();
  document.getElementById("agentRuntimeModal")?.classList.remove("hidden");
  fetchShotRuntimeStatus(shotId);
  syncRuntimePollingTimer();
}

function closeShotRuntimeModal() {
  document.getElementById("agentRuntimeModal")?.classList.add("hidden");
  state.runtimeModal = { open: false, shotId: null };
  syncRuntimePollingTimer();
}

function renderRuntimeModalBody() {
  const bodyEl = document.getElementById("agentRuntimeModalBody");
  const titleEl = document.getElementById("runtimeModalTitle");
  if (!bodyEl) return;
  const sid = state.runtimeModal.shotId;
  const shot = (state.data?.shots || []).find((s) => s.id === sid);
  if (titleEl) {
    titleEl.textContent = shot ? `Agent 处理实时状态 · ${shot.id}` : "Agent 处理实时状态";
  }
  if (!shot) {
    bodyEl.innerHTML = `<div class="runtime-diag-desc">未找到该镜头数据</div>`;
    return;
  }
  const rt = getShotRuntimeSnapshot(shot) || {};
  const health = rt.health || "idle";
  const pidText = rt.agyPid
    ? `PID ${rt.agyPid}（${rt.agyPidAlive === true ? "进程运行中 ✓" : rt.agyPidAlive === false ? "进程已退出 ✕" : "检查中"}）`
    : rt.isProcessing
    ? "未匹配到活跃子进程"
    : "尚未拉起";
  const bridgeText = rt.bridgePaused
    ? "已暂停"
    : rt.bridgeOnline
    ? `在线（PID ${rt.bridgePid || "-"}）`
    : "离线";
  const stepsText = (rt.recentSteps || []).length
    ? rt.recentSteps.map((st) => `[${st.at || ""}] ${st.text || ""}`).join("\n")
    : rt.isProcessing
    ? "等待 Worker 上报步骤流水（点击上方刷新可立刻拉取最新进展）"
    : "暂无本轮执行步骤记录";
  const logsText = (rt.recentLogs || []).length ? rt.recentLogs.join("\n") : "（bridge.log 暂无该镜头最近日志）";

  bodyEl.innerHTML = `
    <div class="runtime-diag-box is-${escapeHtml(health)}">
      <div class="runtime-diag-title">${escapeHtml(rt.healthLabel || "状态未知")}</div>
      <div class="runtime-diag-desc">${escapeHtml(rt.diagnosis || shot.agentSummary || "")}</div>
    </div>
    <div class="runtime-meta-grid">
      <div class="runtime-meta-item"><span>当前任务</span><strong>${escapeHtml(rt.taskLabel || "-")} (${escapeHtml(shot.status)})</strong></div>
      <div class="runtime-meta-item"><span>当前步骤</span><strong>${escapeHtml(rt.phase || "-")}</strong></div>
      <div class="runtime-meta-item"><span>累计耗时 / 无动作</span><strong>${formatDurationZh(rt.elapsedSec || 0)} / ${formatDurationZh(rt.idleSec || 0)}</strong></div>
      <div class="runtime-meta-item"><span>Worker 子进程</span><strong>${escapeHtml(pidText)}</strong></div>
      <div class="runtime-meta-item"><span>后台桥接服务</span><strong>${escapeHtml(bridgeText)}</strong></div>
      <div class="runtime-meta-item"><span>最近检查时间</span><strong>${escapeHtml(rt.checkedAt || "实时")}</strong></div>
    </div>
    <div class="runtime-log-section">
      <div class="runtime-log-head">Agent 实时动作流水（最新在底部）</div>
      <pre class="runtime-log-box">${escapeHtml(stepsText)}</pre>
    </div>
    <div class="runtime-log-section">
      <div class="runtime-log-head">桥接调度日志（bridge.log）</div>
      <pre class="runtime-log-box">${escapeHtml(logsText)}</pre>
    </div>
  `;

  const retryBtn = document.getElementById("runtimeModalRetryBtn");
  const recheckBtn = document.getElementById("runtimeModalRecheckBtn");
  const restoreBtn = document.getElementById("runtimeModalRestoreBtn");
  const busy = Boolean(state.runtimeBusy);
  if (retryBtn) {
    retryBtn.disabled = busy;
    retryBtn.textContent = busy ? "重置重试中…" : "↻ 强制刷新重试";
  }
  if (recheckBtn) recheckBtn.disabled = busy;
  if (restoreBtn) {
    restoreBtn.disabled = busy || (!rt.isProcessing && !rt.isWaiting);
  }
}

function syncRuntimePollingTimer() {
  const curShot =
    state.view === "detail"
      ? (state.data?.shots || []).find((s) => s.id === state.selectedShotId) || null
      : null;
  const needPoll = Boolean(
    state.runtimeModal.open || (curShot && isShotInQueueOrProcessing(curShot))
  );
  if (!needPoll) {
    if (state.runtimePollTimer) {
      clearInterval(state.runtimePollTimer);
      state.runtimePollTimer = null;
    }
    return;
  }
  if (!state.runtimePollTimer) {
    const initialTarget = state.runtimeModal.open ? state.runtimeModal.shotId : curShot?.id;
    if (initialTarget) fetchShotRuntimeStatus(initialTarget);
    state.runtimePollTimer = setInterval(() => {
      const targetId = state.runtimeModal.open
        ? state.runtimeModal.shotId
        : state.view === "detail"
        ? state.selectedShotId
        : null;
      if (targetId) fetchShotRuntimeStatus(targetId);
    }, 2500);
  }
}

function closeUpdatePopover() {
  if (!state.updateMenuOpen) return;
  state.updateMenuOpen = false;
  document.getElementById("updatePopover")?.classList.add("hidden");
  const btn = document.getElementById("updatePillBtn");
  btn?.classList.remove("is-open");
  btn?.setAttribute("aria-expanded", "false");
}

function renderUpdatePill() {
  const btn = document.getElementById("updatePillBtn");
  const dot = document.getElementById("updateDot");
  const labelEl = document.getElementById("updatePillLabel");
  if (!btn || !labelEl) return;
  const info = state.updateInfo;
  const hasUpdate = Boolean(info?.hasUpdate);
  btn.classList.toggle("has-update", hasUpdate);
  btn.classList.toggle("is-applying", Boolean(state.updateApplying));
  btn.classList.toggle("is-open", Boolean(state.updateMenuOpen));
  if (dot) dot.classList.toggle("hidden", !hasUpdate && !state.updateApplying);

  if (state.updateApplying) {
    labelEl.textContent = "热更新重启中…";
  } else if (hasUpdate) {
    labelEl.textContent = `有新版 v${info.latestVersion}`;
  } else if (info?.currentVersion) {
    labelEl.textContent = `v${info.currentVersion}`;
  } else {
    labelEl.textContent = "检查更新";
  }
  if (state.updateMenuOpen) {
    renderUpdatePopover();
  }
}

function renderUpdatePopover() {
  const pop = document.getElementById("updatePopover");
  const btn = document.getElementById("updatePillBtn");
  if (!pop || !btn) return;
  if (!state.updateMenuOpen) {
    pop.classList.add("hidden");
    btn.classList.remove("is-open");
    btn.setAttribute("aria-expanded", "false");
    return;
  }
  pop.classList.remove("hidden");
  btn.classList.add("is-open");
  btn.setAttribute("aria-expanded", "true");

  const info = state.updateInfo || {};
  const curVer = info.currentVersion || "-";
  const latVer = info.latestVersion || curVer;
  const hasUpdate = Boolean(info.hasUpdate);
  const busy = Boolean(state.updateActionPending || state.updateApplying);
  const disabledAttr = busy ? "disabled" : "";
  const statusPill = state.updateApplying
    ? `<span class="pill pill-blue">正在重启…</span>`
    : hasUpdate
    ? `<span class="pill pill-green">发现新版本</span>`
    : info.error
    ? `<span class="pill pill-orange">待连接 GitHub</span>`
    : `<span class="pill pill-muted">已是最新</span>`;

  const changelogText = (hasUpdate ? info.changelog : info.currentChangelog || info.changelog) || "点击下方按钮从 GitHub 同步最新应用代码与通用工作流 Skills（不会改动个人数据与登录态）。";
  const repoUrl = info.repoUrl || "https://github.com/hqy-maker/wash-queue-console";

  const nextHtml = `
    <div class="ai-popover-head">
      <span class="ai-popover-title">GitHub 一键热更新</span>
      ${statusPill}
    </div>
    <div class="ai-popover-meta">
      <div class="ai-popover-row"><span>当前本地版本</span><strong>v${escapeHtml(curVer)}</strong></div>
      <div class="ai-popover-row"><span>GitHub 最新版</span><strong>v${escapeHtml(latVer)}</strong></div>
      <div class="ai-popover-row"><span>更新源仓库</span><strong><a class="update-repo-link" href="${escapeHtml(repoUrl)}" target="_blank" rel="noopener">${escapeHtml(info.repo || "hqy-maker/wash-queue-console")}</a></strong></div>
    </div>
    <div class="update-changelog">${escapeHtml(changelogText)}</div>
    <div class="update-safe-note">仅升级应用代码与通用工作流 Skills，完整保留你的片源、队列历史与平台登录态。</div>
    ${info.error ? `<div class="ai-popover-err">检查提示：${escapeHtml(info.error)}</div>` : ""}
    <div class="ai-popover-actions">
      <button type="button" class="btn btn-secondary btn-sm" data-update-action="check" ${disabledAttr}>${state.updateActionPending && !state.updateApplying ? "检查中…" : "检查更新"}</button>
      ${
        info.canRollback
          ? `<button type="button" class="btn btn-secondary btn-sm" data-update-action="rollback" ${disabledAttr} title="恢复到热更新前的备份版本 v${escapeHtml(info.backupVersion || "")}">回滚上一版</button>`
          : ""
      }
      <button type="button" class="btn btn-primary btn-sm" data-update-action="apply" ${disabledAttr}>
        ${state.updateApplying ? "更新重启中…" : hasUpdate ? `立即热更新 v${escapeHtml(latVer)}` : "强制拉取并重启"}
      </button>
    </div>
  `;
  if (pop.dataset.renderedHtml !== nextHtml) {
    pop.innerHTML = nextHtml;
    pop.dataset.renderedHtml = nextHtml;
  }
}

async function refreshUpdateStatus(forceRefresh = false) {
  try {
    const endpoint = forceRefresh ? "/api/system/check-update" : "/api/system/update-status";
    const opts = forceRefresh ? { method: "POST" } : undefined;
    const res = await apiJson(endpoint, opts);
    state.updateInfo = res;
    renderUpdatePill();
    return res;
  } catch (err) {
    if (forceRefresh) showToast(`检查更新失败: ${err.message}`);
    return null;
  }
}

async function waitForHotRestartAndReload(targetVersion) {
  const deadline = Date.now() + 30000;
  await new Promise((r) => setTimeout(r, 1200));
  while (Date.now() < deadline) {
    try {
      const r = await fetch("/api/health", { cache: "no-store" });
      if (r.ok) {
        const j = await r.json();
        if (j?.ok) {
          showToast(targetVersion ? `已热更新至 v${targetVersion}，正在刷新界面…` : "服务重启完成，正在刷新…");
          setTimeout(() => window.location.reload(), 250);
          return;
        }
      }
    } catch {
      // Server still restarting
    }
    await new Promise((r) => setTimeout(r, 450));
  }
  state.updateApplying = false;
  state.updateActionPending = false;
  renderUpdatePill();
  showToast("后台已完成更新，请手动刷新页面 (⌘R)");
}

async function triggerHotUpdate(action) {
  const isRollback = action === "rollback";
  state.updateActionPending = true;
  state.updateApplying = true;
  renderUpdatePill();
  try {
    const endpoint = isRollback ? "/api/system/rollback-update" : "/api/system/apply-update";
    const res = await apiJson(endpoint, { method: "POST" });
    const targetVer = isRollback ? res.restoredVersion : res.newVersion;
    showToast(
      isRollback
        ? `已恢复备份 v${targetVer || ""}，正在重启服务…`
        : `已下载并校验 v${targetVer || ""}，正在无缝重启…`
    );
    await waitForHotRestartAndReload(targetVer);
  } catch (err) {
    state.updateApplying = false;
    state.updateActionPending = false;
    renderUpdatePill();
    showToast(`${isRollback ? "回滚" : "热更新"}失败: ${err.message}`);
  }
}

function closeShotMoreMenu() {
  if (!state.shotMoreOpen) return;
  state.shotMoreOpen = false;
  document.getElementById("shotMoreMenu")?.classList.add("hidden");
  const btn = document.getElementById("shotMoreBtn");
  btn?.classList.remove("is-open");
  btn?.setAttribute("aria-expanded", "false");
}

function renderTopBar() {
  const d = state.data;
  if (!d) return;

  if (d.agentStatus) {
    state.agentStatus = d.agentStatus;
  }
  renderAiStatusPill();

  const badgeEl = document.getElementById("projectNameBadge");
  const titleEl = document.getElementById("topbarTitle");
  const shotTitleBtn = document.getElementById("topbarShotTitleBtn");
  const scenePillEl = document.getElementById("topbarScenePill");
  const statusPillEl = document.getElementById("topbarStatusPill");
  const moreWrap = document.getElementById("shotMoreWrap");
  const reprocessBtn = document.getElementById("topbarReprocessBtn");
  const delBtn = document.getElementById("topbarDeleteShotBtn");

  const pickerBtn = document.getElementById("canvasPickerBtn");
  const activeCanvas = getActiveCanvasContext();
  const activePlat = activeCanvas.platform;
  const jimengUnauthed = activePlat === "jimeng" && !d.jimengStatus?.authenticated;
  const libtvUnauthed = activePlat === "libtv" && d.libtvStatus && !d.libtvStatus.authenticated;
  document.querySelectorAll("[data-platform-btn]").forEach((pBtn) => {
    const isMatch = pBtn.dataset.platformBtn === activePlat;
    pBtn.classList.toggle("active", isMatch);
    pBtn.setAttribute("aria-pressed", isMatch ? "true" : "false");
  });
  badgeEl.textContent =
    jimengUnauthed && !activeCanvas.uuid
      ? "连接即梦官网"
      : libtvUnauthed && !activeCanvas.uuid
      ? "连接 LibTV"
      : activeCanvas.name || (activePlat === "jimeng" ? "选择即梦画布" : "选择 LibTV 画布");
  if (pickerBtn) {
    pickerBtn.title = `点击切换或新建 ${getPlatformLabel(activePlat)} 画布`;
  }

  if (state.view === "builder") {
    titleEl.textContent = "新建排片";
    shotTitleBtn?.classList.add("hidden");
    scenePillEl.classList.add("hidden");
    statusPillEl?.classList.add("hidden");
    moreWrap?.classList.add("hidden");
    reprocessBtn?.classList.add("hidden");
    delBtn?.classList.add("hidden");
    closeShotMoreMenu();
    closeShotChatMenu();
  } else {
    const shot = (d.shots || []).find((s) => s.id === state.selectedShotId);
    titleEl.textContent = shot ? shot.id : "镜头";
    if (shot) {
      if (shotTitleBtn) {
        const cleanShotTitle = (shot.title || "").trim();
        shotTitleBtn.textContent = cleanShotTitle ? `🏷 ${cleanShotTitle}` : "+ 命名镜头";
        shotTitleBtn.classList.remove("hidden");
      }
      const sceneText = (shot.scene || "").trim();
      scenePillEl.textContent = sceneText || "未分场";
      scenePillEl.classList.remove("hidden");
      if (statusPillEl) {
        const sm = getDisplayStatusMeta(shot);
        statusPillEl.className = `pill ${sm.cls}`;
        statusPillEl.textContent = sm.label;
        statusPillEl.title = shot.status === "error" ? (shot.errorReason || getShotErrorTag(shot)) : "";
      }
      const isPendingRecheck = shot.status === "recheck_requested";
      if (reprocessBtn) {
        reprocessBtn.disabled = isPendingRecheck;
        reprocessBtn.textContent = isPendingRecheck ? "待 Agent 重检..." : "↻ 重新处理·自检";
        reprocessBtn.classList.remove("hidden");
      }
      delBtn?.classList.remove("hidden");
      moreWrap?.classList.remove("hidden");
    } else {
      shotTitleBtn?.classList.add("hidden");
      scenePillEl.classList.add("hidden");
      statusPillEl?.classList.add("hidden");
      moreWrap?.classList.add("hidden");
      reprocessBtn?.classList.add("hidden");
      delBtn?.classList.add("hidden");
      closeShotMoreMenu();
      closeShotChatMenu();
    }
  }

  const popFocused = Boolean(document.activeElement?.closest("#canvasPopover"));
  if (state.canvasPicker.open && !popFocused) {
    renderCanvasPopover();
  }
}

function bindFolderInlineInput(inp, { mode = "rename", oldName = "" } = {}) {
  let finished = false;
  const finish = async (commit) => {
    if (finished) return;
    finished = true;
    const newName = inp.value.trim();
    if (mode === "create") {
      state.creatingFolder = false;
    } else {
      state.editingFolderName = null;
    }

    if (!commit || !newName || newName === "未分场" || (mode === "rename" && newName === oldName)) {
      if (commit && newName === "未分场") {
        showToast("场号不能命名为“未分场”");
      } else if (commit && mode === "create" && !newName) {
        showToast("已取消新建场次");
      }
      renderSidebar();
      return;
    }

    try {
      if (mode === "create") {
        await apiJson("/api/scenes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newName }),
        });
        state.selectedSceneFolder = newName;
        state.expandedFolders.add(newName);
        state.seenFolders.add(newName);
        if (!state.builder.sceneTouched) {
          syncBuilderSceneSelection(newName);
        }
        showToast(`已新建场次「${newName}」`);
      } else {
        await apiJson("/api/scenes/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ oldName, newName }),
        });
        if (state.selectedSceneFolder === oldName) {
          state.selectedSceneFolder = newName;
        }
        if (state.selectedSceneTab === oldName) {
          state.selectedSceneTab = newName;
        }
        if (state.expandedFolders.has(oldName)) {
          state.expandedFolders.delete(oldName);
          state.expandedFolders.add(newName);
        }
        state.seenFolders.add(newName);
        if (!state.builder.sceneTouched && (state.builder.scene === oldName || (oldName === "未分场" && !state.builder.scene))) {
          syncBuilderSceneSelection(newName);
        }
        showToast(`已将「${oldName}」更名为「${newName}」`);
      }
      await refreshState();
      if (state.view === "builder") renderBuilderView();
    } catch (err) {
      showToast(err.message);
      renderSidebar();
    }
  };

  inp.addEventListener("mousedown", (e) => e.stopPropagation());
  inp.addEventListener("click", (e) => e.stopPropagation());
  inp.addEventListener("dblclick", (e) => e.stopPropagation());
  inp.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  });
  inp.addEventListener("blur", () => finish(true));
}

function updateSidebarFolderDom(folderBtn, folderName, isExpanded) {
  const listEl = document.getElementById("shotList");
  if (!listEl || !folderBtn) return;
  for (const hdr of listEl.querySelectorAll(".scene-folder-header[data-select-folder]")) {
    hdr.classList.toggle("selected", hdr.dataset.selectFolder === folderName);
  }
  const folderEl = folderBtn.closest(".scene-folder");
  if (folderEl) {
    folderEl.classList.toggle("collapsed", !isExpanded);
    const iconEl = folderEl.querySelector(".scene-folder-icon");
    if (iconEl) iconEl.textContent = isExpanded ? "📂" : "📁";
  }
}

function renderSidebar() {
  const listEl = document.getElementById("shotList");
  const shots = state.data?.shots || [];
  const customScenes = state.data?.scenes || [];
  const groups = groupShotsByScene(shots, customScenes);
  if (!groups.length && !state.creatingFolder) {
    listEl.innerHTML = `<div style="padding:14px 8px;color:var(--text-muted);font-size:12px;">暂无场次与镜头，点上方「+ 新建场」或「+ 新建镜头」开始</div>`;
    return;
  }

  const activeShot =
    state.view === "detail"
      ? shots.find((s) => s.id === state.selectedShotId) || null
      : null;
  const highlightedFolder =
    state.selectedSceneFolder || (activeShot ? getShotFolderName(activeShot) : null);

  const creatingHtml = state.creatingFolder
    ? `
      <div class="scene-folder">
        <div class="scene-folder-header selected">
          <span class="scene-folder-chevron">▾</span>
          <span class="scene-folder-icon">📂</span>
          <input
            type="text"
            id="sceneFolderInlineInput"
            name="sceneFolderInline"
            class="scene-folder-rename-input"
            data-create-folder-input="1"
            value=""
            placeholder="输入场号后按回车（如 第23场）"
          />
          <span class="scene-folder-count">0</span>
        </div>
      </div>
    `
    : "";

  const groupsHtml = groups
    .map((group) => {
      if (!state.seenFolders.has(group.name)) {
        state.seenFolders.add(group.name);
        state.expandedFolders.add(group.name);
      }
      const isExpanded = state.expandedFolders.has(group.name);
      const isSelected = highlightedFolder === group.name;
      const isEditing = state.editingFolderName === group.name;
      const childrenHtml = group.shots.length
        ? group.shots
            .map((s) => {
              const sm = getDisplayStatusMeta(s);
              const isActive = state.view === "detail" && state.selectedShotId === s.id;
              const vidName = shotMediaSubtitle(s);
              return `
                <div
                  role="button"
                  tabindex="0"
                  draggable="true"
                  class="shot-item ${isActive ? "active" : ""}"
                  data-select-shot="${escapeHtml(s.id)}"
                  data-shot-scene="${escapeHtml(group.name)}"
                  title="点击查看详情 · 拖拽可移动到其他场次"
                >
                  <div class="shot-item-top">
                    <div class="shot-item-id-group">
                      <span class="shot-item-id">${escapeHtml(s.id)}</span>
                      ${normalizePlatform(s.platform) === "jimeng" ? `<span class="shot-platform-tag">即梦</span>` : ""}
                    </div>
                    <span class="pill ${sm.cls}"${s.status === "error" ? ` title="${escapeHtml(s.errorReason || getShotErrorTag(s))}"` : ""}>${escapeHtml(sm.label)}</span>
                  </div>
                  <div class="shot-item-sub">${escapeHtml(vidName)}</div>
                </div>
              `;
            })
            .join("")
        : `<div class="scene-folder-empty">暂无镜头 · 可拖拽镜头到此场</div>`;

      const nameHtml = isEditing
        ? `<input type="text" id="sceneFolderInlineInput" name="sceneFolderInline" class="scene-folder-rename-input" data-rename-folder-input="${escapeHtml(group.name)}" value="${escapeHtml(group.name === "未分场" ? "" : group.name)}" placeholder="输入新场号" />`
        : `<span class="scene-folder-name" title="双击修改场号">${escapeHtml(group.name)}</span>`;

      const deleteBtnHtml =
        group.name !== "未分场" && !isEditing
          ? `<button type="button" class="scene-folder-delete-btn" data-delete-folder="${escapeHtml(group.name)}" title="删除场次（镜头将移回未分场）" aria-label="删除场次 ${escapeHtml(group.name)}">×</button>`
          : "";
      const boundExportDir = (state.data?.sceneExportDirs || {})[group.name] || "";
      const folderExportBtnHtml = !isEditing
        ? `<button type="button" class="scene-folder-export-btn ${boundExportDir ? "is-bound" : ""}" data-folder-export="${escapeHtml(group.name)}" title="${escapeHtml(boundExportDir ? `已绑定本地成片目录：${boundExportDir}（点击管理或同步）` : `为「${group.name}」绑定本地成片同步目录`)}" aria-label="设置场次 ${escapeHtml(group.name)} 导出目录">📁</button>`
        : "";

      return `
        <div class="scene-folder ${isExpanded ? "" : "collapsed"}" data-scene-folder="${escapeHtml(group.name)}">
          <div
            role="button"
            tabindex="0"
            class="scene-folder-header ${isSelected ? "selected" : ""}"
            data-select-folder="${escapeHtml(group.name)}"
          >
            <span class="scene-folder-chevron" data-folder-toggle="1" title="展开/收起">▾</span>
            <span class="scene-folder-icon">${isExpanded ? "📂" : "📁"}</span>
            ${nameHtml}
            <span class="scene-folder-count">${group.shots.length}</span>
            ${folderExportBtnHtml}
            ${deleteBtnHtml}
          </div>
          <div class="scene-folder-children">${childrenHtml}</div>
        </div>
      `;
    })
    .join("");

  listEl.innerHTML = creatingHtml + groupsHtml;

  if (state.creatingFolder) {
    const inp = listEl.querySelector("[data-create-folder-input]");
    if (inp) {
      inp.focus();
      bindFolderInlineInput(inp, { mode: "create" });
    }
  } else if (state.editingFolderName) {
    const inp = listEl.querySelector("[data-rename-folder-input]");
    if (inp) {
      inp.focus();
      inp.select();
      bindFolderInlineInput(inp, { mode: "rename", oldName: state.editingFolderName });
    }
  }
}

function bindDropzoneEl(dropEl, inputEl, onFiles, { clickToOpen = true } = {}) {
  if (!dropEl || !inputEl) return;
  if (clickToOpen) {
    dropEl.addEventListener("click", (e) => {
      if (e.target.closest("button, [data-zoom-url]")) return;
      inputEl.click();
    });
    dropEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        inputEl.click();
      }
    });
  }
  inputEl.addEventListener("change", () => {
    onFiles(inputEl.files);
    inputEl.value = "";
  });
  const hasDraggedFiles = (e) => {
    const types = Array.from(e.dataTransfer?.types || []);
    return types.includes("Files") && !types.includes("text/x-shot-id");
  };
  dropEl.addEventListener("dragover", (e) => {
    if (!hasDraggedFiles(e)) return;
    e.preventDefault();
    dropEl.classList.add("dragover");
  });
  dropEl.addEventListener("dragleave", () => dropEl.classList.remove("dragover"));
  dropEl.addEventListener("drop", (e) => {
    if (!hasDraggedFiles(e)) return;
    e.preventDefault();
    dropEl.classList.remove("dragover");
    onFiles(e.dataTransfer.files);
  });
}

/* Builder View */
function renderBuilderPresetBodyHtml(presets, selectedPresetIds) {
  const tabs = getPresetTabs(presets, state.data, state.builder.scene);
  if (!state.selectedSceneTab || state.selectedSceneTab === "全部" || !tabs.includes(state.selectedSceneTab)) {
    state.selectedSceneTab = (state.builder.scene || "").trim() || "通用";
  }
  const curTab = state.selectedSceneTab;
  const filteredPresets = presets.filter((p) => (p.scene || "通用") === curTab);
  const commonCount = presets.filter((p) => (p.scene || "通用") === "通用").length;

  return `
    <div class="scene-tabs">
      ${tabs
        .map((sc) => {
          const cnt = presets.filter((p) => (p.scene || "通用") === sc).length;
          return `
            <button type="button" class="scene-tab ${curTab === sc ? "active" : ""}" data-scene-tab="${escapeHtml(sc)}">
              ${escapeHtml(sc)}${cnt > 0 ? ` <span style="opacity:.7;font-size:11px;">(${cnt})</span>` : ""}
            </button>`;
        })
        .join("")}
    </div>
    <div class="preset-grid">
      ${
        filteredPresets.length
          ? filteredPresets
              .map(
                (p) => `
                  <div class="preset-card ${selectedPresetIds.has(p.id) ? "selected" : ""}" data-toggle-preset="${escapeHtml(p.id)}">
                    <img class="preset-thumb" src="${escapeHtml(p.mediaUrl)}" alt="${escapeHtml(p.name)}" />
                    <div class="preset-meta">
                      <div class="preset-name">${escapeHtml(p.name)}</div>
                      ${
                        p.roleHint
                          ? `<div class="preset-role-hint" title="${escapeHtml(p.roleHint)}">${escapeHtml(p.roleHint)}</div>`
                          : ""
                      }
                    </div>
                    <button type="button" class="preset-edit-btn" data-edit-preset="${escapeHtml(p.id)}" title="编辑角色与场号">✎</button>
                  </div>`
              )
              .join("")
          : `
            <div class="preset-empty-card">
              <span>「<strong>${escapeHtml(curTab)}</strong>」暂无专属角色</span>
              <div class="preset-empty-actions">
                <button type="button" class="btn btn-primary btn-sm" id="addPresetForTabBtn">+ 添加角色</button>
                ${
                  curTab !== "通用" && commonCount > 0
                    ? `<button type="button" class="btn btn-secondary btn-sm" data-scene-tab="通用">查看通用角色 (${commonCount})</button>`
                    : ""
                }
              </div>
            </div>`
      }
    </div>
  `;
}

function renderBuilderView() {
  const main = document.getElementById("mainPane");
  const d = state.data;
  const b = state.builder;
  if (!b.shotIdTouched) {
    b.shotId = suggestNextShotId(d?.shots || []);
  }

  const presets = d?.presets || [];
  const selectedPresetIds = new Set(b.imageItems.map(getBuilderItemPresetId).filter(Boolean));
  const orderedItems = buildOrderedBuilderAssets();
  const syncedPrompt = syncTokensWithAssets(b.userPromptTokens, b.userPrompt, orderedItems);
  b.userPromptTokens = syncedPrompt.tokens;
  b.userPrompt = syncedPrompt.plainText;
  const uploadedImgCount = b.imageItems.filter((it) => !getBuilderItemPresetId(it)).length;

  const videoSlotHtml = b.videoItem
    ? `
      <div id="videoDropzone" class="video-loaded-box">
        <input id="videoFileInput" type="file" accept="video/*" hidden />
        <div class="video-loaded-thumb"
          data-zoom-url="${escapeHtml(b.videoItem.url)}"
          data-zoom-kind="video"
          data-zoom-caption="${escapeHtml(b.videoItem.name)}"
          title="点击放大播放">
          <video src="${escapeHtml(b.videoItem.url)}" muted playsinline preload="metadata"></video>
          <div class="asset-video-play">▶</div>
        </div>
        <div class="video-loaded-info">
          <div class="video-loaded-name" title="${escapeHtml(b.videoItem.name)}">${escapeHtml(b.videoItem.name)}</div>
          <div class="video-loaded-actions">
            ${
              b.videoItem.meta?.suggestedDuration
                ? `<span id="videoDurationPill" class="pill pill-purple">${escapeHtml(String(b.videoItem.meta.suggestedDuration))}s</span>`
                : `<span id="videoDurationPill" class="pill pill-muted">已载入</span>`
            }
            <button id="replaceVideoBtn" class="btn btn-secondary btn-sm" type="button">更换</button>
            <button class="btn btn-danger-ghost btn-sm" type="button" data-remove-video="1">移除</button>
          </div>
        </div>
      </div>`
    : `
      <div id="videoDropzone" class="split-dropzone is-video" tabindex="0">
        <input id="videoFileInput" type="file" accept="video/*" hidden />
        <div class="dropzone-icon">▶</div>
        <div class="dropzone-title">拖入或选择原片视频</div>
        <div class="dropzone-sub">MP4 / MOV</div>
      </div>`;

  main.innerHTML = `
    <div class="stack-lg is-builder">
      <section class="card">
        <div class="form-row-3">
          <label class="field">
            <span>镜头编号 *</span>
            <input id="builderShotId" type="text" value="${escapeHtml(b.shotId)}" placeholder="如 S02" />
          </label>
          <label class="field">
            <span>镜头名称（可选，用于下载命名）</span>
            <input id="builderTitle" type="text" value="${escapeHtml(b.title || "")}" placeholder="如：室内对峙全景" />
          </label>
          <label class="field">
            <span>所属场号</span>
            <input id="builderScene" type="text" list="sceneDatalist" value="${escapeHtml(b.scene)}" placeholder="如：第75场（可选）" />
          </label>
        </div>

        <div class="upload-split-grid builder-top-divider">
          <div class="upload-pane">
            <div class="upload-pane-head">
              <span class="upload-pane-title">要洗的视频</span>
              <span class="pill pill-purple">视频</span>
            </div>
            ${videoSlotHtml}
          </div>

          <div class="upload-pane">
            <div class="upload-pane-head">
              <span class="upload-pane-title">构图 / 参考图</span>
              ${
                uploadedImgCount > 0
                  ? `<span class="pill pill-blue">${uploadedImgCount} 张</span>`
                  : `<span class="pill pill-muted">可选多张</span>`
              }
            </div>
            <div id="imageDropzone" class="split-dropzone" tabindex="0">
              <input id="imageFilesInput" type="file" multiple accept="image/*" hidden />
              <div class="dropzone-icon">+</div>
              <div class="dropzone-title">拖入构图、场景或道具图</div>
              <div class="dropzone-sub">支持多张同时拖入</div>
            </div>
          </div>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <div class="card-title-group">
            <h2 class="card-title">角色库</h2>
          </div>
          <button id="openAddPresetBtn" class="btn btn-secondary btn-sm" type="button">+ 新增角色</button>
        </div>
        <div id="builderPresetBody">${renderBuilderPresetBodyHtml(presets, selectedPresetIds)}</div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2 class="card-title">提示词</h2>
          ${orderedItems.length ? `<span class="card-subtle-hint">已选 ${orderedItems.length} 项素材 · 点击图片可涂白</span>` : ""}
        </div>

        ${renderAssetStripHtml(orderedItems, {
          editable: true,
          hideWhenEmpty: true,
        })}

        ${renderWhiteoutBarHtml({
          prefix: "builder",
          parts: b.whiteoutParts,
          targetSlot: b.whiteoutTarget,
          imageItems: orderedItems.filter((it) => it.mediaType === "image"),
        })}

        <div class="prompt-editor-shell">
          <div id="builderPrompt" class="prompt-rich-editor" contenteditable="true" role="textbox" aria-multiline="true" data-placeholder="输入提示词，输入 @ 可引用已添加素材"></div>
        </div>

        <div class="builder-submit-bar">
          <button id="resetBuilderBtn" class="btn btn-secondary" type="button">清空</button>
          <button id="submitShotBtn" class="btn btn-primary" type="button">确认提交</button>
        </div>
      </section>
    </div>
  `;

  bindBuilderEvents();
}

function setBuilderVideoFile(vf) {
  if (!vf) return;
  if (state.builder.videoItem?.url?.startsWith("blob:")) {
    URL.revokeObjectURL(state.builder.videoItem.url);
  }
  const item = {
    id: uid(),
    kind: "video",
    file: vf,
    url: URL.createObjectURL(vf),
    name: vf.name,
    meta: null,
  };
  state.builder.videoItem = item;
  preflightFileInBackground(vf, (meta) => {
    if (state.builder.videoItem?.id === item.id) {
      state.builder.videoItem.meta = meta;
      if (state.view === "builder") {
        const pill = document.getElementById("videoDurationPill");
        if (pill && meta?.suggestedDuration) {
          pill.className = "pill pill-purple";
          pill.textContent = `${meta.suggestedDuration}s`;
        }
        renderBuilderAssetStripOnly();
      }
    }
  });
  renderBuilderView();
}

function addBuilderImageFiles(images) {
  if (!images.length) return;
  // Insert uploaded scene/comp images before trailing presets so scene frames naturally lead 图片1
  const firstPresetIdx = state.builder.imageItems.findIndex((it) => Boolean(getBuilderItemPresetId(it)));
  const insertAt = firstPresetIdx >= 0 ? firstPresetIdx : state.builder.imageItems.length;
  const newItems = images.map((imgFile) => {
    const item = {
      id: uid(),
      kind: "upload",
      file: imgFile,
      url: URL.createObjectURL(imgFile),
      label: stripExt(imgFile.name) || "图片",
      roleHint: "",
      meta: null,
    };
    preflightFileInBackground(imgFile, (meta) => {
      const found = state.builder.imageItems.find((x) => x.id === item.id);
      if (found) {
        found.meta = meta;
        if (state.view === "builder") renderBuilderAssetStripOnly();
      }
    });
    return item;
  });
  state.builder.imageItems.splice(insertAt, 0, ...newItems);
  renderBuilderView();
}

function bindBuilderEvents() {
  clearMentionCleanups();

  const shotIdEl = document.getElementById("builderShotId");
  const titleInputEl = document.getElementById("builderTitle");
  const sceneEl = document.getElementById("builderScene");
  const promptEl = document.getElementById("builderPrompt");
  const videoDropEl = document.getElementById("videoDropzone");
  const videoInputEl = document.getElementById("videoFileInput");
  const imageDropEl = document.getElementById("imageDropzone");
  const imageInputEl = document.getElementById("imageFilesInput");

  shotIdEl.addEventListener("input", () => {
    const raw = shotIdEl.value;
    state.builder.shotId = raw;
    state.builder.shotIdTouched = Boolean(raw.trim());
  });

  titleInputEl?.addEventListener("input", () => {
    state.builder.title = titleInputEl.value;
  });

  const refreshBuilderPresetDom = () => {
    const bodyEl = document.getElementById("builderPresetBody");
    if (!bodyEl) return;
    const presets = state.data?.presets || [];
    const selectedIds = new Set(
      state.builder.imageItems.map(getBuilderItemPresetId).filter(Boolean)
    );
    bodyEl.innerHTML = renderBuilderPresetBodyHtml(presets, selectedIds);
    bindBuilderPresetBodyEvents(bodyEl);
  };

  sceneEl.addEventListener("input", () => {
    const val = sceneEl.value.trim();
    state.builder.scene = val;
    state.builder.sceneTouched = Boolean(val);
    const nextTab = val && val !== "未分场" ? val : "通用";
    if (state.selectedSceneTab !== nextTab) {
      state.selectedSceneTab = nextTab;
      refreshBuilderPresetDom();
    }
  });

  sceneEl.addEventListener("change", () => {
    const val = sceneEl.value.trim();
    const nextTab = val && val !== "未分场" ? val : "通用";
    if (state.selectedSceneTab !== nextTab) {
      state.selectedSceneTab = nextTab;
      refreshBuilderPresetDom();
    }
  });

  state.builderPromptEditor = initRichMentionEditor({
    editorEl: promptEl,
    initialTokens: state.builder.userPromptTokens,
    initialPlainText: state.builder.userPrompt,
    getItemsFn: () => buildOrderedBuilderAssets(),
    onChange: ({ tokens, plainText }) => {
      state.builder.userPromptTokens = tokens;
      state.builder.userPrompt = plainText;
    },
  });

  bindDropzoneEl(videoDropEl, videoInputEl, (fileList) => {
    const files = Array.from(fileList || []);
    const videos = files.filter(isVideoFile);
    if (!videos.length) {
      if (files.some(isImageFile)) showToast("图片请放到右侧「其他图片素材」");
      return;
    }
    setBuilderVideoFile(videos[0]);
  });

  document.getElementById("replaceVideoBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    videoInputEl.click();
  });

  bindDropzoneEl(imageDropEl, imageInputEl, (fileList) => {
    const files = Array.from(fileList || []);
    const images = files.filter(isImageFile);
    if (!images.length) {
      if (files.some(isVideoFile)) showToast("要洗的视频请放到左侧「要洗的视频」");
      return;
    }
    if (files.some(isVideoFile)) {
      showToast("已添加图片；视频请拖到左侧「要洗的视频」");
    }
    addBuilderImageFiles(images);
  });

  function bindBuilderPresetBodyEvents(rootEl) {
    rootEl.querySelectorAll("[data-scene-tab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.selectedSceneTab = btn.dataset.sceneTab;
        renderBuilderView();
      });
    });

    rootEl.querySelector("#addPresetForTabBtn")?.addEventListener("click", () => {
      const targetScene = state.selectedSceneTab || state.builder.scene || "通用";
      openPresetModal(null, targetScene);
    });

    rootEl.querySelectorAll("[data-edit-preset]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const p = (state.data?.presets || []).find((x) => x.id === btn.dataset.editPreset);
        if (p) openPresetModal(p, p.scene || "通用");
      });
    });

    rootEl.querySelectorAll("[data-toggle-preset]").forEach((card) => {
      card.addEventListener("click", () => {
        const pid = card.dataset.togglePreset;
        const existingIdx = state.builder.imageItems.findIndex(
          (it) => getBuilderItemPresetId(it) === pid
        );
        if (existingIdx >= 0) {
          const [removed] = state.builder.imageItems.splice(existingIdx, 1);
          if (removed?.url?.startsWith("blob:")) URL.revokeObjectURL(removed.url);
          if (removed?.originalUrl?.startsWith("blob:") && removed.originalUrl !== removed.url) {
            URL.revokeObjectURL(removed.originalUrl);
          }
        } else {
          const preset = (state.data?.presets || []).find((p) => p.id === pid);
          if (!preset) return;
          state.builder.imageItems.push({
            id: uid(),
            kind: "preset",
            presetId: preset.id,
            url: preset.mediaUrl,
            label: preset.name,
            roleHint: preset.roleHint || "",
            scene: preset.scene || "通用",
            meta: preset.meta || null,
          });
        }
        renderBuilderView();
      });
    });
  }

  bindBuilderPresetBodyEvents(document.getElementById("builderPresetBody"));

  // Add preset button
  document.getElementById("openAddPresetBtn").addEventListener("click", () => {
    const defaultScene = state.builder.scene || state.selectedSceneTab || "通用";
    openPresetModal(null, defaultScene);
  });

  bindBuilderStripButtons();

  bindWhiteoutBarEvents({
    prefix: "builder",
    getParts: () => state.builder.whiteoutParts,
    setParts: (parts) => {
      state.builder.whiteoutParts = parts;
    },
    setTarget: (target) => {
      state.builder.whiteoutTarget = target;
    },
  });

  document.getElementById("resetBuilderBtn").addEventListener("click", () => {
    clearBuilderDraft();
    renderBuilderView();
  });

  document.getElementById("submitShotBtn").addEventListener("click", () => submitBuilderShot(false));
}

function bindBuilderStripButtons() {
  document.querySelectorAll("[data-move-img]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = Number(btn.dataset.moveImg);
      const dir = Number(btn.dataset.dir);
      const target = idx + dir;
      if (target < 0 || target >= state.builder.imageItems.length) return;
      const arr = state.builder.imageItems;
      [arr[idx], arr[target]] = [arr[target], arr[idx]];
      renderBuilderView();
    });
  });

  document.querySelectorAll("[data-remove-img]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = Number(btn.dataset.removeImg);
      const removed = state.builder.imageItems.splice(idx, 1)[0];
      if (removed?.url?.startsWith("blob:")) URL.revokeObjectURL(removed.url);
      if (removed?.originalUrl?.startsWith("blob:") && removed.originalUrl !== removed.url) {
        URL.revokeObjectURL(removed.originalUrl);
      }
      renderBuilderView();
    });
  });

  document.querySelectorAll("[data-remove-video]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (state.builder.videoItem?.url?.startsWith("blob:")) {
        URL.revokeObjectURL(state.builder.videoItem.url);
      }
      state.builder.videoItem = null;
      renderBuilderView();
    });
  });
}

function renderBuilderAssetStripOnly() {
  const ordered = buildOrderedBuilderAssets();
  const wrap = document.querySelector(".main-pane .asset-strip-wrap");
  if (wrap) {
    const html = renderAssetStripHtml(ordered, {
      editable: true,
      hideWhenEmpty: true,
    });
    wrap.outerHTML = html;
    bindBuilderStripButtons();
  }
  const sel = document.getElementById("builderWhiteoutTarget");
  if (sel) {
    const imageItems = ordered.filter((it) => it.mediaType === "image");
    sel.disabled = !imageItems.length;
    sel.innerHTML = buildWhiteoutTargetOptionsHtml(imageItems, state.builder.whiteoutTarget);
  }
  state.builderPromptEditor?.syncAssets(ordered);
}

async function preflightFileInBackground(file, onResult) {
  try {
    const fd = new FormData();
    fd.append("file", file);
    const res = await apiJson("/api/preflight", { method: "POST", body: fd });
    onResult(res);
  } catch {
    // Non-fatal preflight failure
  }
}

function clearBuilderDraft() {
  if (state.builder.videoItem?.url?.startsWith("blob:")) {
    URL.revokeObjectURL(state.builder.videoItem.url);
  }
  for (const img of state.builder.imageItems) {
    if (img.url?.startsWith("blob:")) URL.revokeObjectURL(img.url);
    if (img.originalUrl?.startsWith("blob:") && img.originalUrl !== img.url) {
      URL.revokeObjectURL(img.originalUrl);
    }
  }
  const defaultFolder =
    state.selectedSceneFolder && state.selectedSceneFolder !== "未分场"
      ? state.selectedSceneFolder
      : "";
  state.builder = {
    shotId: suggestNextShotId(state.data?.shots || []),
    shotIdTouched: false,
    title: "",
    scene: defaultFolder,
    sceneTouched: false,
    userPrompt: "",
    userPromptTokens: "",
    videoItem: null,
    imageItems: [],
    whiteoutParts: [],
    whiteoutTarget: "",
  };
}

async function submitBuilderShot(forceOverwrite = false) {
  state.builderPromptEditor?.syncAssets(buildOrderedBuilderAssets());
  const b = state.builder;
  const shotId = (b.shotId || "").trim();
  if (!shotId) {
    showToast("请填写镜头编号，例如 S02");
    return;
  }
  if (!b.videoItem && !b.imageItems.length) {
    showToast("请至少拖入原片视频或选择/上传参考图片");
    return;
  }

  const exists = (state.data?.shots || []).some((s) => s.id === shotId);
  if (exists && !forceOverwrite) {
    openConfirmModal({
      title: `覆盖已有镜头 ${shotId}？`,
      message: `队列中已存在镜头「${shotId}」，继续提交将覆盖原有素材与提示词记录。`,
      okText: "覆盖提交",
      cancelText: "取消",
      danger: true,
      onConfirm: () => submitBuilderShot(true),
    });
    return;
  }

  const btn = document.getElementById("submitShotBtn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "提交中…";
  }

  try {
    const fd = new FormData();
    const activeCtx = getActiveCanvasContext();
    fd.append("shot_id", shotId);
    fd.append("title", (b.title || "").trim());
    fd.append("scene", (b.scene || "").trim());
    fd.append("platform", activeCtx.platform);
    fd.append("project_uuid", activeCtx.uuid || "");
    fd.append("project_name", activeCtx.name || "");
    fd.append("canvas_url", activeCtx.url || "");
    fd.append("notes", "");
    fd.append("user_prompt", b.userPrompt || "");
    fd.append("whiteout_parts_json", JSON.stringify(b.whiteoutParts || []));
    fd.append("whiteout_target", b.whiteoutTarget || "");
    fd.append("auto_confirm", "true");
    if (exists) {
      fd.append("overwrite", "true");
    }

    if (b.videoItem) {
      fd.append("source_video", b.videoItem.file);
    }

    const specs = [];
    let uploadIdx = 0;
    for (const img of b.imageItems) {
      if (img.kind === "preset") {
        specs.push({
          kind: "preset",
          id: img.presetId,
          label: img.label,
          roleHint: img.roleHint || "",
        });
      } else {
        specs.push({
          kind: "upload",
          uploadIndex: uploadIdx,
          label: img.label,
          roleHint: img.roleHint || "",
        });
        fd.append("char_images", img.file);
        uploadIdx += 1;
      }
    }
    fd.append("ordered_image_specs_json", JSON.stringify(specs));

    const res = await apiJson("/api/shots", { method: "POST", body: fd });
    showToast(`已提交 ${res.shot.id}`);
    state.selectedShotId = res.shot.id;
    state.selectedSceneFolder = getShotFolderName(res.shot);
    state.expandedFolders.add(state.selectedSceneFolder);
    state.view = "detail";
    await refreshState();
    clearBuilderDraft();
  } catch (err) {
    showToast(`提交失败: ${err.message}`);
  } finally {
    const bEl = document.getElementById("submitShotBtn");
    if (bEl) {
      bEl.disabled = false;
      bEl.textContent = "确认提交";
    }
  }
}

/* Detail View */
function clearDetailDraftAttachments() {
  for (const att of state.detailDraft.attachments || []) {
    if (att.url?.startsWith("blob:")) URL.revokeObjectURL(att.url);
  }
  state.detailDraft.attachments = [];
}

function syncDetailDraftForShot(shot) {
  if (!shot) return;
  const serverPrompt = shot.pendingPromptEdit || shot.promptText || shot.userPrompt || "";
  if (state.detailDraft.shotId !== shot.id) {
    clearDetailDraftAttachments();
    state.detailDraft = {
      shotId: shot.id,
      lastSyncedPrompt: serverPrompt,
      editedPrompt: serverPrompt,
      editedPromptTokens: "",
      comment: "",
      commentTokens: "",
      attachments: [],
      whiteoutParts: [],
      whiteoutTarget: "",
      compareSource: false,
      previewVersion: "auto",
    };
  } else if (
    state.detailDraft.editedPrompt === state.detailDraft.lastSyncedPrompt &&
    serverPrompt !== state.detailDraft.lastSyncedPrompt
  ) {
    state.detailDraft.lastSyncedPrompt = serverPrompt;
    state.detailDraft.editedPrompt = serverPrompt;
    state.detailDraft.editedPromptTokens = "";
  }
}

function renderConfirmationBanner(shot) {
  const req = shot.confirmationRequest;
  if (shot.status !== "needs_confirmation" || !req) return "";
  const rawItems = req.items || req.images || [];
  const thumbs = rawItems
    .map((it) => {
      const url = withMediaVersion(it.mediaUrl || "", it);
      const woBadge = it.whiteoutApplied
        ? `<span class="asset-masked-badge">已涂白</span>`
        : "";
      return `
        <div class="asset-thumb-card" style="width:128px;">
          <div class="asset-thumb-media"
            data-zoom-url="${escapeHtml(url)}"
            data-zoom-kind="image"
            data-zoom-caption="${escapeHtml(it.label || "确认图")}">
            <img src="${escapeHtml(url)}" alt="${escapeHtml(it.label || "")}" />
            ${woBadge}
          </div>
          <div class="asset-thumb-footer">
            <div class="asset-thumb-label">${escapeHtml(it.label || "")}</div>
          </div>
        </div>`;
    })
    .join("");

  return `
    <section class="confirmation-banner">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
        <strong>${escapeHtml(req.title || "需要确认后再继续")}</strong>
        <span class="pill pill-orange">待确认</span>
      </div>
      <div style="font-size:12.5px;color:var(--text-secondary);">${escapeHtml(req.message || "")}</div>
      ${thumbs ? `<div class="asset-thumb-strip">${thumbs}</div>` : ""}
      <div class="form-row-2">
        <label class="field">
          <span>补充说明（可选）</span>
          <input id="confirmCommentInput" type="text" placeholder="如：第2张图改用……" />
        </label>
        <label class="field">
          <span>上传替换图片（可选）</span>
          <input id="confirmFileInput" type="file" accept="image/*" />
        </label>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button id="rejectConfirmBtn" class="btn btn-danger-ghost btn-sm" type="button">驳回重做</button>
        <button id="approveConfirmBtn" class="btn btn-primary btn-sm" type="button">确认继续</button>
      </div>
    </section>
  `;
}

function renderErrorBanner(shot) {
  if (!shot || shot.status !== "error") return "";
  const errTag = getShotErrorTag(shot);
  const errCodeAttr = shot.errorCode ? `错误代码：${shot.errorCode}` : "执行异常";
  const errTypePill = `<span class="pill pill-err-tag" title="${escapeHtml(errCodeAttr)}">🏷 ${escapeHtml(errTag)}</span>`;
  const taskTag = shot.lastFailedTaskLabel
    ? `<span class="pill pill-red">失败环节：${escapeHtml(shot.lastFailedTaskLabel)}</span>`
    : `<span class="pill pill-red">需要处理</span>`;
  const reason = shot.errorReason || "后台执行异常中断，已有提示词与素材仍安全保留。";
  const solution = shot.errorSolution || "点击下方「↻ 重试刚才操作」重新排队，或点「↩ 恢复到稳定状态」。";
  const detail = shot.errorDetail || (shot.agentSummary && shot.agentSummary !== reason ? shot.agentSummary : "");
  const canResumeErr = Boolean(shot.canResumeCliChat && shot.cliConversationId);
  const errChatLabel = canResumeErr ? "💬 接手原对话排查" : "💬 开新对话排查";
  const errChatMode = canResumeErr ? "resume" : "new";
  return `
    <section class="error-banner" role="alert">
      <div class="error-banner-head">
        <div class="error-banner-title-group">
          <strong>❌ 执行出错</strong>
          ${errTypePill}
        </div>
        ${taskTag}
      </div>
      <div class="error-banner-row"><span class="error-banner-label">错误原因：</span>${escapeHtml(reason)}</div>
      <div class="error-banner-row error-banner-solution"><span class="error-banner-label">解决办法：</span>${escapeHtml(solution)}</div>
      ${detail ? `<div class="error-banner-detail">原始报错信息：${escapeHtml(detail)}</div>` : ""}
      <div class="error-banner-actions">
        <button id="openChatFromErrorBtn" class="btn btn-secondary btn-sm" type="button" data-chat-mode="${errChatMode}" title="打开独立终端前台对话直接接手排查此报错">${errChatLabel}</button>
        <button id="restoreErrorBtn" class="btn btn-secondary btn-sm" type="button" title="仅清除出错标记，恢复成片或画布稳态">↩ 恢复到稳定状态</button>
        <button id="recheckFromErrorBtn" class="btn btn-secondary btn-sm" type="button" title="由 Agent 核查画布与提示词状态后修复">🛠 重新自检修复</button>
        <button id="retryErrorBtn" class="btn btn-primary btn-sm" type="button" title="按失败前的队列动作重新执行一次">↻ 重试刚才操作</button>
      </div>
    </section>
  `;
}

function renderFeedbackAttachmentsHtml() {
  const list = state.detailDraft.attachments || [];
  if (!list.length) return "";
  const attachAssets = buildFeedbackAttachAssets();
  return `
    <div class="fb-attach-strip">
      ${list
        .map((att, idx) => {
          const slotTag = attachAssets[idx]?.slotTag || `新图${idx + 1}`;
          return `
            <div class="fb-attach-chip"
              data-zoom-url="${escapeHtml(att.url)}"
              data-zoom-kind="${att.kind}"
              data-zoom-caption="${escapeHtml(`${slotTag} · ${att.name}`)}">
              ${
                att.kind === "video"
                  ? `<video src="${escapeHtml(att.url)}" muted playsinline preload="metadata"></video>`
                  : `<img src="${escapeHtml(att.url)}" alt="${escapeHtml(att.name)}" />`
              }
              <span class="asset-slot-badge is-new-att ${att.kind === "video" ? "is-video" : ""}">${escapeHtml(slotTag)}</span>
              <button type="button" class="fb-attach-remove" data-remove-fb-att="${idx}" title="移除">×</button>
            </div>`;
        })
        .join("")}
    </div>
  `;
}

function formatVideoMetaText(vid) {
  if (!vid) return "";
  const m = vid.meta || {};
  const parts = [];
  if (m.width && m.height) parts.push(`${m.width}×${m.height}`);
  if (m.durationSec) parts.push(`${Number(m.durationSec).toFixed(1).replace(/\.0$/, "")}s`);
  if (vid.watermark === false) parts.push("无水印");
  else if (vid.watermark === true) parts.push("含水印");
  if (vid.downloadedAt) parts.push(vid.downloadedAt);
  return parts.join(" · ");
}

function getShotBaselinePrompt(shot) {
  return (shot?.pendingPromptEdit || shot?.promptText || shot?.userPrompt || "").trim();
}

function isPromptDirty(shot) {
  if (!shot) return false;
  const baseline = getShotBaselinePrompt(shot);
  const current = (state.detailDraft.editedPrompt || "").trim();
  return Boolean(current !== baseline);
}

function getDetailEditedPromptIfChanged(shot) {
  const mentionItems = buildDetailMentionAssets(shot);
  state.detailPromptEditor?.syncAssets(mentionItems);
  state.fbCommentEditor?.syncAssets(mentionItems);
  const baseline = getShotBaselinePrompt(shot);
  const edited = state.detailDraft.editedPrompt.trim();
  return edited && edited !== baseline ? state.detailDraft.editedPrompt : "";
}

function getDisplayRevisionDirection(shot) {
  const raw = String(shot?.latestRevisionDirection || "").trim();
  if (!raw) return "";
  const exactNoise = new Set([
    "首版基于原片与用户描述梳理上画布",
    "历史已执行修改（该早先轮次建立于修改方向字段上线前）",
    "已同步修改",
  ]);
  if (exactNoise.has(raw)) return "";
  if (
    raw.startsWith("初始创建镜头") ||
    raw.startsWith("草稿保存") ||
    raw.startsWith("首版提审记录") ||
    raw.startsWith("已收到本轮修改意见，等待 Agent")
  ) {
    return "";
  }
  return raw;
}

function getEffectiveShotChangeInfo(shot) {
  const dirty = isPromptDirty(shot);
  const hasDraft = Boolean(shot?.draftVideo?.mediaUrl || shot?.finalVideo?.mediaUrl);
  if (dirty) {
    return {
      hasChanges: true,
      hasDraft,
      dirty: true,
      badgeCls: "is-changed",
      badgeText: hasDraft ? "● 提示词已修改，待同步" : "● 提示词已修改",
      badgeTitle: shot?.generationChangeSummary || "提示词有本地未同步修改",
    };
  }
  if (!hasDraft || shot?.generationChangeState === "never_generated") {
    return {
      hasChanges: true,
      hasDraft: false,
      dirty: false,
      badgeCls: "is-unbuilt",
      badgeText: "○ 尚未生成样片",
      badgeTitle: "当前尚未生成 480P 样片",
    };
  }
  if (shot?.hasChangesSinceLastGeneration) {
    return {
      hasChanges: true,
      hasDraft: true,
      dirty: false,
      badgeCls: "is-changed",
      badgeText: "● 已有新改动，待重新生成",
      badgeTitle: shot?.generationChangeSummary || "提示词或素材已更新，尚未重新生成",
    };
  }
  return {
    hasChanges: false,
    hasDraft: true,
    dirty: false,
    badgeCls: "is-unchanged",
    badgeText: "✓ 与当前样片一致",
    badgeTitle: "提示词、参考图与涂白均与当前成片一致（无新改动）",
  };
}

function updatePromptHeaderUi(shot) {
  const badgeEl = document.getElementById("promptSyncBadge");
  const resetBtn = document.getElementById("resetPromptBtn");
  const updateBtn = document.getElementById("updatePromptBtn");
  const hintEl = document.getElementById("promptFooterHint");
  const changeBadgeEl = document.getElementById("shotChangeBadge");
  if (!shot) return;

  const isPending =
    shot.status === "prompt_update_requested" ||
    (shot.status === "processing" && shot.processingTaskType === "prompt_update_requested");
  const dirty = isPromptDirty(shot);

  if (badgeEl) {
    badgeEl.className = "pill";
    if (isPending) {
      badgeEl.classList.add("pill-purple");
      badgeEl.textContent = "等待同步";
    } else if (dirty) {
      badgeEl.classList.add("pill-orange");
      badgeEl.textContent = "已改未同步";
    } else {
      badgeEl.classList.add("pill-muted");
      badgeEl.textContent = "已与画布一致";
    }
  }
  if (changeBadgeEl) {
    const info = getEffectiveShotChangeInfo(shot);
    changeBadgeEl.className = `change-badge ${info.badgeCls}`;
    changeBadgeEl.textContent = info.badgeText;
    changeBadgeEl.title = info.badgeTitle || "";
  }
  if (hintEl) {
    if (isPending) {
      hintEl.textContent = "等待 Agent 同步到画布…";
      hintEl.className = "prompt-editor-footer-hint";
    } else if (dirty) {
      hintEl.textContent = "有未同步修改（仅更新画布，不自动出片）";
      hintEl.className = "prompt-editor-footer-hint is-dirty";
    } else {
      hintEl.textContent = "输入 @ 引用素材 · 双击标签预览";
      hintEl.className = "prompt-editor-footer-hint";
    }
  }
  if (resetBtn) {
    resetBtn.classList.toggle("hidden", !dirty);
  }
  if (updateBtn) {
    updateBtn.disabled = isPending;
    updateBtn.className = `btn btn-sm ${dirty ? "btn-primary" : "btn-secondary"}`;
    updateBtn.textContent = isPending ? "同步中…" : dirty ? "✓ 保存并同步画布" : "同步到画布";
  }
}

function renderShotActionBarHtml(shot) {
  const st = shot.status;
  const info = getEffectiveShotChangeInfo(shot);
  const verCount = (shot.versionHistory || []).length;
  const hasNode = Boolean(shot.libtvSampleNodeKey);
  const platLabel = getPlatformLabel(shot.platform);

  const historyBtn = `
    <button id="openHistoryModalBtn" class="btn btn-secondary btn-sm" type="button" title="查看全部历史修改意见、对话方向与提示词版本，支持回滚">
      🕒 历史回滚${verCount > 0 ? ` (${verCount})` : ""}
    </button>`;

  const syncBtn = hasNode
    ? `<button id="syncLibtvBtn" class="btn btn-secondary btn-sm" type="button" title="从 ${escapeHtml(platLabel)} 画布拉取最新生成的样片">↓ 同步成片</button>`
    : "";

  const rtSnap = isShotInQueueOrProcessing(shot) ? getShotRuntimeSnapshot(shot) : null;
  const rtStalled = rtSnap && (rtSnap.health === "stalled" || rtSnap.health === "offline");
  const runtimeStatusBtn = rtSnap
    ? `<button id="openShotRuntimeBtn" class="btn ${rtStalled ? "btn-warning" : "btn-secondary"} btn-sm" type="button" title="查看后台 Agent 当前处理进度、进程是否存活或强制刷新重试">${rtStalled ? "⚠ 卡住·重试" : "📡 处理状态"}</button>`
    : "";

  const canResumeChat = Boolean(shot.canResumeCliChat && shot.cliConversationId);
  const chatMainLabel = canResumeChat ? "💬 接手原对话" : "💬 前台开聊";
  const chatMainMode = canResumeChat ? "resume" : "new";
  const chatMainTitle = canResumeChat
    ? `打开终端前台窗口，直接续接该镜头后台独立对话（${shot.cliConversationId.slice(0, 8)}…）`
    : "打开终端独立前台 agy 对话，并自动带入本镜提示词、画布与参考素材上下文";
  const resumeMenuBtn = canResumeChat
    ? `<button id="chatResumeMenuBtn" class="more-menu-item" type="button" role="menuitem">💬 续接原对话（终端 · ${escapeHtml(shot.cliConversationId.slice(0, 8))}…）</button>`
    : `<button id="chatResumeMenuBtn" class="more-menu-item" type="button" role="menuitem" disabled title="该镜头尚未沉淀后台 agy 会话（首次 AI 处理后即可续接）">💬 续接原对话（暂无记录）</button>`;

  const chatSplitHtml = `
    <div id="shotChatWrap" class="chat-split-wrap">
      <button id="openShotChatMainBtn" class="btn btn-secondary btn-sm chat-split-main" type="button" data-chat-mode="${chatMainMode}" title="${escapeHtml(chatMainTitle)}">${chatMainLabel}</button>
      <button id="shotChatMenuToggleBtn" class="btn btn-secondary btn-sm chat-split-arrow${state.shotChatMenuOpen ? " is-open" : ""}" type="button" aria-haspopup="menu" aria-expanded="${state.shotChatMenuOpen ? "true" : "false"}" title="更多前台转聊方式">▾</button>
      <div id="shotChatMenu" class="chat-popover${state.shotChatMenuOpen ? "" : " hidden"}" role="menu" aria-label="转到前台独立对话选项">
        ${resumeMenuBtn}
        <button id="chatNewTerminalMenuBtn" class="more-menu-item" type="button" role="menuitem">🆕 新开独立前台对话（终端 agy）</button>
        <button id="chatHandoffDesktopMenuBtn" class="more-menu-item" type="button" role="menuitem">📋 复制上下文 + 唤起 Antigravity</button>
      </div>
    </div>`;

  const finalInProgress = isFinalInProgress(shot);
  const passed = isPassedFinal(shot);
  const outdatedFinal = isOutdatedFinal(shot);

  let primaryBtn = "";
  if (st === "draft" || st === "drafting") {
    primaryBtn = `<button id="confirmShotBtn" class="btn btn-primary btn-sm" type="button">✓ 确认提交</button>`;
  } else if (finalInProgress) {
    primaryBtn = `<button id="runShotBtn" class="btn btn-secondary btn-sm" type="button" disabled title="当前正在转为 1080P 正片，已锁定防止误触重跑样片">⏳ 正片生成中…</button>`;
  } else if (st === "run_requested") {
    primaryBtn = `<button id="runShotBtn" class="btn btn-primary btn-sm" type="button" disabled>待 Agent 运行…</button>`;
  } else if (st === "processing" && shot.processingTaskType === "run_requested") {
    primaryBtn = `<button id="runShotBtn" class="btn btn-primary btn-sm" type="button" disabled>正在生成…</button>`;
  } else if (
    ["ready_on_canvas", "draft_ready", "prompt_update_requested", "feedback_queued", "error", "final_ready", "final_done"].includes(st)
  ) {
    let label = "▶ 生成样片";
    let btnCls = "btn-primary";
    if (passed) {
      label = "↻ 重洗样片（已过条）";
      btnCls = "btn-secondary";
    } else if (info.hasDraft) {
      label = info.hasChanges ? "▶ 按新修改重新生成" : "▶ 重新生成";
    }
    primaryBtn = `<button id="runShotBtn" class="btn ${btnCls} btn-sm" type="button" title="在 ${escapeHtml(platLabel)} 上触发 480P 生成，并在出片后下载回看">${label}</button>`;
  }

  const passedMetaHtml = passed
    ? `<span class="passed-lock-badge" title="该镜头已完成 1080P 正片，已过条">✓ 已过条 · 1080P 正片已定版</span>`
    : finalInProgress
    ? `<span class="pill pill-purple">⏳ 正在转为 1080P 正片</span>`
    : outdatedFinal
    ? `<span class="outdated-final-badge" title="已二次确认解锁定版修改，原有 1080P 正片仍保留对照">↺ 已解锁修改 · 旧版 1080P 已归档</span>`
    : "";

  return `
    <section class="shot-action-bar" aria-label="整镜全局操作栏">
      <div class="shot-action-meta">
        ${passedMetaHtml}
        <span id="shotChangeBadge" class="change-badge ${info.badgeCls}" title="${escapeHtml(info.badgeTitle || "")}">${escapeHtml(passed && !info.hasChanges ? "✓ 与定版正片一致" : info.badgeText)}</span>
      </div>
      <div class="shot-action-btns">
        ${historyBtn}
        ${syncBtn}
        ${runtimeStatusBtn}
        ${chatSplitHtml}
        ${primaryBtn}
      </div>
    </section>
  `;
}

function renderVideoReviewCard(shot) {
  const draftVid = shot.draftVideo?.mediaUrl ? shot.draftVideo : null;
  const finalVid = shot.finalVideo?.mediaUrl ? shot.finalVideo : null;
  const hasBoth = Boolean(draftVid && finalVid);
  const outdatedFinal = isOutdatedFinal(shot);
  const draftRefreshed = Boolean(shot.draftUpdatedSinceFinalReopen);
  const changeInfo = getEffectiveShotChangeInfo(shot);
  let activeVer = state.detailDraft.previewVersion;
  if (activeVer === "auto") {
    activeVer = finalVid && !(outdatedFinal && (draftRefreshed || !changeInfo.hasChanges)) ? "final" : "draft";
  }
  if (activeVer === "final" && !finalVid) activeVer = "draft";
  if (activeVer === "draft" && !draftVid) activeVer = "final";

  const activeVid = activeVer === "final" ? finalVid : draftVid;
  const draftTabLabel = outdatedFinal && draftRefreshed ? "新版 480P 样片" : "480P 样片";
  const finalTabLabel = outdatedFinal ? "旧版 1080P 归档" : "1080P 正片";
  const activeLabel = activeVer === "final" ? finalTabLabel : draftTabLabel;
  const srcVid = shot.sourceVideo?.mediaUrl ? shot.sourceVideo : null;
  const canCompare = Boolean(activeVid && srcVid);
  const isComparing = canCompare && Boolean(state.detailDraft.compareSource);

  const versionTabsHtml = hasBoth
    ? `
      <div class="video-review-tabs">
        <button type="button" class="btn btn-sm ${activeVer === "draft" ? "btn-primary" : "btn-secondary"}" data-preview-ver="draft">${draftTabLabel}</button>
        <button type="button" class="btn btn-sm ${activeVer === "final" ? "btn-primary" : "btn-secondary"}" data-preview-ver="final">${finalTabLabel}</button>
      </div>
    `
    : "";

  const isSteadyVideoState = ["draft_ready", "final_ready", "final_done"].includes(shot.status);
  const summaryHtml =
    shot.agentSummary && (!activeVid || !isSteadyVideoState)
      ? `<div class="video-review-summary">${escapeHtml(shot.agentSummary)}</div>`
      : "";

  if (!activeVid) {
    const platLabel = getPlatformLabel(shot.platform);
    let hintText = "点击上方「▶ 生成样片」后，成片会自动下载到这里供看片。";
    if (shot.status === "run_requested" || (shot.status === "processing" && shot.processingTaskType === "run_requested")) {
      hintText = "已触发生成，Agent 正在等待出片并下载到本地…";
    } else if (shot.status === "prompt_update_requested") {
      hintText = `已记录修改后的提示词，正在同步到 ${platLabel} 画布…`;
    } else if (shot.status === "queued" || shot.status === "processing" || shot.status === "recheck_requested") {
      hintText = "Agent 正在准备画布节点与景深视频，就绪后即可生成样片。";
    }
    return `
      <section class="card">
        <div class="card-head">
          <div class="card-title-group">
            <h2 class="card-title">成片看片</h2>
          </div>
        </div>
        <div style="font-size:12.5px;color:var(--text-secondary);line-height:1.55;">${escapeHtml(hintText)}</div>
        ${summaryHtml}
      </section>
    `;
  }

  const metaStr = formatVideoMetaText(activeVid);
  const srcMetaStr = formatVideoMetaText(srcVid);
  const finalInProgress = isFinalInProgress(shot);
  const passed = isPassedFinal(shot);
  const canApproveFinal = Boolean(
    draftVid &&
      !finalInProgress &&
      !passed &&
      (!finalVid || (outdatedFinal && (draftRefreshed || !changeInfo.hasChanges)))
  );
  const downloadName =
    activeVer === "final"
      ? shot.downloadName1080p || buildClientDownloadFilename(shot, "1080p")
      : shot.downloadName480p || buildClientDownloadFilename(shot, "480p");
  const downloadHref = `${activeVid.mediaUrl}${
    String(activeVid.mediaUrl || "").includes("?") ? "&" : "?"
  }download_name=${encodeURIComponent(downloadName)}`;
  const syncStatus = shot.exportSyncStatus1080p || (finalVid ? "unbound" : "no_final");
  const syncTargetDir = (shot.resolvedExportDir || "").trim();
  const syncedFilePath = shot.localExports?.["1080p"]?.filePath || "";
  const localSyncBtnHtml = finalVid
    ? syncStatus === "synced"
      ? `<button id="syncLocalExportBtn" class="btn btn-secondary btn-sm is-synced" type="button" title="已自动覆盖同步到本地目录：${escapeHtml(syncedFilePath || `${syncTargetDir}/${shot.downloadName1080p}`)}（点击可手动重刷校验）">✓ 已同步本地 1080P</button>`
      : syncStatus === "needs_sync"
      ? `<button id="syncLocalExportBtn" class="btn btn-primary btn-sm" type="button" title="检测到 1080P 画面或名称有更新，点击立即覆盖写入 ${escapeHtml(`${syncTargetDir}/${shot.downloadName1080p || downloadName}`)}">↻ 更新本地 1080P</button>`
      : `<button id="syncLocalExportBtn" class="btn btn-secondary btn-sm" type="button" title="绑定本地场次或默认成片目录，后续修改或重转 1080P 将自动覆盖更新">📁 同步到文件夹</button>`
    : "";
  const replaceFinalBtnHtml = finalVid
    ? `<button id="replaceFinalVideoBtn" class="btn btn-secondary btn-sm" type="button" title="选择本机修改过的新 1080P 视频替换当前正片，并自动覆盖同步到本地目录">↑ 替换 1080P</button><input id="replaceFinalVideoInput" type="file" accept="video/*" hidden />`
    : "";
  const localSyncBannerHtml =
    finalVid && syncStatus === "synced" && syncedFilePath
      ? `<div class="local-sync-status-line is-synced">📁 本地同步目录已就绪：<code>${escapeHtml(syncedFilePath)}</code></div>`
      : finalVid && syncStatus === "needs_sync" && syncTargetDir
      ? `<div class="local-sync-status-line is-needs-sync">⚠ 本地成片待同步更新：<code>${escapeHtml(syncTargetDir)}/${escapeHtml(shot.downloadName1080p || downloadName)}</code></div>`
      : "";
  const finalProgressBannerHtml = finalInProgress
    ? `<div class="final-progress-banner">⏳ <strong>正在转为 1080P 正片</strong>：${escapeHtml(shot.agentSummary || "已提交转正片队列，完成后将自动归档并标为「已过条」，无需重复点击。")}</div>`
    : "";
  const passedBannerHtml =
    passed && finalVid
      ? `<div class="passed-status-banner">✓ <strong>本镜已过条（1080P 正片已归档定版）</strong>，如需继续修改将弹出二次确认弹窗防误触。</div>`
      : outdatedFinal && finalVid
      ? `<div class="outdated-final-banner">↺ <strong>已解锁定版修改</strong>：上方可对照旧版 1080P 正片归档；${
          draftRefreshed || !changeInfo.hasChanges
            ? "新版 480P 样片已就绪，满意后点右下角「✦ 重新转 1080P 正片」即可重新定版。"
            : "请先点上方「▶ 按新修改重新生成」产出新版 480P 样片，出片后即可重新转 1080P 正片。"
        }</div>`
      : "";
  const finalActionHtml = canApproveFinal
    ? `<button id="approveFinalBtn" class="btn btn-primary btn-sm" type="button">${
        finalVid && outdatedFinal ? "✦ 重新转 1080P 正片" : "✦ 转 1080P 正片"
      }</button>`
    : finalInProgress
    ? `<button id="approveFinalBtn" class="btn btn-secondary btn-sm" type="button" disabled title="正片正在生成中，已锁定防误触">⏳ 转 1080P 正片中…</button>`
    : passed
    ? `<span class="passed-lock-badge" title="1080P 正片已完成归档">✓ 已过条 · 1080P 正片</span>`
    : outdatedFinal
    ? `<span class="outdated-final-badge" title="请先按新修改重新生成 480P 样片后再重新转 1080P 正片">↺ 待重洗 480P 后重转正片</span>`
    : "";

  return `
    <section class="card">
      <div class="card-head">
        <div class="card-title-group">
          <h2 class="card-title">成片看片</h2>
          <span class="pill ${activeVer === "final" ? "pill-passed" : "pill-blue"}">${activeLabel}</span>
        </div>
        ${versionTabsHtml}
      </div>

      ${finalProgressBannerHtml}

      <div class="video-review-grid ${isComparing ? "is-compare" : ""}">
        ${
          isComparing
            ? `
              <div class="video-review-slot">
                <div class="video-review-slot-head">
                  <span>原片参考</span>
                  ${srcMetaStr ? `<span class="video-review-slot-meta">${escapeHtml(srcMetaStr)}</span>` : ""}
                </div>
                <video id="compareSourceVideo" class="video-review-player" controls playsinline preload="metadata" src="${escapeHtml(srcVid.mediaUrl)}"></video>
              </div>
            `
            : ""
        }
        <div class="video-review-slot">
          ${
            isComparing
              ? `<div class="video-review-slot-head"><span>${activeLabel}</span>${metaStr ? `<span class="video-review-slot-meta">${escapeHtml(metaStr)}</span>` : ""}</div>`
              : ""
          }
          <video id="reviewActiveVideo" class="video-review-player" controls playsinline preload="metadata" src="${escapeHtml(activeVid.mediaUrl)}"></video>
        </div>
      </div>

      ${!isComparing && metaStr ? `<div class="video-review-meta">${escapeHtml(metaStr)}</div>` : ""}
      ${localSyncBannerHtml}
      ${passedBannerHtml}
      ${summaryHtml}

      <div class="video-review-actions">
        <div class="video-review-actions-left">
          ${
            canCompare
              ? `<button id="toggleCompareBtn" class="btn btn-secondary btn-sm" type="button">${isComparing ? "收起对比" : "对比原片"}</button>`
              : ""
          }
          ${
            isComparing
              ? `<button id="syncPlayCompareBtn" class="btn btn-secondary btn-sm" type="button">▶ 同步播放</button>`
              : ""
          }
          <a id="downloadActiveVideoBtn" class="btn btn-secondary btn-sm" href="${escapeHtml(downloadHref)}" download="${escapeHtml(downloadName)}" title="保存为 ${escapeHtml(downloadName)}">↓ 下载</a>
          <button id="renameDownloadShotBtn" class="btn btn-secondary btn-sm" type="button" data-download-ver="${escapeHtml(activeVer)}" title="设置或修改镜头名称（自动应用到下载文件名与本地目录文件）">✎ 重命名</button>
          ${localSyncBtnHtml}
          ${replaceFinalBtnHtml}
        </div>
        ${finalActionHtml}
      </div>
    </section>
  `;
}

function renderDetailView() {
  const main = document.getElementById("mainPane");
  const shot = (state.data?.shots || []).find((s) => s.id === state.selectedShotId);
  if (!shot) {
    state.view = "builder";
    renderTopBar();
    renderSidebar();
    renderBuilderView();
    return;
  }

  syncDetailDraftForShot(shot);
  const orderedItems = buildOrderedShotAssets(shot);
  const versionHistory = shot.versionHistory || [];
  const latestVerTag = versionHistory.length ? versionHistory[versionHistory.length - 1].version : "";

  const woParts = shot.whiteoutOptions?.parts || [];
  const woTarget = shot.whiteoutOptions?.targetSlot || "";
  const woSummaryHtml = woParts.length
    ? `<div class="whiteout-summary-row">
         <span class="whiteout-label">涂白选项</span>
         ${woParts.map((t) => `<span class="pill pill-blue">${escapeHtml(t)}</span>`).join("")}
         ${woTarget ? `<span class="pill pill-muted">对象：${escapeHtml(woTarget)}</span>` : ""}
       </div>`
    : "";

  const displayDirection = getDisplayRevisionDirection(shot);
  const directionBoxHtml = displayDirection
    ? `
      <div class="prompt-direction-box">
        <div class="prompt-direction-head">
          <span>✦ 修改方向</span>
          ${latestVerTag ? `<span class="pill pill-muted">${escapeHtml(latestVerTag)}</span>` : ""}
        </div>
        <div class="prompt-direction-text">${escapeHtml(displayDirection)}</div>
      </div>`
    : "";

  main.innerHTML = `
    <div class="stack-lg is-detail">
      ${renderShotActionBarHtml(shot)}
      ${renderShotRuntimeBannerHtml(shot)}
      <div class="detail-workspace">
        <div class="detail-main-col">
          ${renderErrorBanner(shot)}
          ${renderConfirmationBanner(shot)}

          <section class="card">
            <div class="card-head">
              <div class="card-title-group">
                <h2 class="card-title">镜头素材</h2>
                <span class="pill pill-muted">${orderedItems.length}</span>
              </div>
              ${orderedItems.some((it) => it.mediaType === "image") ? `<span class="card-subtle-hint">点击图片涂白</span>` : ""}
            </div>
            ${renderAssetStripHtml(orderedItems, {
              editable: false,
              emptyText: "本镜暂无素材",
            })}
            ${woSummaryHtml}
          </section>

          <section class="card">
            <div class="card-head">
              <div class="card-title-group">
                <h2 class="card-title">提示词</h2>
                <span id="promptSyncBadge" class="pill pill-muted">已与画布一致</span>
              </div>
            </div>
            ${directionBoxHtml}
            <div class="prompt-editor-shell">
              <div id="detailPromptEditor" class="prompt-rich-editor is-tall" contenteditable="true" role="textbox" aria-multiline="true" data-placeholder="输入或直接修改提示词，输入 @ 可引用上方素材或新上传参考图"></div>
            </div>
            <div class="prompt-editor-footer">
              <div id="promptFooterHint" class="prompt-editor-footer-hint">输入 @ 引用素材 · 双击标签预览</div>
              <div class="prompt-editor-footer-actions">
                <button id="resetPromptBtn" class="btn btn-secondary btn-sm hidden" type="button" title="放弃未保存修改，恢复上次已同步内容">还原</button>
                <button id="updatePromptBtn" class="btn btn-secondary btn-sm" type="button" title="保存修改并通知 Agent 同步到 ${escapeHtml(getPlatformLabel(shot.platform))} 画布">同步到画布</button>
              </div>
            </div>
          </section>
        </div>

        <div class="detail-feedback-col">
          ${renderVideoReviewCard(shot)}
          <section class="card">
            <div class="card-head">
              <h2 class="card-title">修改意见</h2>
              ${
                (shot.feedbacks || []).some((f) => f.status === "pending")
                  ? `<span class="pill pill-orange">有待处理意见</span>`
                  : ""
              }
            </div>

            <div style="display:flex;flex-direction:column;gap:10px;">
              ${renderWhiteoutBarHtml({
                prefix: "detail",
                parts: state.detailDraft.whiteoutParts,
                targetSlot: state.detailDraft.whiteoutTarget,
                imageItems: orderedItems.filter((it) => it.mediaType === "image"),
              })}

              <div id="fbDropzone" class="fb-composer-shell">
                <input id="fbAttachments" type="file" multiple accept="image/*,video/*" hidden />
                <div class="prompt-editor-shell">
                  <div id="fbComment" class="prompt-rich-editor is-compact" contenteditable="true" role="textbox" aria-multiline="true" data-placeholder="写下修改意见，可拖入参考图或输入 @ 引用素材"></div>
                </div>
                <div id="fbAttachPreviewWrap">${renderFeedbackAttachmentsHtml()}</div>
                <div class="fb-composer-footer">
                  <div class="fb-composer-tools">
                    <button id="fbUploadBtn" class="btn btn-secondary btn-sm" type="button">+ 参考图</button>
                    <span class="fb-composer-hint">可拖拽 · 支持 @新图</span>
                  </div>
                  <button id="submitFeedbackBtn" class="btn btn-primary btn-sm" type="button">提交修改</button>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  `;

  bindDetailEvents(shot);
}

function bindDetailEvents(shot) {
  clearMentionCleanups();

  const promptEl = document.getElementById("detailPromptEditor");
  const fbCommentEl = document.getElementById("fbComment");
  const fbDropEl = document.getElementById("fbDropzone");
  const fbFilesEl = document.getElementById("fbAttachments");
  const fbPreviewWrap = document.getElementById("fbAttachPreviewWrap");

  state.detailPromptEditor = initRichMentionEditor({
    editorEl: promptEl,
    initialTokens: state.detailDraft.editedPromptTokens,
    initialPlainText: state.detailDraft.editedPrompt,
    getItemsFn: () => buildDetailMentionAssets(shot),
    onChange: ({ tokens, plainText }) => {
      state.detailDraft.editedPromptTokens = tokens;
      state.detailDraft.editedPrompt = plainText;
      updatePromptHeaderUi(shot);
    },
  });
  updatePromptHeaderUi(shot);

  document.getElementById("resetPromptBtn")?.addEventListener("click", () => {
    const baseline = getShotBaselinePrompt(shot);
    state.detailDraft.lastSyncedPrompt = baseline;
    state.detailDraft.editedPrompt = baseline;
    state.detailDraft.editedPromptTokens = "";
    renderDetailView();
    showToast("已还原提示词");
  });

  document.getElementById("updatePromptBtn")?.addEventListener("click", () => {
    const mentionItems = buildDetailMentionAssets(shot);
    state.detailPromptEditor?.syncAssets(mentionItems);
    const nextPrompt = (state.detailDraft.editedPrompt || "").trim();
    if (!nextPrompt) {
      showToast("提示词不能为空");
      return;
    }
    const executeUpdatePrompt = async () => {
      const btn = document.getElementById("updatePromptBtn");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "更新提交中…";
      }
      try {
        await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/update-prompt`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ promptText: state.detailDraft.editedPrompt }),
        });
        state.detailDraft.lastSyncedPrompt = state.detailDraft.editedPrompt;
        showToast("已保存最新提示词，并已通知 Agent 同步更新画布");
        await refreshState();
      } catch (err) {
        showToast(err.message);
        updatePromptHeaderUi(shot);
      }
    };
    guardPassedFinalAction(shot, {
      actionName: "修改提示词并同步画布",
      consequenceText: "确认后将保存新版提示词并同步到画布，随后可继续重新生成 480P 样片。",
      okText: "确认解锁并同步画布",
      onProceed: executeUpdatePrompt,
    });
  });

  bindWhiteoutBarEvents({
    prefix: "detail",
    getParts: () => state.detailDraft.whiteoutParts,
    setParts: (parts) => {
      state.detailDraft.whiteoutParts = parts;
    },
    setTarget: (target) => {
      state.detailDraft.whiteoutTarget = target;
    },
  });

  state.fbCommentEditor = initRichMentionEditor({
    editorEl: fbCommentEl,
    initialTokens: state.detailDraft.commentTokens,
    initialPlainText: state.detailDraft.comment,
    getItemsFn: () => buildDetailMentionAssets(shot),
    onChange: ({ tokens, plainText }) => {
      state.detailDraft.commentTokens = tokens;
      state.detailDraft.comment = plainText;
    },
  });

  const refreshAttachPreview = () => {
    fbPreviewWrap.innerHTML = renderFeedbackAttachmentsHtml();
    fbPreviewWrap.querySelectorAll("[data-remove-fb-att]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const idx = Number(btn.dataset.removeFbAtt);
        const removed = state.detailDraft.attachments.splice(idx, 1)[0];
        if (removed?.url?.startsWith("blob:")) URL.revokeObjectURL(removed.url);
        refreshAttachPreview();
      });
    });
    const mentionItems = buildDetailMentionAssets(shot);
    state.detailPromptEditor?.syncAssets(mentionItems);
    state.fbCommentEditor?.syncAssets(mentionItems);
  };
  refreshAttachPreview();

  const addFeedbackFiles = (fileList) => {
    const files = Array.from(fileList || []).filter((f) => isImageFile(f) || isVideoFile(f));
    if (!files.length) return;
    for (const f of files) {
      state.detailDraft.attachments.push({
        id: uid(),
        file: f,
        url: URL.createObjectURL(f),
        kind: isVideoFile(f) ? "video" : "image",
        name: f.name,
      });
    }
    refreshAttachPreview();
  };

  bindDropzoneEl(fbDropEl, fbFilesEl, addFeedbackFiles, { clickToOpen: false });
  document.getElementById("fbUploadBtn")?.addEventListener("click", () => {
    fbFilesEl?.click();
  });

  document.getElementById("confirmShotBtn")?.addEventListener("click", async () => {
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/confirm`, { method: "POST" });
      showToast("已确认提交");
      await refreshState();
    } catch (err) {
      showToast(err.message);
    }
  });

  document.getElementById("openHistoryModalBtn")?.addEventListener("click", () => {
    openHistoryModal(shot);
  });

  const executeRunShot = async () => {
    const btn = document.getElementById("runShotBtn");
    const editedPrompt = getDetailEditedPromptIfChanged(shot);
    if (btn) {
      btn.disabled = true;
      btn.textContent = "提交运行中…";
    }
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edited_prompt: editedPrompt || null }),
      });
      if (editedPrompt) state.detailDraft.lastSyncedPrompt = state.detailDraft.editedPrompt;
      showToast("已请求 Run 生成，Agent 将自动出片并下载到本地看片");
      await refreshState();
    } catch (err) {
      showToast(err.message);
      if (btn) {
        btn.disabled = false;
        btn.textContent = shot.draftVideo ? "▶ 重新生成" : "▶ 帮我 Run 生成";
      }
    }
  };

  document.getElementById("runShotBtn")?.addEventListener("click", () => {
    if (isFinalInProgress(shot)) {
      showToast("当前正在转为 1080P 正片，已锁定防误触");
      return;
    }
    const editedPrompt = getDetailEditedPromptIfChanged(shot);
    const info = getEffectiveShotChangeInfo(shot);
    if (isPassedFinal(shot)) {
      guardPassedFinalAction(shot, {
        actionName: "重洗 480P 样片",
        consequenceText: "确认重洗将按新/当前配置重新发起 480P 样片生成并消耗积分，出片后可重新定版 1080P。",
        okText: "确定解锁并生成 480P",
        onProceed: executeRunShot,
      });
      return;
    }
    if (!editedPrompt && info.hasDraft && !info.hasChanges) {
      const genTime = shot.lastGeneratedAt || shot.draftVideo?.downloadedAt || shot.updatedAt || "上一版";
      openConfirmModal({
        title: "未检测到修改，确定仍要重新生成？",
        message:
          "系统比对后发现：当前提示词、参考图片与涂白遮罩，同你上一版已生成的 480P 样片完全一致，没有新增改动（可能为误触）。",
        meta: `镜头：${shot.id} · 上一版成片时间：${genTime}\n点击「仍要重新生成」将消耗算力再跑一遍相同参数。`,
        okText: "仍要重新生成",
        cancelText: "取消（不生成）",
        onConfirm: executeRunShot,
      });
      return;
    }
    executeRunShot();
  });

  const handleSyncLibtv = async (triggerBtn) => {
    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.textContent = "同步中…";
    }
    try {
      const platLabel = getPlatformLabel(shot.platform);
      const res = await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/sync-canvas`, { method: "POST" });
      if (res.libtv?.hasRemoteFinal) {
        showToast(`已从 ${platLabel} 同步最新 1080P 正片并更新本地状态`);
      } else if (res.libtv?.hasRemoteDraft) {
        showToast(`已将 ${platLabel} 成片下载到本地，可在右侧直接观看`);
      } else if (res.libtv?.loading) {
        showToast(`${platLabel} 云端仍在生成中，请稍后再拉取`);
      } else {
        showToast("已同步画布节点（云端尚无已生成视频）");
      }
      await refreshState();
    } catch (err) {
      showToast(err.message);
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.textContent = "↓ 同步成片";
      }
    }
  };
  document.getElementById("syncLibtvBtn")?.addEventListener("click", (e) => handleSyncLibtv(e.currentTarget));

  document.getElementById("renameDownloadShotBtn")?.addEventListener("click", (e) => {
    const ver = e.currentTarget.dataset.downloadVer || "final";
    openRenameShotModal(shot, { downloadVerAfter: ver });
  });

  document.getElementById("syncLocalExportBtn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (!shot.resolvedExportDir) {
      openExportConfigModal(getShotFolderName(shot));
      return;
    }
    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "同步中…";
    try {
      const res = await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/export-local`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: "1080p" }),
      });
      const r = res.syncResult || {};
      showToast(
        r.renamedFrom
          ? `已同步覆盖并将旧文件重命名为：${r.fileName}`
          : r.unchanged
          ? `本地 1080P 已是最新版本：${r.fileName}`
          : `已同步最新 1080P 到：${r.filePath}`
      );
      await refreshState();
    } catch (err) {
      showToast(err.message || "同步本地成片失败");
      btn.disabled = false;
      btn.textContent = prevText;
    }
  });

  const replaceFinalInput = document.getElementById("replaceFinalVideoInput");
  document.getElementById("replaceFinalVideoBtn")?.addEventListener("click", () => {
    replaceFinalInput?.click();
  });
  replaceFinalInput?.addEventListener("change", async () => {
    const file = replaceFinalInput.files?.[0];
    replaceFinalInput.value = "";
    if (!file) return;
    const btn = document.getElementById("replaceFinalVideoBtn");
    const prevText = btn ? btn.textContent : "";
    if (btn) {
      btn.disabled = true;
      btn.textContent = "上传更新中…";
    }
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("syncLocal", "true");
      const res = await fetch(`/api/shots/${encodeURIComponent(shot.id)}/replace-final`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `替换 1080P 失败 (${res.status})`);
      }
      const sr = data.syncResult;
      state.detailDraft.previewVersion = "final";
      showToast(
        sr?.synced
          ? `已替换 1080P 并自动同步到：${sr.filePath}`
          : `已替换更新 ${shot.id} 的 1080P 正片画面`
      );
      await refreshState();
    } catch (err) {
      showToast(err.message || "替换 1080P 失败");
      if (btn) {
        btn.disabled = false;
        btn.textContent = prevText;
      }
    }
  });

  document.getElementById("openShotChatMainBtn")?.addEventListener("click", (e) => {
    const btn = e.currentTarget;
    openShotForegroundChat(shot.id, btn.dataset.chatMode || "auto", "", btn);
  });

  document.getElementById("shotChatMenuToggleBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    closeCanvasPopover();
    closeAiStatusPopover();
    closeUpdatePopover();
    closeShotMoreMenu();
    state.shotChatMenuOpen = !state.shotChatMenuOpen;
    document.getElementById("shotChatMenu")?.classList.toggle("hidden", !state.shotChatMenuOpen);
    const btn = document.getElementById("shotChatMenuToggleBtn");
    btn?.classList.toggle("is-open", state.shotChatMenuOpen);
    btn?.setAttribute("aria-expanded", state.shotChatMenuOpen ? "true" : "false");
  });

  document.getElementById("chatResumeMenuBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    openShotForegroundChat(shot.id, "resume", "", document.getElementById("openShotChatMainBtn"));
  });

  document.getElementById("chatNewTerminalMenuBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    openShotForegroundChat(shot.id, "new", "", document.getElementById("openShotChatMainBtn"));
  });

  document.getElementById("chatHandoffDesktopMenuBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    handoffShotToDesktopApp(shot.id, "", document.getElementById("openShotChatMainBtn"));
  });

  document.getElementById("toggleCompareBtn")?.addEventListener("click", () => {
    state.detailDraft.compareSource = !state.detailDraft.compareSource;
    renderDetailView();
  });

  document.getElementById("syncPlayCompareBtn")?.addEventListener("click", (e) => {
    const btn = e.currentTarget;
    const srcEl = document.getElementById("compareSourceVideo");
    const actEl = document.getElementById("reviewActiveVideo");
    if (!srcEl || !actEl) return;
    if (!srcEl.paused || !actEl.paused) {
      srcEl.pause();
      actEl.pause();
      btn.textContent = "▶ 同步播放";
      return;
    }
    const startT = Math.min(actEl.currentTime || 0, Math.max(0, (srcEl.duration || 999) - 0.1));
    const restart = Number.isFinite(actEl.duration) && startT >= actEl.duration - 0.15;
    const t = restart ? 0 : startT;
    srcEl.currentTime = t;
    actEl.currentTime = t;
    Promise.allSettled([srcEl.play(), actEl.play()]);
    btn.textContent = "❚❚ 同步暂停";
  });

  document.querySelectorAll("[data-preview-ver]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.detailDraft.previewVersion = btn.dataset.previewVer;
      renderDetailView();
    });
  });

  document.getElementById("approveFinalBtn")?.addEventListener("click", async () => {
    const btn = document.getElementById("approveFinalBtn");
    btn.disabled = true;
    btn.textContent = "提交正片中…";
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/approve-final`, { method: "POST" });
      showToast("已提交转 1080P 正片队列");
      await refreshState();
    } catch (err) {
      showToast(err.message);
      btn.disabled = false;
      btn.textContent = "✦ 转 1080P 正片";
    }
  });

  document.getElementById("submitFeedbackBtn")?.addEventListener("click", () => {
    const editedPrompt = getDetailEditedPromptIfChanged(shot);
    const promptChanged = Boolean(editedPrompt);
    const comment = state.detailDraft.comment.trim();
    const woParts = state.detailDraft.whiteoutParts || [];
    const woTarget = state.detailDraft.whiteoutTarget || "";
    if (!comment && !woParts.length && !promptChanged && !state.detailDraft.attachments.length) {
      showToast("请修改左侧提示词、勾选涂白标签、填写意见或上传参考图");
      return;
    }

    const executeSubmitFeedback = async () => {
      const btn = document.getElementById("submitFeedbackBtn");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "提交中…";
      }
      try {
        const fd = new FormData();
        const autoComment = woParts.length
          ? `${woTarget ? `${woTarget} ` : ""}${woParts.join(" + ")}`
          : promptChanged
          ? "按已修改提示词同步更新"
          : "附带参考图";
        fd.append("comment", comment || autoComment);
        fd.append("tags_json", JSON.stringify(woParts));
        fd.append("target_slot", woTarget);
        fd.append("edited_prompt", promptChanged ? state.detailDraft.editedPrompt : "");
        for (const att of state.detailDraft.attachments) {
          fd.append("attachments", att.file);
        }
        await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/feedback`, { method: "POST", body: fd });
        showToast("已提交修改");
        state.detailDraft.lastSyncedPrompt = state.detailDraft.editedPrompt;
        state.detailDraft.comment = "";
        state.detailDraft.commentTokens = "";
        state.detailDraft.whiteoutParts = [];
        state.detailDraft.whiteoutTarget = "";
        clearDetailDraftAttachments();
        await refreshState();
      } catch (err) {
        showToast(err.message);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "提交修改";
        }
      }
    };

    guardPassedFinalAction(shot, {
      actionName: "提交修改",
      consequenceText: "确认后将解锁本镜定版状态并通知 Agent 按修改意见更新画布，完成后可继续生成。",
      okText: "确认解锁并提交修改",
      onProceed: executeSubmitFeedback,
    });
  });

  let confirmResolving = false;
  const handleResolveConfirm = async (decision) => {
    if (confirmResolving) return;
    confirmResolving = true;
    const approveBtn = document.getElementById("approveConfirmBtn");
    const rejectBtn = document.getElementById("rejectConfirmBtn");
    if (approveBtn) approveBtn.disabled = true;
    if (rejectBtn) rejectBtn.disabled = true;
    const activeBtn = decision === "approve" ? approveBtn : rejectBtn;
    const origText = activeBtn ? activeBtn.textContent : "";
    if (activeBtn) activeBtn.textContent = "提交中…";
    const fd = new FormData();
    fd.append("decision", decision);
    fd.append("comment", document.getElementById("confirmCommentInput")?.value || "");
    const f = document.getElementById("confirmFileInput")?.files?.[0];
    if (f) fd.append("corrected_mask", f);
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/resolve-confirmation`, {
        method: "POST",
        body: fd,
      });
      showToast("已提交确认");
      await refreshState();
    } catch (err) {
      showToast(err.message);
      if (approveBtn) approveBtn.disabled = false;
      if (rejectBtn) rejectBtn.disabled = false;
      if (activeBtn) activeBtn.textContent = origText;
      confirmResolving = false;
    }
  };
  document.getElementById("approveConfirmBtn")?.addEventListener("click", () => handleResolveConfirm("approve"));
  document.getElementById("rejectConfirmBtn")?.addEventListener("click", () => handleResolveConfirm("reject"));

  const handleRecoverError = async (mode, btnEl, busyText, okToast) => {
    if (btnEl) {
      btnEl.disabled = true;
      btnEl.textContent = busyText;
    }
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/recover-error`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      showToast(okToast);
      await refreshState();
    } catch (err) {
      showToast(err.message);
      await refreshState();
    }
  };
  document.getElementById("openChatFromErrorBtn")?.addEventListener("click", (e) => {
    const btn = e.currentTarget;
    const reason = (shot.errorReason || "后台执行异常中断").trim();
    openShotForegroundChat(
      shot.id,
      btn.dataset.chatMode || "auto",
      `当前镜头处于出错状态（${reason}），请直接帮我排查原因并决定修复或改写方案。`,
      btn
    );
  });
  document.getElementById("retryErrorBtn")?.addEventListener("click", (e) =>
    handleRecoverError("retry", e.currentTarget, "重试中…", "已重新入队重试，等待 Agent 处理")
  );
  document.getElementById("restoreErrorBtn")?.addEventListener("click", (e) =>
    handleRecoverError("restore", e.currentTarget, "恢复中…", "已清除异常标记并恢复稳定状态")
  );
  document.getElementById("recheckFromErrorBtn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "提交重检…";
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/reprocess`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "出错后一键自检修复" }),
      });
      showToast("已触发重新自检修复");
      await refreshState();
    } catch (err) {
      showToast(err.message);
      await refreshState();
    }
  });
}

/* Preset Add/Edit Modal */
function openPresetModal(preset = null, defaultScene = "通用") {
  const modal = document.getElementById("presetModal");
  const isEdit = Boolean(preset);
  document.getElementById("presetModalTitle").textContent = isEdit ? `编辑角色 · ${preset.name}` : "新增角色";
  document.getElementById("presetEditId").value = isEdit ? preset.id : "";
  document.getElementById("presetNameInput").value = isEdit ? preset.name : "";
  document.getElementById("presetSceneInput").value = isEdit ? preset.scene || "通用" : defaultScene || "通用";
  document.getElementById("presetHintInput").value = isEdit ? preset.roleHint || "" : "";

  const fileField = document.getElementById("presetFileField");
  const fileInput = document.getElementById("presetFileInput");
  fileInput.value = "";
  fileInput.required = !isEdit;
  fileField.classList.toggle("hidden", isEdit);

  document.getElementById("deletePresetBtn").classList.toggle("hidden", !isEdit);
  modal.classList.remove("hidden");
}

function closePresetModal() {
  document.getElementById("presetModal").classList.add("hidden");
}

/* Confirm Modal */
function openConfirmModal({
  title = "请确认操作",
  message = "",
  meta = "",
  okText = "确认执行",
  cancelText = "取消",
  danger = false,
  onConfirm = null,
} = {}) {
  const modal = document.getElementById("confirmModal");
  if (!modal) return;
  document.getElementById("confirmModalTitle").textContent = title;
  document.getElementById("confirmModalMessage").textContent = message;
  const metaEl = document.getElementById("confirmModalMeta");
  if (metaEl) {
    if (meta) {
      metaEl.textContent = meta;
      metaEl.style.whiteSpace = "pre-wrap";
      metaEl.classList.remove("hidden");
    } else {
      metaEl.textContent = "";
      metaEl.classList.add("hidden");
    }
  }
  const okBtn = document.getElementById("confirmOkBtn");
  const cancelBtn = document.getElementById("confirmCancelBtn");
  if (okBtn) {
    okBtn.disabled = false;
    okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    okBtn.textContent = okText;
  }
  if (cancelBtn) cancelBtn.textContent = cancelText;
  state.confirmDialog = { open: true, onConfirm };
  modal.classList.remove("hidden");
}

function closeConfirmModal() {
  const modal = document.getElementById("confirmModal");
  if (modal) modal.classList.add("hidden");
  state.confirmDialog = { open: false, onConfirm: null };
}

function updateRenameModalPreview(shot, draftTitle) {
  const prev1080 = document.getElementById("renamePreview1080");
  const prev480 = document.getElementById("renamePreview480");
  if (prev1080) prev1080.textContent = buildClientDownloadFilename(shot, "1080p", draftTitle);
  if (prev480) prev480.textContent = buildClientDownloadFilename(shot, "480p", draftTitle);
}

function openRenameShotModal(shot, { downloadVerAfter = null } = {}) {
  if (!shot) return;
  closeShotMoreMenu();
  closeShotChatMenu();
  state.renameModal = {
    open: true,
    shotId: shot.id,
    downloadVerAfter,
  };
  const modal = document.getElementById("renameShotModal");
  const metaEl = document.getElementById("renameShotMeta");
  const inp = document.getElementById("renameShotTitleInput");
  const dlBtn = document.getElementById("saveAndDownloadBtn");
  const syncChk = document.getElementById("renameSyncLocalCheck");
  const sceneLabel = (shot.scene || "").trim() || "未分场";
  if (metaEl) {
    metaEl.textContent = `镜头：${shot.id} · 所属场次：${sceneLabel}${shot.resolvedExportDir ? ` · 绑定目录：${shot.resolvedExportDir}` : ""}`;
  }
  if (inp) inp.value = shot.title || "";
  if (syncChk) syncChk.checked = true;
  if (dlBtn) {
    const canDownload = Boolean(
      (downloadVerAfter === "final" && shot.finalVideo?.mediaUrl) ||
        (downloadVerAfter && (shot.finalVideo?.mediaUrl || shot.draftVideo?.mediaUrl))
    );
    dlBtn.classList.toggle("hidden", !canDownload);
  }
  updateRenameModalPreview(shot, shot.title || "");
  modal?.classList.remove("hidden");
  setTimeout(() => {
    inp?.focus();
    inp?.select();
  }, 10);
}

function closeRenameShotModal() {
  document.getElementById("renameShotModal")?.classList.add("hidden");
  state.renameModal = { open: false, shotId: null, downloadVerAfter: null };
}

function triggerBrowserMediaDownload(shot, ver = "final") {
  if (!shot) return;
  const isFinal = ver === "final" && Boolean(shot.finalVideo?.mediaUrl);
  const targetVid = isFinal ? shot.finalVideo : shot.draftVideo || shot.finalVideo;
  if (!targetVid?.mediaUrl) return;
  const dlName = isFinal
    ? shot.downloadName1080p || buildClientDownloadFilename(shot, "1080p")
    : shot.downloadName480p || buildClientDownloadFilename(shot, "480p");
  const href = `${targetVid.mediaUrl}${String(targetVid.mediaUrl).includes("?") ? "&" : "?"}download_name=${encodeURIComponent(dlName)}`;
  const a = document.createElement("a");
  a.href = href;
  a.download = dlName;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function collectExportSceneNames(focusScene = "") {
  const names = new Set();
  for (const sc of state.data?.scenes || []) {
    const clean = String(sc || "").trim();
    if (clean) names.add(clean);
  }
  for (const s of state.data?.shots || []) {
    names.add(getShotFolderName(s));
  }
  for (const k of Object.keys(state.data?.sceneExportDirs || {})) {
    if (String(k || "").trim()) names.add(String(k).trim());
  }
  if (focusScene) names.add(focusScene);
  return [...names];
}

function renderExportSceneRows(focusScene = "") {
  const wrap = document.getElementById("exportSceneDirsList");
  if (!wrap) return;
  const sceneMap = state.data?.sceneExportDirs || {};
  const sceneNames = collectExportSceneNames(focusScene);
  if (!sceneNames.length) {
    wrap.innerHTML = `<div style="font-size:12px;color:var(--text-muted);padding:4px 0;">暂无场次（可先配置上方全局默认目录，或先新建场次）</div>`;
    return;
  }
  wrap.innerHTML = sceneNames
    .map((scName) => {
      const val = sceneMap[scName] || "";
      const focusedCls = focusScene && focusScene === scName ? " is-focused" : "";
      return `
        <div class="export-scene-row${focusedCls}" data-scene-dir-row="${escapeHtml(scName)}">
          <span class="export-scene-label" title="${escapeHtml(scName)}">${escapeHtml(scName)}</span>
          <input type="text" data-scene-dir-input="${escapeHtml(scName)}" value="${escapeHtml(val)}" placeholder="留空则跟随全局默认目录" />
          <button type="button" class="btn btn-secondary btn-sm" data-pick-scene-folder="${escapeHtml(scName)}">选择…</button>
        </div>
      `;
    })
    .join("");
}

function openExportConfigModal(focusScene = "") {
  closeShotMoreMenu();
  closeShotChatMenu();
  state.exportConfigModal = { open: true, focusScene: focusScene || "" };
  const defInp = document.getElementById("exportDefaultDirInput");
  const autoChk = document.getElementById("exportAutoSyncCheck");
  if (defInp) defInp.value = state.data?.defaultExportDir || "";
  if (autoChk) autoChk.checked = state.data?.autoSyncExport !== false;
  renderExportSceneRows(focusScene);
  document.getElementById("exportConfigModal")?.classList.remove("hidden");
  if (focusScene) {
    setTimeout(() => {
      const targetInp = document.querySelector(`[data-scene-dir-input="${CSS.escape(focusScene)}"]`);
      targetInp?.focus();
    }, 10);
  }
}

function closeExportConfigModal() {
  document.getElementById("exportConfigModal")?.classList.add("hidden");
  state.exportConfigModal = { open: false, focusScene: "" };
}

async function pickFolderIntoInput(inputEl, promptTitle) {
  if (!inputEl) return;
  try {
    const res = await apiJson("/api/system/pick-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        initialDir: inputEl.value.trim(),
        prompt: promptTitle || "选择成片同步导出文件夹",
      }),
    });
    if (res.cancelled) return;
    if (res.unsupported) {
      showToast(res.detail || "当前系统请直接输入文件夹绝对路径");
      return;
    }
    if (res.path) {
      inputEl.value = res.path;
      showToast(`已选择目录：${res.path}`);
    }
  } catch (err) {
    showToast(err.message || "打开目录选择器失败");
  }
}

function readExportConfigFormValues() {
  const defaultExportDir = document.getElementById("exportDefaultDirInput")?.value.trim() || "";
  const autoSyncExport = Boolean(document.getElementById("exportAutoSyncCheck")?.checked);
  const sceneExportDirs = {};
  document.querySelectorAll("[data-scene-dir-input]").forEach((inp) => {
    const sc = inp.dataset.sceneDirInput;
    const val = inp.value.trim();
    if (sc && val) sceneExportDirs[sc] = val;
  });
  return { defaultExportDir, sceneExportDirs, autoSyncExport };
}

/* Standalone Version History & Rollback Modal */
function openHistoryModal(shot, preferredVersionId = null) {
  if (!shot) return;
  closeShotMoreMenu();
  closeShotChatMenu();
  const history = shot.versionHistory || [];
  const latest = history.length ? history[history.length - 1] : null;
  state.historyModal = {
    open: true,
    shotId: shot.id,
    selectedVersionId: preferredVersionId || latest?.id || null,
  };
  renderHistoryModal();
  document.getElementById("historyModal")?.classList.remove("hidden");
}

function closeHistoryModal() {
  document.getElementById("historyModal")?.classList.add("hidden");
  state.historyModal.open = false;
}

function formatHistoryActorLabel(actor) {
  if (actor === "agent") return "AI Agent 对话";
  if (actor === "user") return "用户操作";
  if (actor === "sync") return "画布同步";
  return actor || "系统";
}

function renderHistoryModal() {
  const listEl = document.getElementById("historyVersionList");
  const detailEl = document.getElementById("historyDetailPane");
  const subEl = document.getElementById("historyModalSub");
  if (!listEl || !detailEl) return;

  const shot = (state.data?.shots || []).find((s) => s.id === state.historyModal.shotId);
  if (!shot) {
    closeHistoryModal();
    return;
  }

  const historyDesc = (shot.versionHistory || []).slice().reverse();
  const curPrompt = getShotBaselinePrompt(shot);
  if (subEl) {
    subEl.textContent = `${shot.id}${shot.scene ? ` · ${shot.scene}` : ""} · 共 ${historyDesc.length} 轮会话 / 版本记录`;
  }

  if (!historyDesc.length) {
    listEl.innerHTML = `<div style="color:var(--text-muted);font-size:12.5px;padding:12px;">暂无历史版本记录</div>`;
    detailEl.innerHTML = `<div style="color:var(--text-muted);font-size:13px;padding:24px;text-align:center;">该镜头还没有对话或改词历史记录。</div>`;
    return;
  }

  let selected = historyDesc.find(
    (h) => h.id === state.historyModal.selectedVersionId || h.version === state.historyModal.selectedVersionId
  );
  if (!selected) {
    selected = historyDesc[0];
    state.historyModal.selectedVersionId = selected.id;
  }

  const currentMatchId =
    historyDesc.find((h) => h.promptRecorded && h.promptText && h.promptText.trim() === curPrompt)?.id || null;

  listEl.innerHTML = historyDesc
    .map((entry) => {
      const isActive = entry.id === selected.id;
      const isCurrentPrompt = Boolean(currentMatchId && entry.id === currentMatchId);
      const snippet = entry.revisionDirection || entry.userFeedback || (entry.promptText ? entry.promptText.slice(0, 80) : "");
      return `
        <button type="button" class="history-item ${isActive ? "is-active" : ""}" data-history-ver="${escapeHtml(entry.id)}">
          <div class="history-item-head">
            <span class="history-ver-tag">${escapeHtml(entry.version || `#${entry.seq}`)} · ${escapeHtml(formatHistoryActorLabel(entry.actor))}</span>
            <span class="history-time">${escapeHtml(entry.createdAt || "")}</span>
          </div>
          <div class="history-item-title">${escapeHtml(entry.title || entry.kind || "版本快照")}</div>
          ${snippet ? `<div class="history-item-snippet">${escapeHtml(snippet)}</div>` : ""}
          <div class="history-item-badges">
            ${isCurrentPrompt ? `<span class="history-mini-pill is-current">当前版本</span>` : ""}
            ${entry.kind === "rollback" ? `<span class="history-mini-pill is-rollback">回滚记录</span>` : ""}
            ${entry.draftVideoUrl ? `<span class="history-mini-pill is-video">含成片</span>` : ""}
            ${!entry.promptRecorded ? `<span class="history-mini-pill">仅意见摘要</span>` : ""}
          </div>
        </button>`;
    })
    .join("");

  const hasPrompt = Boolean(selected.promptRecorded && selected.promptText);
  const matchesCurrent = hasPrompt && selected.promptText.trim() === curPrompt;
  const tagBadges = [
    ...(selected.feedbackTags || []),
    ...(selected.targetSlot ? [`对象:${selected.targetSlot}`] : []),
  ]
    .map((t) => `<span class="pill pill-muted" style="font-size:10.5px;padding:1px 7px;">${escapeHtml(t)}</span>`)
    .join("");

  const attsHtml = (selected.attachments || []).length
    ? `
      <div class="history-att-strip">
        ${selected.attachments
          .map((a) => {
            const isVid = String(a.filePath || a.mediaUrl || "").match(/\.(mp4|mov|mkv|webm|avi|m4v)$/i);
            return isVid
              ? `<video class="history-att-thumb" src="${escapeHtml(a.mediaUrl || "")}" muted playsinline preload="metadata"
                   data-zoom-url="${escapeHtml(a.mediaUrl || "")}" data-zoom-kind="video" data-zoom-caption="${escapeHtml(a.name || "附件")}"></video>`
              : `<img class="history-att-thumb" src="${escapeHtml(a.mediaUrl || "")}" alt="${escapeHtml(a.name || "")}"
                   data-zoom-url="${escapeHtml(a.mediaUrl || "")}" data-zoom-kind="image" data-zoom-caption="${escapeHtml(a.name || "参考图")}" />`;
          })
          .join("")}
      </div>`
    : "";

  detailEl.innerHTML = `
    <div class="history-detail-head">
      <div>
        <h4 class="history-detail-title">${escapeHtml(selected.version || "")} · ${escapeHtml(selected.title || "版本快照")}</h4>
        <div class="history-detail-meta">
          <span>来源：${escapeHtml(formatHistoryActorLabel(selected.actor))}</span>
          <span>时间：${escapeHtml(selected.createdAt || "未记录")}</span>
          ${selected.conversationId ? `<span>会话 ID：<code>${escapeHtml(selected.conversationId.slice(0, 12))}</code></span>` : ""}
        </div>
      </div>
      ${matchesCurrent ? `<span class="pill pill-blue">与当前提示词一致</span>` : ""}
    </div>

    ${
      selected.userFeedback || tagBadges || attsHtml
        ? `
          <div class="history-section">
            <div class="history-section-label">用户要求 / 修改意见</div>
            ${tagBadges ? `<div class="fb-tag-row" style="margin-bottom:4px;">${tagBadges}</div>` : ""}
            ${selected.userFeedback ? `<div class="history-user-box">${escapeHtml(selected.userFeedback)}</div>` : ""}
            ${attsHtml}
          </div>`
        : ""
    }

    ${
      selected.revisionDirection
        ? `
          <div class="prompt-direction-box" style="margin-bottom:0;">
            <div class="prompt-direction-head"><span>✦ AI 理解的修改方向</span></div>
            <div class="prompt-direction-text">${escapeHtml(selected.revisionDirection)}</div>
          </div>`
        : ""
    }

    <div class="history-section">
      <div class="history-section-label">当时提示词快照${hasPrompt ? `（${selected.promptText.length} 字）` : ""}</div>
      ${
        hasPrompt
          ? `<pre class="history-prompt-box">${escapeHtml(selected.promptText)}</pre>`
          : `<div class="history-user-box" style="color:var(--text-muted);">该早先修改意见记录于「全量会话版本快照」功能上线前，仅留存文字意见与修改方向，未单独存档当轮中间完整提示词（可选择首版或带有快照的后续版本回滚）。</div>`
      }
    </div>

    ${
      selected.draftVideoUrl
        ? `
          <div class="history-section">
            <div class="history-section-label">本轮对应成片${selected.draftDownloadedAt ? `（${escapeHtml(selected.draftDownloadedAt)}）` : ""}</div>
            <video class="history-video" src="${escapeHtml(selected.draftVideoUrl)}" controls playsinline preload="metadata"></video>
          </div>`
        : ""
    }

    <div class="history-actions">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
        ${hasPrompt ? `<button type="button" class="btn btn-secondary btn-sm" id="copyHistoryPromptBtn">📋 复制此版提示词</button>` : ""}
      </div>
      <div style="display:flex;align-items:center;gap:8px;">
        <button type="button" class="btn btn-primary btn-sm" id="rollbackVersionBtn" ${!hasPrompt || matchesCurrent ? "disabled" : ""}>
          ${!hasPrompt ? "此早期节点无完整提示词" : matchesCurrent ? "当前已是此版本" : `↺ 回滚到 ${escapeHtml(selected.version)}`}
        </button>
      </div>
    </div>
  `;

  listEl.querySelectorAll("[data-history-ver]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.historyModal.selectedVersionId = btn.dataset.historyVer;
      renderHistoryModal();
    });
  });

  document.getElementById("copyHistoryPromptBtn")?.addEventListener("click", async () => {
    if (!hasPrompt) return;
    try {
      await navigator.clipboard.writeText(selected.promptText);
      showToast(`已复制 ${selected.version} 的提示词`);
    } catch {
      showToast("复制失败，请手动选中文本复制");
    }
  });

  document.getElementById("rollbackVersionBtn")?.addEventListener("click", () => {
    if (!hasPrompt || matchesCurrent) return;
    const isPassed = isPassedFinal(shot);
    openConfirmModal({
      title: isPassed
        ? `本镜头已定版（1080P 已过条），确定回滚到 ${selected.version} 吗？`
        : `回滚到 ${selected.version}？`,
      message:
        `确定把「${shot.id}」的提示词恢复为「${selected.version} · ${selected.title}」吗？\n\n` +
        (isPassed
          ? "确认后将解锁本镜定版并通知 Agent 同步该版提示词到画布，随后可继续重新生成（原 1080P 归档与所有对话记录仍保留）。"
          : `本次回滚不会删除任何既有对话记录（会追加一条回滚版本），并通知 Agent 同步回 ${getPlatformLabel(shot.platform)} 画布，不会自动触发消耗算力的 Run 生成。`),
      meta: selected.revisionDirection ? `该版本修改方向：${selected.revisionDirection}` : "",
      okText: isPassed ? `确认解锁并回滚到 ${selected.version}` : `确认回滚到 ${selected.version}`,
      cancelText: isPassed ? "取消（保持定版）" : "再想想",
      danger: isPassed,
      onConfirm: async () => {
        const res = await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/rollback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ versionId: selected.id, syncToCanvas: true }),
        });
        const restored = res.shot?.promptText || selected.promptText;
        state.detailDraft.lastSyncedPrompt = restored;
        state.detailDraft.editedPrompt = restored;
        state.detailDraft.editedPromptTokens = "";
        closeHistoryModal();
        showToast(`已回滚至 ${selected.version} 提示词，并通知 Agent 同步画布`);
        await refreshState();
      },
    });
  });
}

/* Refresh & Long-poll */
function isUserActivelyEditing() {
  const active = document.activeElement;
  if (!active) return false;
  const tag = active.tagName;
  const isEditable = tag === "INPUT" || tag === "TEXTAREA" || active.isContentEditable;
  if (!isEditable) return false;
  return Boolean(
    active.closest("#mainPane") ||
      active.closest(".modal-backdrop:not(.hidden)") ||
      active.closest("#canvasPopover:not(.hidden)")
  );
}

function renderActiveView() {
  renderSceneDatalist();
  renderTopBar();
  renderSidebar();
  if (state.view === "builder") renderBuilderView();
  else renderDetailView();
  syncRuntimePollingTimer();
}

async function refreshState({ preserveEditing = false } = {}) {
  const prevSeq = state.eventSeq;
  state.data = await apiJson("/api/state");
  state.eventSeq = state.data.eventSeq || prevSeq;
  if (!state.builder.shotIdTouched && document.activeElement?.id !== "builderShotId") {
    state.builder.shotId = suggestNextShotId(state.data?.shots || []);
    const shotIdEl = document.getElementById("builderShotId");
    if (shotIdEl && state.view === "builder") {
      shotIdEl.value = state.builder.shotId;
    }
  }
  renderSceneDatalist();
  renderTopBar();
  if (!state.editingFolderName && !state.creatingFolder && !state.draggingShotId) {
    renderSidebar();
  }
  if (state.historyModal?.open) {
    renderHistoryModal();
  }
  if (preserveEditing && (state.view === "builder" || isUserActivelyEditing())) {
    return;
  }
  if (state.view === "builder") renderBuilderView();
  else renderDetailView();
}

async function startEventLoop() {
  while (true) {
    try {
      const res = await apiJson(`/api/events/wait?since=${state.eventSeq}&timeout=20`);
      if (res.eventSeq && res.eventSeq > state.eventSeq) {
        state.eventSeq = res.eventSeq;
        await refreshState({ preserveEditing: true });
      }
    } catch {
      await new Promise((r) => setTimeout(r, 2500));
    }
  }
}

function bindGlobalEvents() {
  document.getElementById("aiStatusBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    closeCanvasPopover();
    closeUpdatePopover();
    closeShotMoreMenu();
    closeShotChatMenu();
    state.aiStatusOpen = !state.aiStatusOpen;
    renderAiStatusPopover();
    if (state.aiStatusOpen) {
      refreshAgentStatus();
    }
  });

  document.getElementById("updatePillBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    closeCanvasPopover();
    closeAiStatusPopover();
    closeShotMoreMenu();
    closeShotChatMenu();
    state.updateMenuOpen = !state.updateMenuOpen;
    renderUpdatePopover();
    if (state.updateMenuOpen && !state.updateActionPending) {
      refreshUpdateStatus(true);
    }
  });

  document.getElementById("updatePopover")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    const actBtn = e.target.closest("[data-update-action]");
    if (!actBtn || actBtn.disabled) return;
    const action = actBtn.dataset.updateAction;
    if (action === "check") {
      state.updateActionPending = true;
      renderUpdatePopover();
      try {
        const res = await refreshUpdateStatus(true);
        if (res) {
          showToast(res.hasUpdate ? `发现新版本 v${res.latestVersion}` : `当前已是最新版本 v${res.currentVersion}`);
        }
      } finally {
        state.updateActionPending = false;
        renderUpdatePill();
      }
    } else if (action === "apply" || action === "rollback") {
      await triggerHotUpdate(action);
    }
  });

  document.getElementById("aiStatusPopover")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    const actionBtn = e.target.closest("[data-ai-action]");
    if (!actionBtn || actionBtn.disabled) return;
    const action = actionBtn.dataset.aiAction;
    if (action === "refresh") {
      await refreshAgentStatus();
      showToast(state.serverUnreachable ? "仍未连接到本地服务 (127.0.0.1:8787)" : "已刷新后台并发桥接状态");
    } else if (action === "stop") {
      await runAiBridgeAction("/api/agent/bridge/stop", "已暂停本地 agy 后台桥接");
    } else if (action === "start") {
      await runAiBridgeAction("/api/agent/bridge/start", "已重连并启动本地 agy 后台桥接");
    } else if (action === "restart") {
      await runAiBridgeAction("/api/agent/bridge/restart", "已重连本地 agy 后台桥接");
    } else if (action === "open-terminal-agy") {
      await launchForegroundAppWindow("terminal_agy");
    } else if (action === "open-antigravity") {
      await launchForegroundAppWindow("antigravity");
    }
  });

  document.getElementById("platformSwitch")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-platform-btn]");
    if (!btn) return;
    e.stopPropagation();
    closeAiStatusPopover();
    closeUpdatePopover();
    closeShotMoreMenu();
    closeShotChatMenu();
    switchPlatform(btn.dataset.platformBtn);
  });

  document.getElementById("canvasPickerBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    closeAiStatusPopover();
    closeUpdatePopover();
    closeShotMoreMenu();
    closeShotChatMenu();
    state.canvasPicker.open = !state.canvasPicker.open;
    if (!state.canvasPicker.open) state.canvasPicker.creating = false;
    renderCanvasPopover();
    if (state.canvasPicker.open) {
      document.getElementById("canvasSearchInput")?.focus();
    }
  });

  document.getElementById("shotMoreBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    closeCanvasPopover();
    closeAiStatusPopover();
    closeUpdatePopover();
    closeShotChatMenu();
    state.shotMoreOpen = !state.shotMoreOpen;
    document.getElementById("shotMoreMenu")?.classList.toggle("hidden", !state.shotMoreOpen);
    const btn = document.getElementById("shotMoreBtn");
    btn?.classList.toggle("is-open", state.shotMoreOpen);
    btn?.setAttribute("aria-expanded", state.shotMoreOpen ? "true" : "false");
  });

  document.addEventListener("click", (e) => {
    if (state.canvasPicker.open && !e.target.closest("#canvasPickerWrap")) {
      closeCanvasPopover();
    }
    if (state.aiStatusOpen && !e.target.closest("#aiStatusWrap")) {
      closeAiStatusPopover();
    }
    if (state.updateMenuOpen && !e.target.closest("#updateWrap")) {
      closeUpdatePopover();
    }
    if (state.shotMoreOpen && !e.target.closest("#shotMoreWrap")) {
      closeShotMoreMenu();
    }
    if (state.shotChatMenuOpen && !e.target.closest("#shotChatWrap")) {
      closeShotChatMenu();
    }
  });

  document.getElementById("newShotNavBtn").addEventListener("click", () => {
    closeShotMoreMenu();
    closeShotChatMenu();
    if (!state.builder.shotIdTouched) {
      state.builder.shotId = suggestNextShotId(state.data?.shots || []);
    }
    syncBuilderSceneSelection(state.selectedSceneFolder);
    state.view = "builder";
    renderActiveView();
    const pane = document.getElementById("mainPane");
    if (pane) pane.scrollTop = 0;
  });

  document.getElementById("exportSettingsNavBtn")?.addEventListener("click", () => {
    const focusSc = state.selectedSceneFolder || "";
    openExportConfigModal(focusSc);
  });

  document.getElementById("topbarShotTitleBtn")?.addEventListener("click", () => {
    const shot = (state.data?.shots || []).find((s) => s.id === state.selectedShotId);
    if (shot) openRenameShotModal(shot);
  });

  document.getElementById("newSceneBtn")?.addEventListener("click", () => {
    state.editingFolderName = null;
    state.creatingFolder = true;
    renderSidebar();
  });

  const shotListEl = document.getElementById("shotList");
  shotListEl.addEventListener("click", async (e) => {
    if (e.target.closest("[data-rename-folder-input], [data-create-folder-input]")) return;

    const folderExportBtn = e.target.closest("[data-folder-export]");
    if (folderExportBtn) {
      e.stopPropagation();
      openExportConfigModal(folderExportBtn.dataset.folderExport || "");
      return;
    }

    const delFolderBtn = e.target.closest("[data-delete-folder]");
    if (delFolderBtn) {
      e.stopPropagation();
      const folderName = delFolderBtn.dataset.deleteFolder;
      const folderShots = (state.data?.shots || []).filter((s) => (s.scene || "").trim() === folderName);
      const confirmMsg = folderShots.length
        ? `该场下的 ${folderShots.length} 个镜头将安全移回「未分场」，专属角色将归入「通用」（不会删除视频和提示词）。`
        : `确定删除空场次「${folderName}」吗？该场专属角色将归入「通用」。`;
      openConfirmModal({
        title: `删除场次「${folderName}」？`,
        message: confirmMsg,
        okText: "确认删除",
        cancelText: "取消",
        danger: true,
        onConfirm: async () => {
          const res = await apiJson(`/api/scenes?name=${encodeURIComponent(folderName)}`, { method: "DELETE" });
          state.expandedFolders.delete(folderName);
          state.seenFolders.delete(folderName);
          if (state.selectedSceneFolder === folderName) {
            state.selectedSceneFolder = null;
          }
          if (state.selectedSceneTab === folderName) {
            state.selectedSceneTab = "通用";
          }
          if (state.builder.scene === folderName) {
            state.builder.scene = "";
            state.builder.sceneTouched = false;
          }
          showToast(
            res.movedCount
              ? `已删除场次「${folderName}」，${res.movedCount} 个镜头已移回未分场`
              : `已删除场次「${folderName}」`
          );
          await refreshState();
        },
      });
      return;
    }

    const folderBtn = e.target.closest("[data-select-folder]");
    if (folderBtn) {
      const folderName = folderBtn.dataset.selectFolder;
      const wasExpanded = state.expandedFolders.has(folderName);
      const clickedToggle = Boolean(e.target.closest("[data-folder-toggle]"));

      state.selectedSceneFolder = folderName;
      let nextExpanded = wasExpanded;
      if (clickedToggle) {
        nextExpanded = !wasExpanded;
        if (nextExpanded) state.expandedFolders.add(folderName);
        else state.expandedFolders.delete(folderName);
      } else {
        nextExpanded = true;
        state.expandedFolders.add(folderName);
      }

      // Preserve the clicked folder DOM node so browser native dblclick can complete across clicks.
      updateSidebarFolderDom(folderBtn, folderName, nextExpanded);
      syncBuilderSceneSelection(folderName);
      if (state.view === "builder") {
        renderBuilderView();
      }
      return;
    }

    const btn = e.target.closest("[data-select-shot]");
    if (!btn) return;
    const shot = (state.data?.shots || []).find((s) => s.id === btn.dataset.selectShot);
    if (shot) {
      const folderName = getShotFolderName(shot);
      state.selectedSceneFolder = folderName;
      state.expandedFolders.add(folderName);
    }
    state.selectedShotId = btn.dataset.selectShot;
    state.view = "detail";
    renderActiveView();
    const pane = document.getElementById("mainPane");
    if (pane) pane.scrollTop = 0;
  });

  shotListEl.addEventListener("dblclick", (e) => {
    if (e.target.closest("[data-folder-toggle], [data-delete-folder], [data-rename-folder-input], [data-create-folder-input]")) return;
    const folderBtn = e.target.closest("[data-select-folder]");
    if (!folderBtn) return;
    const folderName = folderBtn.dataset.selectFolder;
    state.creatingFolder = false;
    state.selectedSceneFolder = folderName;
    state.expandedFolders.add(folderName);
    state.editingFolderName = folderName;
    renderSidebar();
  });

  shotListEl.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea")) return;
    if (e.key !== "Enter" && e.key !== " ") return;
    const target = e.target.closest("[data-select-shot], [data-select-folder]");
    if (!target) return;
    e.preventDefault();
    target.click();
  });

  const clearSidebarDropTargets = () => {
    for (const el of shotListEl.querySelectorAll(".scene-folder.drop-target")) {
      el.classList.remove("drop-target");
    }
  };

  const getDraggedShotFolder = (shotId) => {
    const shot = (state.data?.shots || []).find((s) => s.id === shotId);
    if (shot) return getShotFolderName(shot);
    const el = shotListEl.querySelector(`[data-select-shot="${CSS.escape(shotId)}"]`);
    return el?.dataset.shotScene || "";
  };

  const isShotDragEvent = (e) =>
    Boolean(state.draggingShotId || Array.from(e.dataTransfer?.types || []).includes("text/x-shot-id"));

  shotListEl.addEventListener("dragstart", (e) => {
    const shotEl = e.target.closest("[data-select-shot]");
    if (!shotEl || state.editingFolderName || state.creatingFolder) return;
    const shotId = shotEl.dataset.selectShot;
    state.draggingShotId = shotId;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/x-shot-id", shotId);
      e.dataTransfer.setData("text/plain", shotId);
    }
    requestAnimationFrame(() => shotEl.classList.add("dragging"));
  });

  shotListEl.addEventListener("dragend", () => {
    state.draggingShotId = null;
    clearSidebarDropTargets();
    for (const el of shotListEl.querySelectorAll(".shot-item.dragging")) {
      el.classList.remove("dragging");
    }
  });

  shotListEl.addEventListener("dragover", (e) => {
    if (!isShotDragEvent(e)) return;
    const folderEl = e.target.closest(".scene-folder[data-scene-folder]");
    if (!folderEl) {
      clearSidebarDropTargets();
      return;
    }
    const targetFolder = folderEl.dataset.sceneFolder;
    const shotId = state.draggingShotId;
    if (shotId && getDraggedShotFolder(shotId) === targetFolder) {
      clearSidebarDropTargets();
      return;
    }
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
    for (const el of shotListEl.querySelectorAll(".scene-folder[data-scene-folder]")) {
      el.classList.toggle("drop-target", el === folderEl);
    }
  });

  shotListEl.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget || !shotListEl.contains(e.relatedTarget)) {
      clearSidebarDropTargets();
    }
  });

  shotListEl.addEventListener("drop", async (e) => {
    if (!isShotDragEvent(e)) return;
    const folderEl = e.target.closest(".scene-folder[data-scene-folder]");
    const shotId = e.dataTransfer?.getData("text/x-shot-id") || state.draggingShotId;
    clearSidebarDropTargets();
    state.draggingShotId = null;
    if (!folderEl || !shotId) return;

    const targetFolder = folderEl.dataset.sceneFolder;
    if (getDraggedShotFolder(shotId) === targetFolder) return;

    e.preventDefault();
    const apiScene = targetFolder === "未分场" ? "" : targetFolder;
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shotId)}/scene`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scene: apiScene }),
      });
      state.selectedSceneFolder = targetFolder;
      state.expandedFolders.add(targetFolder);
      state.seenFolders.add(targetFolder);
      if (!state.builder.sceneTouched) {
        syncBuilderSceneSelection(targetFolder);
      }
      showToast(`已将 ${shotId} 移入「${targetFolder}」`);
      await refreshState();
      if (state.view === "builder") renderBuilderView();
    } catch (err) {
      showToast(`移动失败: ${err.message}`);
      renderSidebar();
    }
  });

  // Delegated click for runtime status buttons, original comparison preview, Paint Canvas, or video Lightbox
  document.getElementById("mainPane").addEventListener("click", (e) => {
    const rtDetailBtn = e.target.closest("#openShotRuntimeBtn, #runtimeBannerDetailBtn");
    if (rtDetailBtn) {
      e.stopPropagation();
      if (state.selectedShotId) openShotRuntimeModal(state.selectedShotId);
      return;
    }
    const rtRefreshBtn = e.target.closest("#runtimeBannerRefreshBtn");
    if (rtRefreshBtn) {
      e.stopPropagation();
      if (state.selectedShotId) fetchShotRuntimeStatus(state.selectedShotId, { showToastOnResult: true });
      return;
    }
    const rtRetryBtn = e.target.closest("#runtimeBannerRetryBtn");
    if (rtRetryBtn) {
      e.stopPropagation();
      const curShot = (state.data?.shots || []).find((s) => s.id === state.selectedShotId);
      if (curShot) retryShotProcessing(curShot.id, "retry");
      return;
    }
    const origBtn = e.target.closest("[data-zoom-orig-url]");
    if (origBtn) {
      e.stopPropagation();
      openLightbox(origBtn.dataset.zoomOrigUrl, "image", origBtn.dataset.zoomCaption);
      return;
    }
    const mediaEl = e.target.closest("[data-zoom-url]");
    if (!mediaEl) return;
    if (mediaEl.dataset.zoomKind === "image" && mediaEl.dataset.paintSlot) {
      openImagePaintModal({
        url: mediaEl.dataset.zoomUrl,
        origUrl: mediaEl.dataset.paintOrig || mediaEl.dataset.zoomUrl,
        caption: mediaEl.dataset.zoomCaption,
        mode: mediaEl.dataset.paintMode || "detail",
        slotTag: mediaEl.dataset.paintSlot,
        slotId: mediaEl.dataset.paintId || "",
        shotId: mediaEl.dataset.paintMode === "detail" ? state.selectedShotId || "" : "",
      });
      return;
    }
    openLightbox(mediaEl.dataset.zoomUrl, mediaEl.dataset.zoomKind, mediaEl.dataset.zoomCaption);
  });

  document.getElementById("topbarScenePill").addEventListener("click", async () => {
    const shot = (state.data?.shots || []).find((s) => s.id === state.selectedShotId);
    if (!shot) return;
    const nextScene = window.prompt(
      `设置镜头 ${shot.id} 的所属场号（留空则归入“未分场”，例如：第75场）：`,
      shot.scene || ""
    );
    if (nextScene === null) return;
    const trimmed = nextScene.trim();
    try {
      await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/scene`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scene: trimmed }),
      });
      const folderName = trimmed || "未分场";
      state.selectedSceneFolder = folderName;
      state.expandedFolders.add(folderName);
      showToast(trimmed ? `已归入「${trimmed}」` : "已移入「未分场」");
      await refreshState();
    } catch (err) {
      showToast(err.message);
    }
  });

  document.getElementById("topbarReprocessBtn").addEventListener("click", () => {
    closeShotMoreMenu();
    const shot = (state.data?.shots || []).find((s) => s.id === state.selectedShotId);
    if (!shot) return;
    if (shot.status === "recheck_requested") {
      showToast(`${shot.id} 已在重检队列中，等待 Agent 核查`);
      return;
    }
    const executeReprocess = async () => {
      state.detailPromptEditor?.syncAssets(buildDetailMentionAssets(shot));
      const baseline = getShotBaselinePrompt(shot);
      const edited = state.detailDraft.editedPrompt || "";
      const payload = { reason: "用户在控制台点击「重新处理·自检」" };
      if (edited.trim() && edited.trim() !== baseline) {
        payload.edited_prompt = edited;
      }
      try {
        await apiJson(`/api/shots/${encodeURIComponent(shot.id)}/reprocess`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (payload.edited_prompt) {
          state.detailDraft.lastSyncedPrompt = edited;
        }
        showToast(`已提交 ${shot.id} 一键重新处理与自检`);
        await refreshState();
      } catch (err) {
        showToast(err.message);
      }
    };
    guardPassedFinalAction(shot, {
      actionName: "重新处理并自检",
      consequenceText: "确认后将解锁定版状态并通知 Agent 重新核查画布连线与配置。",
      okText: "确认解锁并重检",
      onProceed: executeReprocess,
    });
  });

  document.getElementById("topbarDeleteShotBtn").addEventListener("click", () => {
    closeShotMoreMenu();
    const shotId = state.selectedShotId;
    if (!shotId) return;
    openConfirmModal({
      title: `删除镜头 ${shotId}？`,
      message: `删除后将移除「${shotId}」本地队列记录与素材文件，此操作不可撤销。`,
      okText: "确认删除",
      cancelText: "取消",
      danger: true,
      onConfirm: async () => {
        await apiJson(`/api/shots/${encodeURIComponent(shotId)}`, { method: "DELETE" });
        showToast(`已删除 ${shotId}`);
        state.selectedShotId = null;
        state.view = "builder";
        await refreshState();
      },
    });
  });

  // Preset modal
  document.getElementById("closePresetModalBtn").addEventListener("click", closePresetModal);
  document.getElementById("cancelPresetBtn").addEventListener("click", closePresetModal);
  document.getElementById("presetForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const editId = document.getElementById("presetEditId").value.trim();
    const name = document.getElementById("presetNameInput").value.trim();
    const scene = document.getElementById("presetSceneInput").value.trim() || "通用";
    const roleHint = document.getElementById("presetHintInput").value.trim();

    try {
      if (editId) {
        await apiJson(`/api/presets/${encodeURIComponent(editId)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, scene, roleHint }),
        });
        state.selectedSceneTab = scene;
        showToast(`已更新角色「${name}」`);
      } else {
        const file = document.getElementById("presetFileInput").files?.[0];
        if (!file) {
          showToast("请选择角色参考图");
          return;
        }
        const fd = new FormData();
        fd.append("name", name);
        fd.append("scene", scene);
        fd.append("role_hint", roleHint);
        fd.append("file", file);
        await apiJson("/api/presets", { method: "POST", body: fd });
        state.selectedSceneTab = scene;
        showToast(`已添加角色「${name}」到 ${scene}`);
      }
      closePresetModal();
      await refreshState({ preserveEditing: state.view === "builder" });
      if (state.view === "builder") renderBuilderView();
    } catch (err) {
      showToast(err.message);
    }
  });

  document.getElementById("deletePresetBtn").addEventListener("click", () => {
    const editId = document.getElementById("presetEditId").value.trim();
    if (!editId) return;
    openConfirmModal({
      title: "删除常用角色？",
      message: "确定从角色库移除该角色吗？",
      okText: "确认删除",
      cancelText: "取消",
      danger: true,
      onConfirm: async () => {
        await apiJson(`/api/presets/${encodeURIComponent(editId)}`, { method: "DELETE" });
        const kept = [];
        for (const it of state.builder.imageItems) {
          if (getBuilderItemPresetId(it) === editId) {
            if (it.url?.startsWith("blob:")) URL.revokeObjectURL(it.url);
            if (it.originalUrl?.startsWith("blob:") && it.originalUrl !== it.url) {
              URL.revokeObjectURL(it.originalUrl);
            }
          } else {
            kept.push(it);
          }
        }
        state.builder.imageItems = kept;
        closePresetModal();
        showToast("已删除角色");
        await refreshState({ preserveEditing: state.view === "builder" });
        if (state.view === "builder") renderBuilderView();
      },
    });
  });

  // History & Confirm modals
  document.getElementById("closeHistoryModalBtn")?.addEventListener("click", closeHistoryModal);
  document.getElementById("historyModal")?.addEventListener("click", (e) => {
    if (e.target.id === "historyModal") closeHistoryModal();
  });

  document.getElementById("closeConfirmModalBtn")?.addEventListener("click", closeConfirmModal);
  document.getElementById("confirmCancelBtn")?.addEventListener("click", closeConfirmModal);
  document.getElementById("confirmModal")?.addEventListener("click", (e) => {
    if (e.target.id === "confirmModal") closeConfirmModal();
  });
  document.getElementById("confirmOkBtn")?.addEventListener("click", async () => {
    const fn = state.confirmDialog?.onConfirm;
    const okBtn = document.getElementById("confirmOkBtn");
    if (okBtn) {
      okBtn.disabled = true;
      okBtn.textContent = "处理中…";
    }
    try {
      closeConfirmModal();
      if (typeof fn === "function") await fn();
    } catch (err) {
      showToast(err.message || "操作失败");
    }
  });

  // Rename Shot Modal
  document.getElementById("closeRenameModalBtn")?.addEventListener("click", closeRenameShotModal);
  document.getElementById("cancelRenameBtn")?.addEventListener("click", closeRenameShotModal);
  document.getElementById("renameShotModal")?.addEventListener("click", (e) => {
    if (e.target.id === "renameShotModal") closeRenameShotModal();
  });
  document.getElementById("renameShotTitleInput")?.addEventListener("input", (e) => {
    const shot = (state.data?.shots || []).find((s) => s.id === state.renameModal.shotId);
    if (shot) updateRenameModalPreview(shot, e.target.value);
  });

  const handleSaveShotRename = async ({ downloadAfter = false } = {}) => {
    const sid = state.renameModal.shotId;
    const dlVer = state.renameModal.downloadVerAfter || "final";
    if (!sid) return;
    const nextTitle = document.getElementById("renameShotTitleInput")?.value.trim() || "";
    const syncLocal = Boolean(document.getElementById("renameSyncLocalCheck")?.checked);
    const saveBtn = document.getElementById("saveRenameOnlyBtn");
    const dlBtn = document.getElementById("saveAndDownloadBtn");
    if (saveBtn) saveBtn.disabled = true;
    if (dlBtn) dlBtn.disabled = true;
    try {
      const res = await apiJson(`/api/shots/${encodeURIComponent(sid)}/meta`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: nextTitle, syncLocal }),
      });
      const updatedShot = res.shot;
      const syncedList = res.syncedExports || [];
      closeRenameShotModal();
      if (syncedList.length) {
        const first = syncedList[0];
        showToast(
          first.renamedFrom
            ? `已重命名并同步更新本地文件为：${first.fileName}`
            : `已保存命名并同步本地目录：${first.fileName}`
        );
      } else {
        showToast(nextTitle ? `已命名为「${nextTitle}」` : `已重置 ${sid} 为默认编号命名`);
      }
      await refreshState();
      if (downloadAfter && updatedShot) {
        triggerBrowserMediaDownload(updatedShot, dlVer);
      }
    } catch (err) {
      showToast(err.message || "保存镜头命名失败");
    } finally {
      if (saveBtn) saveBtn.disabled = false;
      if (dlBtn) dlBtn.disabled = false;
    }
  };
  document.getElementById("renameShotForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    handleSaveShotRename({ downloadAfter: false });
  });
  document.getElementById("saveAndDownloadBtn")?.addEventListener("click", () => {
    handleSaveShotRename({ downloadAfter: true });
  });

  // Export Dir Config Modal
  document.getElementById("closeExportConfigModalBtn")?.addEventListener("click", closeExportConfigModal);
  document.getElementById("cancelExportConfigBtn")?.addEventListener("click", closeExportConfigModal);
  document.getElementById("exportConfigModal")?.addEventListener("click", (e) => {
    if (e.target.id === "exportConfigModal") closeExportConfigModal();
  });
  document.getElementById("pickDefaultExportDirBtn")?.addEventListener("click", () => {
    pickFolderIntoInput(document.getElementById("exportDefaultDirInput"), "选择全局默认成片同步目录");
  });
  document.getElementById("clearDefaultExportDirBtn")?.addEventListener("click", () => {
    const inp = document.getElementById("exportDefaultDirInput");
    if (inp) inp.value = "";
  });
  document.getElementById("exportSceneDirsList")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-pick-scene-folder]");
    if (!btn) return;
    const sc = btn.dataset.pickSceneFolder;
    const row = btn.closest("[data-scene-dir-row]");
    const inp = row?.querySelector("[data-scene-dir-input]");
    pickFolderIntoInput(inp, `选择「${sc}」成片同步目录`);
  });

  const saveExportConfig = async ({ syncAllAfter = false } = {}) => {
    const saveBtn = document.getElementById("saveExportConfigBtn");
    const syncBtn = document.getElementById("exportSyncAllBtn");
    if (saveBtn) saveBtn.disabled = true;
    if (syncBtn) syncBtn.disabled = true;
    try {
      const cfgPayload = readExportConfigFormValues();
      await apiJson("/api/config/export-dirs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfgPayload),
      });
      if (syncAllAfter) {
        const res = await apiJson("/api/exports/sync-all", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ scene: "", role: "1080p" }),
        });
        showToast(
          `已保存设置，同步 ${res.syncedCount || 0} 个 1080P 成片（其中新写入/更新 ${res.updatedCount || 0} 个）`
        );
      } else {
        showToast("已保存本地成片同步目录设置");
      }
      closeExportConfigModal();
      await refreshState();
    } catch (err) {
      showToast(err.message || "保存同步目录设置失败");
    } finally {
      if (saveBtn) saveBtn.disabled = false;
      if (syncBtn) syncBtn.disabled = false;
    }
  };
  document.getElementById("exportConfigForm")?.addEventListener("submit", (e) => {
    e.preventDefault();
    saveExportConfig({ syncAllAfter: false });
  });
  document.getElementById("exportSyncAllBtn")?.addEventListener("click", () => {
    saveExportConfig({ syncAllAfter: true });
  });

  // Agent Runtime Modal
  document.getElementById("closeRuntimeModalBtn")?.addEventListener("click", closeShotRuntimeModal);
  document.getElementById("agentRuntimeModal")?.addEventListener("click", (e) => {
    if (e.target.id === "agentRuntimeModal") closeShotRuntimeModal();
  });
  document.getElementById("runtimeModalRefreshBtn")?.addEventListener("click", () => {
    if (state.runtimeModal.shotId) fetchShotRuntimeStatus(state.runtimeModal.shotId, { showToastOnResult: true });
  });
  document.getElementById("runtimeModalRetryBtn")?.addEventListener("click", () => {
    if (state.runtimeModal.shotId) retryShotProcessing(state.runtimeModal.shotId, "retry");
  });
  document.getElementById("runtimeModalRecheckBtn")?.addEventListener("click", () => {
    if (state.runtimeModal.shotId) retryShotProcessing(state.runtimeModal.shotId, "recheck");
  });
  document.getElementById("runtimeModalRestoreBtn")?.addEventListener("click", () => {
    if (state.runtimeModal.shotId) retryShotProcessing(state.runtimeModal.shotId, "restore");
  });

  // Lightbox & Paint Canvas modal
  bindPaintModalEvents();
  document.getElementById("closeLightboxBtn").addEventListener("click", closeLightbox);
  document.getElementById("lightboxModal").addEventListener("click", (e) => {
    if (e.target.id === "lightboxModal" && !paintState.isDrawing && !paintState.isPanning) {
      closeLightbox();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (state.confirmDialog?.open) {
        closeConfirmModal();
        return;
      }
      if (state.renameModal?.open) {
        closeRenameShotModal();
        return;
      }
      if (state.exportConfigModal?.open) {
        closeExportConfigModal();
        return;
      }
      if (state.runtimeModal?.open) {
        closeShotRuntimeModal();
        return;
      }
      if (!document.getElementById("lightboxModal")?.classList.contains("hidden")) {
        closeLightbox();
        return;
      }
      if (state.historyModal?.open) {
        closeHistoryModal();
        return;
      }
      closeCanvasPopover();
      closeAiStatusPopover();
      closeUpdatePopover();
      closeShotMoreMenu();
      closeShotChatMenu();
      closePresetModal();
      return;
    }
    if (!paintState.isPaint) return;
    if (e.code === "Space" && !e.repeat) {
      e.preventDefault();
      paintState.spacePressed = true;
      updatePaintToolbarUi();
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (paintState.strokes.length) {
        paintState.strokes.pop();
        rebuildPaintWorkCanvas();
      }
    }
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && paintState.spacePressed) {
      paintState.spacePressed = false;
      updatePaintToolbarUi();
    }
  });
}

async function init() {
  bindGlobalEvents();
  try {
    await refreshState();
    refreshUpdateStatus(false);
    setInterval(refreshAgentStatus, 4000);
    setInterval(() => refreshUpdateStatus(true), 20 * 60 * 1000);
    startEventLoop();
  } catch (err) {
    showToast(`初始化失败: ${err.message}`);
  }
}

init();
