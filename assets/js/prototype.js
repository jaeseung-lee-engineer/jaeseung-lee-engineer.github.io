const API_BASE_URL = "https://jaeseung-lee-engineer-github-io.onrender.com";
const CASE_DATA_URL = "https://jaeseung-lee.s3.us-east-2.amazonaws.com/public-test/case-data.json";
const S3_ASSET_BASE_URL = "https://jaeseung-lee.s3.us-east-2.amazonaws.com/public-test/";

let caseData = {};
let caseSummaries = [];
let hasFullDatasetFallback = false;

let currentCaseId = "";
let currentSlideIndex = -1;
let heatmapOn = false;
let viewer = null;
let isDemoFullscreen = false;
const roiStorageKey = "digital-pathology-rois-v1";
const savedRois = loadSavedRois();
let activeRoiEditId = null;
let suppressRoiEditReset = false;
let roiOverlayDragState = null;
let roiOverlayRenderFrame = 0;
function loadSavedRois() {
  try {
    const raw = window.localStorage.getItem(roiStorageKey);
    return raw ? JSON.parse(raw) : {};
  } catch (error) {
    return {};
  }
}

function normalizeAssetUrl(assetPath) {
  if (!assetPath) return assetPath;
  if (/^https?:\/\//.test(assetPath)) return assetPath;

  return `${S3_ASSET_BASE_URL}${assetPath.replace(/^images\//, "")}`;
}

function normalizeCaseDataAssets(data) {
  Object.values(data).forEach((entry) => {
    if (!entry || !Array.isArray(entry.slides)) return;

    entry.slides.forEach((slide) => {
      slide.thumbnail = normalizeAssetUrl(slide.thumbnail);
      slide.preview = normalizeAssetUrl(slide.preview);
    });
  });
}

function normalizeSingleCaseAssets(entry) {
  if (!entry || !Array.isArray(entry.slides)) return entry;

  entry.slides.forEach((slide) => {
    slide.thumbnail = normalizeAssetUrl(slide.thumbnail);
    slide.preview = normalizeAssetUrl(slide.preview);
  });

  return entry;
}

function getApiUrl(path) {
  if (!API_BASE_URL) return "";
  return `${API_BASE_URL.replace(/\/$/, "")}${path}`;
}

async function loadCaseSummariesFromApi() {
  const apiUrl = getApiUrl("/cases");
  if (!apiUrl) {
    throw new Error("API base URL is not configured.");
  }

  const response = await fetch(apiUrl, {
    method: "GET",
    mode: "cors",
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error(`Failed to load case summaries from API: ${response.status}`);
  }

  const payload = await response.json();
  caseSummaries = Array.isArray(payload.cases) ? payload.cases : [];
}

async function loadCaseDataFromApi(caseId) {
  const apiUrl = getApiUrl(`/cases/${encodeURIComponent(caseId)}`);
  if (!apiUrl) {
    throw new Error("API base URL is not configured.");
  }

  const response = await fetch(apiUrl, {
    method: "GET",
    mode: "cors",
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error(`Failed to load case data from API: ${response.status}`);
  }

  const entry = await response.json();
  caseData[caseId] = normalizeSingleCaseAssets(entry);
  return caseData[caseId];
}

async function loadFullCaseDataFromS3() {
  const response = await fetch(CASE_DATA_URL, {
    method: "GET",
    mode: "cors",
    cache: "no-store"
  });

  if (!response.ok) {
    throw new Error(`Failed to load case data: ${response.status}`);
  }

  caseData = await response.json();
  normalizeCaseDataAssets(caseData);
  caseSummaries = Object.entries(caseData).map(([caseId, entry]) => ({
    caseId,
    diagnosis: entry.diagnosis,
    grade: entry.grade,
    project: entry.project,
    site: entry.site,
    slidesLinked: entry.slidesLinked,
    status: entry.status
  }));
  hasFullDatasetFallback = true;
}

async function loadCaseList() {
  try {
    await loadCaseSummariesFromApi();
  } catch (apiError) {
    console.warn("API case summary load failed; falling back to S3.", apiError);
    await loadFullCaseDataFromS3();
  }
}

async function ensureCaseLoaded(caseId) {
  if (caseData[caseId]) return caseData[caseId];

  if (hasFullDatasetFallback) {
    return caseData[caseId] || null;
  }

  try {
    return await loadCaseDataFromApi(caseId);
  } catch (apiError) {
    console.warn(`API case load failed for ${caseId}; falling back to S3.`, apiError);
    await loadFullCaseDataFromS3();
    return caseData[caseId] || null;
  }
}

function persistSavedRois() {
  try {
    window.localStorage.setItem(roiStorageKey, JSON.stringify(savedRois));
  } catch (error) {
    setViewerStatus("Unable to persist ROI data in this browser.");
  }
}

function getCurrentSlide() {
  const slides = caseData[currentCaseId]?.slides;
  if (!Array.isArray(slides) || currentSlideIndex < 0 || currentSlideIndex >= slides.length) {
    return null;
  }

  return slides[currentSlideIndex];
}

function getRoiStorageKey() {
  const slide = getCurrentSlide();
  if (!slide) return `${currentCaseId}::no-slide`;
  return `${currentCaseId}::${slide.slideId}`;
}

function getCurrentRois() {
  return savedRois[getRoiStorageKey()] || [];
}

function getFilteredCaseIds(query) {
  const q = (query || "").trim().toLowerCase();
  const allCaseIds = caseSummaries.map((entry) => entry.caseId);
  if (!q) return allCaseIds;

  return caseSummaries
    .filter((entry) => {
      const loadedCase = caseData[entry.caseId];
      const slideText = loadedCase && Array.isArray(loadedCase.slides)
        ? loadedCase.slides
          .map((slide) =>
            `${slide.label} ${slide.submitterId} ${slide.slideId} ${slide.section || ""} ${slide.stain || ""}`.toLowerCase()
          )
          .join(" ")
        : "";

      return (
        entry.caseId.toLowerCase().includes(q) ||
        (entry.diagnosis || "").toLowerCase().includes(q) ||
        (entry.site || "").toLowerCase().includes(q) ||
        (entry.project || "").toLowerCase().includes(q) ||
        slideText.includes(q)
      );
    })
    .map((entry) => entry.caseId);
}

function renderQuickCaseList(caseIds) {
  const ids = caseIds && caseIds.length ? caseIds : caseSummaries.map((entry) => entry.caseId);
  const quickList = document.getElementById("quickCaseList");

  if (!ids.length) {
    quickList.replaceChildren(createEmptyText("No matching cases found."));
    return;
  }

  quickList.replaceChildren(...ids.map((caseId) => {
    const button = document.createElement("button");
    button.className = `btn${caseId === currentCaseId ? " primary" : ""}`;
    button.type = "button";
    button.dataset.case = caseId;
    button.textContent = caseId;
    button.addEventListener("click", () => selectCase(caseId));
    return button;
  }));
}

function createEmptyText(message) {
  const emptyText = document.createElement("div");
  emptyText.className = "empty-text";
  emptyText.textContent = message;
  return emptyText;
}

function createThumbnailPlaceholder() {
  const placeholder = document.createElement("div");
  placeholder.className = "thumb-placeholder";
  placeholder.textContent = "Thumbnail";
  return placeholder;
}

function createThumbnailNode(slide) {
  const thumbContainer = document.createElement("div");
  thumbContainer.className = "slide-list-thumb";

  if (!slide.thumbnail) {
    thumbContainer.appendChild(createThumbnailPlaceholder());
    return thumbContainer;
  }

  const image = document.createElement("img");
  image.src = slide.thumbnail;
  image.alt = `${slide.label || "Slide"} thumbnail`;
  image.addEventListener("error", () => {
    thumbContainer.replaceChildren(createThumbnailPlaceholder());
  });

  thumbContainer.appendChild(image);
  return thumbContainer;
}

function renderSlides(caseId) {
  const slideThumbs = document.getElementById("slideThumbs");
  const slides = caseData[caseId].slides;

  currentSlideIndex = -1;

  slideThumbs.replaceChildren(...slides.map((slide, idx) => {
    const activeClass = idx === currentSlideIndex ? "active-thumb" : "";
    const quantTag = slide.hasQuant ? "Quant data" : "No data";

    const card = document.createElement("div");
    card.className = `slide-list-card ${activeClass}`.trim();
    card.dataset.slideIndex = String(idx);
    card.addEventListener("click", () => selectSlide(idx));

    card.appendChild(createThumbnailNode(slide));

    const meta = document.createElement("div");
    meta.className = "slide-list-meta";

    const top = document.createElement("div");
    top.className = "slide-list-top";

    const label = document.createElement("div");
    label.className = "slide-list-label";
    label.textContent = slide.label || "-";

    const status = document.createElement("span");
    status.className = "status-tag";
    status.textContent = quantTag;

    top.append(label, status);

    const subtext = document.createElement("div");
    subtext.className = "slide-list-subtext";
    subtext.textContent = `${slide.section || "Unknown"} · ${slide.stain || "H&E"}`;

    meta.append(top, subtext);
    card.appendChild(meta);
    return card;
  }));
}

function renderCaseHeader(data) {
  document.getElementById("caseTitle").textContent = currentCaseId;
  document.getElementById("heroDiagnosis").textContent =
    `${data.diagnosis}${data.grade && data.grade !== "Not Reported" ? ` (${data.grade})` : ""}`;
  document.getElementById("ageSex").textContent = `${data.age} / ${data.sex}`;
  document.getElementById("vitalStatus").textContent = data.status;
  document.getElementById("tumorContext").textContent = data.tumorContext;
  document.getElementById("slidesLinked").textContent = data.slidesLinked;
}

function renderCaseDetails(data) {
  document.getElementById("detailProject").textContent = data.project || "-";
  document.getElementById("detailDiseaseType").textContent = data.diseaseType || "-";
  document.getElementById("detailDiagnosis").textContent = data.diagnosis || "-";
  document.getElementById("detailGrade").textContent = data.grade || "-";
  document.getElementById("detailPrimarySite").textContent = data.primarySite || "-";
  document.getElementById("detailTissueOrigin").textContent = data.tissueOrigin || "-";
  document.getElementById("detailResectionSite").textContent = data.resectionSite || "-";
  document.getElementById("detailMorphology").textContent = data.morphology || "-";
  document.getElementById("detailFollowup").textContent = data.followup || "-";
  document.getElementById("detailRaceEthnicity").textContent = `${data.race || "-"} / ${data.ethnicity || "-"}`;
}

function setViewerStatus(message) {
  const statusElement = document.getElementById("viewerStatus");
  if (statusElement) {
    statusElement.textContent = message;
  }
}

function setViewerPlaceholder(title, message) {
  const slidePreview = document.getElementById("slidePreview");
  if (!slidePreview) return;

  const wrapper = document.createElement("div");
  wrapper.className = "viewer-empty-state";

  const card = document.createElement("div");
  card.className = "viewer-empty-card";

  const heading = document.createElement("h4");
  heading.textContent = title;

  const body = document.createElement("p");
  body.textContent = message;

  card.append(heading, body);
  wrapper.appendChild(card);
  slidePreview.replaceChildren(wrapper);
}

function buildCompositionRow(label, value) {
  if (value === null || value === undefined || value === "--") return null;

  const numericValue = Number(value);
  if (!Number.isFinite(numericValue)) return null;

  const row = document.createElement("div");
  row.className = "composition-row";

  const head = document.createElement("div");
  head.className = "composition-head";

  const labelNode = document.createElement("span");
  labelNode.textContent = label;

  const valueNode = document.createElement("strong");
  valueNode.textContent = `${numericValue}%`;

  head.append(labelNode, valueNode);

  const bar = document.createElement("div");
  bar.className = "bar";

  const fill = document.createElement("span");
  fill.style.width = `${Math.max(0, Math.min(100, numericValue))}%`;
  bar.appendChild(fill);

  row.append(head, bar);
  return row;
}

function formatRoiRect(rect) {
  return [
    `x ${rect.x.toFixed(0)}`,
    `y ${rect.y.toFixed(0)}`,
    `w ${rect.width.toFixed(0)}`,
    `h ${rect.height.toFixed(0)}`
  ].join("  ");
}

function sanitizeFilename(value) {
  return (value || "slide")
    .replace(/[^a-z0-9._-]+/gi, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "");
}

function triggerFileDownload(filename, blob) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function triggerUrlDownload(url) {
  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function buildRoiGeoJson(slide, rois) {
  return {
    type: "FeatureCollection",
    name: `${slide.submitterId}-portal-rois`,
    features: rois.map((roi) => {
      const x = Number(roi.imageRect.x.toFixed(3));
      const y = Number(roi.imageRect.y.toFixed(3));
      const width = Number(roi.imageRect.width.toFixed(3));
      const height = Number(roi.imageRect.height.toFixed(3));
      const shape = roi.shape || "rectangle";
      const circleCoordinates = Array.from({ length: 25 }, (_, index) => {
        const theta = (Math.PI * 2 * index) / 24;
        const centerX = x + width / 2;
        const centerY = y + height / 2;
        return [
          Number((centerX + (width / 2) * Math.cos(theta)).toFixed(3)),
          Number((centerY + (height / 2) * Math.sin(theta)).toFixed(3))
        ];
      });

      return {
        type: "Feature",
        properties: {
          name: roi.name,
          note: roi.note || "",
          shape,
          slideId: slide.slideId,
          submitterId: slide.submitterId,
          zoom: Number(roi.zoom.toFixed(4)),
          savedAt: roi.savedAt
        },
        geometry: {
          type: "Polygon",
          coordinates: [shape === "circle"
            ? circleCoordinates
            : [
              [x, y],
              [x + width, y],
              [x + width, y + height],
              [x, y + height],
              [x, y]
            ]]
        }
      };
    })
  };
}

function buildQuPathScript(slide, rois) {
  const roiPayload = rois.map((roi) => ({
    name: roi.name,
    note: roi.note || "",
    shape: roi.shape || "rectangle",
    x: Number(roi.imageRect.x.toFixed(3)),
    y: Number(roi.imageRect.y.toFixed(3)),
    width: Number(roi.imageRect.width.toFixed(3)),
    height: Number(roi.imageRect.height.toFixed(3))
  }));
  const groovyRoiList = roiPayload.length
    ? roiPayload.map((roi) => `  [
name: ${JSON.stringify(roi.name)},
note: ${JSON.stringify(roi.note)},
shape: ${JSON.stringify(roi.shape)},
x: ${roi.x},
y: ${roi.y},
width: ${roi.width},
height: ${roi.height}
  ]`).join(",\n")
    : "";

  return `import qupath.lib.objects.PathObjects
import qupath.lib.regions.ImagePlane
import qupath.lib.roi.ROIs

def rois = [
${groovyRoiList}
]

if (getCurrentImageData() == null) {
print "Open the matching SVS in QuPath before running this script."
return
}

rois.each { roi ->
def annotation = PathObjects.createAnnotationObject(
    roi.shape == 'circle'
        ? ROIs.createEllipseROI(roi.x, roi.y, roi.width, roi.height, ImagePlane.getDefaultPlane())
        : ROIs.createRectangleROI(roi.x, roi.y, roi.width, roi.height, ImagePlane.getDefaultPlane())
)
annotation.setName(roi.name)
if (roi.note) {
    annotation.setDescription(roi.note)
}
addObject(annotation)
}

println "Imported ${roiPayload.length} ROI annotation(s) from the portal package for ${slide.submitterId}."
`;
}

function buildQuPathReadme(caseInfo, slide, rois) {
  return [
    "QuPath Handoff Package",
    "=====================",
    "",
    `Case ID: ${currentCaseId}`,
    `Slide label: ${slide.label}`,
    `Submitter ID: ${slide.submitterId}`,
    `Slide ID: ${slide.slideId}`,
    `Saved ROI count: ${rois.length}`,
    "",
    "Recommended workflow:",
    "1. Open the matching SVS in QuPath.",
    "2. Run open_in_qupath.groovy in the QuPath script editor to recreate saved ROIs.",
    "3. Optionally keep roi.geojson and slide-info.json with your project record.",
    "",
    "Notes:",
    "- ROI coordinates are exported in full-resolution image pixel units.",
    "- This package is generated from the portal's current saved ROI views."
  ].join("\n");
}

function downloadCurrentSvs() {
  const slide = getCurrentSlide();
  if (!slide) {
    setViewerStatus("Select a slide to download its source SVS.");
    return;
  }

  if (!slide.svsUrl) {
    setViewerStatus("SVS source URL is not configured for this slide.");
    return;
  }

  triggerUrlDownload(slide.svsUrl);
  setViewerStatus(`Started SVS download for ${slide.label}`);
}

async function downloadQuPathPackage() {
  const slide = getCurrentSlide();
  if (!slide) {
    setViewerStatus("Select a slide before exporting to QuPath.");
    return;
  }

  const caseInfo = caseData[currentCaseId];
  const rois = getCurrentRois();
  const packageButton = document.getElementById("packageBtn");

  if (!slide.svsUrl) {
    setViewerStatus("SVS source URL is not configured for this slide.");
    return;
  }

  if (typeof JSZip === "undefined") {
    setViewerStatus("ZIP packaging library failed to load.");
    return;
  }

  packageButton.disabled = true;
  packageButton.textContent = "□ Packaging...";
  setViewerStatus("Building the QuPath package...");

  const zip = new JSZip();
  const safeBaseName = sanitizeFilename(slide.submitterId || slide.label);
  const geoJson = buildRoiGeoJson(slide, rois);
  const handoffPayload = {
    generatedAt: new Date().toISOString(),
    caseId: currentCaseId,
    caseSummary: {
      diagnosis: caseInfo.diagnosis,
      grade: caseInfo.grade,
      project: caseInfo.project,
      primarySite: caseInfo.primarySite
    },
    slide: {
      label: slide.label,
      submitterId: slide.submitterId,
      slideId: slide.slideId,
      fileId: slide.fileId,
      section: slide.section,
      stain: slide.stain
    },
    rois: rois.map((roi) => ({
      shape: roi.shape || "rectangle",
      name: roi.name,
      note: roi.note || "",
      zoom: roi.zoom,
      savedAt: roi.savedAt,
      viewportRect: roi.viewportRect,
      imageRect: roi.imageRect
    }))
  };

  zip.file("slide-info.json", JSON.stringify(handoffPayload, null, 2));
  zip.file("roi.geojson", JSON.stringify(geoJson, null, 2));
  zip.file("open_in_qupath.groovy", buildQuPathScript(slide, rois));
  zip.file("README.txt", buildQuPathReadme(caseInfo, slide, rois));

  try {
    const blob = await zip.generateAsync({ type: "blob" });
    triggerFileDownload(`${safeBaseName}_qupath_package.zip`, blob);
    setViewerStatus(`Downloaded QuPath package for ${slide.label}`);
  } catch (error) {
    setViewerStatus("Unable to build the QuPath package in this browser.");
  } finally {
    packageButton.disabled = false;
    packageButton.textContent = "□ Export to QuPath";
  }
}

function getRoiDraftValues() {
  return {
    shape: document.getElementById("roiShapeSelect").value,
    name: document.getElementById("roiNameInput").value.trim(),
    note: document.getElementById("roiNoteInput").value.trim()
  };
}

function clearRoiDraftValues() {
  document.getElementById("roiShapeSelect").value = "rectangle";
  document.getElementById("roiNameInput").value = "";
  document.getElementById("roiNoteInput").value = "";
  activeRoiEditId = null;
  updateRoiFormUi();
  hideEditableRoiOverlay();
  renderPersistentRoiOverlays();
}

function updateRoiFormUi() {
  const saveButton = document.getElementById("addRoiSecondaryBtn");
  const deleteButton = document.getElementById("deleteRoiBtn");

  if (!saveButton || !deleteButton) return;

  const isEditing = Boolean(activeRoiEditId);
  saveButton.textContent = isEditing ? "Update ROI" : "Save Current View";
  deleteButton.hidden = !isEditing;
}

function getActiveRoi() {
  if (!activeRoiEditId) return null;
  return getCurrentRois().find((entry) => entry.id === activeRoiEditId) || null;
}

function approximatelyEqual(a, b, tolerance = 0.0005) {
  return Math.abs(a - b) <= tolerance;
}

function isCurrentViewportMatchingRoi(roi) {
  if (!roi || !viewer || !viewer.viewport) return false;

  const bounds = viewer.viewport.getBounds(true);
  return approximatelyEqual(bounds.x, roi.viewportRect.x)
    && approximatelyEqual(bounds.y, roi.viewportRect.y)
    && approximatelyEqual(bounds.width, roi.viewportRect.width)
    && approximatelyEqual(bounds.height, roi.viewportRect.height);
}

function resetRoiEditIfViewportChanged() {
  if (!activeRoiEditId || suppressRoiEditReset) return;

  const activeRoi = getActiveRoi();
  if (!activeRoi) {
    clearRoiDraftValues();
    renderRoiList();
    return;
  }

  if (isCurrentViewportMatchingRoi(activeRoi)) return;

  activeRoiEditId = null;
  updateRoiFormUi();
  renderRoiList();
  hideEditableRoiOverlay();
  setViewerStatus("Viewer moved from the selected ROI. Save Current View will create a new ROI.");
}

function getRoiEditElements() {
  return {
    displayLayer: document.getElementById("roiDisplayLayer"),
    layer: document.getElementById("roiEditLayer"),
    box: document.getElementById("roiEditBox"),
    viewerElement: document.querySelector(".viewer")
  };
}

function hideEditableRoiOverlay() {
  const { layer, box } = getRoiEditElements();
  if (!layer || !box) return;
  layer.classList.add("hidden");
  box.classList.add("hidden");
  box.classList.remove("circle");
}

function getPixelRectFromViewportRect(viewportRect) {
  if (!viewer || !viewer.viewport) return null;
  const topLeft = viewer.viewport.pixelFromPoint(
    new OpenSeadragon.Point(viewportRect.x, viewportRect.y),
    true
  );
  const bottomRight = viewer.viewport.pixelFromPoint(
    new OpenSeadragon.Point(viewportRect.x + viewportRect.width, viewportRect.y + viewportRect.height),
    true
  );
  return {
    left: topLeft.x,
    top: topLeft.y,
    width: bottomRight.x - topLeft.x,
    height: bottomRight.y - topLeft.y
  };
}

function clampOverlayRect(rect, bounds) {
  const minSize = 24;
  const next = { ...rect };

  next.width = Math.max(minSize, next.width);
  next.height = Math.max(minSize, next.height);
  next.left = Math.min(Math.max(0, next.left), Math.max(0, bounds.width - next.width));
  next.top = Math.min(Math.max(0, next.top), Math.max(0, bounds.height - next.height));
  next.width = Math.min(next.width, bounds.width - next.left);
  next.height = Math.min(next.height, bounds.height - next.top);
  next.width = Math.max(minSize, next.width);
  next.height = Math.max(minSize, next.height);
  return next;
}

function setEditableRoiOverlayRect(rect) {
  const { layer, box, viewerElement } = getRoiEditElements();
  if (!layer || !box || !viewerElement) return;

  const clamped = clampOverlayRect(rect, {
    width: viewerElement.clientWidth,
    height: viewerElement.clientHeight
  });

  layer.classList.remove("hidden");
  box.classList.remove("hidden");
  box.style.left = `${clamped.left}px`;
  box.style.top = `${clamped.top}px`;
  box.style.width = `${clamped.width}px`;
  box.style.height = `${clamped.height}px`;
}

function renderPersistentRoiOverlaysNow() {
  const { displayLayer } = getRoiEditElements();
  if (!displayLayer || !viewer || !viewer.viewport) return;

  const rois = getCurrentRois();
  if (!rois.length) {
    displayLayer.replaceChildren();
    return;
  }

  displayLayer.replaceChildren(...rois.map((roi) => {
    const pixelRect = getPixelRectFromViewportRect(roi.viewportRect);
    const item = document.createElement("button");
    item.type = "button";
    item.className = `roi-display-item${roi.shape === "circle" ? " circle" : ""}${roi.id === activeRoiEditId ? " active" : ""}`;
    item.style.left = `${pixelRect.left}px`;
    item.style.top = `${pixelRect.top}px`;
    item.style.width = `${pixelRect.width}px`;
    item.style.height = `${pixelRect.height}px`;
    item.title = roi.name || "ROI";
    const moveHandle = document.createElement("div");
    moveHandle.className = "roi-display-move";
    moveHandle.dataset.handle = "move";
    moveHandle.textContent = "⋮⋮";
    moveHandle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      focusRoiForEditing(roi.id, { jump: false });
      requestAnimationFrame(() => {
        startRoiOverlayDrag(event, "move");
      });
    });
    item.appendChild(moveHandle);
    ["nw", "ne", "sw", "se"].forEach((handleName) => {
      const handle = document.createElement("div");
      handle.className = `roi-display-handle ${handleName}`;
      handle.dataset.handle = handleName;
      handle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        event.stopPropagation();
        focusRoiForEditing(roi.id, { jump: false });
        requestAnimationFrame(() => {
          startRoiOverlayDrag(event, handleName);
        });
      });
      item.appendChild(handle);
    });
    return item;
  }));
}

function renderPersistentRoiOverlays() {
  if (roiOverlayRenderFrame) return;

  roiOverlayRenderFrame = requestAnimationFrame(() => {
    roiOverlayRenderFrame = 0;
    renderPersistentRoiOverlaysNow();
  });
}

function focusRoiForEditing(roiId, options = {}) {
  const { jump = true } = options;
  prefillRoiDraft(roiId);
  if (jump) {
    jumpToRoi(roiId);
  }

  const refreshOverlay = () => {
    syncViewerLayout();
    showEditableRoiOverlayForActiveRoi();
  };

  requestAnimationFrame(() => {
    refreshOverlay();
    requestAnimationFrame(refreshOverlay);
  });

  setTimeout(refreshOverlay, 60);
}

function showEditableRoiOverlayForActiveRoi() {
  const roi = getActiveRoi();
  if (!roi || !viewer || !viewer.viewport) {
    hideEditableRoiOverlay();
    renderPersistentRoiOverlays();
    return;
  }

  const pixelRect = getPixelRectFromViewportRect(roi.viewportRect);
  if (!pixelRect) {
    hideEditableRoiOverlay();
    renderPersistentRoiOverlays();
    return;
  }

  const { box } = getRoiEditElements();
  if (box) {
    box.classList.toggle("circle", roi.shape === "circle");
  }
  setEditableRoiOverlayRect(pixelRect);
  renderPersistentRoiOverlays();
}

function getViewportRectFromOverlay() {
  const { box } = getRoiEditElements();
  if (!box || !viewer || !viewer.viewport) return null;

  const left = Number.parseFloat(box.style.left || "0");
  const top = Number.parseFloat(box.style.top || "0");
  const width = Number.parseFloat(box.style.width || "0");
  const height = Number.parseFloat(box.style.height || "0");

  const topLeft = viewer.viewport.pointFromPixel(new OpenSeadragon.Point(left, top), true);
  const bottomRight = viewer.viewport.pointFromPixel(new OpenSeadragon.Point(left + width, top + height), true);

  return {
    x: topLeft.x,
    y: topLeft.y,
    width: bottomRight.x - topLeft.x,
    height: bottomRight.y - topLeft.y
  };
}

function applyOverlayRectToActiveRoi() {
  if (!activeRoiEditId || !viewer || !viewer.viewport || !viewer.world.getItemAt(0)) return;

  const viewportRect = getViewportRectFromOverlay();
  if (!viewportRect) return;

  const imageItem = viewer.world.getItemAt(0);
  const imageRect = imageItem.viewportToImageRectangle(
    new OpenSeadragon.Rect(
      viewportRect.x,
      viewportRect.y,
      viewportRect.width,
      viewportRect.height
    )
  );

  const storageKey = getRoiStorageKey();
  const rois = getCurrentRois();
  const roiIndex = rois.findIndex((entry) => entry.id === activeRoiEditId);
  if (roiIndex < 0) return;

  rois[roiIndex] = {
    ...rois[roiIndex],
    shape: rois[roiIndex].shape || "rectangle",
    zoom: viewer.viewport.getZoom(true),
    savedAt: new Date().toLocaleString(),
    viewportRect,
    imageRect: {
      x: imageRect.x,
      y: imageRect.y,
      width: imageRect.width,
      height: imageRect.height
    }
  };

  savedRois[storageKey] = rois;
  persistSavedRois();
  renderRoiList();
  setViewerStatus(`Updated ${rois[roiIndex].name} ROI bounds`);
}

function buildOverlayRectFromDrag(state, clientX, clientY) {
  const dx = clientX - state.startX;
  const dy = clientY - state.startY;
  const rect = { ...state.startRect };

  if (state.mode === "move") {
    rect.left += dx;
    rect.top += dy;
    return rect;
  }

  if (state.mode.includes("n")) {
    rect.top += dy;
    rect.height -= dy;
  }
  if (state.mode.includes("s")) {
    rect.height += dy;
  }
  if (state.mode.includes("w")) {
    rect.left += dx;
    rect.width -= dx;
  }
  if (state.mode.includes("e")) {
    rect.width += dx;
  }

  return rect;
}

function stopRoiOverlayDrag() {
  if (!roiOverlayDragState) return;
  roiOverlayDragState = null;
  applyOverlayRectToActiveRoi();
  showEditableRoiOverlayForActiveRoi();
}

function handleRoiOverlayPointerMove(event) {
  if (!roiOverlayDragState) return;
  event.preventDefault();
  setEditableRoiOverlayRect(
    buildOverlayRectFromDrag(roiOverlayDragState, event.clientX, event.clientY)
  );
}

function startRoiOverlayDrag(event, mode) {
  if (!activeRoiEditId) return;
  const { box } = getRoiEditElements();
  if (!box) return;

  event.preventDefault();
  event.stopPropagation();
  roiOverlayDragState = {
    mode,
    startX: event.clientX,
    startY: event.clientY,
    startRect: {
      left: Number.parseFloat(box.style.left || "0"),
      top: Number.parseFloat(box.style.top || "0"),
      width: Number.parseFloat(box.style.width || "0"),
      height: Number.parseFloat(box.style.height || "0")
    }
  };
}

function buildSeedViewportRect(bounds, shape) {
  const nextShape = shape || "rectangle";
  const scale = 0.25;

  if (nextShape === "circle") {
    const diameter = Math.min(bounds.width, bounds.height) * scale;
    return {
      x: bounds.x + (bounds.width - diameter) / 2,
      y: bounds.y + (bounds.height - diameter) / 2,
      width: diameter,
      height: diameter
    };
  }

  const width = bounds.width * scale;
  const height = bounds.height * scale;
  return {
    x: bounds.x + (bounds.width - width) / 2,
    y: bounds.y + (bounds.height - height) / 2,
    width,
    height
  };
}

function syncActiveRoiForCurrentSlide() {
  if (!activeRoiEditId) {
    updateRoiFormUi();
    return;
  }

  const activeRoiExists = getCurrentRois().some((entry) => entry.id === activeRoiEditId);
  if (!activeRoiExists) {
    clearRoiDraftValues();
    return;
  }

  updateRoiFormUi();
}

function renderRoiList() {
  const roiList = document.getElementById("roiList");
  syncActiveRoiForCurrentSlide();
  const rois = getCurrentRois();

  renderPersistentRoiOverlays();

  if (!rois.length) {
    roiList.replaceChildren(
      createEmptyText("No saved views yet. Create a region to export or analyze in QuPath.")
    );
    return;
  }

  roiList.replaceChildren(...rois.map((roi) => {
    const card = document.createElement("div");
    card.className = `roi-card${roi.id === activeRoiEditId ? " active" : ""}`;
    card.addEventListener("click", () => {
      focusRoiForEditing(roi.id);
    });

    const title = document.createElement("div");
    title.className = "roi-card-title";
    title.textContent = roi.name;
    card.appendChild(title);

    if (roi.note) {
      const note = document.createElement("div");
      note.className = "roi-card-note";
      note.textContent = roi.note;
      card.appendChild(note);
    }

    return card;
  }));
}

function prefillRoiDraft(roiId) {
  const roi = getCurrentRois().find((entry) => entry.id === roiId);
  if (!roi) return;

  activeRoiEditId = roiId;
  document.getElementById("roiShapeSelect").value = roi.shape || "rectangle";
  document.getElementById("roiNameInput").value = roi.name || "";
  document.getElementById("roiNoteInput").value = roi.note || "";
  updateRoiFormUi();
  renderRoiList();
  showEditableRoiOverlayForActiveRoi();
  setViewerStatus(`Editing ${roi.name}. Save Current View will update this ROI.`);
}

function saveCurrentViewAsRoi() {
  if (!viewer || !viewer.viewport || !viewer.world.getItemAt(0)) {
    setViewerStatus("ROI capture unavailable until the slide has loaded.");
    return;
  }

  const imageItem = viewer.world.getItemAt(0);
  const bounds = viewer.viewport.getBounds(true);
  const imageRect = imageItem.viewportToImageRectangle(bounds);
  const storageKey = getRoiStorageKey();
  const rois = savedRois[storageKey] || [];
  const savedAt = new Date().toLocaleString();
  const draft = getRoiDraftValues();
  const existingIndex = activeRoiEditId
    ? rois.findIndex((entry) => entry.id === activeRoiEditId)
    : -1;
  const existingRoi = existingIndex >= 0 ? rois[existingIndex] : null;
  const overlayViewportRect = getViewportRectFromOverlay();
  const baseViewportRect = existingRoi
    ? (overlayViewportRect || existingRoi.viewportRect)
    : buildSeedViewportRect(bounds, draft.shape);
  const nextBoundsRect = new OpenSeadragon.Rect(
    baseViewportRect.x,
    baseViewportRect.y,
    baseViewportRect.width,
    baseViewportRect.height
  );
  const nextImageRect = imageItem.viewportToImageRectangle(nextBoundsRect);
  const roiId = existingIndex >= 0 ? activeRoiEditId : `${storageKey}::${Date.now()}`;
  const roiPayload = {
    id: roiId,
    shape: draft.shape || existingRoi?.shape || "rectangle",
    name: draft.name || `ROI ${existingIndex >= 0 ? existingIndex + 1 : rois.length + 1}`,
    note: draft.note,
    zoom: viewer.viewport.getZoom(true),
    savedAt,
    viewportRect: baseViewportRect,
    imageRect: {
      x: nextImageRect.x,
      y: nextImageRect.y,
      width: nextImageRect.width,
      height: nextImageRect.height
    }
  };

  if (existingIndex >= 0) {
    rois[existingIndex] = roiPayload;
  } else {
    rois.push(roiPayload);
  }

  savedRois[storageKey] = rois;
  persistSavedRois();
  renderRoiList();
  switchTab("slideTab");

  if (existingIndex >= 0) {
    clearRoiDraftValues();
    setViewerStatus(`Updated ${roiPayload.name} from the current view`);
    return;
  }

  prefillRoiDraft(roiId);
  setViewerStatus(`Seeded ${roiPayload.shape} ROI at the viewport center. Drag or resize to fine-tune.`);
}

function deleteRoi(roiId) {
  const storageKey = getRoiStorageKey();
  const rois = getCurrentRois();
  const nextRois = rois.filter((entry) => entry.id !== roiId);

  if (nextRois.length === rois.length) {
    setViewerStatus("Unable to delete the selected ROI.");
    return;
  }

  if (activeRoiEditId === roiId) {
    clearRoiDraftValues();
  }

  savedRois[storageKey] = nextRois;
  persistSavedRois();
  renderRoiList();
  setViewerStatus("Deleted the selected ROI");
}

function deleteActiveRoi() {
  if (!activeRoiEditId) return;
  deleteRoi(activeRoiEditId);
}

function jumpToRoi(roiId) {
  const roi = getCurrentRois().find((entry) => entry.id === roiId);
  if (!roi || !viewer || !viewer.viewport) {
    setViewerStatus("Unable to restore the selected ROI view.");
    return;
  }

  const viewportRect = new OpenSeadragon.Rect(
    roi.viewportRect.x,
    roi.viewportRect.y,
    roi.viewportRect.width,
    roi.viewportRect.height
  );

  suppressRoiEditReset = true;
  viewer.viewport.fitBounds(viewportRect, true);
  requestAnimationFrame(() => {
    showEditableRoiOverlayForActiveRoi();
  });
  setViewerStatus(`Restored ${roi.name}`);
}

function initViewer(dziPath) {
  const viewerElement = document.getElementById("slidePreview");
  viewerElement.replaceChildren();

  if (viewer) {
    viewer.destroy();
    viewer = null;
  }

  viewer = OpenSeadragon({
    id: "slidePreview",
    prefixUrl: "https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/images/",
    tileSources: dziPath,
    showNavigator: true,
    animationTime: 0.35,
    blendTime: 0,
    constrainDuringPan: true,
    maxZoomPixelRatio: 2,
    minZoomLevel: 0.5,
    visibilityRatio: 1.0
  });

  viewer.addOnceHandler("open", () => {
    renderPersistentRoiOverlays();
  });

  viewer.addHandler("animation-finish", () => {
    if (suppressRoiEditReset) {
      suppressRoiEditReset = false;
      showEditableRoiOverlayForActiveRoi();
      return;
    }

    showEditableRoiOverlayForActiveRoi();
    resetRoiEditIfViewportChanged();
  });

  viewer.addHandler("canvas-drag-end", resetRoiEditIfViewportChanged);
  viewer.addHandler("canvas-click", resetRoiEditIfViewportChanged);
  viewer.addHandler("canvas-scroll", () => {
    requestAnimationFrame(resetRoiEditIfViewportChanged);
  });
}

async function renderSlideDetails() {
  const slide = getCurrentSlide();
  if (!slide) {
    return;
  }

  document.getElementById("slideMetaLabel").textContent = slide.label || "-";
  document.getElementById("slideMetaSubmitter").textContent = slide.submitterId || "-";
  document.getElementById("slideMetaId").textContent = slide.slideId || "-";
  document.getElementById("slideMetaSection").textContent = slide.section || "-";

  setViewerStatus("Fetching slide data...");
  hideEditableRoiOverlay();
  initViewer(slide.preview);
  setViewerStatus("Ready");

  const compositionContainer = document.getElementById("compositionContainer");
  const interpretation = document.getElementById("compositionInterpretation");

  if (slide.hasQuant && slide.composition) {
    const compositionGrid = document.createElement("div");
    compositionGrid.className = "composition-grid";

    [
      buildCompositionRow("Tumor cells", slide.composition.tumorCells),
      buildCompositionRow("Tumor nuclei", slide.composition.tumorNuclei),
      buildCompositionRow("Stromal cells", slide.composition.stromalCells),
      buildCompositionRow("Normal cells", slide.composition.normalCells)
    ].filter(Boolean).forEach((row) => compositionGrid.appendChild(row));

    compositionContainer.replaceChildren(compositionGrid);
    interpretation.textContent = slide.interpretation || "";
  } else {
    compositionContainer.replaceChildren(
      createEmptyText("Quantitative composition data not available for this slide.")
    );
    interpretation.textContent = slide.interpretation || "";
  }

  const reviewNotes = document.getElementById("reviewNotes");
  reviewNotes.replaceChildren(...(slide.notes || []).map((note) => {
    const item = document.createElement("li");
    item.textContent = note;
    return item;
  }));

  renderRoiList();
}

function clearSlideSelectionUi() {
  document.getElementById("slideMetaLabel").textContent = "-";
  document.getElementById("slideMetaSubmitter").textContent = "-";
  document.getElementById("slideMetaId").textContent = "-";
  document.getElementById("slideMetaSection").textContent = "-";
  document.getElementById("compositionContainer").replaceChildren(
    createEmptyText("Select a slide to load quantitative composition data.")
  );
  document.getElementById("compositionInterpretation").textContent = "";
  document.getElementById("reviewNotes").replaceChildren(
    createEmptyText("Select a slide to load review notes.")
  );
  hideEditableRoiOverlay();
  const roiDisplayLayer = document.getElementById("roiDisplayLayer");
  if (roiDisplayLayer) {
    roiDisplayLayer.replaceChildren();
  }
  const slidePreview = document.getElementById("slidePreview");
  if (slidePreview) {
    slidePreview.replaceChildren();
  }
  if (viewer) {
    viewer.destroy();
    viewer = null;
  }
  setViewerPlaceholder(
    currentCaseId ? "Select a slide" : "Select a case",
    currentCaseId
      ? "Choose a linked slide to load whole-slide viewing, ROI tools, and slide-level details."
      : "Choose a case from the left panel to load metadata, linked slides, and whole-slide review."
  );
  renderRoiList();
}

async function selectSlide(index) {
  currentSlideIndex = index;

  document.querySelectorAll(".slide-list-card").forEach((card, idx) => {
    card.classList.toggle("active-thumb", idx === index);
  });

  await renderSlideDetails();
}

async function selectCase(caseId) {
  const data = await ensureCaseLoaded(caseId);
  if (!data) return;

  currentCaseId = caseId;
  heatmapOn = false;
  document.getElementById("heatmapOverlay").classList.remove("visible");

  renderCaseHeader(data);
  renderCaseDetails(data);
  renderSlides(caseId);
  clearSlideSelectionUi();

  const filtered = getFilteredCaseIds(document.getElementById("caseSearch").value);
  renderQuickCaseList(filtered);
}

function applySearch() {
  const filtered = getFilteredCaseIds(document.getElementById("caseSearch").value);
  renderQuickCaseList(filtered);
}

function liveFilter() {
  const filtered = getFilteredCaseIds(document.getElementById("caseSearch").value);
  renderQuickCaseList(filtered);
}

function toggleHeatmap() {
  heatmapOn = !heatmapOn;
  document.getElementById("heatmapOverlay").classList.toggle("visible", heatmapOn);
}

function openIMS() {
  alert("Prototype action: this would open the selected slide in Proscia Concentriq LS using an API deep-link.");
}

function isCompactPanelMode() {
  return window.matchMedia("(max-width: 2000px)").matches;
}

function updateCompactPanelButtons() {
  const mainGrid = document.querySelector(".main-grid");
  const roiPanel = document.querySelector(".roi-panel");
  const sidePanel = document.querySelector(".side-panel");
  const roiButton = document.getElementById("toggleRoiPanelBtn");
  const detailsButton = document.getElementById("toggleDetailsPanelBtn");

  if (!mainGrid || !roiPanel || !sidePanel || !roiButton || !detailsButton) return;

  if (!isCompactPanelMode()) {
    mainGrid.classList.remove("compact-show-roi", "compact-show-side");
    roiButton.textContent = "▣ Regions of Interest";
    detailsButton.textContent = "☰ Case/Slide Detail";
    return;
  }

  roiButton.textContent = mainGrid.classList.contains("compact-show-roi")
    ? "▣ Hide Regions of Interest"
    : "▣ Regions of Interest";
  detailsButton.textContent = mainGrid.classList.contains("compact-show-side")
    ? "☰ Hide Case/Slide Detail"
    : "☰ Case/Slide Detail";
}

function toggleCompactPanel(panelName) {
  if (!isCompactPanelMode()) return;

  const mainGrid = document.querySelector(".main-grid");
  if (!mainGrid) return;

  const targetClass = panelName === "roi" ? "compact-show-roi" : "compact-show-side";
  const otherClass = panelName === "roi" ? "compact-show-side" : "compact-show-roi";
  const shouldOpen = !mainGrid.classList.contains(targetClass);

  mainGrid.classList.remove(otherClass);
  mainGrid.classList.toggle(targetClass, shouldOpen);
  updateCompactPanelButtons();
  requestAnimationFrame(() => {
    if (isDemoFullscreen) {
      measureFullscreenLayout();
    }
    syncViewerLayout();
  });
}

function syncViewerLayout() {
  if (!viewer || !viewer.viewport) return;

  requestAnimationFrame(() => {
    viewer.viewport.resize();
    viewer.forceRedraw();
    showEditableRoiOverlayForActiveRoi();
  });
}

function measureFullscreenLayout() {
  const demo = document.querySelector(".prototype-demo");
  const pageHeader = document.querySelector(".page-header");
  const roiPanel = document.querySelector(".roi-panel");
  const sidePanel = document.querySelector(".side-panel");
  if (!demo || !pageHeader || !roiPanel || !sidePanel) return;

  const topOffset = Math.max(pageHeader.getBoundingClientRect().bottom + 12, 16);
  demo.style.setProperty("--demo-fullscreen-top", `${Math.round(topOffset)}px`);
  const roiWidth = roiPanel.offsetParent === null
    ? 220
    : Math.max(Math.round(roiPanel.getBoundingClientRect().width), 220);
  const sidePanelWidth = Math.max(Math.round(sidePanel.getBoundingClientRect().width), 340);
  demo.style.setProperty(
    "--fullscreen-roi-width",
    `${roiWidth}px`
  );
  demo.style.setProperty(
    "--fullscreen-sidepanel-width",
    `${sidePanelWidth}px`
  );
}

function isUsingNativeFullscreen(demo) {
  return document.fullscreenElement === demo;
}

function updateFullscreenUi(nextState) {
  const demo = document.querySelector(".prototype-demo");
  const toggleButton = document.getElementById("fullscreenToggleBtn");
  if (!demo || !toggleButton) return;

  isDemoFullscreen = nextState;

  document.body.classList.toggle("demo-fullscreen", nextState);
  demo.classList.toggle("is-fullscreen", nextState);
  toggleButton.textContent = nextState ? "⛶ Exit Fullscreen" : "⛶ Fullscreen";
  if (nextState) {
    requestAnimationFrame(() => {
      measureFullscreenLayout();
      syncViewerLayout();
    });
  }
  syncViewerLayout();
}

async function setDemoFullscreen(nextState) {
  const demo = document.querySelector(".prototype-demo");
  if (!demo) return;

  if (nextState) {
    measureFullscreenLayout();
  }

  const canUseNativeFullscreen = typeof demo.requestFullscreen === "function";

  if (canUseNativeFullscreen) {
    try {
      if (nextState && !isUsingNativeFullscreen(demo)) {
        await demo.requestFullscreen();
      } else if (!nextState && document.fullscreenElement) {
        await document.exitFullscreen();
      }
      updateFullscreenUi(nextState);
      return;
    } catch (error) {
      console.error("Unable to toggle native fullscreen.", error);
    }
  }

  updateFullscreenUi(nextState);
}

async function toggleDemoFullscreen() {
  await setDemoFullscreen(!isDemoFullscreen);
}

function exportReport() {
  alert("Prototype action: this would export a case summary including case-level details and slide-level metrics.");
}

function switchTab(tabId) {
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabId);
  });

  document.querySelectorAll(".tab-content").forEach((content) => {
    content.classList.toggle("active", content.id === tabId);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const roiEditBox = document.getElementById("roiEditBox");
  const roiEditLayer = document.getElementById("roiEditLayer");
  document.getElementById("searchBtn").addEventListener("click", applySearch);
  document.getElementById("caseSearch").addEventListener("input", liveFilter);
  document.getElementById("caseSearch").addEventListener("keydown", (event) => {
    if (event.key === "Enter") applySearch();
  });
  document.getElementById("heatmapBtn").addEventListener("click", toggleHeatmap);
  document.getElementById("addRoiSecondaryBtn").addEventListener("click", saveCurrentViewAsRoi);
  document.getElementById("deleteRoiBtn").addEventListener("click", deleteActiveRoi);
  document.getElementById("downloadSvsBtn").addEventListener("click", downloadCurrentSvs);
  document.getElementById("packageBtn").addEventListener("click", downloadQuPathPackage);
  document.getElementById("imsBtn").addEventListener("click", openIMS);
  document.getElementById("toggleRoiPanelBtn").addEventListener("click", () => toggleCompactPanel("roi"));
  document.getElementById("toggleDetailsPanelBtn").addEventListener("click", () => toggleCompactPanel("side"));
  document.getElementById("fullscreenToggleBtn").addEventListener("click", toggleDemoFullscreen);
  ["click", "dblclick"].forEach((eventName) => {
    roiEditLayer.addEventListener(eventName, (event) => {
      if (!event.target.closest(".roi-edit-edge, .roi-edit-handle")) return;
      event.preventDefault();
      event.stopPropagation();
    }, true);
  });
  roiEditBox.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest("[data-handle]");
    startRoiOverlayDrag(event, handle ? handle.dataset.handle : "move");
  });
  window.addEventListener("pointermove", handleRoiOverlayPointerMove);
  window.addEventListener("pointerup", stopRoiOverlayDrag);
  window.addEventListener("pointercancel", stopRoiOverlayDrag);
  document.addEventListener("fullscreenchange", () => {
    const demo = document.querySelector(".prototype-demo");
    updateFullscreenUi(Boolean(demo && isUsingNativeFullscreen(demo)));
  });
  window.addEventListener("resize", () => {
    updateCompactPanelButtons();
    if (isDemoFullscreen) {
      measureFullscreenLayout();
      syncViewerLayout();
    }
  });

  document.querySelectorAll(".tab-button").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });

  loadCaseList()
    .then(() => {
      const caseIds = caseSummaries.map((entry) => entry.caseId);
      if (!caseIds.length) {
        setViewerStatus("No case data available from AWS.");
        return;
      }

      renderQuickCaseList(caseIds);
      switchTab("caseTab");
      updateCompactPanelButtons();
      document.getElementById("slideThumbs").replaceChildren(
        createEmptyText("Select a case to load linked slides.")
      );
      document.getElementById("compositionContainer").replaceChildren(
        createEmptyText("Select a case and slide to load quantitative composition data.")
      );
      document.getElementById("reviewNotes").replaceChildren(
        createEmptyText("Select a case and slide to load review notes.")
      );
      clearSlideSelectionUi();
    })
    .catch((error) => {
      console.error(error);
      document.getElementById("quickCaseList").replaceChildren(
        createEmptyText("Unable to load cases from AWS.")
      );
      document.getElementById("slideThumbs").replaceChildren(
        createEmptyText("No slide data loaded.")
      );
      setViewerStatus("Unable to load case data from AWS S3.");
    });
});
