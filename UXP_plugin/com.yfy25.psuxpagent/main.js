const uxp = require("uxp");
const { entrypoints } = uxp;
const photoshop = require("photoshop");
const app = photoshop.app;
const core = photoshop.core;
const action = photoshop.action;
const constants = photoshop.constants || {};
const fs = uxp.storage.localFileSystem;
const storageFormats = uxp.storage.formats;

const BACKEND_URLS = [
  "http://127.0.0.1:17860",
  "http://localhost:17860"
];
const HEARTBEAT_MS = 2000;
const POLL_MS = 1000;
const REQUEST_TIMEOUT_MS = 6000;
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 8000;
const DEFAULT_REGION_MAX_SIDE = 1536;
const PLUGIN_VERSION = "0.11.14";
const START_COMMAND = "python D:\\Photo_sontrol\\backend\\cli.py daemon start";
const MIN_POLYGON_POINTS = 3;
const MAX_POLYGON_POINTS = 256;
const MAX_COMPOSITE_SELECTION_ITEMS = 16;
const SELECTION_OPERATION_TO_CONSTANT = {
  replace: "REPLACE",
  add: "EXTEND",
  subtract: "DIMINISH",
  intersect: "INTERSECT"
};
const CHANNEL_SELECTION_MODIFIER_BY_OPERATION = {
  add: "addToSelection",
  subtract: "subtractFromSelection",
  intersect: "intersectWithSelection"
};
const ACR_AI_MASK_INTERNAL_TYPES = new Set([
  "subject",
  "background",
  "sky",
  "person",
  "face_skin",
  "body_skin",
  "eyes",
  "lips",
  "hair",
  "teeth"
]);
const ACR_SELECTION_FALLBACK_TYPES = new Set(["subject", "background", "sky"]);

const runtime = {
  initialized: false,
  heartbeatTimer: null,
  pollTimer: null,
  backendConnected: false,
  connectionState: "disconnected",
  currentJob: null,
  lastError: null,
  lastHeartbeat: null,
  queue: null,
  documentState: null,
  diagnostics: null,
  backendUrl: null,
  lastAgentEdit: null,
  pendingResult: null,
  heartbeatInFlight: false,
  pollInFlight: false,
  requestBackoffMs: BACKOFF_MIN_MS,
  nextPollAt: 0,
  revisionCounter: 0
};

function $(id) {
  return document.getElementById(id);
}

function setText(id, value) {
  const node = $(id);
  if (node) {
    node.textContent = value;
  }
}

function setBackendStatus(stateOrConnected) {
  const state = typeof stateOrConnected === "boolean"
    ? (stateOrConnected ? "connected" : "disconnected")
    : stateOrConnected;
  runtime.connectionState = state || "disconnected";
  runtime.backendConnected = runtime.connectionState === "connected" || runtime.connectionState === "busy";
  const dot = $("backendDot");
  if (dot) {
    dot.classList.remove("connected", "disconnected", "busy", "stale", "waiting", "error");
    dot.classList.add(runtime.currentJob ? "busy" : runtime.connectionState);
  }
  const labels = {
    connected: "Backend Connected",
    disconnected: "Backend Disconnected",
    waiting: "Backend Waiting",
    stale: "Stale Heartbeat",
    busy: "Job Running",
    error: "Backend Error"
  };
  setText("backendStatus", labels[runtime.currentJob ? "busy" : runtime.connectionState] || "Backend Disconnected");
}

function render() {
  setBackendStatus(runtime.backendConnected);
  setText("backendUrl", runtime.backendUrl || "trying 127.0.0.1 / localhost");
  setText("pluginStatus", runtime.currentJob ? "running" : "idle");
  setText("lastHeartbeat", runtime.lastHeartbeat || "never");
  const queue = runtime.queue || {};
  setText("queueStatus", `pending ${queue.pending || 0} / running ${queue.running || 0}`);
  setText("jobStatus", runtime.currentJob ? `${runtime.currentJob.job_type} (${runtime.currentJob.job_id})` : "none");
  setText("documentStatus", runtime.documentState ? JSON.stringify(runtime.documentState, null, 2) : "No document data yet.");
  setText("lastError", runtime.lastError || "None");
  setText("diagnosticsStatus", runtime.diagnostics ? JSON.stringify(runtime.diagnostics, null, 2) : "No diagnostics yet.");
}

function safeNumber(value) {
  if (value == null) {
    return null;
  }
  if (typeof value === "number") {
    return value;
  }
  if (typeof value.value === "number") {
    return value.value;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function readActiveLayerName(doc) {
  try {
    if (doc.activeLayers && doc.activeLayers.length > 0) {
      return doc.activeLayers[0].name || null;
    }
  } catch (error) {
  }
  try {
    if (app.activeDocument && app.activeDocument.activeLayers && app.activeDocument.activeLayers.length > 0) {
      return app.activeDocument.activeLayers[0].name || null;
    }
  } catch (error) {
  }
  return null;
}

function readActiveLayerIds(doc) {
  const ids = new Set();
  try {
    const layers = doc && doc.activeLayers ? Array.from(doc.activeLayers) : [];
    layers.forEach((layer) => {
      if (layer && layer.id != null) {
        ids.add(Number(layer.id));
      }
    });
  } catch (error) {
  }
  return ids;
}

function collectLayerTree(layersLike, activeIds, pathParts) {
  const items = [];
  const layers = Array.isArray(layersLike) ? layersLike : Array.from(layersLike || []);
  layers.forEach((layer, index) => {
    if (!layer) {
      return;
    }
    const name = layer.name || "";
    const currentPath = pathParts.concat(name || `layer_${index + 1}`);
    let children = [];
    try {
      if (layer.layers) {
        children = collectLayerTree(layer.layers, activeIds, currentPath);
      }
    } catch (error) {
    }
    const id = layer.id == null ? null : Number(layer.id);
    items.push({
      id,
      name,
      visible: layer.visible == null ? null : Boolean(layer.visible),
      kind: layer.kind == null ? null : String(layer.kind),
      typename: layer.constructor && layer.constructor.name ? layer.constructor.name : null,
      index,
      path: currentPath.join(" > "),
      is_group: children.length > 0,
      is_active: id != null ? activeIds.has(id) : false,
      child_count: children.length,
      children
    });
  });
  return items;
}

function countLayerTree(items) {
  return items.reduce((total, item) => {
    return total + 1 + countLayerTree(Array.isArray(item.children) ? item.children : []);
  }, 0);
}

async function readDocumentState(includeLayers) {
  try {
    const doc = app.activeDocument;
    if (!doc) {
      return {
        has_active_document: false,
        revision: `doc-rev-${Date.now()}-${runtime.revisionCounter++}`
      };
    }

    const layers = [];
    let totalLayerCount = 0;
    const activeLayerIds = readActiveLayerIds(doc);
    if (includeLayers !== false) {
      try {
        const docLayers = collectLayerTree(doc.layers || [], activeLayerIds, []);
        docLayers.forEach((layer) => layers.push(layer));
        totalLayerCount = countLayerTree(docLayers);
      } catch (error) {
      }
    }

    const state = {
      has_active_document: true,
      name: doc.title || doc.name || "Untitled",
      width: safeNumber(doc.width),
      height: safeNumber(doc.height),
      mode: doc.mode == null ? null : String(doc.mode),
      layer_count: layers.length,
      total_layer_count: totalLayerCount || layers.length,
      active_layer_name: readActiveLayerName(doc),
      active_layer_ids: Array.from(activeLayerIds),
      revision: `doc-rev-${Date.now()}-${runtime.revisionCounter++}`
    };
    if (includeLayers !== false) {
      state.layers = layers;
    }
    return state;
  } catch (error) {
    return {
      has_active_document: false,
      revision: `doc-rev-${Date.now()}-${runtime.revisionCounter++}`,
      error: {
        code: "no_active_document",
        message: String(error && error.message ? error.message : error)
      }
    };
  }
}

function candidateBackendUrls() {
  if (!runtime.backendUrl) {
    return BACKEND_URLS;
  }
  return [runtime.backendUrl].concat(BACKEND_URLS.filter((url) => url !== runtime.backendUrl));
}

function increaseBackoff() {
  runtime.requestBackoffMs = Math.min(BACKOFF_MAX_MS, Math.max(BACKOFF_MIN_MS, runtime.requestBackoffMs * 2));
  runtime.nextPollAt = Date.now() + runtime.requestBackoffMs;
}

function resetBackoff() {
  runtime.requestBackoffMs = BACKOFF_MIN_MS;
  runtime.nextPollAt = 0;
}

function fetchWithTimeout(url, options, timeoutMs) {
  return Promise.race([
    fetch(url, options),
    new Promise((resolve, reject) => {
      setTimeout(() => reject(new Error(`request timeout after ${timeoutMs}ms`)), timeoutMs);
    })
  ]);
}

async function requestJson(method, path, payload) {
  const errors = [];
  for (const baseUrl of candidateBackendUrls()) {
    try {
      const options = {
        method,
        headers: {
          "Accept": "application/json",
          "Cache-Control": "no-store"
        }
      };
      if (payload !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(payload || {});
      }

      const response = await fetchWithTimeout(`${baseUrl}${path}`, options, REQUEST_TIMEOUT_MS);
      const text = await response.text();
      let data = {};
      if (text) {
        data = JSON.parse(text);
      }
      if (!response.ok) {
        throw new Error(data && data.error ? data.error.message : `HTTP ${response.status}`);
      }
      runtime.backendUrl = baseUrl;
      resetBackoff();
      return data;
    } catch (error) {
      errors.push(`${baseUrl}: ${error && error.message ? error.message : error}`);
    }
  }
  runtime.backendUrl = null;
  increaseBackoff();
  throw new Error(errors.join(" | "));
}

async function postJson(path, payload) {
  return requestJson("POST", path, payload);
}

async function getJson(path) {
  return requestJson("GET", path);
}

async function uploadBinary(path, bytes, mimeType) {
  const errors = [];
  for (const baseUrl of candidateBackendUrls()) {
    try {
      const response = await fetchWithTimeout(`${baseUrl}${path}`, {
        method: "POST",
        headers: {
          "Content-Type": mimeType,
          "Cache-Control": "no-store"
        },
        body: bytes
      }, REQUEST_TIMEOUT_MS);
      const text = await response.text();
      const data = text ? JSON.parse(text) : {};
      if (!response.ok) {
        throw new Error(data && data.error ? data.error.message : `HTTP ${response.status}`);
      }
      runtime.backendUrl = baseUrl;
      resetBackoff();
      return data;
    } catch (error) {
      errors.push(`${baseUrl}: ${error && error.message ? error.message : error}`);
    }
  }
  runtime.backendUrl = null;
  increaseBackoff();
  throw new Error(errors.join(" | "));
}

async function downloadBinaryFromBackend(uri) {
  const errors = [];
  const candidates = [];
  if (uri && /^https?:\/\//i.test(String(uri))) {
    candidates.push(String(uri));
  } else if (uri) {
    for (const baseUrl of candidateBackendUrls()) {
      candidates.push(`${baseUrl}${String(uri).startsWith("/") ? "" : "/"}${uri}`);
    }
  }

  for (const candidate of candidates) {
    try {
      const response = await fetchWithTimeout(candidate, {
        method: "GET",
        headers: {
          "Cache-Control": "no-store"
        }
      }, REQUEST_TIMEOUT_MS);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const bytes = await response.arrayBuffer();
      return { bytes, uri: candidate };
    } catch (error) {
      errors.push(`${candidate}: ${error && error.message ? error.message : error}`);
    }
  }
  throw new Error(errors.join(" | ") || "No alpha mask asset URI was provided.");
}

async function writeTempBinaryFile(fileName, bytes) {
  const tempFolder = await fs.getTemporaryFolder();
  const file = await tempFolder.createFile(fileName, { overwrite: true });
  await file.write(bytes, { format: storageFormats.binary });
  return file;
}

async function writeTempTextFile(fileName, text) {
  const tempFolder = await fs.getTemporaryFolder();
  const file = await tempFolder.createFile(fileName, { overwrite: true });
  if (storageFormats && storageFormats.utf8) {
    await file.write(String(text), { format: storageFormats.utf8 });
  } else {
    await file.write(String(text));
  }
  return file;
}

async function sendHeartbeat() {
  if (runtime.heartbeatInFlight) {
    return;
  }
  runtime.heartbeatInFlight = true;
  try {
    runtime.documentState = await readDocumentState(false);
    const payload = {
      schema_version: "ps-agent/v1",
      plugin_id: "com.yfy25.psuxpagent",
      plugin_version: PLUGIN_VERSION,
      status: runtime.currentJob ? "running" : "idle",
      current_job_id: runtime.currentJob ? runtime.currentJob.job_id : null,
      document: runtime.documentState,
      last_error: runtime.lastError,
      local_time: new Date().toISOString(),
      path_dom_runtime: pathDomRuntimeDiagnostics({ construct_sample: false })
    };
    const health = await postJson("/uxp/heartbeat", payload);
    runtime.queue = health.queue || runtime.queue;
    runtime.lastHeartbeat = new Date().toLocaleTimeString();
    runtime.lastError = null;
    const age = Number(health.uxp_age_seconds == null ? 0 : health.uxp_age_seconds);
    setBackendStatus(age > 6.5 ? "stale" : "connected");
  } catch (error) {
    runtime.lastError = `heartbeat failed: ${error && error.message ? error.message : error}`;
    runtime.lastHeartbeat = null;
    setBackendStatus("disconnected");
  } finally {
    runtime.heartbeatInFlight = false;
  }
  render();
}

function pathDomRuntimeDiagnostics(options = {}) {
  const photoshopKeys = Object.keys(photoshop || {});
  const pathRelatedKeys = photoshopKeys.filter((key) => key.toLowerCase().indexOf("path") >= 0).sort();
  const pointKind = constants.PointKind || {};
  const shapeOperation = constants.ShapeOperation || {};
  return {
    schema_version: "ps-agent/v1",
    plugin_version: PLUGIN_VERSION,
    photoshop_path_keys: pathRelatedKeys,
    photoshop_path_point_info_type: typeof (photoshop && photoshop.PathPointInfo),
    photoshop_sub_path_info_type: typeof (photoshop && photoshop.SubPathInfo),
    global_path_point_info_type: typeof globalThis.PathPointInfo,
    global_sub_path_info_type: typeof globalThis.SubPathInfo,
    point_kind_keys: Object.keys(pointKind),
    point_kind_values: Object.fromEntries(Object.keys(pointKind).map((key) => [key, String(pointKind[key])])),
    shape_operation_keys: Object.keys(shapeOperation),
    shape_operation_values: Object.fromEntries(Object.keys(shapeOperation).map((key) => [key, String(shapeOperation[key])])),
    class_route_available: false,
    class_route_removed: true,
    plain_dom_route_available: true,
    svg_route_available: typeof fs.createSessionToken === "function",
    note: "PathPointInfo/SubPathInfo class constructors are not exposed in this Photoshop UXP runtime; default drawing no longer tries that route."
  };
}
function clampNumber(value, min, max, fallback) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(min, Math.min(parsed, max));
}

function previewDimensions(width, height, maxSide, allowUpscale) {
  const longest = Math.max(width || 0, height || 0);
  if (!longest || !maxSide || (!allowUpscale && longest <= maxSide)) {
    return {
      width,
      height,
      scale_factor: 1
    };
  }
  const scale = maxSide / longest;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
    scale_factor: scale
  };
}

function safeIdentifier(value, fallback) {
  const raw = String(value || fallback || "region");
  const cleaned = raw.replace(/[^A-Za-z0-9_.:-]/g, "_").replace(/^[._:-]+|[._:-]+$/g, "");
  return cleaned || fallback || "region";
}

function safeLayerName(value, fallback) {
  const raw = String(value || fallback || "Codex Agent Layer").replace(/[\r\n\t]/g, " ").trim();
  return raw.slice(0, 80) || fallback || "Codex Agent Layer";
}

function safeJobId(value) {
  const raw = String(value || "").trim();
  return /^job-[A-Za-z0-9_.:-]{1,120}$/.test(raw) ? raw : null;
}

function layerIdValue(layer) {
  if (!layer) {
    return null;
  }
  return layer.layerID == null ? layer.id || null : layer.layerID;
}

function layerKindValue(layer) {
  if (!layer) {
    return "";
  }
  try {
    return String(layer.kind || "");
  } catch (error) {
    return "";
  }
}

function layerTypeName(layer) {
  try {
    return layer && layer.constructor && layer.constructor.name ? String(layer.constructor.name) : "";
  } catch (error) {
    return "";
  }
}

function isLayerGroup(layer) {
  const kind = layerKindValue(layer).toLowerCase();
  const typeName = layerTypeName(layer).toLowerCase();
  if (kind.includes("group") || typeName.includes("group")) {
    return true;
  }
  try {
    return !!(layer && layer.layers && typeof layer.layers[Symbol.iterator] === "function");
  } catch (error) {
    return false;
  }
}

function findLayerGroupByName(layers, groupName, depth) {
  if (!layers || depth > 8) {
    return null;
  }
  let layerList = [];
  try {
    layerList = Array.from(layers || []);
  } catch (error) {
    return null;
  }
  for (const layer of layerList) {
    const name = layer && layer.name ? String(layer.name) : "";
    if (name === groupName && isLayerGroup(layer)) {
      return layer;
    }
    let nested = null;
    try {
      nested = layer && layer.layers ? findLayerGroupByName(layer.layers, groupName, depth + 1) : null;
    } catch (error) {
      nested = null;
    }
    if (nested) {
      return nested;
    }
  }
  return null;
}

function numberParam(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(min, Math.min(parsed, max));
}

function colorBalanceTriplet(value) {
  if (!Array.isArray(value) || value.length !== 3) {
    return [0, 0, 0];
  }
  return value.map((item) => Math.round(numberParam(item, 0, -100, 100)));
}

function hueSaturationRangeDescriptor(value) {
  const map = {
    master: null,
    reds: {
      localRange: 1,
      beginRamp: 315,
      beginSustain: 345,
      endSustain: 15,
      endRamp: 45
    },
    yellows: {
      localRange: 2,
      beginRamp: 15,
      beginSustain: 45,
      endSustain: 75,
      endRamp: 105
    },
    greens: {
      localRange: 3,
      beginRamp: 75,
      beginSustain: 105,
      endSustain: 135,
      endRamp: 165
    },
    cyans: {
      localRange: 4,
      beginRamp: 135,
      beginSustain: 165,
      endSustain: 195,
      endRamp: 225
    },
    blues: {
      localRange: 5,
      beginRamp: 195,
      beginSustain: 225,
      endSustain: 255,
      endRamp: 285
    },
    magentas: {
      localRange: 6,
      beginRamp: 255,
      beginSustain: 285,
      endSustain: 315,
      endRamp: 345
    }
  };
  const key = String(value || "master");
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : null;
}

function adjustmentChannelRef(value) {
  const key = String(value || "composite").toLowerCase();
  const map = {
    composite: "composite",
    rgb: "composite",
    red: "red",
    reds: "red",
    green: "green",
    greens: "green",
    blue: "blue",
    blues: "blue"
  };
  return {
    _ref: "channel",
    _enum: "channel",
    _value: map[key] || "composite"
  };
}

function curvePointDescriptor(point) {
  let x = null;
  let y = null;
  if (Array.isArray(point)) {
    x = point[0];
    y = point[1];
  } else if (point && typeof point === "object") {
    x = point.x == null ? point.input : point.x;
    y = point.y == null ? point.output : point.y;
  }
  return {
    _obj: "paint",
    horizontal: Math.round(numberParam(x, 0, 0, 255)),
    vertical: Math.round(numberParam(y, 0, 0, 255))
  };
}

function curvesPresetPoints(preset) {
  const key = String(preset || "").toLowerCase().replace(/[\s-]+/g, "_");
  const presets = {
    gentle_s: [[0, 0], [64, 52], [128, 132], [192, 204], [255, 255]],
    soft_matte: [[0, 12], [64, 70], [128, 132], [192, 196], [255, 246]],
    lift_shadows: [[0, 18], [80, 88], [160, 166], [255, 255]],
    rolloff_highlights: [[0, 0], [96, 100], [190, 205], [255, 242]]
  };
  return presets[key] || [[0, 0], [255, 255]];
}

function curvesAdjustmentDescriptor(params) {
  const points = Array.isArray(params.points) && params.points.length >= 2
    ? params.points
    : curvesPresetPoints(params.preset);
  return {
    _obj: "curves",
    adjustment: [
      {
        _obj: "curvesAdjustment",
        channel: adjustmentChannelRef(params.channel),
        curve: points.map(curvePointDescriptor)
      }
    ]
  };
}

function levelsAdjustmentDescriptor(params) {
  return {
    _obj: "levels",
    adjustment: [
      {
        _obj: "levelsAdjustment",
        channel: adjustmentChannelRef(params.channel),
        input: [
          Math.round(numberParam(params.input_black, 0, 0, 255)),
          Math.round(numberParam(params.input_white, 255, 0, 255))
        ],
        gamma: numberParam(params.gamma, 1, 0.1, 9.99),
        output: [
          Math.round(numberParam(params.output_black, 0, 0, 255)),
          Math.round(numberParam(params.output_white, 255, 0, 255))
        ]
      }
    ]
  };
}

function selectiveColorRangeValue(value) {
  const key = String(value || "neutrals").toLowerCase();
  const map = {
    red: "reds",
    reds: "reds",
    yellow: "yellows",
    yellows: "yellows",
    green: "greens",
    greens: "greens",
    cyan: "cyans",
    cyans: "cyans",
    blue: "blues",
    blues: "blues",
    magenta: "magentas",
    magentas: "magentas",
    white: "whites",
    whites: "whites",
    neutral: "neutrals",
    neutrals: "neutrals",
    black: "blacks",
    blacks: "blacks"
  };
  return map[key] || "neutrals";
}

function selectiveColorCorrectionDescriptor(item) {
  const correction = item && typeof item === "object" ? item : {};
  return {
    _obj: "colorCorrection",
    colors: {
      _enum: "colors",
      _value: selectiveColorRangeValue(correction.range || correction.color)
    },
    cyan: Math.round(numberParam(correction.cyan, 0, -100, 100)),
    magenta: Math.round(numberParam(correction.magenta, 0, -100, 100)),
    yellowColor: Math.round(numberParam(correction.yellow, 0, -100, 100)),
    black: Math.round(numberParam(correction.black, 0, -100, 100))
  };
}

function selectiveColorDescriptor(params) {
  const corrections = Array.isArray(params.corrections)
    ? params.corrections
    : Array.isArray(params.colors) ? params.colors : [];
  return {
    _obj: "selectiveColor",
    method: {
      _enum: "correctionMethod",
      _value: String(params.method || "relative").toLowerCase() === "absolute" ? "absolute" : "relative"
    },
    colorCorrection: (corrections.length ? corrections : [{ range: "neutrals" }]).map(selectiveColorCorrectionDescriptor)
  };
}

function normalizedGradientStops(params, fallback) {
  const stops = Array.isArray(params && params.stops) && params.stops.length >= 2
    ? params.stops
    : fallback;
  return stops.map((stop, index) => {
    const raw = stop && typeof stop === "object" ? stop : {};
    const location = raw.location == null ? raw.position : raw.location;
    const fallbackLocation = stops.length <= 1 ? 0 : index / (stops.length - 1) * 100;
    return {
      color: rgbColor(raw.color || raw, index === 0 ? [0, 0, 0] : [255, 255, 255]),
      location: Math.round(numberParam(location, fallbackLocation, 0, 100) * 40.96),
      midpoint: Math.round(numberParam(raw.midpoint, 50, 0, 100))
    };
  });
}

function gradientDescriptor(params, fallbackStops) {
  const stops = normalizedGradientStops(params || {}, fallbackStops || [
    { location: 0, color: { rgb: [0, 0, 0] } },
    { location: 100, color: { rgb: [255, 255, 255] } }
  ]);
  return {
    _obj: "gradientClassEvent",
    name: safeLayerName(params && params.name, "Codex Gradient"),
    gradientForm: {
      _enum: "gradientForm",
      _value: "customStops"
    },
    interfaceIconFrameDimmed: 4096,
    colors: stops.map((stop, index) => ({
      _obj: "colorStop",
      color: stop.color,
      type: {
        _enum: "colorStopType",
        _value: "userStop"
      },
      location: stop.location,
      midpoint: stop.midpoint
    })),
    transparency: [
      {
        _obj: "transferSpec",
        opacity: percentUnit(100),
        location: 0,
        midpoint: 50
      },
      {
        _obj: "transferSpec",
        opacity: percentUnit(100),
        location: 4096,
        midpoint: 50
      }
    ]
  };
}

function gradientMapDescriptor(params) {
  return {
    _obj: "gradientMapClass",
    gradient: gradientDescriptor(params, [
      { location: 0, color: { rgb: [0, 0, 0] } },
      { location: 100, color: { rgb: [255, 255, 255] } }
    ]),
    reverse: params.reverse === true,
    dither: params.dither !== false
  };
}

function colorLookupDescriptor(params) {
  const name = String(params.name || params.lookup || params.profile || "");
  if (!name) {
    throw codedError("color_lookup_missing", "adjust_color_lookup requires params.name, lookup, or profile.");
  }
  return {
    _obj: "colorLookup",
    lookupType: {
      _enum: "colorLookupType",
      _value: String(params.type || "3dlut").toLowerCase().includes("abstract") ? "abstractProfile" : "3DLUT"
    },
    name
  };
}

function blendModeValue(value) {
  const map = {
    normal: "normal",
    multiply: "multiply",
    screen: "screen",
    overlay: "overlay",
    soft_light: "softLight",
    hard_light: "hardLight",
    color_dodge: "colorDodge",
    linear_dodge: "linearDodge",
    add: "linearDodge",
    lighten: "lighten",
    darken: "darken",
    difference: "difference",
    exclusion: "exclusion",
    vivid_light: "vividLight",
    linear_light: "linearLight",
    pin_light: "pinLight",
    hard_mix: "hardMix",
    color: "color",
    luminosity: "luminosity"
  };
  return map[String(value || "normal")] || "normal";
}

function makeBbox(x, y, width, height) {
  return {
    x: Math.max(0, Math.round(x)),
    y: Math.max(0, Math.round(y)),
    width: Math.max(1, Math.round(width)),
    height: Math.max(1, Math.round(height))
  };
}

function normalizeRegion(region, index, state) {
  if (!region || region.type !== "bbox" || !region.bbox) {
    return {
      error: `region ${index + 1} must use type=bbox and include bbox`
    };
  }

  const raw = region.bbox;
  const x = Number(raw.x);
  const y = Number(raw.y);
  const width = Number(raw.width);
  const height = Number(raw.height);
  if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
    return {
      error: `region ${region.id || index + 1} has invalid bbox numbers`
    };
  }

  const docWidth = Math.max(1, Math.round(state.width || 0));
  const docHeight = Math.max(1, Math.round(state.height || 0));
  const baseLeft = Math.max(0, Math.min(docWidth, x));
  const baseTop = Math.max(0, Math.min(docHeight, y));
  const baseRight = Math.max(0, Math.min(docWidth, x + width));
  const baseBottom = Math.max(0, Math.min(docHeight, y + height));
  if (baseRight <= baseLeft || baseBottom <= baseTop) {
    return {
      error: `region ${region.id || index + 1} is outside the active document`
    };
  }

  const padding = clampNumber(region.padding, 0, 512, 0);
  const paddedLeft = Math.max(0, Math.floor(baseLeft - padding));
  const paddedTop = Math.max(0, Math.floor(baseTop - padding));
  const paddedRight = Math.min(docWidth, Math.ceil(baseRight + padding));
  const paddedBottom = Math.min(docHeight, Math.ceil(baseBottom + padding));

  return {
    id: safeIdentifier(region.id, `region_${index + 1}`),
    purpose: region.purpose == null ? undefined : String(region.purpose),
    bbox: makeBbox(baseLeft, baseTop, baseRight - baseLeft, baseBottom - baseTop),
    padded_bbox: makeBbox(paddedLeft, paddedTop, paddedRight - paddedLeft, paddedBottom - paddedTop),
    crop_bounds: {
      left: paddedLeft,
      top: paddedTop,
      right: paddedRight,
      bottom: paddedBottom
    }
  };
}

async function readBinaryFile(file) {
  const bytes = await file.read({ format: storageFormats.binary });
  if (bytes instanceof ArrayBuffer) {
    return bytes;
  }
  if (bytes && bytes.buffer instanceof ArrayBuffer) {
    return bytes.buffer;
  }
  return bytes;
}

async function exportPreview(job) {
  const payload = job.payload || {};
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return {
      schema_version: "ps-agent/v1",
      job_id: job.job_id,
      status: "error",
      state,
      error: state.error || {
        code: "no_active_document",
        message: "No active Photoshop document is available."
      }
    };
  }

  const format = payload.format === "png" ? "png" : "jpeg";
  const extension = format === "png" ? "png" : "jpg";
  const mimeType = format === "png" ? "image/png" : "image/jpeg";
  const maxSide = clampNumber(payload.max_side, 256, 4096, 1600);
  const quality = clampNumber(payload.quality, 1, 12, 8);
  const dims = previewDimensions(state.width, state.height, maxSide);
  const fileName = `${job.job_id}-preview-${dims.width}x${dims.height}.${extension}`;
  const tempFolder = await fs.getTemporaryFolder();
  const file = await tempFolder.createFile(fileName, { overwrite: true });

  await core.executeAsModal(async () => {
    const duplicate = await app.activeDocument.duplicate(`Codex Preview ${job.job_id}`, true);
    try {
      if (dims.scale_factor < 1) {
        await duplicate.resizeImage(dims.width, dims.height);
      }
      if (format === "png") {
        await duplicate.saveAs.png(file, {}, true);
      } else {
        await duplicate.saveAs.jpg(file, { quality }, true);
      }
    } finally {
      try {
        await duplicate.closeWithoutSaving();
      } catch (error) {
      }
    }
  }, { commandName: "Export Codex Preview" });

  const bytes = await readBinaryFile(file);
  const upload = await uploadBinary(
    `/uxp/assets/${encodeURIComponent(job.job_id)}/${encodeURIComponent(fileName)}`,
    bytes,
    mimeType
  );
  const asset = upload.asset || {};
  asset.width = dims.width;
  asset.height = dims.height;
  asset.scale_factor = dims.scale_factor;

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    document: state,
    preview: asset,
    global_preview: {
      after: asset,
      scale_factor: dims.scale_factor
    },
    warnings: []
  };
}

async function exportSingleRegion(job, normalizedRegion, exportOptions) {
  const dims = previewDimensions(
    normalizedRegion.padded_bbox.width,
    normalizedRegion.padded_bbox.height,
    exportOptions.maxSide,
    exportOptions.upscaleSmallRegions
  );
  const fileName = `${job.job_id}-region-${normalizedRegion.id}-${dims.width}x${dims.height}.${exportOptions.extension}`;
  const tempFolder = await fs.getTemporaryFolder();
  const file = await tempFolder.createFile(fileName, { overwrite: true });

  await core.executeAsModal(async () => {
    const duplicate = await app.activeDocument.duplicate(`Codex Region ${normalizedRegion.id}`, true);
    try {
      await duplicate.crop(normalizedRegion.crop_bounds);
      if (Math.abs(dims.scale_factor - 1) > 0.000001) {
        await duplicate.resizeImage(dims.width, dims.height);
      }
      if (exportOptions.format === "png") {
        await duplicate.saveAs.png(file, {}, true);
      } else {
        await duplicate.saveAs.jpg(file, { quality: exportOptions.quality }, true);
      }
    } finally {
      try {
        await duplicate.closeWithoutSaving();
      } catch (error) {
      }
    }
  }, { commandName: `Export Codex Region ${normalizedRegion.id}` });

  const bytes = await readBinaryFile(file);
  const upload = await uploadBinary(
    `/uxp/assets/${encodeURIComponent(job.job_id)}/${encodeURIComponent(fileName)}`,
    bytes,
    exportOptions.mimeType
  );
  const asset = upload.asset || {};
  asset.width = dims.width;
  asset.height = dims.height;
  asset.scale_factor = dims.scale_factor;

  const result = {
    id: normalizedRegion.id,
    bbox: normalizedRegion.bbox,
    padded_bbox: normalizedRegion.padded_bbox,
    after: asset
  };
  if (normalizedRegion.purpose) {
    result.purpose = normalizedRegion.purpose;
  }
  return result;
}

async function exportRegions(job) {
  const payload = job.payload || {};
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return {
      schema_version: "ps-agent/v1",
      job_id: job.job_id,
      status: "error",
      state,
      error: state.error || {
        code: "no_active_document",
        message: "No active Photoshop document is available."
      }
    };
  }

  const requestedRegions = Array.isArray(payload.regions) ? payload.regions.slice(0, 12) : [];
  if (requestedRegions.length === 0) {
    return {
      schema_version: "ps-agent/v1",
      job_id: job.job_id,
      status: "error",
      document: state,
      error: {
        code: "invalid_regions",
        message: "regions must contain at least one bbox request."
      }
    };
  }

  const format = payload.format === "png" ? "png" : "jpeg";
  const exportOptions = {
    format,
    extension: format === "png" ? "png" : "jpg",
    mimeType: format === "png" ? "image/png" : "image/jpeg",
    maxSide: clampNumber(payload.max_side, 128, 2048, DEFAULT_REGION_MAX_SIDE),
    upscaleSmallRegions: payload.upscale_small_regions !== false,
    quality: clampNumber(payload.quality, 1, 12, 8)
  };
  const warnings = [];
  const regions = [];

  for (let index = 0; index < requestedRegions.length; index += 1) {
    const normalized = normalizeRegion(requestedRegions[index], index, state);
    if (normalized.error) {
      warnings.push(normalized.error);
      continue;
    }
    try {
      regions.push(await exportSingleRegion(job, normalized, exportOptions));
    } catch (error) {
      warnings.push(`region ${normalized.id} export failed: ${error && error.message ? error.message : error}`);
    }
  }

  if (regions.length === 0) {
    return {
      schema_version: "ps-agent/v1",
      job_id: job.job_id,
      status: "error",
      document: state,
      warnings,
      error: {
        code: "region_export_failed",
        message: "No requested regions could be exported."
      }
    };
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    document: state,
    regions,
    warnings
  };
}

async function playAction(commands) {
  const result = await action.batchPlay(commands, {});
  assertActionOk(result);
  return result;
}

function assertActionOk(result) {
  if (!Array.isArray(result)) {
    return;
  }
  for (const item of result) {
    if (item && item._obj === "error") {
      const message = item.message || item.messageID || "Photoshop action failed.";
      throw new Error(message);
    }
  }
}

function errorResult(job, code, message, details) {
  const error = { code, message };
  if (details !== undefined) {
    error.details = details;
  }
  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "error",
    error
  };
}

function codedError(code, message, details) {
  const error = new Error(message);
  error.code = code;
  if (details !== undefined) {
    error.details = details;
  }
  return error;
}

function pixelUnit(value) {
  return {
    _unit: "pixelsUnit",
    _value: value
  };
}

function percentUnit(value) {
  return {
    _unit: "percentUnit",
    _value: value
  };
}

function angleUnit(value) {
  return {
    _unit: "angleUnit",
    _value: value
  };
}

function unitNumber(value, fallback) {
  if (value && typeof value === "object" && value._value != null) {
    const parsed = Number(value._value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function rgbColor(value, fallback) {
  const source = value && typeof value === "object" ? value : {};
  const array = Array.isArray(source.rgb) ? source.rgb : Array.isArray(value) ? value : null;
  const red = array ? array[0] : source.red;
  const green = array ? array[1] : source.green;
  const blue = array ? array[2] : source.blue;
  const fb = Array.isArray(fallback) ? fallback : [255, 255, 255];
  return {
    _obj: "RGBColor",
    red: numberParam(red, fb[0], 0, 255),
    green: numberParam(green, fb[1], 0, 255),
    blue: numberParam(blue, fb[2], 0, 255)
  };
}

function activeLayerRef() {
  return { _ref: "layer", _enum: "ordinal", _value: "targetEnum" };
}

function layerRefFromId(layerId) {
  if (layerId == null || layerId === "") {
    return activeLayerRef();
  }
  const numeric = Number(layerId);
  if (Number.isFinite(numeric)) {
    return { _ref: "layer", _id: numeric };
  }
  return { _ref: "layer", _name: String(layerId) };
}

async function getActiveLayerId() {
  const result = await playAction([
    {
      _obj: "get",
      _target: [activeLayerRef()],
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  const layer = result && result[0] ? result[0] : {};
  return layer.layerID == null ? layer.id || null : layer.layerID;
}

async function getActiveLayerDescriptor() {
  const result = await playAction([
    {
      _obj: "get",
      _target: [activeLayerRef()],
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  return result && result[0] ? result[0] : {};
}

async function getLayerDescriptorById(layerId) {
  if (layerId != null) {
    await selectLayer(layerId);
  }
  return await getActiveLayerDescriptor();
}

function layerBoundsFromDescriptor(descriptor) {
  const bounds = descriptor && descriptor.bounds ? descriptor.bounds : null;
  if (!bounds) {
    return null;
  }
  const left = unitNumber(bounds.left, 0);
  const top = unitNumber(bounds.top, 0);
  const right = unitNumber(bounds.right, left);
  const bottom = unitNumber(bounds.bottom, top);
  return {
    left,
    top,
    right,
    bottom,
    width: Math.max(1, right - left),
    height: Math.max(1, bottom - top),
    centerX: (left + right) / 2,
    centerY: (top + bottom) / 2
  };
}

function activeDocumentPixelSize() {
  return {
    width: Math.max(1, unitNumber(app.activeDocument && app.activeDocument.width, 1)),
    height: Math.max(1, unitNumber(app.activeDocument && app.activeDocument.height, 1))
  };
}

function activeDocumentResolution() {
  return Math.max(1, unitNumber(app.activeDocument && app.activeDocument.resolution, 72));
}

function unitNumberInPixels(value, axisLength, fallback) {
  if (value && typeof value === "object" && value._value != null) {
    const parsed = Number(value._value);
    if (!Number.isFinite(parsed)) {
      return fallback;
    }
    if (String(value._unit || "").toLowerCase() === "percentunit") {
      return axisLength * parsed / 100;
    }
    return parsed;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function textAnchorFromDescriptor(descriptor) {
  if (!descriptor || descriptor.textKey === undefined) {
    return null;
  }
  const point = descriptor.textClickPoint || null;
  if (!point) {
    return null;
  }
  const docSize = activeDocumentPixelSize();
  const x = unitNumberInPixels(point.horizontal, docSize.width, null);
  const y = unitNumberInPixels(point.vertical, docSize.height, null);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    return null;
  }
  return { x, y };
}

async function selectLayer(layerId) {
  await playAction([
    {
      _obj: "select",
      _target: [layerRefFromId(layerId)],
      makeVisible: true,
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function selectLayerIds(layerIds) {
  const ids = Array.from(new Set((layerIds || []).map((value) => Number(value)).filter((value) => Number.isFinite(value))));
  if (!ids.length) {
    return;
  }
  for (let index = 0; index < ids.length; index += 1) {
    const descriptor = {
      _obj: "select",
      _target: [layerRefFromId(ids[index])],
      makeVisible: true,
      _options: { dialogOptions: "dontDisplay" }
    };
    if (index > 0) {
      descriptor.selectionModifier = {
        _enum: "selectionModifierType",
        _value: "addToSelectionContinuous"
      };
    }
    await playAction([descriptor]);
  }
}

function findLayerObjectById(layersLike, layerId) {
  const targetId = Number(layerId);
  if (!Number.isFinite(targetId)) {
    return null;
  }
  const layers = Array.isArray(layersLike) ? layersLike : Array.from(layersLike || []);
  for (const layer of layers) {
    if (!layer) {
      continue;
    }
    if (Number(layer.id) === targetId) {
      return layer;
    }
    try {
      if (layer.layers) {
        const child = findLayerObjectById(layer.layers, targetId);
        if (child) {
          return child;
        }
      }
    } catch (error) {
    }
  }
  return null;
}

function isCameraRawUnsupportedLayer(descriptor) {
  const section = descriptor.layerSection && descriptor.layerSection._value
    ? String(descriptor.layerSection._value)
    : "";
  if (section && section !== "layerSectionContent") {
    return "layer_group";
  }
  if (descriptor.adjustment !== undefined) {
    return "adjustment_layer";
  }
  if (descriptor.textKey !== undefined) {
    return "text_layer";
  }
  return null;
}

async function assertCameraRawTargetLayer(layerId) {
  await selectLayer(layerId);
  const descriptor = await getActiveLayerDescriptor();
  const unsupported = isCameraRawUnsupportedLayer(descriptor);
  if (unsupported) {
    throw codedError(
      "unsupported_camera_raw_target",
      "camera_raw_filter requires a pixel or smart object layer target.",
      {
        reason: unsupported,
        layer_id: descriptor.layerID == null ? descriptor.id || null : descriptor.layerID,
        layer_name: descriptor.name || null
      }
    );
  }
  return descriptor;
}

async function clearSelection() {
  try {
    await playAction([
      {
        _obj: "set",
        _target: [
          { _ref: "channel", _property: "selection" }
        ],
        to: {
          _enum: "ordinal",
          _value: "none"
        },
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  } catch (error) {
  }
}

async function hasActiveSelection() {
  try {
    await playAction([
      {
        _obj: "get",
        _target: [
          { _ref: "property", _property: "selection" },
          { _ref: "document", _enum: "ordinal", _value: "targetEnum" }
        ],
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
    return true;
  } catch (error) {
    return false;
  }
}

function normalizedSelectionOperation(value, fallback) {
  const operation = String(value || fallback || "replace");
  if (!Object.prototype.hasOwnProperty.call(SELECTION_OPERATION_TO_CONSTANT, operation)) {
    throw codedError(
      "invalid_selection_operation",
      `selection_mask.operation must be one of: ${Object.keys(SELECTION_OPERATION_TO_CONSTANT).join(", ")}.`,
      { operation }
    );
  }
  return operation;
}

function selectionTypeForOperation(operation) {
  const selectionType = constants.SelectionType || {};
  const key = SELECTION_OPERATION_TO_CONSTANT[operation];
  const value = selectionType[key];
  if (value == null) {
    throw codedError(
      "selection_api_unavailable",
      "Photoshop SelectionType constants are unavailable; reload the plugin in Photoshop 25+.",
      { operation, constant: key }
    );
  }
  return value;
}

function activeSelectionApi() {
  const document = app.activeDocument;
  const selection = document && document.selection;
  if (!selection || typeof selection.selectPolygon !== "function") {
    throw codedError(
      "selection_api_unavailable",
      "Photoshop 25 Selection API is unavailable for the active document."
    );
  }
  return selection;
}

async function ensureSelectionOperationHasBase(operation) {
  if (operation === "replace") {
    return;
  }
  if (!(await hasActiveSelection())) {
    throw codedError(
      "selection_base_missing",
      `selection operation ${operation} requires an existing base selection.`
    );
  }
}

function normalizedSelectionBbox(mask, state) {
  const bbox = mask.bbox || {};
  const x = Number(bbox.x);
  const y = Number(bbox.y);
  const width = Number(bbox.width);
  const height = Number(bbox.height);
  if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
    throw codedError(
      "invalid_selection_bbox",
      "selection_mask.bbox must include positive x, y, width, and height values."
    );
  }

  const docWidth = Math.max(1, Math.round(state.width || 0));
  const docHeight = Math.max(1, Math.round(state.height || 0));
  const left = Math.max(0, Math.min(docWidth, Math.round(x)));
  const top = Math.max(0, Math.min(docHeight, Math.round(y)));
  const right = Math.max(0, Math.min(docWidth, Math.round(x + width)));
  const bottom = Math.max(0, Math.min(docHeight, Math.round(y + height)));
  if (right <= left || bottom <= top) {
    throw codedError("invalid_selection_bbox", "selection_mask.bbox is outside the active document.");
  }
  return { left, top, right, bottom };
}

function rectanglePointsFromBbox(bbox) {
  return [
    { x: bbox.left, y: bbox.top },
    { x: bbox.right, y: bbox.top },
    { x: bbox.right, y: bbox.bottom },
    { x: bbox.left, y: bbox.bottom }
  ];
}

async function selectBboxBatchPlayReplace(mask, state) {
  const bbox = normalizedSelectionBbox(mask, state);
  await playAction([
    {
      _obj: "set",
      _target: [
        { _ref: "channel", _property: "selection" }
      ],
      to: {
        _obj: "rectangle",
        top: pixelUnit(bbox.top),
        left: pixelUnit(bbox.left),
        bottom: pixelUnit(bbox.bottom),
        right: pixelUnit(bbox.right)
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function selectBbox(mask, state, operation) {
  const bbox = normalizedSelectionBbox(mask, state);
  await selectPolygonPoints(rectanglePointsFromBbox(bbox), mask, state, operation, "bbox_selection_failed");
}

function normalizedEllipseBoundsFromCenter(point, state) {
  const x = Number(point && point.x);
  const y = Number(point && point.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw codedError("invalid_retouch_points", "Retouch point x/y must be numbers.", { point });
  }
  const radius = point.radius == null ? point.r : point.radius;
  const width = point.width == null
    ? numberParam(radius == null ? 24 : Number(radius) * 2, 24, 1, 1000)
    : numberParam(point.width, 24, 1, 1000);
  const height = point.height == null
    ? numberParam(radius == null ? width : Number(radius) * 2, width, 1, 1000)
    : numberParam(point.height, width, 1, 1000);
  const docWidth = Math.max(1, Math.round(state && state.width || 0));
  const docHeight = Math.max(1, Math.round(state && state.height || 0));
  const left = Math.max(0, Math.min(docWidth, x - width / 2));
  const top = Math.max(0, Math.min(docHeight, y - height / 2));
  const right = Math.max(0, Math.min(docWidth, x + width / 2));
  const bottom = Math.max(0, Math.min(docHeight, y + height / 2));
  if (right <= left || bottom <= top) {
    throw codedError("invalid_retouch_points", "Retouch point is outside the active document.", { point });
  }
  return { left, top, right, bottom, center_x: x, center_y: y, width: right - left, height: bottom - top };
}

async function selectEllipseReplace(bounds, feather) {
  await playAction([
    {
      _obj: "set",
      _target: [
        { _ref: "channel", _property: "selection" }
      ],
      to: {
        _obj: "ellipse",
        top: pixelUnit(bounds.top),
        left: pixelUnit(bounds.left),
        bottom: pixelUnit(bounds.bottom),
        right: pixelUnit(bounds.right)
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await featherSelection(feather);
}

function normalizedPolygonPoints(mask, state) {
  const rawPoints = Array.isArray(mask.points) ? mask.points : [];
  if (rawPoints.length < MIN_POLYGON_POINTS || rawPoints.length > MAX_POLYGON_POINTS) {
    throw codedError(
      "invalid_polygon_points",
      `selection_mask.points must contain ${MIN_POLYGON_POINTS}-${MAX_POLYGON_POINTS} points.`
    );
  }

  const docWidth = Math.max(1, Math.round(state.width || 0));
  const docHeight = Math.max(1, Math.round(state.height || 0));
  const points = rawPoints.map((point, index) => {
    const x = Array.isArray(point) ? point[0] : point && point.x;
    const y = Array.isArray(point) ? point[1] : point && point.y;
    const parsedX = Number(x);
    const parsedY = Number(y);
    if (!Number.isFinite(parsedX) || !Number.isFinite(parsedY)) {
      throw codedError(
        "invalid_polygon_points",
        `selection_mask.points[${index}] must be [x, y] or {x, y}.`
      );
    }
    return {
      x: Math.max(0, Math.min(docWidth, parsedX)),
      y: Math.max(0, Math.min(docHeight, parsedY))
    };
  });

  const unique = new Set(points.map((point) => `${Math.round(point.x * 1000)}/${Math.round(point.y * 1000)}`));
  if (unique.size < MIN_POLYGON_POINTS) {
    throw codedError("invalid_polygon_points", "selection_mask.points must contain at least three unique points.");
  }
  return points;
}

async function selectPolygonBatchPlayReplace(mask, state) {
  const points = normalizedPolygonPoints(mask, state);
  const descriptorPoints = points.map((point) => ({
    _obj: "point",
    horizontal: pixelUnit(point.x),
    vertical: pixelUnit(point.y)
  }));

  try {
    await playAction([
      {
        _obj: "set",
        _target: [
          { _ref: "channel", _property: "selection" }
        ],
        to: {
          _obj: "polygon",
          points: descriptorPoints
        },
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  } catch (error) {
    throw codedError(
      "polygon_selection_unavailable",
      "Photoshop rejected polygon selection for this UXP build.",
      {
        message: error && error.message ? error.message : String(error),
        point_count: points.length
      }
    );
  }
}

async function selectPolygonPoints(points, mask, state, operation, errorCode) {
  await ensureSelectionOperationHasBase(operation);
  const feather = numberParam(mask && mask.feather, 0, 0, 500);
  let selection;
  let selectionType;
  try {
    selection = activeSelectionApi();
    selectionType = selectionTypeForOperation(operation);
  } catch (apiError) {
    if (operation === "replace") {
      if (errorCode === "bbox_selection_failed") {
        await selectBboxBatchPlayReplace(mask, state);
      } else {
        await selectPolygonBatchPlayReplace(mask, state);
      }
      await featherSelection(feather);
      return;
    }
    throw apiError;
  }
  const pointPairs = points.map((point) => [point.x, point.y]);

  try {
    await selection.selectPolygon(pointPairs, selectionType, feather, true);
    return;
  } catch (firstError) {
    const objectPoints = points.map((point) => ({ x: point.x, y: point.y }));
    try {
      await selection.selectPolygon(objectPoints, selectionType, feather, true);
      return;
    } catch (secondError) {
      if (operation === "replace") {
        try {
          if (errorCode === "bbox_selection_failed") {
            await selectBboxBatchPlayReplace(mask, state);
          } else {
            await selectPolygonBatchPlayReplace(mask, state);
          }
          await featherSelection(feather);
          return;
        } catch (fallbackError) {
          throw codedError(
            errorCode || "polygon_selection_unavailable",
            "Photoshop rejected this selection through both PS 25 Selection API and legacy batchPlay fallback.",
            {
              operation,
              point_count: points.length,
              selection_api_error: firstError && firstError.message ? firstError.message : String(firstError),
              fallback_error: fallbackError && fallbackError.message ? fallbackError.message : String(fallbackError)
            }
          );
        }
      }
      throw codedError(
        errorCode || "polygon_selection_unavailable",
        "Photoshop rejected this selection operation through the PS 25 Selection API.",
        {
          operation,
          point_count: points.length,
          selection_api_error: firstError && firstError.message ? firstError.message : String(firstError),
          retry_error: secondError && secondError.message ? secondError.message : String(secondError)
        }
      );
    }
  }
}

async function selectPolygon(mask, state, operation) {
  const points = normalizedPolygonPoints(mask, state);
  await selectPolygonPoints(points, mask, state, operation, "polygon_selection_unavailable");
}

async function selectSubject() {
  await playAction([
    {
      _obj: "autoCutout",
      sampleAllLayers: true,
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function selectSky() {
  await playAction([
    {
      _obj: "selectSky",
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function featherSelection(feather) {
  const radius = numberParam(feather, 0, 0, 500);
  if (radius <= 0) {
    return;
  }
  await playAction([
    {
      _obj: "feather",
      radius: pixelUnit(radius),
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function invertSelection() {
  await playAction([
    {
      _obj: "inverse",
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function selectAll() {
  await playAction([
    {
      _obj: "set",
      _target: [
        { _ref: "channel", _property: "selection" }
      ],
      to: {
        _enum: "ordinal",
        _value: "allEnum"
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function expandSelection(amount) {
  await playAction([
    {
      _obj: "expand",
      by: pixelUnit(numberParam(amount, 0, 0, 500)),
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function contractSelection(amount) {
  await playAction([
    {
      _obj: "contract",
      by: pixelUnit(numberParam(amount, 0, 0, 500)),
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function smoothSelection(amount) {
  await playAction([
    {
      _obj: "smooth",
      radius: pixelUnit(numberParam(amount, 0, 0, 500)),
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function borderSelection(amount) {
  await playAction([
    {
      _obj: "border",
      width: pixelUnit(numberParam(amount, 0, 0, 500)),
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

function rgbColorDescriptor(color) {
  const value = color || {};
  return {
    _obj: "RGBColor",
    red: numberParam(value.r, 0, 0, 255),
    green: numberParam(value.g, 0, 0, 255),
    blue: numberParam(value.b, 0, 0, 255)
  };
}

function colorRangePresetValue(preset) {
  const mapping = {
    reds: "reds",
    yellows: "yellows",
    greens: "greens",
    cyans: "cyans",
    blues: "blues",
    magentas: "magentas",
    skin_tones: "skinTone",
    highlights: "highlights",
    midtones: "midtones",
    shadows: "shadows"
  };
  return mapping[String(preset || "")] || null;
}

async function selectColorRange(payload) {
  const descriptor = {
    _obj: "colorRange",
    fuzziness: numberParam(payload.fuzziness, 40, 0, 200),
    _options: { dialogOptions: "dontDisplay" }
  };
  const preset = colorRangePresetValue(payload.preset);
  if (preset) {
    descriptor.colors = {
      _enum: "colors",
      _value: preset
    };
  } else {
    descriptor.color = rgbColorDescriptor(payload.color);
  }
  if (payload.localized_color_clusters === true) {
    descriptor.localizedColorClusters = true;
  }
  try {
    await playAction([descriptor]);
  } catch (error) {
    throw codedError(
      "color_range_descriptor_unavailable",
      "Photoshop rejected the Color Range descriptor; capture an Action.json descriptor for this Photoshop version to calibrate it.",
      { message: error && error.message ? error.message : String(error), descriptor }
    );
  }
}

async function selectColorRangeSelectionMask(mask, operation) {
  if (operation === "replace") {
    await selectColorRange(mask);
    await featherSelection(mask.feather);
    return;
  }

  await ensureSelectionOperationHasBase(operation);
  const baseChannelName = tempSelectionChannelName("base");
  const colorRangeChannelName = tempSelectionChannelName("color-range");
  let baseSaved = false;
  let colorRangeSaved = false;
  try {
    await saveSelectionChannel(baseChannelName);
    baseSaved = true;

    await selectColorRange(mask);
    await featherSelection(mask.feather);
    if (!(await hasActiveSelection())) {
      throw codedError(
        "selection_empty",
        `selection_mask source color_range with operation ${operation} did not create a usable temporary selection.`
      );
    }
    await saveSelectionChannel(colorRangeChannelName);
    colorRangeSaved = true;

    await loadSelectionChannel(baseChannelName, "replace");
    await loadSelectionChannel(colorRangeChannelName, operation);
  } catch (error) {
    if (baseSaved) {
      try {
        await loadSelectionChannel(baseChannelName, "replace");
      } catch (restoreError) {
      }
    }
    throw error;
  } finally {
    if (colorRangeSaved) {
      await deleteSelectionChannel(colorRangeChannelName);
    }
    if (baseSaved) {
      await deleteSelectionChannel(baseChannelName);
    }
  }
}

async function selectGeneratedSelectionMask(mask, operation, source, runSelector) {
  if (operation === "replace") {
    await runSelector();
    await featherSelection(mask.feather);
    return;
  }

  await ensureSelectionOperationHasBase(operation);
  const baseChannelName = tempSelectionChannelName("base");
  const generatedChannelName = tempSelectionChannelName(source);
  let baseSaved = false;
  let generatedSaved = false;
  try {
    await saveSelectionChannel(baseChannelName);
    baseSaved = true;

    await runSelector();
    await featherSelection(mask.feather);
    if (!(await hasActiveSelection())) {
      throw codedError(
        "selection_empty",
        `selection_mask source ${source} with operation ${operation} did not create a usable temporary selection.`
      );
    }
    await saveSelectionChannel(generatedChannelName);
    generatedSaved = true;

    await loadSelectionChannel(baseChannelName, "replace");
    await loadSelectionChannel(generatedChannelName, operation);
  } catch (error) {
    if (baseSaved) {
      try {
        await loadSelectionChannel(baseChannelName, "replace");
      } catch (restoreError) {
      }
    }
    throw error;
  } finally {
    if (generatedSaved) {
      await deleteSelectionChannel(generatedChannelName);
    }
    if (baseSaved) {
      await deleteSelectionChannel(baseChannelName);
    }
  }
}

async function selectFocusArea(payload) {
  const descriptor = {
    _obj: "focusArea",
    inFocusRange: numberParam(payload.in_focus_range, 4, 0, 10),
    noiseLevel: numberParam(payload.noise_level, 1, 0, 10),
    _options: { dialogOptions: "dontDisplay" }
  };
  try {
    await playAction([descriptor]);
  } catch (error) {
    throw codedError(
      "focus_area_descriptor_unavailable",
      "Photoshop rejected the Focus Area descriptor; this command may require descriptor calibration on the current Photoshop build.",
      { message: error && error.message ? error.message : String(error), descriptor }
    );
  }
}

async function saveSelectionChannel(channelName) {
  if (!(await hasActiveSelection())) {
    throw codedError("no_active_selection", "Cannot save selection because no active Photoshop selection exists.");
  }
  try {
    await playAction([
      {
        _obj: "duplicate",
        _target: [
          { _ref: "channel", _property: "selection" }
        ],
        name: String(channelName),
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  } catch (error) {
    throw codedError(
      "save_selection_channel_failed",
      "Photoshop rejected saving the active selection to an alpha channel.",
      { message: error && error.message ? error.message : String(error), channel_name: channelName }
    );
  }
}

function tempSelectionChannelName(prefix) {
  const randomPart = Math.random().toString(16).slice(2, 8);
  return `Codex ${prefix} ${Date.now()} ${randomPart}`.slice(0, 120);
}

async function loadSelectionChannel(channelName, operation) {
  const normalizedOperation = String(operation || "replace");
  const descriptor = {
    _obj: "set",
    _target: [
      { _ref: "channel", _property: "selection" }
    ],
    to: {
      _ref: "channel",
      _name: String(channelName)
    },
    _options: { dialogOptions: "dontDisplay" }
  };
  if (normalizedOperation !== "replace") {
    const modifier = CHANNEL_SELECTION_MODIFIER_BY_OPERATION[normalizedOperation];
    if (!modifier) {
      throw codedError(
        "invalid_selection_operation",
        "Selection channel loading supports replace, add, subtract, or intersect.",
        { operation: normalizedOperation }
      );
    }
    descriptor.selectionModifier = {
      _enum: "selectionModifierType",
      _value: modifier
    };
  }
  try {
    await playAction([descriptor]);
  } catch (error) {
    throw codedError(
      "load_selection_channel_failed",
      "Photoshop rejected loading the named alpha channel as a selection.",
      {
        message: error && error.message ? error.message : String(error),
        channel_name: channelName,
        operation: normalizedOperation,
        descriptor
      }
    );
  }
}

async function deleteSelectionChannel(channelName) {
  try {
    await playAction([
      {
        _obj: "delete",
        _target: [
          { _ref: "channel", _name: String(channelName) }
        ],
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  } catch (error) {
  }
}

async function makeLayerMaskFromSelection() {
  await playAction([
    {
      _obj: "make",
      new: { _class: "channel" },
      at: {
        _ref: "channel",
        _enum: "channel",
        _value: "mask"
      },
      using: {
        _enum: "userMaskEnabled",
        _value: "revealSelection"
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

function alphaMaskAssetUri(mask) {
  const direct = mask.asset_uri || mask.uri;
  if (direct && /^https?:\/\//i.test(String(direct))) {
    return String(direct);
  }
  if (direct) {
    return String(direct);
  }

  const assetPath = String(mask.asset_path || "").replace(/\\/g, "/");
  const lower = assetPath.toLowerCase();
  const marker = "/backend/runtime/assets/";
  const index = lower.indexOf(marker);
  if (index >= 0) {
    const relative = assetPath.slice(index + marker.length).split("/").filter(Boolean);
    if (relative.length >= 2) {
      return `/assets/${encodeURIComponent(relative[0])}/${encodeURIComponent(relative[1])}`;
    }
  }
  throw codedError(
    "alpha_mask_asset_missing",
    "selection_mask source alpha_mask requires asset_uri, uri, or an asset_path under backend/runtime/assets.",
    { asset_path: mask.asset_path || null, asset_uri: mask.asset_uri || mask.uri || null }
  );
}

async function placeFileAsLayer(file, options = {}) {
  const failureCode = options.failureCode || "asset_place_failed";
  const assetLabel = options.assetLabel || "asset";
  if (typeof fs.createSessionToken !== "function") {
    throw codedError(
      options.unavailableCode || "asset_place_unavailable",
      `UXP localFileSystem.createSessionToken is unavailable, so the ${assetLabel} cannot be placed.`
    );
  }
  const token = fs.createSessionToken(file);
  try {
    await playAction([
      {
        _obj: "placeEvent",
        null: {
          _path: token,
          _kind: "local"
        },
        freeTransformCenterState: {
          _enum: "quadCenterState",
          _value: "QCSAverage"
        },
        offset: {
          _obj: "offset",
          horizontal: pixelUnit(0),
          vertical: pixelUnit(0)
        },
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  } catch (error) {
    throw codedError(
      failureCode,
      `Photoshop rejected the ${assetLabel} placement descriptor.`,
      { message: error && error.message ? error.message : String(error) }
    );
  }
}

async function loadActiveLayerTransparencyAsSelection() {
  await playAction([
    {
      _obj: "set",
      _target: [
        { _ref: "channel", _property: "selection" }
      ],
      to: {
        _ref: "channel",
        _enum: "channel",
        _value: "transparencyEnum"
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function selectAlphaMask(mask, operation) {
  if (operation !== "replace") {
    throw codedError(
      "alpha_mask_operation_unavailable",
      "alpha_mask currently supports operation=replace only. Generate a composited alpha mask for add/subtract/intersect behavior.",
      { operation }
    );
  }

  const uri = alphaMaskAssetUri(mask);
  const download = await downloadBinaryFromBackend(uri);
  const tempFile = await writeTempBinaryFile(`codex-alpha-mask-${Date.now()}.png`, download.bytes);
  let tempLayerId = null;
  try {
    await placeFileAsLayer(tempFile, { failureCode: "alpha_mask_import_failed", unavailableCode: "alpha_mask_import_unavailable", assetLabel: "alpha mask PNG" });
    tempLayerId = await getActiveLayerId();
    await loadActiveLayerTransparencyAsSelection();
  } catch (error) {
    throw error;
  } finally {
    if (tempLayerId != null) {
      try {
        await selectLayer(tempLayerId);
        await deleteLayerById(tempLayerId);
      } catch (cleanupError) {
      }
    }
  }

  await featherSelection(mask.feather);
  return {
    asset_uri: download.uri,
    asset_path: mask.asset_path || null,
    threshold: mask.threshold == null ? 0.5 : Number(mask.threshold),
    show_marching_ants: mask.show_marching_ants === true
  };
}

function selectionMaskLabel(mask) {
  return typeof mask.label === "string" ? mask.label : null;
}

function selectionMaskPointCount(mask) {
  return mask.source === "polygon" && Array.isArray(mask.points) ? mask.points.length : null;
}

function selectionMaskInfo(mask, operation, extra) {
  const info = Object.assign({
    source: mask.source,
    operation,
    label: selectionMaskLabel(mask),
    point_count: selectionMaskPointCount(mask),
    feather: numberParam(mask.feather, 0, 0, 500),
    invert: mask.invert === true
  }, extra || {});
  if (mask.source === "alpha_mask") {
    info.asset_path = mask.asset_path || null;
    info.asset_uri = mask.asset_uri || mask.uri || null;
    info.threshold = mask.threshold == null ? 0.5 : Number(mask.threshold);
    info.show_marching_ants = mask.show_marching_ants === true;
  }
  return info;
}

function normalizeSelectionMaskForTarget(target) {
  const mask = Object.assign({}, target.selection_mask || {});
  if (mask.source === "bbox" && !mask.bbox && target.bbox) {
    mask.bbox = target.bbox;
  }
  return mask;
}

function assertLegacyAcrMaskUnused(mask) {
  if (mask.use_acr_mask) {
    throw codedError(
      "legacy_acr_mask_field",
      "selection_mask.use_acr_mask is deprecated. Use target.type=acr_ai_mask with camera_raw_filter."
    );
  }
}

async function applySingleSelectionMask(mask, state, operation, options) {
  const source = mask.source;
  const opts = options || {};
  assertLegacyAcrMaskUnused(mask);

  if (operation !== "replace" && mask.invert === true) {
    throw codedError(
      "selection_invert_combine_unavailable",
      "selection_mask.invert can only be used with operation=replace or as a final composite invert.",
      { source, operation }
    );
  }

  if (source === "current_selection") {
    if (operation !== "replace") {
      throw codedError(
        "current_selection_combine_unavailable",
        "current_selection can only be used with operation=replace."
      );
    }
    if (!(await hasActiveSelection())) {
      throw codedError(
        "no_active_selection",
        "selection_mask source current_selection requires an active Photoshop selection."
      );
    }
  } else if (source === "bbox") {
    await selectBbox(mask, state, operation);
  } else if (source === "polygon") {
    await selectPolygon(mask, state, operation);
  } else if (source === "alpha_mask") {
    const alphaInfo = await selectAlphaMask(mask, operation);
    if (alphaInfo) {
      opts.alphaInfo = alphaInfo;
    }
  } else if (source === "color_range") {
    await selectColorRangeSelectionMask(mask, operation);
  } else if (source === "select_subject") {
    await selectGeneratedSelectionMask(mask, operation, source, selectSubject);
  } else if (source === "select_sky") {
    await selectGeneratedSelectionMask(mask, operation, source, selectSky);
  } else {
    throw codedError("unsupported_selection_source", `Unsupported selection_mask source: ${source}`);
  }

  if (mask.invert === true) {
    await invertSelection();
  }

  if (!opts.skipVerify && !(await hasActiveSelection())) {
    throw codedError(
      "selection_empty",
      `selection_mask source ${source} with operation ${operation} did not leave a usable selection.`
    );
  }

  return selectionMaskInfo(mask, operation, opts.alphaInfo || null);
}

async function applyCompositeSelectionMask(mask, state) {
  const items = Array.isArray(mask.items) ? mask.items : [];
  if (items.length < 1 || items.length > MAX_COMPOSITE_SELECTION_ITEMS) {
    throw codedError(
      "invalid_composite_selection",
      `selection_mask.items must contain 1-${MAX_COMPOSITE_SELECTION_ITEMS} items.`
    );
  }

  const itemInfos = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = Object.assign({}, items[index] || {});
    const operation = normalizedSelectionOperation(item.operation, index === 0 ? "replace" : "add");
    if (index === 0 && operation !== "replace") {
      throw codedError(
        "invalid_composite_selection",
        "selection_mask.items[0].operation must be replace so the composite mask is deterministic."
      );
    }
    itemInfos.push(await applySingleSelectionMask(item, state, operation));
  }

  await featherSelection(mask.feather);
  if (mask.invert === true) {
    await invertSelection();
  }
  if (!(await hasActiveSelection())) {
    throw codedError("selection_empty", "Composite selection did not leave a usable final selection.");
  }

  return selectionMaskInfo(mask, "replace", {
    item_count: itemInfos.length,
    items: itemInfos,
    label: selectionMaskLabel(mask),
    feather: numberParam(mask.feather, 0, 0, 500),
    invert: mask.invert === true
  });
}

async function prepareSelectionMask(target, state) {
  if (!target || target.type !== "selection_mask") {
    await clearSelection();
    return null;
  }

  const mask = normalizeSelectionMaskForTarget(target);
  assertLegacyAcrMaskUnused(mask);

  let info;
  if (mask.source === "composite") {
    info = await applyCompositeSelectionMask(mask, state);
  } else {
    const operation = normalizedSelectionOperation(mask.operation, "replace");
    info = await applySingleSelectionMask(mask, state, operation);
  }

  if (!(await hasActiveSelection())) {
    throw codedError("selection_empty", "selection_mask did not create a usable final selection.");
  }

  return info;
}

async function duplicateActiveLayer(layerName) {
  await playAction([
    {
      _obj: "duplicate",
      _target: [activeLayerRef()],
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await showActiveLayer();
  await setActiveLayerProperties({
    name: layerName,
    opacity: 100,
    blendMode: "normal"
  });
}

async function showActiveLayer() {
  await playAction([
    {
      _obj: "show",
      _target: [{ _ref: "layer", _enum: "ordinal", _value: "targetEnum" }],
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function convertActiveLayerToSmartObject() {
  await playAction([
    {
      _obj: "newPlacedLayer",
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function createLayerGroup(groupName) {
  return playAction([
    {
      _obj: "make",
      _target: [{ _ref: "layerSection" }],
      using: {
        _obj: "layerSection",
        name: groupName
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function groupSelectedLayers(groupName) {
  await playAction([
    {
      _obj: "make",
      _target: [{ _ref: "layerSection" }],
      from: activeLayerRef(),
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await setActiveLayerProperties({
    name: groupName,
    opacity: 100,
    blendMode: "normal"
  });
  return getActiveLayerId();
}

async function setActiveLayerProperties(layerOptions) {
  const to = {
    _obj: "layer",
    name: layerOptions.name,
    opacity: {
      _unit: "percentUnit",
      _value: layerOptions.opacity
    },
    mode: {
      _enum: "blendMode",
      _value: layerOptions.blendMode
    }
  };
  return playAction([
    {
      _obj: "set",
      _target: [{ _ref: "layer", _enum: "ordinal", _value: "targetEnum" }],
      to,
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

function operationStepRef(value) {
  if (typeof value !== "string" || !value.startsWith("$steps.")) {
    return null;
  }
  const parts = value.split(".");
  if (parts.length < 3) {
    return null;
  }
  return {
    key: parts[1],
    field: parts.slice(2).join(".")
  };
}

function resolveRecipeValue(value, stepResults) {
  const ref = operationStepRef(value);
  if (!ref) {
    if (Array.isArray(value)) {
      return value.map((item) => resolveRecipeValue(item, stepResults));
    }
    if (value && typeof value === "object") {
      const copy = {};
      for (const key of Object.keys(value)) {
        copy[key] = resolveRecipeValue(value[key], stepResults);
      }
      return copy;
    }
    return value;
  }

  const record = stepResults.byId[ref.key] || stepResults.byIndex[Number(ref.key)];
  if (!record) {
    throw codedError("operation_step_reference_missing", `Step reference ${value} could not be resolved.`);
  }
  if (!Object.prototype.hasOwnProperty.call(record, ref.field)) {
    throw codedError("operation_step_field_missing", `Step reference ${value} resolved to a step without field ${ref.field}.`);
  }
  return record[ref.field];
}

function resolveLayerTarget(step, params, stepResults) {
  const target = resolveRecipeValue(step.target, stepResults);
  if (target != null) {
    return target;
  }
  const targetLayerId = resolveRecipeValue(params.target_layer_id || params.layer_id, stepResults);
  return targetLayerId == null ? null : targetLayerId;
}

async function setLayerPropertiesById(layerId, params) {
  if (layerId != null) {
    await selectLayer(layerId);
  }

  const to = { _obj: "layer" };
  let hasSetProperties = false;
  if (params.name != null) {
    to.name = safeLayerName(params.name, "Codex Layer");
    hasSetProperties = true;
  }
  if (params.opacity != null) {
    to.opacity = {
      _unit: "percentUnit",
      _value: numberParam(params.opacity, 100, 0, 100)
    };
    hasSetProperties = true;
  }
  if (params.blend_mode != null) {
    to.mode = {
      _enum: "blendMode",
      _value: blendModeValue(params.blend_mode)
    };
    hasSetProperties = true;
  }
  if (hasSetProperties) {
    await playAction([
      {
        _obj: "set",
        _target: [{ _ref: "layer", _enum: "ordinal", _value: "targetEnum" }],
        to,
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  }

  if (params.visible === true || params.visible === false) {
    await playAction([
      {
        _obj: params.visible ? "show" : "hide",
        _target: [{ _ref: "layer", _enum: "ordinal", _value: "targetEnum" }],
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  } else {
    await showActiveLayer();
  }
  return getActiveLayerId();
}

async function applyGaussianBlurToLayer(layerId, radius) {
  if (layerId != null) {
    await selectLayer(layerId);
  }
  const parsedRadius = numberParam(radius, 24, 0.1, 500);
  await playAction([
    {
      _obj: "gaussianBlur",
      radius: { _unit: "pixelsUnit", _value: parsedRadius },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  return { layer_id: await getActiveLayerId(), radius: parsedRadius };
}

async function rasterizeActiveLayerForRetouch() {
  await playAction([
    {
      _obj: "rasterizeLayer",
      _target: [activeLayerRef()],
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function prepareRetouchPixelLayer(params, fallbackName) {
  const duplicateSource = params.duplicate_source !== false;
  const sourceLayerId = params.source_layer_id == null ? params.layer_id : params.source_layer_id;
  const targetLayerId = params.target_layer_id == null ? null : params.target_layer_id;
  const name = safeLayerName(params.name, fallbackName);
  const warnings = [];

  if (duplicateSource) {
    if (sourceLayerId != null) {
      await selectLayer(sourceLayerId);
    } else if (targetLayerId != null) {
      await selectLayer(targetLayerId);
    }
    await duplicateActiveLayer(name);
    if (params.rasterize_duplicate !== false) {
      try {
        await rasterizeActiveLayerForRetouch();
      } catch (error) {
        warnings.push({
          code: "rasterize_duplicate_failed",
          message: error && error.message ? error.message : String(error)
        });
      }
    }
    return {
      layer_id: await getActiveLayerId(),
      layer_name: name,
      duplicated: true,
      warnings
    };
  }

  const layerId = targetLayerId == null ? sourceLayerId : targetLayerId;
  if (layerId != null) {
    await selectLayer(layerId);
  }
  return {
    layer_id: await getActiveLayerId(),
    layer_name: null,
    duplicated: false,
    warnings
  };
}

async function contentAwareFillActiveSelection(params) {
  if (!(await hasActiveSelection())) {
    throw codedError("no_active_selection", "Content-aware fill requires an active selection.");
  }
  const opacity = numberParam(params.opacity, 100, 0, 100);
  const descriptors = [
    {
      _obj: "fill",
      using: { _enum: "fillContents", _value: "contentAware" },
      opacity: percentUnit(opacity),
      mode: { _enum: "blendMode", _value: blendModeValue(params.blend_mode || "normal") },
      _options: { dialogOptions: "dontDisplay" }
    },
    {
      _obj: "fill",
      using: { _enum: "fillContents", _value: "contentAware" },
      contentAwareColorAdaptationFill: true,
      opacity: percentUnit(opacity),
      mode: { _enum: "blendMode", _value: blendModeValue(params.blend_mode || "normal") },
      _options: { dialogOptions: "dontDisplay" }
    }
  ];
  let lastError = null;
  for (const descriptor of descriptors) {
    try {
      await playAction([descriptor]);
      return true;
    } catch (error) {
      lastError = error;
    }
  }
  throw codedError(
    "content_aware_fill_failed",
    "Photoshop rejected content-aware fill for the active selection.",
    { message: lastError && lastError.message ? lastError.message : String(lastError) }
  );
}

async function retouchContentAwareFillSelection(params) {
  const feather = numberParam(params.feather, 0, 0, 200);
  const expand = numberParam(params.expand, 0, 0, 200);
  const prepared = await prepareRetouchPixelLayer(params, `Codex Retouch - Content Aware ${Date.now()}`);
  if (expand > 0) {
    await expandSelection(expand);
  }
  if (feather > 0) {
    await featherSelection(feather);
  }
  await contentAwareFillActiveSelection(params);
  if (params.clear_selection !== false) {
    await clearSelection();
  }
  return {
    layer_id: prepared.layer_id,
    layer_name: prepared.layer_name,
    used_active_selection: true,
    duplicated: prepared.duplicated,
    warnings: prepared.warnings
  };
}

async function retouchSpotHealPoints(params, state) {
  const points = Array.isArray(params.points) ? params.points : [];
  if (!points.length) {
    throw codedError("invalid_retouch_points", "retouch.spot_heal_points requires a non-empty points array.");
  }
  if (points.length > 80) {
    throw codedError("invalid_retouch_points", "retouch.spot_heal_points supports at most 80 points per recipe step.", { point_count: points.length });
  }

  const feather = numberParam(params.feather, 2, 0, 200);
  const expand = numberParam(params.expand, 0, 0, 200);
  const prepared = await prepareRetouchPixelLayer(params, `Codex Retouch - Spot Heal ${Date.now()}`);
  const applied = [];
  for (let index = 0; index < points.length; index += 1) {
    const point = points[index] || {};
    const bounds = normalizedEllipseBoundsFromCenter(point, state);
    await selectEllipseReplace(bounds, feather);
    if (expand > 0) {
      await expandSelection(expand);
    }
    await contentAwareFillActiveSelection(params);
    applied.push({
      index,
      x: bounds.center_x,
      y: bounds.center_y,
      width: bounds.width,
      height: bounds.height,
      label: point.label || null
    });
  }
  if (params.clear_selection !== false) {
    await clearSelection();
  }
  return {
    layer_id: prepared.layer_id,
    layer_name: prepared.layer_name,
    duplicated: prepared.duplicated,
    point_count: applied.length,
    points: applied,
    warnings: prepared.warnings
  };
}

async function createAdjustmentLayerFromAtom(params, index, state, jobId) {
  const rawTargetLayerId = params.target_layer_id == null ? params.clip_to_layer_id : params.target_layer_id;
  const parsedTargetLayerId = Number(rawTargetLayerId);
  const targetLayerId = rawTargetLayerId == null || !Number.isFinite(parsedTargetLayerId)
    ? null : parsedTargetLayerId;
  if (targetLayerId != null) {
    await selectLayer(targetLayerId);
  }
  const op = {
    op: String(params.op || params.operation || params.adjustment_type || ""),
    target: params.target || { type: "global" },
    params: params.params || {},
    layer: params.layer || {
      name: params.name,
      opacity: params.opacity,
      blend_mode: params.blend_mode
    }
  };
  const baseLayerId = await getActiveLayerId();
  const applied = await applyOperation(op, index, baseLayerId, state, jobId);
  if (targetLayerId != null && applied.layer_id != null) {
    await moveLayerRelative(applied.layer_id, targetLayerId, "above");
  }
  let clippingMask = false;
  if (params.clipping_mask === true || params.clip_to_target === true) {
    if (targetLayerId == null) {
      throw codedError(
        "clipping_mask_target_missing",
        "adjustment.create requires target_layer_id when clipping_mask=true."
      );
    }
    await createClippingMask(applied.layer_id);
    clippingMask = true;
  }
  return {
    layer_id: applied.layer_id,
    layer_name: applied.layer_name,
    op: applied.op,
    target_type: applied.target_type,
    mask_source: applied.mask_source || null,
    target_layer_id: targetLayerId,
    clipping_mask: clippingMask,
    implementation: clippingMask
      ? "adjustment_layer_dom_clipping_mask"
      : "adjustment_layer"
  };
}

async function applySelectionMaskToLayer(layerId, selectionMask, state) {
  const targetLayerId = layerId == null ? await getActiveLayerId() : layerId;
  const maskInfo = await prepareSelectionMask({ type: "selection_mask", selection_mask: selectionMask }, state);
  await selectLayer(targetLayerId);
  await makeLayerMaskFromSelection();
  return {
    layer_id: targetLayerId,
    mask_applied: true,
    selection: maskInfo
  };
}

function backendAssetUri(asset) {
  const direct = asset.asset_uri || asset.uri;
  if (direct && /^https?:\/\//i.test(String(direct))) {
    return String(direct);
  }
  if (direct) {
    return String(direct);
  }
  const assetPath = String(asset.asset_path || "").replace(/\\/g, "/");
  const lower = assetPath.toLowerCase();
  const marker = "/backend/runtime/assets/";
  const index = lower.indexOf(marker);
  if (index >= 0) {
    const relative = assetPath.slice(index + marker.length).split("/").filter(Boolean);
    if (relative.length >= 2) {
      return `/assets/${encodeURIComponent(relative[0])}/${encodeURIComponent(relative[1])}`;
    }
  }
  throw codedError(
    "asset_uri_missing",
    "asset.place_embedded requires asset_uri, uri, or an asset_path under backend/runtime/assets.",
    { asset_path: asset.asset_path || null, asset_uri: asset.asset_uri || asset.uri || null }
  );
}

async function createDesignDocument(params) {
  const width = numberParam(params.width, 1080, 16, 30000);
  const height = numberParam(params.height, 1350, 16, 30000);
  const resolution = numberParam(params.resolution, 72, 1, 1200);
  const name = safeLayerName(params.name, "Codex Design");
  const failures = [];
  const rgbMode =
    (constants.NewDocumentMode && (constants.NewDocumentMode.RGB || constants.NewDocumentMode.RGBCOLOR)) ||
    (constants.DocumentMode && (constants.DocumentMode.RGB || constants.DocumentMode.RGBCOLOR)) ||
    "RGBColorMode";
  const whiteFill =
    (constants.DocumentFill && (constants.DocumentFill.WHITE || constants.DocumentFill.BACKGROUNDCOLOR)) ||
    "white";
  const domOptions = {
    name,
    width,
    height,
    resolution,
    mode: rgbMode,
    fill: whiteFill
  };
  const domCreateCandidates = [
    [
      "app.createDocument(options)",
      async () => {
        if (!app || typeof app.createDocument !== "function") {
          throw new Error("app.createDocument is unavailable");
        }
        await app.createDocument(domOptions);
      }
    ],
    [
      "app.createDocument(legacy options)",
      async () => {
        if (!app || typeof app.createDocument !== "function") {
          throw new Error("app.createDocument is unavailable");
        }
        await app.createDocument({
          name,
          width,
          height,
          resolution,
          mode: "RGBColorMode",
          fill: "white"
        });
      }
    ],
    [
      "app.documents.add(options)",
      async () => {
        if (!app || !app.documents || typeof app.documents.add !== "function") {
          throw new Error("app.documents.add is unavailable");
        }
        await app.documents.add(domOptions);
      }
    ],
    [
      "app.documents.add(positional)",
      async () => {
        if (!app || !app.documents || typeof app.documents.add !== "function") {
          throw new Error("app.documents.add is unavailable");
        }
        await app.documents.add(width, height, resolution, name, rgbMode, whiteFill, 1);
      }
    ],
    [
      "app.documents.add(positional legacy strings)",
      async () => {
        if (!app || !app.documents || typeof app.documents.add !== "function") {
          throw new Error("app.documents.add is unavailable");
        }
        await app.documents.add(width, height, resolution, name, "RGBColorMode", "white", 1);
      }
    ]
  ];
  let created = false;
  for (const [label, createFn] of domCreateCandidates) {
    try {
      await createFn();
      created = true;
      break;
    } catch (error) {
      failures.push({
        method: label,
        message: error && error.message ? error.message : String(error)
      });
    }
  }

  if (!created) {
    const descriptors = [
      {
        _obj: "make",
        _target: [{ _ref: "document" }],
        using: {
          _obj: "document",
          name,
          width: pixelUnit(width),
          height: pixelUnit(height),
          resolution: { _unit: "densityUnit", _value: resolution },
          mode: { _class: "RGBColorMode" },
          fill: { _enum: "fill", _value: "white" },
          pixelScaleFactor: 1
        },
        _options: { dialogOptions: "dontDisplay" }
      },
      {
        _obj: "make",
        new: { _class: "document" },
        using: {
          _obj: "document",
          name,
          width: pixelUnit(width),
          height: pixelUnit(height),
          resolution: { _unit: "densityUnit", _value: resolution },
          mode: { _class: "RGBColorMode" },
          fill: { _enum: "fill", _value: "white" }
        },
        _options: { dialogOptions: "dontDisplay" }
      },
      {
        _obj: "make",
        _target: [{ _ref: "document" }],
        using: {
          _obj: "document",
          name,
          width: { _unit: "distanceUnit", _value: width },
          height: { _unit: "distanceUnit", _value: height },
          resolution,
          mode: { _enum: "mode", _value: "RGBColor" },
          fill: { _enum: "fill", _value: "white" }
        },
        _options: { dialogOptions: "dontDisplay" }
      },
      {
        _obj: "make",
        _target: [{ _ref: "application" }],
        using: {
          _obj: "document",
          name,
          width: pixelUnit(width),
          height: pixelUnit(height),
          resolution,
          mode: { _enum: "mode", _value: "RGBColor" },
          fill: { _enum: "fill", _value: "white" }
        },
        _options: { dialogOptions: "dontDisplay" }
      }
    ];

    for (let index = 0; index < descriptors.length; index += 1) {
      try {
        await playAction([descriptors[index]]);
        created = true;
        break;
      } catch (error) {
        failures.push({
          method: `batchPlay.makeDocument.${index + 1}`,
          message: error && error.message ? error.message : String(error)
        });
      }
    }
  }

  if (!created) {
    throw codedError(
      "document_create_failed",
      "Photoshop rejected all document.create fallbacks.",
      { width, height, resolution, name, failures }
    );
  }
  const background = params.background && typeof params.background === "object" ? params.background : null;
  if (background) {
    await createRectangleShapeLayer({
      name: "Background",
      x: 0,
      y: 0,
      width,
      height,
      fill: background.rgb ? background : { rgb: [255, 255, 255] }
    });
  }
  return await readDocumentState(false);
}

function canvasAnchorDescriptor(anchor) {
  const key = String(anchor || "center").toLowerCase().replace(/[\s-]+/g, "_");
  const map = {
    top_left: ["left", "top"],
    top: ["center", "top"],
    top_center: ["center", "top"],
    top_right: ["right", "top"],
    left: ["left", "center"],
    center: ["center", "center"],
    middle: ["center", "center"],
    right: ["right", "center"],
    bottom_left: ["left", "bottom"],
    bottom: ["center", "bottom"],
    bottom_center: ["center", "bottom"],
    bottom_right: ["right", "bottom"]
  };
  const values = map[key] || map.center;
  return {
    horizontal: { _enum: "horizontalLocation", _value: values[0] },
    vertical: { _enum: "verticalLocation", _value: values[1] }
  };
}

async function setCanvasSize(params, currentState) {
  const state = currentState && currentState.has_active_document ? currentState : await readDocumentState(false);
  const width = params.width == null
    ? numberParam(state.width, 1080, 1, 30000)
    : numberParam(params.width, state.width || 1080, 1, 30000);
  const height = params.height == null
    ? numberParam(state.height, 1080, 1, 30000)
    : numberParam(params.height, state.height || 1080, 1, 30000);
  const anchor = canvasAnchorDescriptor(params.anchor);
  await playAction([
    {
      _obj: "canvasSize",
      width: pixelUnit(width),
      height: pixelUnit(height),
      horizontal: anchor.horizontal,
      vertical: anchor.vertical,
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  return await readDocumentState(false);
}

async function placeEmbeddedAsset(params) {
  const uri = backendAssetUri(params);
  const download = await downloadBinaryFromBackend(uri);
  const fileName = safeLayerName(params.name || `design-asset-${Date.now()}`, "design-asset").replace(/\.[^.]+$/, "") + ".png";
  const tempFile = await writeTempBinaryFile(fileName, download.bytes);
  await placeFileAsLayer(tempFile, { assetLabel: "embedded design asset" });
  const name = safeLayerName(params.name, "Placed Asset");
  await setActiveLayerProperties({ name, opacity: 100, blendMode: "normal" });
  const layerId = await getActiveLayerId();
  if (
    params.x != null || params.y != null || params.width != null || params.height != null ||
    params.scale_x != null || params.scale_y != null || params.rotation != null
  ) {
    await transformLayerById(layerId, params);
  }
  return {
    layer_id: layerId,
    layer_name: name,
    asset_uri: download.uri
  };
}

function svgNumber(value, fallback, min, max) {
  return numberParam(value, fallback, min, max);
}

function escapeXml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function cssColor(value, fallback) {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (Array.isArray(value)) {
    return `rgb(${Math.round(Number(value[0]) || 0)}, ${Math.round(Number(value[1]) || 0)}, ${Math.round(Number(value[2]) || 0)})`;
  }
  if (value && Array.isArray(value.rgb)) {
    return cssColor(value.rgb, fallback);
  }
  if (value && value._obj === "RGBColor") {
    return `rgb(${Math.round(Number(value.red) || 0)}, ${Math.round(Number(value.grain || value.green) || 0)}, ${Math.round(Number(value.blue) || 0)})`;
  }
  if (value && (value.r != null || value.red != null)) {
    return `rgb(${Math.round(Number(value.r != null ? value.r : value.red) || 0)}, ${Math.round(Number(value.g != null ? value.g : value.green) || 0)}, ${Math.round(Number(value.b != null ? value.b : value.blue) || 0)})`;
  }
  return fallback;
}

function svgViewBoxString(value, width, height) {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (Array.isArray(value) && value.length >= 4) {
    return value.slice(0, 4).map((item) => Number(item) || 0).join(" ");
  }
  if (value && typeof value === "object") {
    const x = Number(value.x != null ? value.x : value.min_x) || 0;
    const y = Number(value.y != null ? value.y : value.min_y) || 0;
    const w = Number(value.width != null ? value.width : value.w) || width;
    const h = Number(value.height != null ? value.height : value.h) || height;
    return `${x} ${y} ${w} ${h}`;
  }
  return `0 0 ${width} ${height}`;
}

function buildSvgMarkup(params) {
  const directSvg = typeof params.svg === "string" ? params.svg.trim() : "";
  if (directSvg) {
    if (/^<svg[\s>]/i.test(directSvg)) {
      return directSvg;
    }
    throw codedError("svg_asset_invalid", "shape.svg_asset_place params.svg must be a complete <svg> document.");
  }
  const pathData = String(params.path_data || params.svg_path || params.d || "").trim();
  if (!pathData) {
    throw codedError("svg_asset_missing", "shape.svg_asset_place requires params.svg or params.path_data/svg_path/d.");
  }
  const width = svgNumber(params.svg_width || params.view_width || params.width, 512, 1, 30000);
  const height = svgNumber(params.svg_height || params.view_height || params.height, 512, 1, 30000);
  const fill = cssColor(params.fill != null ? params.fill : params.color, "#ffffff");
  const stroke = params.stroke == null ? "none" : cssColor(params.stroke, "none");
  const strokeWidth = params.stroke_width == null ? 0 : svgNumber(params.stroke_width, 0, 0, 3000);
  const viewBox = svgViewBoxString(params.viewBox || params.view_box, width, height);
  const pathOpacity = params.path_opacity == null ? 1 : Math.max(0, Math.min(1, Number(params.path_opacity) || 0));
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="${escapeXml(viewBox)}"><path d="${escapeXml(pathData)}" fill="${escapeXml(fill)}" stroke="${escapeXml(stroke)}" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" opacity="${pathOpacity}"/></svg>`;
}

async function placeSvgAssetLayer(params) {
  const svg = buildSvgMarkup(params);
  const name = safeLayerName(params.name, "SVG Asset");
  const assetHash = String(params.asset_hash || "").replace(/[^A-Za-z0-9]/g, "").slice(0, 12);
  const fileStem = safeLayerName(name, "svg-asset").replace(/\.[^.]+$/, "");
  const fileName = `${fileStem}${assetHash ? `-${assetHash}` : ""}.svg`;
  const tempFile = await writeTempTextFile(fileName, svg);
  await placeFileAsLayer(tempFile, { failureCode: "svg_asset_place_failed", assetLabel: "SVG asset" });
  await setActiveLayerProperties({
    name,
    opacity: params.opacity == null ? 100 : numberParam(params.opacity, 100, 0, 100),
    blendMode: params.blend_mode || params.blendMode || "normal"
  });
  const layerId = await getActiveLayerId();
  if (
    params.x != null || params.y != null || params.width != null || params.height != null ||
    params.scale != null || params.scale_x != null || params.scale_y != null || params.rotation != null
  ) {
    const transformParams = Object.assign({}, params);
    if (params.scale != null && params.scale_x == null && params.scale_y == null) {
      transformParams.scale_x = numberParam(params.scale, 100, 0.1, 10000);
      transformParams.scale_y = transformParams.scale_x;
    }
    await transformLayerById(layerId, transformParams);
  }
  const descriptor = await getActiveLayerDescriptor();
  return {
    layer_id: layerId,
    layer_name: name,
    asset_kind: "svg",
    implementation: "svg_place_embedded",
    bounds: layerBoundsFromDescriptor(descriptor),
    svg_length: svg.length,
    object_id: params.object_id || null,
    part_id: params.part_id || null,
    style_role: params.style_role || null,
    asset_hash: params.asset_hash || null
  };
}


function fileExtensionFromAssetUri(uri, fallback) {
  const match = String(uri || "").split("?")[0].match(/\.([A-Za-z0-9]{2,5})$/);
  return match ? `.${match[1].toLowerCase()}` : fallback;
}

async function replaceSmartObjectContents(params) {
  const layerId = params.layer_id || params.target_layer_id || null;
  if (layerId != null) {
    await selectLayer(layerId);
  }
  const uri = backendAssetUri(params);
  const download = await downloadBinaryFromBackend(uri);
  const extension = fileExtensionFromAssetUri(download.uri, ".png");
  const fileName = safeLayerName(params.name || `replacement-${Date.now()}`, "replacement").replace(/\.[^.]+$/, "") + extension;
  const tempFile = await writeTempBinaryFile(fileName, download.bytes);
  if (typeof fs.createSessionToken !== "function") {
    throw codedError("asset_replace_unavailable", "UXP localFileSystem.createSessionToken is unavailable.");
  }
  const token = fs.createSessionToken(tempFile);
  await playAction([
    {
      _obj: "placedLayerReplaceContents",
      null: {
        _path: token,
        _kind: "local"
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  return {
    layer_id: await getActiveLayerId(),
    asset_uri: download.uri
  };
}

async function getLayerBoundsById(layerId) {
  const descriptor = await getLayerDescriptorById(layerId);
  const bounds = layerBoundsFromDescriptor(descriptor);
  if (!bounds) {
    throw codedError("layer_bounds_unavailable", "The target layer does not expose usable pixel bounds.", { layer_id: layerId });
  }
  return Object.assign({ layer_id: descriptor.layerID == null ? descriptor.id || layerId : descriptor.layerID }, bounds);
}

function normalizedInsetBounds(bounds, params) {
  const padding = numberParam(params.padding, numberParam(params.inset, 0, 0, 30000), 0, 30000);
  const paddingX = numberParam(params.padding_x, numberParam(params.inset_x, padding, 0, 30000), 0, 30000);
  const paddingY = numberParam(params.padding_y, numberParam(params.inset_y, padding, 0, 30000), 0, 30000);
  const leftInset = numberParam(params.padding_left, numberParam(params.inset_left, paddingX, 0, 30000), 0, 30000);
  const rightInset = numberParam(params.padding_right, numberParam(params.inset_right, paddingX, 0, 30000), 0, 30000);
  const topInset = numberParam(params.padding_top, numberParam(params.inset_top, paddingY, 0, 30000), 0, 30000);
  const bottomInset = numberParam(params.padding_bottom, numberParam(params.inset_bottom, paddingY, 0, 30000), 0, 30000);
  let left = bounds.left + leftInset;
  let right = bounds.right - rightInset;
  let top = bounds.top + topInset;
  let bottom = bounds.bottom - bottomInset;
  if (right <= left) {
    const midX = bounds.centerX;
    left = midX - 0.5;
    right = midX + 0.5;
  }
  if (bottom <= top) {
    const midY = bounds.centerY;
    top = midY - 0.5;
    bottom = midY + 0.5;
  }
  return {
    left,
    top,
    right,
    bottom,
    width: Math.max(1, right - left),
    height: Math.max(1, bottom - top),
    centerX: (left + right) / 2,
    centerY: (top + bottom) / 2
  };
}

function normalizedTextFitMode(value) {
  const raw = String(value || "position").toLowerCase().replace(/-/g, "_");
  if (["fit", "scale_to_fit"].includes(raw)) {
    return "fit";
  }
  if (["shrink", "shrink_to_fit", "scale_down_to_fit"].includes(raw)) {
    return "shrink_to_fit";
  }
  return "position";
}

function normalizedAxisAlignment(value, axis) {
  const fallback = "center";
  const raw = String(value || fallback).toLowerCase();
  const allowed = axis === "x" ? ["left", "center", "right"] : ["top", "center", "bottom"];
  return allowed.includes(raw) ? raw : fallback;
}

function shouldUseNativeParagraphText(params, target) {
  const kind = String(
    (params && (params.text_kind || params.kind || params.text_type || params.mode)) || ""
  ).toLowerCase().replace(/-/g, "_");
  if (["paragraph", "paragraph_text", "text_box", "native_paragraph"].includes(kind)) {
    return true;
  }
  if (params && (params.paragraph === true || params.native_paragraph === true || params.use_text_box === true)) {
    return true;
  }
  return !!(target && target.target_bounds);
}

function paragraphJustificationValue(alignX) {
  const justification = constants.Justification || {};
  if (alignX === "right") {
    return justification.RIGHT || "right";
  }
  if (alignX === "center") {
    return justification.CENTER || "center";
  }
  return justification.LEFT || "left";
}

function descriptorRectangleInPoints(bounds) {
  const resolution = activeDocumentResolution();
  const toPoints = (value) => Number(value) * 72 / resolution;
  return {
    _obj: "rectangle",
    top: { _unit: "pointsUnit", _value: toPoints(bounds.top) },
    left: { _unit: "pointsUnit", _value: toPoints(bounds.left) },
    bottom: { _unit: "pointsUnit", _value: toPoints(bounds.bottom) },
    right: { _unit: "pointsUnit", _value: toPoints(bounds.right) }
  };
}

function textShapeDebugSummary(shape) {
  if (!shape || typeof shape !== "object") {
    return null;
  }
  return {
    keys: Object.keys(shape),
    char: shape.char && shape.char._value ? shape.char._value : null,
    orientation: shape.orientation && shape.orientation._value ? shape.orientation._value : null,
    bounds_keys: shape.bounds && typeof shape.bounds === "object" ? Object.keys(shape.bounds) : null,
    has_path: shape.path != null,
    has_bounds: shape.bounds != null,
    has_boxBounds: shape.boxBounds != null
  };
}

function buildNativeParagraphTextShape(targetBounds) {
  if (!targetBounds) {
    return null;
  }
  return {
    _obj: "textShape",
    char: {
      _enum: "char",
      _value: "box"
    },
    orientation: {
      _enum: "orientation",
      _value: "horizontal"
    },
    transform: {
      _obj: "transform",
      xx: 1,
      xy: 0,
      yx: 0,
      yy: 1,
      tx: 0,
      ty: 0
    },
    rowCount: 1,
    columnCount: 1,
    rowMajorOrder: true,
    rowGutter: {
      _unit: "pointsUnit",
      _value: 0
    },
    columnGutter: {
      _unit: "pointsUnit",
      _value: 0
    },
    spacing: {
      _unit: "pointsUnit",
      _value: 0
    },
    frameBaselineAlignment: {
      _enum: "frameBaselineAlignment",
      _value: "alignByAscent"
    },
    firstBaselineMinimum: {
      _unit: "pointsUnit",
      _value: 0
    },
    base: {
      _obj: "paint",
      horizontal: 0,
      vertical: 0
    },
    bounds: descriptorRectangleInPoints(targetBounds)
  };
}

async function configureNativeParagraphTextLayer(layerId, text, fontSize, targetBounds, params) {
  await selectLayer(layerId);
  const activeLayer = app.activeDocument && app.activeDocument.activeLayers && app.activeDocument.activeLayers.length
    ? app.activeDocument.activeLayers[0]
    : null;
  if (!activeLayer || !activeLayer.textItem) {
    throw codedError("text_create_failed", "The active layer is not an editable Photoshop text layer.", {
      layer_id: layerId
    });
  }
  const textItem = activeLayer.textItem;
  if (textItem.isPointText && typeof textItem.convertToParagraphText === "function" && !targetBounds) {
    await textItem.convertToParagraphText();
  }
  textItem.contents = String(text || "");
  if (targetBounds) {
    try {
      textItem.textClickPoint = { x: targetBounds.left, y: targetBounds.top };
    } catch (error) {
    }
  }
  const lineHeightPx = params && params.line_height_px != null
    ? numberParam(params.line_height_px, fontSize * 1.32, 0, 10000)
    : fontSize * numberParam(params && params.line_height_multiplier, 1.32, 0.5, 4);
  if (lineHeightPx > 0) {
    try {
      textItem.characterStyle.leading = lineHeightPx;
    } catch (error) {
    }
  }
  const alignX = normalizedAxisAlignment(params && params.align_x, "x");
  try {
    textItem.paragraphStyle.justification = paragraphJustificationValue(alignX);
  } catch (error) {
  }
  const firstLineIndent = numberParam(params && params.first_line_indent, 0, -30000, 30000);
  const leftIndent = numberParam(params && params.paragraph_left_indent, numberParam(params && params.left_indent, 0, -30000, 30000), -30000, 30000);
  const rightIndent = numberParam(params && params.paragraph_right_indent, numberParam(params && params.right_indent, 0, -30000, 30000), -30000, 30000);
  const spaceBefore = numberParam(params && params.paragraph_space_before, numberParam(params && params.space_before, 0, -30000, 30000), -30000, 30000);
  const spaceAfter = numberParam(params && params.paragraph_space_after, numberParam(params && params.space_after, 0, -30000, 30000), -30000, 30000);
  try {
    textItem.paragraphStyle.firstLineIndent = firstLineIndent;
  } catch (error) {
  }
  try {
    textItem.paragraphStyle.leftIndent = leftIndent;
  } catch (error) {
  }
  try {
    textItem.paragraphStyle.rightIndent = rightIndent;
  } catch (error) {
  }
  try {
    textItem.paragraphStyle.spaceBefore = spaceBefore;
  } catch (error) {
  }
  try {
    textItem.paragraphStyle.spaceAfter = spaceAfter;
  } catch (error) {
  }
  const descriptor = await getActiveLayerDescriptor();
  const shape = descriptor && descriptor.textKey && Array.isArray(descriptor.textKey.textShape) && descriptor.textKey.textShape.length
    ? descriptor.textKey.textShape[0]
    : null;
  return {
    is_paragraph_text: !!textItem.isParagraphText,
    text_shape: textShapeDebugSummary(shape),
    paragraph_box_method: targetBounds ? "make_descriptor" : "convert_to_paragraph_text"
  };
}

function explicitTextTargetBounds(params) {
  if (!params || typeof params !== "object") {
    return null;
  }
  const raw = params.box_bounds && typeof params.box_bounds === "object"
    ? params.box_bounds
    : null;
  let left = null;
  let top = null;
  let right = null;
  let bottom = null;
  if (raw && raw.left != null && raw.top != null && raw.right != null && raw.bottom != null) {
    left = numberParam(raw.left, 0, -100000, 100000);
    top = numberParam(raw.top, 0, -100000, 100000);
    right = numberParam(raw.right, 0, -100000, 100000);
    bottom = numberParam(raw.bottom, 0, -100000, 100000);
  } else if (raw && raw.x != null && raw.y != null && raw.width != null && raw.height != null) {
    left = numberParam(raw.x, 0, -100000, 100000);
    top = numberParam(raw.y, 0, -100000, 100000);
    right = left + numberParam(raw.width, 1, 1, 100000);
    bottom = top + numberParam(raw.height, 1, 1, 100000);
  } else if (params.box_left != null && params.box_top != null && params.box_right != null && params.box_bottom != null) {
    left = numberParam(params.box_left, 0, -100000, 100000);
    top = numberParam(params.box_top, 0, -100000, 100000);
    right = numberParam(params.box_right, 0, -100000, 100000);
    bottom = numberParam(params.box_bottom, 0, -100000, 100000);
  } else if (params.box_x != null && params.box_y != null && params.box_width != null && params.box_height != null) {
    left = numberParam(params.box_x, 0, -100000, 100000);
    top = numberParam(params.box_y, 0, -100000, 100000);
    right = left + numberParam(params.box_width, 1, 1, 100000);
    bottom = top + numberParam(params.box_height, 1, 1, 100000);
  } else {
    return null;
  }
  return normalizedInsetBounds({
    left,
    top,
    right,
    bottom,
    width: Math.max(1, right - left),
    height: Math.max(1, bottom - top),
    centerX: (left + right) / 2,
    centerY: (top + bottom) / 2
  }, params);
}

async function targetTextBoundsFromParams(params) {
  const explicitBounds = explicitTextTargetBounds(params);
  if (explicitBounds) {
    return {
      box_layer_id: null,
      target_bounds: explicitBounds,
      target_source: "explicit_bounds"
    };
  }
  const boxLayerId = normalizeLayerId(params && (params.box_layer_id || params.container_layer_id || params.reference_layer_id));
  if (boxLayerId == null) {
    return null;
  }
  const boxBounds = await getLayerBoundsById(boxLayerId);
  return {
    box_layer_id: boxLayerId,
    target_bounds: normalizedInsetBounds(boxBounds, params || {}),
    target_source: "layer_bounds"
  };
}

function estimateTextUnitForChar(ch) {
  if (!ch) {
    return 0;
  }
  if (ch === " " || ch === "\t") {
    return 0.33;
  }
  if (/\d/.test(ch)) {
    return 0.62;
  }
  if (/[A-Z]/.test(ch)) {
    return 0.68;
  }
  if (/[a-z]/.test(ch)) {
    return 0.56;
  }
  if (/[.,;:!'`]/.test(ch)) {
    return 0.28;
  }
  if (/[?]/.test(ch)) {
    return 0.42;
  }
  if (/[-_/\\|]/.test(ch)) {
    return 0.36;
  }
  if (/[(){}\[\]<>]/.test(ch)) {
    return 0.34;
  }
  if (/[&%@#$*+=]/.test(ch)) {
    return 0.78;
  }
  const code = ch.charCodeAt(0);
  if (
    (code >= 0x2e80 && code <= 0x9fff)
    || (code >= 0xf900 && code <= 0xfaff)
    || (code >= 0xff00 && code <= 0xffef)
  ) {
    return 1;
  }
  return code > 255 ? 0.95 : 0.68;
}

function estimateTextUnits(text) {
  return Array.from(String(text || "")).reduce((sum, ch) => sum + estimateTextUnitForChar(ch), 0);
}

function normalizedWrapMode(value) {
  const raw = String(value || "mixed").toLowerCase().replace(/-/g, "_");
  if (raw === "char" || raw === "character") {
    return "char";
  }
  return "mixed";
}

function isLatinWordCharacter(ch) {
  return /[A-Za-z0-9&+/#._:-]/.test(ch);
}

function tokenizeTextForWrap(text, mode) {
  const source = String(text || "").replace(/\r\n?/g, "\n");
  const tokens = [];
  let index = 0;
  while (index < source.length) {
    const ch = source[index];
    if (ch === "\n") {
      tokens.push({ type: "newline", value: "\n" });
      index += 1;
      continue;
    }
    if (/\s/.test(ch)) {
      let end = index + 1;
      while (end < source.length && source[end] !== "\n" && /\s/.test(source[end])) {
        end += 1;
      }
      tokens.push({ type: "space", value: " " });
      index = end;
      continue;
    }
    if (mode !== "char" && /[A-Za-z0-9]/.test(ch)) {
      let end = index + 1;
      while (end < source.length && isLatinWordCharacter(source[end])) {
        end += 1;
      }
      tokens.push({ type: "text", value: source.slice(index, end) });
      index = end;
      continue;
    }
    tokens.push({ type: "text", value: ch });
    index += 1;
  }
  return tokens;
}

function splitTokenToFitUnits(token, maxUnits) {
  const chars = Array.from(String(token || ""));
  if (!chars.length) {
    return ["", ""];
  }
  let units = 0;
  let splitIndex = 0;
  for (let index = 0; index < chars.length; index += 1) {
    const nextUnits = units + estimateTextUnitForChar(chars[index]);
    if (index > 0 && nextUnits > maxUnits) {
      break;
    }
    units = nextUnits;
    splitIndex = index + 1;
  }
  if (splitIndex <= 0) {
    splitIndex = 1;
  }
  return [chars.slice(0, splitIndex).join(""), chars.slice(splitIndex).join("")];
}

function wrapTextToEstimatedWidth(text, maxUnits, mode) {
  const safeMaxUnits = Math.max(1.5, Number(maxUnits) || 1.5);
  const tokens = tokenizeTextForWrap(text, mode);
  const lines = [];
  let current = "";
  let currentUnits = 0;
  const pushCurrent = () => {
    lines.push(current.trimEnd());
    current = "";
    currentUnits = 0;
  };
  for (const token of tokens) {
    if (token.type === "newline") {
      pushCurrent();
      continue;
    }
    if (token.type === "space") {
      if (!current) {
        continue;
      }
      if (currentUnits + 0.33 <= safeMaxUnits) {
        current += " ";
        currentUnits += 0.33;
      } else {
        pushCurrent();
      }
      continue;
    }
    let tokenValue = token.value;
    let tokenUnits = estimateTextUnits(tokenValue);
    if (current && currentUnits + tokenUnits <= safeMaxUnits) {
      current += tokenValue;
      currentUnits += tokenUnits;
      continue;
    }
    if (current) {
      pushCurrent();
    }
    while (tokenValue) {
      tokenUnits = estimateTextUnits(tokenValue);
      if (tokenUnits <= safeMaxUnits || Array.from(tokenValue).length <= 1) {
        current = tokenValue;
        currentUnits = tokenUnits;
        tokenValue = "";
      } else {
        const split = splitTokenToFitUnits(tokenValue, safeMaxUnits);
        lines.push(split[0]);
        tokenValue = split[1];
      }
    }
  }
  if (current || !lines.length) {
    lines.push(current.trimEnd());
  }
  return lines.join("\n");
}

function estimatedTextBlockMetrics(text, fontSize, params) {
  const normalized = String(text || "").replace(/\r\n?/g, "\n");
  const lines = normalized.split("\n");
  const wrapWidthFactor = numberParam(params && params.wrap_width_factor, 0.92, 0.4, 2);
  const lineHeightPx = params && params.line_height_px != null
    ? numberParam(params.line_height_px, fontSize * 1.32, fontSize * 0.8, fontSize * 4)
    : fontSize * numberParam(params && params.line_height_multiplier, 1.32, 0.8, 4);
  const longestUnits = lines.reduce((maxUnits, line) => Math.max(maxUnits, estimateTextUnits(line)), 0);
  return {
    line_count: lines.length,
    longest_units: longestUnits,
    line_height_px: lineHeightPx,
    estimated_width: longestUnits * fontSize * wrapWidthFactor,
    estimated_height: Math.max(1, lines.length) * lineHeightPx
  };
}

function prepareTextForBounds(text, requestedFontSize, targetBounds, params) {
  const wrapEnabled = !!targetBounds && params && params.wrap_text !== false;
  const autoFit = !!targetBounds && params && params.auto_fit !== false;
  const wrapMode = normalizedWrapMode(params && params.wrap_mode);
  const minFontSize = numberParam(params && params.min_font_size, Math.min(requestedFontSize, 10), 1, requestedFontSize);
  const shrinkOnly = normalizedTextFitMode(params && params.fit_mode) !== "fit";
  const rawText = String(text || "").replace(/\r\n?/g, "\n");
  let fontSize = requestedFontSize;
  let wrappedText = rawText;
  const passes = targetBounds ? 4 : 1;
  for (let pass = 0; pass < passes; pass += 1) {
    if (wrapEnabled) {
      const maxUnits = targetBounds.width / Math.max(1, fontSize * numberParam(params && params.wrap_width_factor, 0.92, 0.4, 2));
      wrappedText = wrapTextToEstimatedWidth(rawText, maxUnits, wrapMode);
    } else {
      wrappedText = rawText;
    }
    if (!autoFit) {
      break;
    }
    const metrics = estimatedTextBlockMetrics(wrappedText, fontSize, params);
    const widthFactor = targetBounds.width / Math.max(1, metrics.estimated_width);
    const heightFactor = targetBounds.height / Math.max(1, metrics.estimated_height);
    let scaleFactor = Math.min(widthFactor, heightFactor);
    if (shrinkOnly) {
      scaleFactor = Math.min(1, scaleFactor);
    }
    if (!(scaleFactor > 0) || scaleFactor >= 0.995) {
      break;
    }
    const nextFontSize = Math.max(minFontSize, Math.floor(fontSize * scaleFactor * 100) / 100);
    if (Math.abs(nextFontSize - fontSize) < 0.1) {
      break;
    }
    fontSize = nextFontSize;
  }
  return {
    text: wrappedText,
    font_size: fontSize,
    metrics: estimatedTextBlockMetrics(wrappedText, fontSize, params)
  };
}

function alignmentErrorForBounds(textBounds, targetBounds, alignX, alignY) {
  let x = 0;
  let y = 0;
  if (alignX === "left") {
    x = targetBounds.left - textBounds.left;
  } else if (alignX === "right") {
    x = targetBounds.right - textBounds.right;
  } else {
    x = targetBounds.centerX - textBounds.centerX;
  }
  if (alignY === "top") {
    y = targetBounds.top - textBounds.top;
  } else if (alignY === "bottom") {
    y = targetBounds.bottom - textBounds.bottom;
  } else {
    y = targetBounds.centerY - textBounds.centerY;
  }
  return { x, y };
}

async function fitTextLayerToBounds(textLayerId, targetBounds, params, meta) {
  if (textLayerId == null) {
    throw codedError("text_fit_to_box_failed", "text.fit_to_box requires a target text layer.");
  }
  if (!targetBounds) {
    throw codedError("text_fit_to_box_failed", "A usable target bounds box is required for text fitting.");
  }
  const textDescriptor = await getLayerDescriptorById(textLayerId);
  if (textDescriptor.textKey === undefined) {
    throw codedError("text_fit_to_box_failed", "text.fit_to_box only supports editable Photoshop text layers.", { layer_id: textLayerId });
  }
  const initialBounds = layerBoundsFromDescriptor(textDescriptor);
  if (!initialBounds) {
    throw codedError("text_fit_to_box_failed", "The target text layer does not expose usable bounds.", { layer_id: textLayerId });
  }
  const alignX = normalizedAxisAlignment(params && params.align_x, "x");
  const alignY = normalizedAxisAlignment(params && params.align_y, "y");
  const fitMode = normalizedTextFitMode(params && params.fit_mode);
  const maxIterations = Math.round(numberParam(params && params.max_iterations, 3, 1, 8));
  const tolerance = numberParam(
    params && (params.tolerance != null ? params.tolerance : params.tolerance_px),
    2,
    0.5,
    100
  );
  const damping = numberParam(params && params.damping, 0.8, 0.05, 1);
  const applied = [];
  let currentBounds = initialBounds;
  for (let iteration = 1; iteration <= maxIterations; iteration += 1) {
    let scaleFactor = 1;
    if (fitMode !== "position") {
      const widthFactor = targetBounds.width / Math.max(1, currentBounds.width);
      const heightFactor = targetBounds.height / Math.max(1, currentBounds.height);
      scaleFactor = Math.min(widthFactor, heightFactor);
      if (fitMode === "shrink_to_fit") {
        scaleFactor = Math.min(1, scaleFactor);
      }
      if (scaleFactor > 0 && Math.abs(scaleFactor - 1) > 0.01) {
        await transformLayerById(textLayerId, { scale_x: scaleFactor * 100, scale_y: scaleFactor * 100 });
        currentBounds = await getLayerBoundsById(textLayerId);
      }
    }
    const beforeError = alignmentErrorForBounds(currentBounds, targetBounds, alignX, alignY);
    const moveX = Math.abs(beforeError.x) <= tolerance ? 0 : beforeError.x * (iteration === maxIterations ? 1 : damping);
    const moveY = Math.abs(beforeError.y) <= tolerance ? 0 : beforeError.y * (iteration === maxIterations ? 1 : damping);
    if (Math.abs(moveX) > 0.01 || Math.abs(moveY) > 0.01) {
      await transformLayerById(textLayerId, { offset_x: moveX, offset_y: moveY });
      currentBounds = await getLayerBoundsById(textLayerId);
    }
    const afterError = alignmentErrorForBounds(currentBounds, targetBounds, alignX, alignY);
    applied.push({
      iteration,
      scale_factor: scaleFactor,
      offset_x: moveX,
      offset_y: moveY,
      error_x: afterError.x,
      error_y: afterError.y
    });
    if (Math.abs(afterError.x) <= tolerance && Math.abs(afterError.y) <= tolerance) {
      break;
    }
  }
  const finalError = alignmentErrorForBounds(currentBounds, targetBounds, alignX, alignY);
  return {
    layer_id: textLayerId,
    box_layer_id: meta && meta.box_layer_id != null ? meta.box_layer_id : null,
    target_source: meta && meta.target_source ? meta.target_source : "explicit_bounds",
    align_x: alignX,
    align_y: alignY,
    fit_mode: fitMode,
    tolerance,
    damping,
    max_iterations: maxIterations,
    converged: Math.abs(finalError.x) <= tolerance && Math.abs(finalError.y) <= tolerance,
    initial_bounds: initialBounds,
    target_bounds: targetBounds,
    final_bounds: currentBounds,
    final_error: finalError,
    iterations: applied
  };
}

async function fitTextLayerToBox(textLayerId, boxLayerId, params) {
  if (boxLayerId == null) {
    throw codedError("text_fit_to_box_failed", "text.fit_to_box requires box_layer_id.");
  }
  const target = await targetTextBoundsFromParams(Object.assign({}, params, { box_layer_id: boxLayerId }));
  return fitTextLayerToBounds(textLayerId, target && target.target_bounds, params, target);
}

async function transformLayerById(layerId, params) {
  if (layerId != null) {
    await selectLayer(layerId);
  }
  const descriptor = await getActiveLayerDescriptor();
  const bounds = layerBoundsFromDescriptor(descriptor);
  const textAnchor = textAnchorFromDescriptor(descriptor);
  let scaleX = params.scale_x == null ? null : numberParam(params.scale_x, 100, 0.1, 10000);
  let scaleY = params.scale_y == null ? null : numberParam(params.scale_y, 100, 0.1, 10000);
  if (bounds && params.width != null) {
    scaleX = numberParam(params.width, bounds.width, 1, 30000) / bounds.width * 100;
  }
  if (bounds && params.height != null) {
    scaleY = numberParam(params.height, bounds.height, 1, 30000) / bounds.height * 100;
  }
  if (scaleX == null) {
    scaleX = 100;
  }
  if (scaleY == null) {
    scaleY = scaleX;
  }
  let dx = numberParam(params.offset_x, 0, -100000, 100000);
  let dy = numberParam(params.offset_y, 0, -100000, 100000);
  if (bounds && textAnchor && (params.x != null || params.y != null) && params.rotation == null) {
    const desiredAnchorX = (params.x == null ? textAnchor.x : numberParam(params.x, textAnchor.x, -100000, 100000))
      + numberParam(params.offset_x, 0, -100000, 100000);
    const desiredAnchorY = (params.y == null ? textAnchor.y : numberParam(params.y, textAnchor.y, -100000, 100000))
      + numberParam(params.offset_y, 0, -100000, 100000);
    const relativeAnchorX = textAnchor.x - bounds.centerX;
    const relativeAnchorY = textAnchor.y - bounds.centerY;
    dx = desiredAnchorX - (bounds.centerX + relativeAnchorX * scaleX / 100);
    dy = desiredAnchorY - (bounds.centerY + relativeAnchorY * scaleY / 100);
  } else if (bounds && (params.x != null || params.y != null)) {
    const targetWidth = params.width == null ? bounds.width * scaleX / 100 : numberParam(params.width, bounds.width, 1, 30000);
    const targetHeight = params.height == null ? bounds.height * scaleY / 100 : numberParam(params.height, bounds.height, 1, 30000);
    const desiredLeft = params.x == null ? bounds.left : numberParam(params.x, bounds.left, -100000, 100000);
    const desiredTop = params.y == null ? bounds.top : numberParam(params.y, bounds.top, -100000, 100000);
    dx = desiredLeft + targetWidth / 2 - bounds.centerX;
    dy = desiredTop + targetHeight / 2 - bounds.centerY;
  }
  const command = {
    _obj: "transform",
    _target: [activeLayerRef()],
    freeTransformCenterState: {
      _enum: "quadCenterState",
      _value: "QCSAverage"
    },
    width: percentUnit(scaleX),
    height: percentUnit(scaleY),
    offset: {
      _obj: "offset",
      horizontal: pixelUnit(dx),
      vertical: pixelUnit(dy)
    },
    _options: { dialogOptions: "dontDisplay" }
  };
  if (params.rotation != null) {
    command.angle = angleUnit(numberParam(params.rotation, 0, -360, 360));
  }
  await playAction([command]);
  return {
    layer_id: await getActiveLayerId(),
    transform: { scale_x: scaleX, scale_y: scaleY, offset_x: dx, offset_y: dy, rotation: params.rotation || 0 }
  };
}

async function moveActiveLayerToBack() {
  await playAction([
    {
      _obj: "move",
      _target: [activeLayerRef()],
      to: { _ref: "layer", _enum: "ordinal", _value: "back" },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

function normalizeLayerId(layerId) {
  if (layerId == null || layerId === "") {
    return null;
  }
  const numeric = Number(layerId);
  return Number.isFinite(numeric) ? numeric : null;
}

function topLevelLayerObjectsExcluding(layerId) {
  const targetId = normalizeLayerId(layerId);
  const doc = app.activeDocument;
  const layers = Array.isArray(doc && doc.layers) ? doc.layers : Array.from(doc && doc.layers ? doc.layers : []);
  return layers.filter((layer) => layer && (targetId == null || Number(layer.id) !== targetId));
}

function layerMoveUnavailableError(actionName, layerId, error, details) {
  const message = error && error.message ? error.message : String(error || "Photoshop layer move command is unavailable.");
  return codedError(
    "layer_move_unavailable",
    `Photoshop could not ${actionName}; the layer may be in text-edit mode, locked, inside an unsupported group move, or already at that boundary.`,
    Object.assign({
      action: actionName,
      layer_id: layerId == null ? null : layerId,
      photoshop_message: message
    }, details || {})
  );
}

async function moveLayerToTop(layerId) {
  const targetId = normalizeLayerId(layerId);
  const doc = app.activeDocument;
  const source = targetId == null ? null : findLayerObjectById(doc && doc.layers, targetId);
  const candidates = topLevelLayerObjectsExcluding(targetId);
  let domError = null;
  if (source && typeof source.moveAbove === "function") {
    if (!candidates.length) {
      await selectLayer(targetId);
      return targetId;
    }
    try {
      await source.moveAbove(candidates[0]);
      await selectLayer(targetId);
      return targetId;
    } catch (error) {
      domError = error;
    }
  }
  try {
    if (layerId != null) {
      await selectLayer(layerId);
    }
    await playAction([
      {
        _obj: "move",
        _target: [activeLayerRef()],
        to: { _ref: "layer", _enum: "ordinal", _value: "front" },
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
    return getActiveLayerId();
  } catch (error) {
    throw layerMoveUnavailableError("move layer to front", layerId, error, {
      dom_message: domError && domError.message ? domError.message : (domError ? String(domError) : null)
    });
  }
}

async function moveLayerToBack(layerId) {
  const targetId = normalizeLayerId(layerId);
  const doc = app.activeDocument;
  const source = targetId == null ? null : findLayerObjectById(doc && doc.layers, targetId);
  const candidates = topLevelLayerObjectsExcluding(targetId);
  let domError = null;
  if (source && typeof source.moveBelow === "function") {
    if (!candidates.length) {
      await selectLayer(targetId);
      return targetId;
    }
    try {
      await source.moveBelow(candidates[candidates.length - 1]);
      await selectLayer(targetId);
      return targetId;
    } catch (error) {
      domError = error;
    }
  }
  try {
    if (layerId != null) {
      await selectLayer(layerId);
    }
    await moveActiveLayerToBack();
    return getActiveLayerId();
  } catch (error) {
    throw layerMoveUnavailableError("move layer to back", layerId, error, {
      dom_message: domError && domError.message ? domError.message : (domError ? String(domError) : null)
    });
  }
}

async function moveLayerRelative(layerId, referenceLayerId, position) {
  if (layerId == null || referenceLayerId == null) {
    throw codedError("layer_move_failed", "layer.move_above/move_below requires layer_id and reference_layer_id.");
  }
  const doc = app.activeDocument;
  const source = findLayerObjectById(doc && doc.layers, layerId);
  const reference = findLayerObjectById(doc && doc.layers, referenceLayerId);
  let domError = null;
  if (source && reference) {
    const method = position === "below" ? "moveBelow" : "moveAbove";
    if (typeof source[method] === "function") {
      try {
        await source[method](reference);
        await selectLayer(layerId);
        return Number(layerId);
      } catch (error) {
        domError = error;
      }
    }
  }
  try {
    await selectLayer(layerId);
    const descriptor = {
      _obj: "move",
      _target: [activeLayerRef()],
      to: { _ref: "layer", _id: Number(referenceLayerId) },
      adjustment: position === "below",
      version: 5,
      _options: { dialogOptions: "dontDisplay" }
    };
    await playAction([descriptor]);
    return getActiveLayerId();
  } catch (error) {
    throw layerMoveUnavailableError(`move layer ${position}`, layerId, error, {
      reference_layer_id: referenceLayerId,
      dom_message: domError && domError.message ? domError.message : (domError ? String(domError) : null)
    });
  }
}

async function reorderLayer(params, step, stepResults) {
  const layerId = resolveLayerTarget(step, params, stepResults);
  const position = String(params.position || params.to || params.order || "front").toLowerCase().replace(/[\s-]+/g, "_");
  if (["front", "top", "move_to_top", "bring_to_front"].includes(position)) {
    return {
      layer_id: await moveLayerToTop(layerId),
      position: "front"
    };
  }
  if (["back", "bottom", "move_to_back", "send_to_back"].includes(position)) {
    return {
      layer_id: await moveLayerToBack(layerId),
      position: "back"
    };
  }
  if (["above", "before", "move_above"].includes(position)) {
    const referenceLayerId = resolveRecipeValue(params.reference_layer_id || params.above_layer_id, stepResults);
    return {
      layer_id: await moveLayerRelative(layerId, referenceLayerId, "above"),
      reference_layer_id: referenceLayerId,
      position: "above"
    };
  }
  if (["below", "after", "move_below"].includes(position)) {
    const referenceLayerId = resolveRecipeValue(params.reference_layer_id || params.below_layer_id, stepResults);
    return {
      layer_id: await moveLayerRelative(layerId, referenceLayerId, "below"),
      reference_layer_id: referenceLayerId,
      position: "below"
    };
  }
  throw codedError(
    "layer_reorder_failed",
    "layer.reorder position must be front/top, back/bottom, above, or below.",
    { position }
  );
}

async function createClippingMask(layerId) {
  if (layerId != null) {
    await selectLayer(layerId);
  }
  const activeId = await getActiveLayerId();
  const descriptor = await getLayerDescriptorById(activeId);
  const section = descriptor.layerSection && descriptor.layerSection._value
    ? String(descriptor.layerSection._value)
    : "";
  if (section && section !== "layerSectionContent") {
    throw codedError(
      "clipping_mask_target_is_group",
      "A Photoshop layer group cannot be used as the clipping layer through this route. Target a child layer instead.",
      { layer_id: activeId, layer_section: section }
    );
  }

  const doc = app.activeDocument;
  const layer = findLayerObjectById(doc && doc.layers, activeId);
  if (layer && "isClippingMask" in layer) {
    try {
      layer.isClippingMask = true;
      return activeId;
    } catch (error) {
    }
  }

  await playAction([
    {
      _obj: "groupEvent",
      _options: { dialogOptions: "silent" }
    }
  ]);
  return getActiveLayerId();
}

async function releaseClippingMask(layerId) {
  if (layerId != null) {
    await selectLayer(layerId);
  }
  const doc = app.activeDocument;
  const activeId = await getActiveLayerId();
  const layer = findLayerObjectById(doc && doc.layers, activeId);
  if (layer && "isClippingMask" in layer) {
    try {
      layer.isClippingMask = false;
      return getActiveLayerId();
    } catch (error) {
    }
  }
  await playAction([
    {
      _obj: "ungroupEvent",
      _options: { dialogOptions: "silent" }
    }
  ]);
  return getActiveLayerId();
}

function layerIdsFromParams(params, fallback) {
  const raw = params.layer_ids || params.layers || (params.layer_id != null ? [params.layer_id] : fallback);
  const ids = Array.isArray(raw) ? raw : [raw];
  return ids.map((value) => Number(value)).filter((value) => Number.isFinite(value));
}

function normalizedAlignments(params) {
  const raw = params.alignments || params.align || params.mode || [];
  const values = Array.isArray(raw) ? raw : [raw];
  const map = {
    left: "left",
    horizontal_left: "left",
    center: "horizontal_center",
    horizontal_center: "horizontal_center",
    right: "right",
    horizontal_right: "right",
    top: "top",
    vertical_top: "top",
    middle: "vertical_center",
    vertical_center: "vertical_center",
    bottom: "bottom",
    vertical_bottom: "bottom"
  };
  return values.map((value) => map[String(value || "").toLowerCase()]).filter(Boolean);
}

async function alignLayers(params, state) {
  const ids = layerIdsFromParams(params, null);
  if (!ids.length) {
    throw codedError("layer_align_failed", "layer.align requires layer_id or layer_ids.");
  }
  const to = String(params.to || params.align_to || "canvas").toLowerCase();
  if (to !== "canvas") {
    throw codedError("layer_align_target_unsupported", "layer.align currently supports to=canvas only.", { to });
  }
  const alignments = normalizedAlignments(params);
  if (!alignments.length) {
    throw codedError("layer_align_failed", "layer.align requires align, mode, or alignments.");
  }
  const docWidth = numberParam(state && state.width, unitNumber(app.activeDocument && app.activeDocument.width, 1080), 1, 30000);
  const docHeight = numberParam(state && state.height, unitNumber(app.activeDocument && app.activeDocument.height, 1080), 1, 30000);
  const applied = [];
  for (const id of ids) {
    const bounds = await getLayerBoundsById(id);
    let dx = 0;
    let dy = 0;
    if (alignments.includes("left")) {
      dx = -bounds.left;
    } else if (alignments.includes("horizontal_center")) {
      dx = docWidth / 2 - bounds.centerX;
    } else if (alignments.includes("right")) {
      dx = docWidth - bounds.right;
    }
    if (alignments.includes("top")) {
      dy = -bounds.top;
    } else if (alignments.includes("vertical_center")) {
      dy = docHeight / 2 - bounds.centerY;
    } else if (alignments.includes("bottom")) {
      dy = docHeight - bounds.bottom;
    }
    if (Math.abs(dx) > 0.01 || Math.abs(dy) > 0.01) {
      await transformLayerById(id, { offset_x: dx, offset_y: dy });
    }
    applied.push({ layer_id: id, offset_x: dx, offset_y: dy });
  }
  return { layer_ids: ids, alignments, to, applied };
}

async function distributeLayers(params) {
  const ids = layerIdsFromParams(params, null);
  if (ids.length < 2) {
    throw codedError("layer_distribute_failed", "layer.distribute requires at least two layer_ids.");
  }
  const axis = String(params.axis || params.direction || "horizontal").toLowerCase();
  if (!["horizontal", "vertical", "x", "y"].includes(axis)) {
    throw codedError("layer_distribute_failed", "layer.distribute axis must be horizontal or vertical.", { axis });
  }
  const isHorizontal = axis === "horizontal" || axis === "x";
  const bounds = [];
  for (const id of ids) {
    bounds.push(await getLayerBoundsById(id));
  }
  bounds.sort((a, b) => (isHorizontal ? a.centerX - b.centerX : a.centerY - b.centerY));
  const spacing = params.spacing == null ? null : numberParam(params.spacing, 0, -30000, 30000);
  if (spacing == null && bounds.length < 3) {
    throw codedError("layer_distribute_failed", "layer.distribute without spacing requires at least three layers.");
  }
  const applied = [];
  if (spacing != null) {
    let cursor = isHorizontal ? bounds[0].right : bounds[0].bottom;
    applied.push({ layer_id: bounds[0].layer_id, offset_x: 0, offset_y: 0 });
    for (let index = 1; index < bounds.length; index += 1) {
      const item = bounds[index];
      const targetStart = cursor + spacing;
      const delta = targetStart - (isHorizontal ? item.left : item.top);
      await transformLayerById(item.layer_id, isHorizontal ? { offset_x: delta, offset_y: 0 } : { offset_x: 0, offset_y: delta });
      applied.push({ layer_id: item.layer_id, offset_x: isHorizontal ? delta : 0, offset_y: isHorizontal ? 0 : delta });
      cursor = targetStart + (isHorizontal ? item.width : item.height);
    }
  } else {
    const first = bounds[0];
    const last = bounds[bounds.length - 1];
    const step = ((isHorizontal ? last.centerX - first.centerX : last.centerY - first.centerY) / (bounds.length - 1));
    for (let index = 0; index < bounds.length; index += 1) {
      const item = bounds[index];
      const targetCenter = (isHorizontal ? first.centerX : first.centerY) + step * index;
      const delta = targetCenter - (isHorizontal ? item.centerX : item.centerY);
      if (index !== 0 && index !== bounds.length - 1 && Math.abs(delta) > 0.01) {
        await transformLayerById(item.layer_id, isHorizontal ? { offset_x: delta, offset_y: 0 } : { offset_x: 0, offset_y: delta });
      }
      applied.push({ layer_id: item.layer_id, offset_x: isHorizontal ? delta : 0, offset_y: isHorizontal ? 0 : delta });
    }
  }
  return { layer_ids: bounds.map((item) => item.layer_id), axis: isHorizontal ? "horizontal" : "vertical", spacing, applied };
}

async function createTextLayer(params) {
  const text = String(params.text || "");
  const name = safeLayerName(params.name, text.slice(0, 40) || "Text");
  const requestedFontSize = numberParam(params.font_size || params.size, 72, 1, 1200);
  const font = String(params.font || params.font_postscript_name || "MicrosoftYaHei");
  const docWidth = Math.max(1, unitNumber(app.activeDocument && app.activeDocument.width, 1));
  const docHeight = Math.max(1, unitNumber(app.activeDocument && app.activeDocument.height, 1));
  const target = await targetTextBoundsFromParams(params);
  const useNativeParagraph = shouldUseNativeParagraphText(params, target);
  const prepared = useNativeParagraph
    ? { text, font_size: requestedFontSize }
    : prepareTextForBounds(text, requestedFontSize, target && target.target_bounds, params);
  const effectiveText = prepared.text;
  const fontSize = prepared.font_size;
  const x = numberParam(params.x, target && target.target_bounds ? target.target_bounds.left : 80, -100000, 100000);
  const y = numberParam(params.y, target && target.target_bounds ? target.target_bounds.top : 120, -100000, 100000);
  const coordSpace = String(params.coord_space || params.coordinate_space || params.coordinate_mode || "pixels").toLowerCase();
  const clickX = coordSpace === "percent" ? x : x / docWidth * 100;
  const clickY = coordSpace === "percent" ? y : y / docHeight * 100;
  const textLayerDescriptor = {
    _obj: "textLayer",
    textKey: effectiveText,
    textClickPoint: {
      _obj: "paint",
      horizontal: percentUnit(clickX),
      vertical: percentUnit(clickY)
    },
    textStyleRange: [
      {
        _obj: "textStyleRange",
        from: 0,
        to: effectiveText.length,
        textStyle: {
          _obj: "textStyle",
          fontPostScriptName: font,
          size: pixelUnit(fontSize),
          color: rgbColor(params.color, [0, 0, 0])
        }
      }
    ]
  };
  if (useNativeParagraph && target && target.target_bounds) {
    textLayerDescriptor.textShape = [buildNativeParagraphTextShape(target.target_bounds)];
  }
  try {
    await playAction([
      {
        _obj: "make",
        _target: [{ _ref: "textLayer" }],
        using: textLayerDescriptor,
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
  } catch (error) {
    throw codedError(
      "text_create_failed",
      "Photoshop failed to create the requested text layer.",
      {
        use_native_paragraph: useNativeParagraph,
        target_source: target && target.target_source ? target.target_source : null,
        target_bounds: target && target.target_bounds ? target.target_bounds : null,
        text_shape: textLayerDescriptor.textShape ? textShapeDebugSummary(textLayerDescriptor.textShape[0]) : null,
        photoshop_message: error && error.message ? error.message : String(error)
      }
    );
  }
  await setActiveLayerProperties({ name, opacity: numberParam(params.opacity, 100, 0, 100), blendMode: blendModeValue(params.blend_mode || "normal") });
  const layerId = await getActiveLayerId();
  const result = {
    layer_id: layerId,
    layer_name: name,
    font_size: fontSize
  };
  if (effectiveText !== text) {
    result.wrapped_text = effectiveText;
  }
  if (useNativeParagraph) {
    result.paragraph = await configureNativeParagraphTextLayer(layerId, text, fontSize, target && target.target_bounds, params);
  } else if (target && target.target_bounds) {
    const fitParams = Object.assign(
      {
        align_x: "left",
        align_y: "top",
        fit_mode: "shrink_to_fit",
        max_iterations: 3,
        tolerance: 2,
        damping: 0.8
      },
      params || {}
    );
    result.fit = await fitTextLayerToBounds(layerId, target.target_bounds, fitParams, target);
  }
  return result;
}

async function createRectangleShapeLayer(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 100, 1, 30000);
  const height = numberParam(params.height, 100, 1, 30000);
  const name = safeLayerName(params.name, "Rectangle");
  await playAction([
    {
      _obj: "make",
      _target: [{ _ref: "contentLayer" }],
      using: {
        _obj: "contentLayer",
        type: {
          _obj: "solidColorLayer",
          color: rgbColor(params.fill || params.color, [255, 255, 255])
        },
        shape: {
          _obj: "rectangle",
          top: pixelUnit(y),
          left: pixelUnit(x),
          bottom: pixelUnit(y + height),
          right: pixelUnit(x + width)
        }
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await setActiveLayerProperties({ name, opacity: numberParam(params.opacity, 100, 0, 100), blendMode: blendModeValue(params.blend_mode || "normal") });
  return { layer_id: await getActiveLayerId(), layer_name: name };
}

async function createEllipseShapeLayer(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, params.diameter || 100, 1, 30000);
  const height = numberParam(params.height, params.diameter || 100, 1, 30000);
  const name = safeLayerName(params.name, "Ellipse");
  await playAction([
    {
      _obj: "make",
      _target: [{ _ref: "contentLayer" }],
      using: {
        _obj: "contentLayer",
        type: {
          _obj: "solidColorLayer",
          color: rgbColor(params.fill || params.color, [255, 255, 255])
        },
        shape: {
          _obj: "ellipse",
          top: pixelUnit(y),
          left: pixelUnit(x),
          bottom: pixelUnit(y + height),
          right: pixelUnit(x + width)
        }
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await setActiveLayerProperties({ name, opacity: numberParam(params.opacity, 100, 0, 100), blendMode: blendModeValue(params.blend_mode || "normal") });
  return { layer_id: await getActiveLayerId(), layer_name: name };
}

function rawNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function pointObject(x, y) {
  return { x, y };
}

function anglePoint(cx, cy, radius, angleRadians) {
  return {
    x: cx + Math.cos(angleRadians) * radius,
    y: cy + Math.sin(angleRadians) * radius
  };
}

function appendArcPoints(points, cx, cy, radius, startRadians, endRadians, segments, includeStart) {
  if (!(radius > 0)) {
    if (includeStart) {
      points.push(pointObject(cx, cy));
    }
    return;
  }
  const safeSegments = Math.max(1, Math.round(segments || 1));
  const startIndex = includeStart ? 0 : 1;
  for (let index = startIndex; index <= safeSegments; index += 1) {
    const t = index / safeSegments;
    const angle = startRadians + (endRadians - startRadians) * t;
    points.push(anglePoint(cx, cy, radius, angle));
  }
}

function roundedCornerSegments(radius, sweepDegrees) {
  const safeRadius = Math.max(0, Number(radius) || 0);
  const sweep = Math.max(1, Math.abs(Number(sweepDegrees) || 90));
  return Math.max(4, Math.min(18, Math.ceil((safeRadius / 16) * (sweep / 90))));
}

function scaledCornerRadii(width, height, radii) {
  const clamped = {
    tl: Math.max(0, Math.min(width / 2, height / 2, rawNumber(radii.tl) == null ? 0 : rawNumber(radii.tl))),
    tr: Math.max(0, Math.min(width / 2, height / 2, rawNumber(radii.tr) == null ? 0 : rawNumber(radii.tr))),
    br: Math.max(0, Math.min(width / 2, height / 2, rawNumber(radii.br) == null ? 0 : rawNumber(radii.br))),
    bl: Math.max(0, Math.min(width / 2, height / 2, rawNumber(radii.bl) == null ? 0 : rawNumber(radii.bl)))
  };
  let scale = 1;
  const candidates = [];
  if (clamped.tl + clamped.tr > width && clamped.tl + clamped.tr > 0) {
    candidates.push(width / (clamped.tl + clamped.tr));
  }
  if (clamped.bl + clamped.br > width && clamped.bl + clamped.br > 0) {
    candidates.push(width / (clamped.bl + clamped.br));
  }
  if (clamped.tl + clamped.bl > height && clamped.tl + clamped.bl > 0) {
    candidates.push(height / (clamped.tl + clamped.bl));
  }
  if (clamped.tr + clamped.br > height && clamped.tr + clamped.br > 0) {
    candidates.push(height / (clamped.tr + clamped.br));
  }
  if (candidates.length) {
    scale = Math.max(0, Math.min(1, ...candidates));
  }
  return {
    tl: clamped.tl * scale,
    tr: clamped.tr * scale,
    br: clamped.br * scale,
    bl: clamped.bl * scale
  };
}

function normalizedCornerRadii(params, width, height) {
  const defaultRadius = numberParam(params.radius, Math.min(width, height) * 0.12, 0, 30000);
  return scaledCornerRadii(width, height, {
    tl: rawNumber(params.radius_top_left ?? params.top_left_radius ?? params.radius_tl ?? defaultRadius),
    tr: rawNumber(params.radius_top_right ?? params.top_right_radius ?? params.radius_tr ?? defaultRadius),
    br: rawNumber(params.radius_bottom_right ?? params.bottom_right_radius ?? params.radius_br ?? defaultRadius),
    bl: rawNumber(params.radius_bottom_left ?? params.bottom_left_radius ?? params.radius_bl ?? defaultRadius)
  });
}

function roundedRectPolygonPoints(x, y, width, height, radii) {
  const points = [];
  const tl = radii.tl || 0;
  const tr = radii.tr || 0;
  const br = radii.br || 0;
  const bl = radii.bl || 0;
  points.push(pointObject(x + tl, y));
  points.push(pointObject(x + width - tr, y));
  appendArcPoints(points, x + width - tr, y + tr, tr, -Math.PI / 2, 0, roundedCornerSegments(tr, 90), false);
  points.push(pointObject(x + width, y + height - br));
  appendArcPoints(points, x + width - br, y + height - br, br, 0, Math.PI / 2, roundedCornerSegments(br, 90), false);
  points.push(pointObject(x + bl, y + height));
  appendArcPoints(points, x + bl, y + height - bl, bl, Math.PI / 2, Math.PI, roundedCornerSegments(bl, 90), false);
  points.push(pointObject(x, y + tl));
  appendArcPoints(points, x + tl, y + tl, tl, Math.PI, Math.PI * 1.5, roundedCornerSegments(tl, 90), false);
  return points;
}

function cutCornerPolygonPoints(x, y, width, height, params) {
  const defaultCut = numberParam(params.corner_cut, Math.min(width, height) * 0.14, 0, 30000);
  const tl = Math.max(0, Math.min(width / 2, height / 2, numberParam(params.cut_top_left, defaultCut, 0, 30000)));
  const tr = Math.max(0, Math.min(width / 2, height / 2, numberParam(params.cut_top_right, defaultCut, 0, 30000)));
  const br = Math.max(0, Math.min(width / 2, height / 2, numberParam(params.cut_bottom_right, defaultCut, 0, 30000)));
  const bl = Math.max(0, Math.min(width / 2, height / 2, numberParam(params.cut_bottom_left, defaultCut, 0, 30000)));
  return [
    pointObject(x + tl, y),
    pointObject(x + width - tr, y),
    pointObject(x + width, y + tr),
    pointObject(x + width, y + height - br),
    pointObject(x + width - br, y + height),
    pointObject(x + bl, y + height),
    pointObject(x, y + height - bl),
    pointObject(x, y + tl)
  ];
}

function mirrorPolygonHorizontally(points, axisX) {
  return points.map((point) => ({ x: axisX * 2 - point.x, y: point.y }));
}

function chevronPolygonPoints(x, y, width, height, params) {
  const pointDepth = numberParam(params.point_depth, width * 0.24, 1, 30000);
  const notchDepth = numberParam(params.notch_depth, Math.min(width * 0.18, height * 0.32), 0, 30000);
  const safePoint = Math.max(1, Math.min(width * 0.45, pointDepth));
  const safeNotch = Math.max(0, Math.min(width * 0.35, notchDepth));
  const rightPoints = [
    pointObject(x, y),
    pointObject(x + width - safePoint, y),
    pointObject(x + width, y + height / 2),
    pointObject(x + width - safePoint, y + height),
    pointObject(x, y + height),
    pointObject(x + safeNotch, y + height / 2)
  ];
  const direction = String(params.direction || params.chevron_direction || "right").toLowerCase();
  return direction === "left" ? mirrorPolygonHorizontally(rightPoints, x + width / 2) : rightPoints;
}

function ribbonPolygonPoints(x, y, width, height, params) {
  const tipDepth = numberParam(params.point_depth || params.tip_depth, Math.min(width * 0.18, height * 0.9), 1, 30000);
  const tailWidth = numberParam(params.tail_width, Math.min(width * 0.16, height * 0.8), 0, 30000);
  const notchDepth = numberParam(params.notch_depth, Math.min(height * 0.28, tailWidth * 0.9), 0, 30000);
  const safeTip = Math.max(1, Math.min(width * 0.35, tipDepth));
  const safeTail = Math.max(0, Math.min(width * 0.3, tailWidth));
  const safeNotch = Math.max(0, Math.min(height / 2 - 1, notchDepth));
  const rightPoints = [
    pointObject(x + safeTail, y),
    pointObject(x + width - safeTip, y),
    pointObject(x + width, y + height / 2),
    pointObject(x + width - safeTip, y + height),
    pointObject(x + safeTail, y + height),
    pointObject(x, y + height / 2 + safeNotch),
    pointObject(x + safeTail * 0.55, y + height / 2),
    pointObject(x, y + height / 2 - safeNotch)
  ];
  const direction = String(params.direction || params.tip_side || "right").toLowerCase();
  return direction === "left" ? mirrorPolygonHorizontally(rightPoints, x + width / 2) : rightPoints;
}

function arcBandPolygonPoints(params, state) {
  const docWidth = Math.max(1, Math.round(state && state.width || 1600));
  const docHeight = Math.max(1, Math.round(state && state.height || 1600));
  const cx = numberParam(params.x == null ? params.center_x : params.x, docWidth / 2, -100000, 100000);
  const cy = numberParam(params.y == null ? params.center_y : params.y, docHeight / 2, -100000, 100000);
  const thickness = numberParam(params.thickness, Math.max(12, Math.min(docWidth, docHeight) * 0.06), 1, 30000);
  const outerRadius = numberParam(params.outer_radius || params.radius, Math.min(docWidth, docHeight) * 0.2, 1, 30000);
  const innerRadius = Math.max(1, outerRadius - thickness);
  const startDeg = rawNumber(params.start_angle);
  const endDeg = rawNumber(params.end_angle);
  const spanDeg = numberParam(params.arc_span || params.span, 220, 1, 359.5);
  const rotationDeg = numberParam(params.rotation || params.angle, -90, -3600, 3600);
  const startAngle = (startDeg == null ? rotationDeg - spanDeg / 2 : startDeg) * Math.PI / 180;
  const endAngle = (endDeg == null ? rotationDeg + spanDeg / 2 : endDeg) * Math.PI / 180;
  const totalSweep = endAngle - startAngle;
  const segments = Math.max(12, Math.min(96, Math.ceil(Math.abs(totalSweep) / (Math.PI / 18))));
  const points = [];
  appendArcPoints(points, cx, cy, outerRadius, startAngle, endAngle, segments, true);
  appendArcPoints(points, cx, cy, innerRadius, endAngle, startAngle, segments, true);
  return points;
}

function bracketPolygonPoints(x, y, width, height, params) {
  const thickness = numberParam(params.thickness, Math.min(width, height) * 0.18, 1, 30000);
  const safeThickness = Math.max(1, Math.min(width - 1, height / 2 - 1, thickness));
  const leftBracket = [
    pointObject(x + width, y),
    pointObject(x, y),
    pointObject(x, y + height),
    pointObject(x + width, y + height),
    pointObject(x + width, y + height - safeThickness),
    pointObject(x + safeThickness, y + height - safeThickness),
    pointObject(x + safeThickness, y + safeThickness),
    pointObject(x + width, y + safeThickness)
  ];
  const side = String(params.side || params.opening || "left").toLowerCase();
  return side === "right" ? mirrorPolygonHorizontally(leftBracket, x + width / 2) : leftBracket;
}

async function createGeneratedPolygonShapeLayer(params, state, points, fallbackName, implementation, extras) {
  await selectPolygonPoints(points, { points, feather: params.feather }, state, "replace", "shape_polygon_selection_failed");
  const result = await createSolidColorLayerFromSelection(params, fallbackName);
  result.implementation = implementation;
  result.point_count = points.length;
  return Object.assign(result, extras || {});
}

async function createRoundedRectangleShapeLayer(params, state) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 100, 1, 30000);
  const height = numberParam(params.height, 100, 1, 30000);
  const radii = normalizedCornerRadii(params, width, height);
  const points = roundedRectPolygonPoints(x, y, width, height, radii);
  return createGeneratedPolygonShapeLayer(params, state, points, "Rounded Rectangle", "generated_rounded_rect_fill", {
    radii
  });
}

async function createCapsuleShapeLayer(params, state) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 100, 1, 30000);
  const height = numberParam(params.height, 100, 1, 30000);
  const radius = Math.min(width, height) / 2;
  const points = roundedRectPolygonPoints(x, y, width, height, { tl: radius, tr: radius, br: radius, bl: radius });
  return createGeneratedPolygonShapeLayer(params, state, points, "Capsule", "generated_capsule_fill", {
    radius
  });
}

async function createCutCornerRectShapeLayer(params, state) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 100, 1, 30000);
  const height = numberParam(params.height, 100, 1, 30000);
  const points = cutCornerPolygonPoints(x, y, width, height, params);
  return createGeneratedPolygonShapeLayer(params, state, points, "Cut Corner Rect", "generated_cut_corner_fill");
}

async function createRibbonShapeLayer(params, state) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 180, 1, 30000);
  const height = numberParam(params.height, 60, 1, 30000);
  const points = ribbonPolygonPoints(x, y, width, height, params);
  return createGeneratedPolygonShapeLayer(params, state, points, "Ribbon", "generated_ribbon_fill");
}

async function createArcBandShapeLayer(params, state) {
  const points = arcBandPolygonPoints(params, state);
  return createGeneratedPolygonShapeLayer(params, state, points, "Arc Band", "generated_arc_band_fill", {
    arc_span: numberParam(params.arc_span || params.span, 220, 1, 359.5),
    thickness: numberParam(params.thickness, 40, 1, 30000)
  });
}

async function createChevronShapeLayer(params, state) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 180, 1, 30000);
  const height = numberParam(params.height, 60, 1, 30000);
  const points = chevronPolygonPoints(x, y, width, height, params);
  return createGeneratedPolygonShapeLayer(params, state, points, "Chevron", "generated_chevron_fill");
}

async function createBracketShapeLayer(params, state) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 120, 1, 30000);
  const height = numberParam(params.height, 240, 1, 30000);
  const points = bracketPolygonPoints(x, y, width, height, params);
  return createGeneratedPolygonShapeLayer(params, state, points, "Bracket", "generated_bracket_fill", {
    thickness: numberParam(params.thickness, Math.min(width, height) * 0.18, 1, 30000)
  });
}

function finitePoint(point) {
  const x = Number(point && point.x);
  const y = Number(point && point.y);
  return Number.isFinite(x) && Number.isFinite(y) ? pointObject(x, y) : null;
}

function pointDistance(a, b) {
  const dx = Number(b.x) - Number(a.x);
  const dy = Number(b.y) - Number(a.y);
  return Math.sqrt(dx * dx + dy * dy);
}

function compactClosedPoints(points) {
  const compact = [];
  for (const raw of Array.isArray(points) ? points : []) {
    const point = finitePoint(raw);
    if (!point) {
      continue;
    }
    const previous = compact[compact.length - 1];
    if (!previous || pointDistance(previous, point) > 0.01) {
      compact.push(point);
    }
  }
  if (compact.length > 1 && pointDistance(compact[0], compact[compact.length - 1]) <= 0.01) {
    compact.pop();
  }
  return compact;
}

function chaikinClosedPoints(points, iterations, ratio) {
  let current = compactClosedPoints(points);
  const safeIterations = Math.max(0, Math.min(5, Math.round(Number(iterations) || 0)));
  const safeRatio = Math.max(0.05, Math.min(0.45, Number(ratio) || 0.25));
  if (current.length < 3 || safeIterations <= 0) {
    return current;
  }
  for (let pass = 0; pass < safeIterations; pass += 1) {
    const next = [];
    for (let index = 0; index < current.length; index += 1) {
      const a = current[index];
      const b = current[(index + 1) % current.length];
      next.push(pointObject(
        a.x * (1 - safeRatio) + b.x * safeRatio,
        a.y * (1 - safeRatio) + b.y * safeRatio
      ));
      next.push(pointObject(
        a.x * safeRatio + b.x * (1 - safeRatio),
        a.y * safeRatio + b.y * (1 - safeRatio)
      ));
    }
    current = next;
  }
  return current;
}

function closedPathLength(points) {
  const current = compactClosedPoints(points);
  let length = 0;
  for (let index = 0; index < current.length; index += 1) {
    length += pointDistance(current[index], current[(index + 1) % current.length]);
  }
  return length;
}

function sampleClosedPointAtLength(points, distance) {
  const current = compactClosedPoints(points);
  const total = closedPathLength(current);
  if (current.length < 3 || !(total > 0)) {
    return current[0] || pointObject(0, 0);
  }
  let remaining = ((Number(distance) || 0) % total + total) % total;
  for (let index = 0; index < current.length; index += 1) {
    const start = current[index];
    const end = current[(index + 1) % current.length];
    const segmentLength = pointDistance(start, end);
    if (segmentLength < 0.001) {
      continue;
    }
    if (remaining <= segmentLength) {
      const t = remaining / segmentLength;
      return pointObject(start.x + (end.x - start.x) * t, start.y + (end.y - start.y) * t);
    }
    remaining -= segmentLength;
  }
  return current[current.length - 1];
}

function resampleClosedPoints(points, maxPoints) {
  const current = compactClosedPoints(points);
  const safeMax = Math.max(MIN_POLYGON_POINTS, Math.min(MAX_POLYGON_POINTS, Math.round(Number(maxPoints) || MAX_POLYGON_POINTS)));
  if (current.length <= safeMax) {
    return current;
  }
  const total = closedPathLength(current);
  if (!(total > 0)) {
    return current.slice(0, safeMax);
  }
  const sampled = [];
  for (let index = 0; index < safeMax; index += 1) {
    sampled.push(sampleClosedPointAtLength(current, total * index / safeMax));
  }
  return compactClosedPoints(sampled);
}

function smoothClosedShapePoints(params, points, defaultIterations, defaultRatio, defaultMaxPoints) {
  const original = compactClosedPoints(points);
  const smoothFlag = params.smooth !== false && params.smoothing !== false;
  const requestedIterations = params.smooth_iterations ?? params.smoothing_iterations ?? params.smoothness;
  const iterations = smoothFlag
    ? Math.max(0, Math.min(5, Math.round(numberParam(requestedIterations, defaultIterations || 0, 0, 5))))
    : 0;
  const ratio = numberParam(params.smooth_ratio ?? params.smoothing_ratio, defaultRatio || 0.25, 0.05, 0.45);
  const maxPoints = Math.max(
    MIN_POLYGON_POINTS,
    Math.min(MAX_POLYGON_POINTS, Math.round(numberParam(params.max_points || params.max_point_count, defaultMaxPoints || 240, MIN_POLYGON_POINTS, MAX_POLYGON_POINTS)))
  );
  const smoothed = iterations > 0 ? chaikinClosedPoints(original, iterations, ratio) : original;
  const limited = resampleClosedPoints(smoothed, maxPoints);
  return {
    points: limited,
    smoothing: {
      iterations,
      ratio,
      original_point_count: original.length,
      smoothed_point_count: smoothed.length,
      output_point_count: limited.length,
      max_points: maxPoints
    }
  };
}

function scallopedTrianglePolygonPoints(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 260, 1, 30000);
  const height = numberParam(params.height, 180, 1, 30000);
  const tipX = numberParam(params.tip_x, x + width / 2, x - width, x + width * 2);
  const scallopCount = Math.max(1, Math.min(24, Math.round(numberParam(params.scallop_count, 5, 1, 24))));
  const scallopDepth = numberParam(params.scallop_depth, height * 0.14, 0, height * 0.45);
  const baseY = y + height - scallopDepth;
  const points = [
    pointObject(tipX, y),
    pointObject(x + width, baseY)
  ];
  const step = width / scallopCount;
  for (let scallop = scallopCount - 1; scallop >= 0; scallop -= 1) {
    const rightX = x + (scallop + 1) * step;
    const leftX = x + scallop * step;
    const centerX = (leftX + rightX) / 2;
    appendArcPoints(points, centerX, baseY, step / 2, 0, Math.PI, Math.max(6, Math.ceil(step / 20)), false);
    if (scallop > 0) {
      points.push(pointObject(leftX, baseY));
    }
  }
  points.push(pointObject(x, baseY));
  return points;
}

function seededUnit(seed, index) {
  let value = (Number(seed) || 1) + index * 374761393;
  value = (value ^ (value >>> 13)) * 1274126177;
  value = value ^ (value >>> 16);
  return (Math.abs(value) % 100000) / 100000;
}

function blobPolygonPoints(params) {
  const cx = numberParam(params.center_x == null ? params.x : params.center_x, 400, -100000, 100000);
  const cy = numberParam(params.center_y == null ? params.y : params.center_y, 400, -100000, 100000);
  const radiusX = numberParam(params.radius_x || params.width && Number(params.width) / 2, 160, 1, 30000);
  const radiusY = numberParam(params.radius_y || params.height && Number(params.height) / 2, 110, 1, 30000);
  const seed = Math.round(numberParam(params.seed, 17, -1000000, 1000000));
  const pointCount = Math.max(8, Math.min(96, Math.round(numberParam(params.point_count, 28, 8, 96))));
  const roughness = numberParam(params.roughness, 0.18, 0, 0.75);
  const rotation = numberParam(params.rotation, 0, -3600, 3600) * Math.PI / 180;
  const points = [];
  for (let index = 0; index < pointCount; index += 1) {
    const baseAngle = rotation + index * Math.PI * 2 / pointCount;
    const jitterA = (seededUnit(seed, index) - 0.5) * roughness * 0.7;
    const jitterR = 1 + (seededUnit(seed, index + 101) - 0.5) * roughness * 2;
    const angle = baseAngle + jitterA;
    points.push(pointObject(
      cx + Math.cos(angle) * radiusX * jitterR,
      cy + Math.sin(angle) * radiusY * jitterR
    ));
  }
  return { points, seed };
}

function wavyBandPolygonPoints(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 360, 1, 30000);
  const height = numberParam(params.height, 80, 1, 30000);
  const waveCount = Math.max(1, Math.min(48, Math.round(numberParam(params.wave_count, 4, 1, 48))));
  const amplitude = numberParam(params.amplitude, height * 0.22, 0, height);
  const phase = numberParam(params.phase, 0, -3600, 3600) * Math.PI / 180;
  const samples = Math.max(24, Math.min(256, waveCount * 16));
  const top = [];
  const bottom = [];
  for (let index = 0; index <= samples; index += 1) {
    const t = index / samples;
    const px = x + width * t;
    const offset = Math.sin(t * Math.PI * 2 * waveCount + phase) * amplitude;
    top.push(pointObject(px, y + offset));
    bottom.push(pointObject(px, y + height + offset));
  }
  return top.concat(bottom.reverse());
}

function starburstPolygonPoints(params, state) {
  const docWidth = Math.max(1, Math.round(state && state.width || 1600));
  const docHeight = Math.max(1, Math.round(state && state.height || 1600));
  const cx = numberParam(params.center_x == null ? params.x : params.center_x, docWidth / 2, -100000, 100000);
  const cy = numberParam(params.center_y == null ? params.y : params.center_y, docHeight / 2, -100000, 100000);
  const pointCount = Math.max(6, Math.min(96, Math.round(numberParam(params.points || params.point_count, 18, 3, 96))));
  const outerRadius = numberParam(params.outer_radius || params.radius, 140, 1, 30000);
  const innerRadius = numberParam(params.inner_radius, outerRadius * 0.72, 1, 30000);
  const rotation = numberParam(params.rotation, -90, -3600, 3600) * Math.PI / 180;
  const points = [];
  for (let index = 0; index < pointCount * 2; index += 1) {
    const radius = index % 2 === 0 ? outerRadius : innerRadius;
    const angle = rotation + index * Math.PI / pointCount;
    points.push(anglePoint(cx, cy, radius, angle));
  }
  return { points, starburst_points: pointCount };
}

function calloutPolygonPoints(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 260, 1, 30000);
  const height = numberParam(params.height, 140, 1, 30000);
  const radius = Math.min(width / 2, height / 2, numberParam(params.radius, Math.min(width, height) * 0.14, 0, 30000));
  const tailSide = String(params.tail_side || params.side || "bottom").toLowerCase();
  const tailWidth = numberParam(params.tail_width, Math.min(width, height) * 0.18, 1, 30000);
  const tailX = numberParam(params.tail_x, x + width / 2, x - width, x + width * 2);
  const tailY = numberParam(params.tail_y, y + height + Math.min(width, height) * 0.18, y - height, y + height * 2);
  const base = roundedRectPolygonPoints(x, y, width, height, { tl: radius, tr: radius, br: radius, bl: radius });
  if (tailSide === "top") {
    base.splice(1, 0, pointObject(tailX + tailWidth / 2, y), pointObject(tailX, tailY), pointObject(tailX - tailWidth / 2, y));
  } else if (tailSide === "left") {
    base.splice(base.length - 1, 0, pointObject(x, tailY - tailWidth / 2), pointObject(tailX, tailY), pointObject(x, tailY + tailWidth / 2));
  } else if (tailSide === "right") {
    base.splice(3, 0, pointObject(x + width, tailY - tailWidth / 2), pointObject(tailX, tailY), pointObject(x + width, tailY + tailWidth / 2));
  } else {
    base.splice(4, 0, pointObject(tailX + tailWidth / 2, y + height), pointObject(tailX, tailY), pointObject(tailX - tailWidth / 2, y + height));
  }
  return base;
}

function notchedPanelPolygonPoints(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 320, 1, 30000);
  const height = numberParam(params.height, 180, 1, 30000);
  const notch = Math.min(width / 3, height / 3, numberParam(params.notch_size, Math.min(width, height) * 0.12, 0, 30000));
  const positions = Array.isArray(params.notch_positions) ? params.notch_positions.map((value) => String(value).toLowerCase()) : ["top_right", "bottom_left"];
  const ntl = positions.includes("top_left") ? notch : 0;
  const ntr = positions.includes("top_right") ? notch : 0;
  const nbr = positions.includes("bottom_right") ? notch : 0;
  const nbl = positions.includes("bottom_left") ? notch : 0;
  return [
    pointObject(x + ntl, y),
    pointObject(x + width - ntr, y),
    pointObject(x + width, y + ntr),
    pointObject(x + width, y + height - nbr),
    pointObject(x + width - nbr, y + height),
    pointObject(x + nbl, y + height),
    pointObject(x, y + height - nbl),
    pointObject(x, y + ntl)
  ];
}

function ticketCardPolygonPoints(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 420, 1, 30000);
  const height = numberParam(params.height, 180, 1, 30000);
  const notchRadius = numberParam(params.notch_radius, Math.min(width, height) * 0.06, 0, Math.min(width, height) / 3);
  const notchCount = Math.max(1, Math.min(24, Math.round(numberParam(params.notch_count, 5, 1, 24))));
  const side = String(params.notch_side || params.side || "both").toLowerCase();
  const radius = Math.min(width / 2, height / 2, numberParam(params.radius, Math.min(width, height) * 0.08, 0, 30000));
  const points = [];
  function addHorizontal(leftToRight, yy) {
    const start = leftToRight ? x + radius : x + width - radius;
    const end = leftToRight ? x + width - radius : x + radius;
    const steps = Math.max(1, notchCount);
    for (let index = 0; index <= steps; index += 1) {
      const t = index / steps;
      const px = start + (end - start) * t;
      const wave = Math.sin(t * Math.PI * steps) * notchRadius * 0.45;
      points.push(pointObject(px, yy + (yy === y ? wave : -wave)));
    }
  }
  function addVertical(topToBottom, xx) {
    const start = topToBottom ? y + radius : y + height - radius;
    const end = topToBottom ? y + height - radius : y + radius;
    const steps = Math.max(1, notchCount);
    for (let index = 0; index <= steps; index += 1) {
      const t = index / steps;
      const py = start + (end - start) * t;
      const wave = Math.sin(t * Math.PI * steps) * notchRadius * 0.45;
      points.push(pointObject(xx + (xx === x ? wave : -wave), py));
    }
  }
  points.push(pointObject(x + radius, y));
  if (side === "top" || side === "both" || side === "horizontal") {
    addHorizontal(true, y);
  } else {
    points.push(pointObject(x + width - radius, y));
  }
  appendArcPoints(points, x + width - radius, y + radius, radius, -Math.PI / 2, 0, roundedCornerSegments(radius, 90), false);
  if (side === "right" || side === "both" || side === "vertical") {
    addVertical(true, x + width);
  } else {
    points.push(pointObject(x + width, y + height - radius));
  }
  appendArcPoints(points, x + width - radius, y + height - radius, radius, 0, Math.PI / 2, roundedCornerSegments(radius, 90), false);
  if (side === "bottom" || side === "both" || side === "horizontal") {
    addHorizontal(false, y + height);
  } else {
    points.push(pointObject(x + radius, y + height));
  }
  appendArcPoints(points, x + radius, y + height - radius, radius, Math.PI / 2, Math.PI, roundedCornerSegments(radius, 90), false);
  if (side === "left" || side === "both" || side === "vertical") {
    addVertical(false, x);
  } else {
    points.push(pointObject(x, y + radius));
  }
  appendArcPoints(points, x + radius, y + radius, radius, Math.PI, Math.PI * 1.5, roundedCornerSegments(radius, 90), false);
  return points;
}

function foldedCornerPoints(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, 300, 1, 30000);
  const height = numberParam(params.height, 200, 1, 30000);
  const fold = Math.min(width / 2, height / 2, numberParam(params.fold_size, Math.min(width, height) * 0.18, 1, 30000));
  const corner = String(params.corner || "top_right").toLowerCase();
  if (corner === "top_left") {
    return {
      body: [pointObject(x + fold, y), pointObject(x + width, y), pointObject(x + width, y + height), pointObject(x, y + height), pointObject(x, y + fold)],
      fold: [pointObject(x, y), pointObject(x + fold, y), pointObject(x, y + fold)]
    };
  }
  if (corner === "bottom_right") {
    return {
      body: [pointObject(x, y), pointObject(x + width, y), pointObject(x + width, y + height - fold), pointObject(x + width - fold, y + height), pointObject(x, y + height)],
      fold: [pointObject(x + width, y + height), pointObject(x + width, y + height - fold), pointObject(x + width - fold, y + height)]
    };
  }
  if (corner === "bottom_left") {
    return {
      body: [pointObject(x, y), pointObject(x + width, y), pointObject(x + width, y + height), pointObject(x + fold, y + height), pointObject(x, y + height - fold)],
      fold: [pointObject(x, y + height), pointObject(x + fold, y + height), pointObject(x, y + height - fold)]
    };
  }
  return {
    body: [pointObject(x, y), pointObject(x + width - fold, y), pointObject(x + width, y + fold), pointObject(x + width, y + height), pointObject(x, y + height)],
    fold: [pointObject(x + width, y), pointObject(x + width - fold, y), pointObject(x + width, y + fold)]
  };
}

function pathLength(points) {
  let length = 0;
  for (let index = 0; index < points.length - 1; index += 1) {
    const dx = points[index + 1].x - points[index].x;
    const dy = points[index + 1].y - points[index].y;
    length += Math.sqrt(dx * dx + dy * dy);
  }
  return length;
}

function samplePointAtLength(points, distance) {
  let remaining = Math.max(0, Number(distance) || 0);
  for (let index = 0; index < points.length - 1; index += 1) {
    const start = points[index];
    const end = points[index + 1];
    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const segmentLength = Math.sqrt(dx * dx + dy * dy);
    if (segmentLength < 0.001) {
      continue;
    }
    if (remaining <= segmentLength) {
      const t = remaining / segmentLength;
      return { x: start.x + dx * t, y: start.y + dy * t, angle: Math.atan2(dy, dx) };
    }
    remaining -= segmentLength;
  }
  const last = points[points.length - 1];
  const prev = points[Math.max(0, points.length - 2)];
  return { x: last.x, y: last.y, angle: Math.atan2(last.y - prev.y, last.x - prev.x) };
}

function samplePolyline(points, spacing, maxCount, includeEnds) {
  const total = pathLength(points);
  if (!(total > 0)) {
    return [];
  }
  const safeSpacing = Math.max(1, Number(spacing) || total);
  const count = Math.max(1, Math.min(maxCount || 256, Math.floor(total / safeSpacing) + (includeEnds ? 1 : 0)));
  const samples = [];
  if (includeEnds && count === 1) {
    samples.push(samplePointAtLength(points, total / 2));
    return samples;
  }
  for (let index = 0; index < count; index += 1) {
    const distance = includeEnds && count > 1 ? total * index / (count - 1) : Math.min(total, safeSpacing * (index + 0.5));
    samples.push(samplePointAtLength(points, distance));
  }
  return samples;
}

async function groupLayerIdsIfRequested(layerIds, name, groupRequested) {
  const ids = (layerIds || []).filter((value) => Number.isFinite(Number(value)));
  if (!ids.length || groupRequested === false) {
    return null;
  }
  await selectLayerIds(ids);
  return groupSelectedLayers(safeLayerName(name, "Composite Shape"));
}

async function createScallopedTriangleShapeLayer(params, state) {
  const smoothed = smoothClosedShapePoints(params, scallopedTrianglePolygonPoints(params), 1, 0.18, 240);
  return createGeneratedPolygonShapeLayer(params, state, smoothed.points, "Scalloped Triangle", "generated_scalloped_triangle_fill", {
    smoothing: smoothed.smoothing
  });
}

async function createBlobShapeLayer(params, state) {
  const generated = blobPolygonPoints(params);
  const smoothed = smoothClosedShapePoints(params, generated.points, 2, 0.25, 240);
  return createGeneratedPolygonShapeLayer(params, state, smoothed.points, "Blob", "generated_blob_fill", {
    seed: generated.seed,
    smoothing: smoothed.smoothing
  });
}

async function createWavyBandShapeLayer(params, state) {
  const smoothed = smoothClosedShapePoints(params, wavyBandPolygonPoints(params), 0, 0.18, 240);
  return createGeneratedPolygonShapeLayer(params, state, smoothed.points, "Wavy Band", "generated_wavy_band_fill", {
    smoothing: smoothed.smoothing
  });
}

async function createStarburstShapeLayer(params, state) {
  const generated = starburstPolygonPoints(params, state);
  return createGeneratedPolygonShapeLayer(params, state, generated.points, "Starburst", "generated_starburst_fill", {
    starburst_points: generated.starburst_points
  });
}

async function createCalloutShapeLayer(params, state) {
  const smoothed = smoothClosedShapePoints(params, calloutPolygonPoints(params), 0, 0.18, 180);
  return createGeneratedPolygonShapeLayer(params, state, smoothed.points, "Callout", "generated_callout_fill", {
    smoothing: smoothed.smoothing
  });
}

async function createTicketCardShapeLayer(params, state) {
  const smoothed = smoothClosedShapePoints(params, ticketCardPolygonPoints(params), 1, 0.18, 240);
  return createGeneratedPolygonShapeLayer(params, state, smoothed.points, "Ticket Card", "generated_ticket_card_fill", {
    smoothing: smoothed.smoothing
  });
}

async function createNotchedPanelShapeLayer(params, state) {
  const smoothed = smoothClosedShapePoints(params, notchedPanelPolygonPoints(params), 1, 0.16, 120);
  return createGeneratedPolygonShapeLayer(params, state, smoothed.points, "Notched Panel", "generated_notched_panel_fill", {
    smoothing: smoothed.smoothing
  });
}

async function createBeadsOnPathShapeLayers(params, state) {
  const points = polylineInputPoints(params);
  if (points.length < 2 || points.length > 256) {
    throw codedError("invalid_shape_polyline_points", "shape.beads_on_path points must contain 2-256 points.");
  }
  const radius = numberParam(params.bead_radius || params.radius, 10, 0.5, 1000);
  const spacing = numberParam(params.spacing, radius * 2.2, 1, 3000);
  const maxBeads = Math.max(1, Math.min(512, Math.round(numberParam(params.max_beads, 128, 1, 512))));
  const samples = samplePolyline(points, spacing, maxBeads, true);
  const layerIds = [];
  const name = safeLayerName(params.name, "Beads On Path");
  for (let index = 0; index < samples.length; index += 1) {
    const sample = samples[index];
    const bead = await createEllipseShapeLayer(Object.assign({}, params, {
      x: sample.x - radius,
      y: sample.y - radius,
      width: radius * 2,
      height: radius * 2,
      fill: params.fill || params.color,
      name: `${name} ${index + 1}`
    }));
    layerIds.push(bead.layer_id);
    if (params.highlight_fill) {
      const highlightRadius = Math.max(1, radius * 0.34);
      const highlight = await createEllipseShapeLayer({
        x: sample.x - radius * 0.42,
        y: sample.y - radius * 0.48,
        width: highlightRadius,
        height: highlightRadius,
        fill: params.highlight_fill,
        name: `${name} Highlight ${index + 1}`,
        opacity: numberParam(params.highlight_opacity, 85, 0, 100)
      });
      layerIds.push(highlight.layer_id);
    }
  }
  const groupId = await groupLayerIdsIfRequested(layerIds, name, params.group !== false);
  return {
    group_id: groupId,
    group_name: groupId == null ? null : name,
    layer_ids: layerIds,
    bead_count: samples.length,
    sampled_points: samples.map((sample) => ({ x: sample.x, y: sample.y })),
    implementation: "sampled_ellipse_beads_on_polyline"
  };
}

async function createDashedPathShapeLayers(params, state) {
  const points = polylineInputPoints(params);
  if (points.length < 2 || points.length > 256) {
    throw codedError("invalid_shape_polyline_points", "shape.dashed_path points must contain 2-256 points.");
  }
  const width = numberParam(params.width || params.stroke_width, 8, 0.5, 3000);
  const dashLength = numberParam(params.dash_length, width * 4, 1, 10000);
  const gapLength = numberParam(params.gap_length, width * 2, 0, 10000);
  const total = pathLength(points);
  const layerIds = [];
  const name = safeLayerName(params.name, "Dashed Path");
  if (!(total > 0.1) || total < Math.max(1, dashLength * 0.25)) {
    return { group_id: null, layer_ids: [], dash_count: 0, implementation: "no_op_short_path" };
  }
  let distance = 0;
  let dashIndex = 0;
  while (distance < total && dashIndex < 512) {
    const start = samplePointAtLength(points, distance);
    const end = samplePointAtLength(points, Math.min(total, distance + dashLength));
    if (Math.hypot(end.x - start.x, end.y - start.y) >= 0.5) {
      const dash = await createLineShapeLayer(Object.assign({}, params, {
        x1: start.x,
        y1: start.y,
        x2: end.x,
        y2: end.y,
        width,
        name: `${name} ${dashIndex + 1}`
      }), state);
      layerIds.push(dash.layer_id);
      dashIndex += 1;
    }
    distance += dashLength + gapLength;
  }
  const groupId = await groupLayerIdsIfRequested(layerIds, name, params.group !== false);
  return {
    group_id: groupId,
    group_name: groupId == null ? null : name,
    layer_ids: layerIds,
    dash_count: layerIds.length,
    implementation: "sampled_line_dashes_on_polyline"
  };
}

async function createArrowPathShapeLayers(params, state) {
  const points = polylineInputPoints(params);
  if (points.length < 2 || points.length > 256) {
    throw codedError("invalid_shape_polyline_points", "shape.arrow_path points must contain 2-256 points.");
  }
  const width = numberParam(params.width || params.stroke_width, 10, 0.5, 3000);
  const headSize = numberParam(params.head_size, Math.max(width * 3, 24), 1, 30000);
  const name = safeLayerName(params.name, "Arrow Path");
  const shaft = await createPolylineShapeLayer(Object.assign({}, params, { width, name: `${name} Shaft` }), state);
  const end = points[points.length - 1];
  let prev = points[points.length - 2];
  for (let index = points.length - 2; index >= 0; index -= 1) {
    if (Math.hypot(end.x - points[index].x, end.y - points[index].y) > 0.5) {
      prev = points[index];
      break;
    }
  }
  const angle = Math.atan2(end.y - prev.y, end.x - prev.x);
  const base = headSize * 0.82;
  const halfWidth = headSize * 0.46;
  const backX = end.x - Math.cos(angle) * base;
  const backY = end.y - Math.sin(angle) * base;
  const px = -Math.sin(angle) * halfWidth;
  const py = Math.cos(angle) * halfWidth;
  const headPoints = [
    pointObject(end.x, end.y),
    pointObject(backX + px, backY + py),
    pointObject(backX - px, backY - py)
  ];
  const head = await createGeneratedPolygonShapeLayer(Object.assign({}, params, { name: `${name} Head` }), state, headPoints, "Arrow Head", "generated_arrow_head_fill");
  const layerIds = [shaft.layer_id, head.layer_id];
  const groupId = await groupLayerIdsIfRequested(layerIds, name, params.group === true);
  return {
    group_id: groupId,
    group_name: groupId == null ? null : name,
    layer_ids: layerIds,
    shaft_layer_id: shaft.layer_id,
    head_layer_id: head.layer_id,
    implementation: "polyline_shaft_polygon_head"
  };
}

async function createBaubleShapeLayers(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const diameter = numberParam(params.diameter || params.size, 80, 1, 30000);
  const name = safeLayerName(params.name, "Bauble");
  const layerIds = [];
  const hookWidth = Math.max(2, diameter * 0.13);
  const hookHeight = Math.max(4, diameter * 0.18);
  const hook = await createRectangleShapeLayer({
    x: x + diameter / 2 - hookWidth / 2,
    y: y - hookHeight * 0.42,
    width: hookWidth,
    height: hookHeight,
    fill: params.hook_fill || { r: 238, g: 210, b: 56 },
    name: `${name} Hook`
  });
  layerIds.push(hook.layer_id);
  const body = await createEllipseShapeLayer(Object.assign({}, params, {
    x,
    y,
    width: diameter,
    height: diameter,
    fill: params.fill || params.color || { r: 224, g: 32, b: 54 },
    name: `${name} Body`
  }));
  layerIds.push(body.layer_id);
  const highlight = await createEllipseShapeLayer({
    x: x + diameter * 0.18,
    y: y + diameter * 0.15,
    width: diameter * 0.24,
    height: diameter * 0.15,
    fill: params.highlight_fill || { r: 255, g: 255, b: 255 },
    name: `${name} Highlight`,
    opacity: numberParam(params.highlight_opacity, 88, 0, 100)
  });
  layerIds.push(highlight.layer_id);
  const groupId = await groupLayerIdsIfRequested(layerIds, name, params.group !== false);
  return {
    group_id: groupId,
    group_name: groupId == null ? null : name,
    layer_ids: layerIds,
    body_layer_id: body.layer_id,
    highlight_layer_id: highlight.layer_id,
    hook_layer_id: hook.layer_id,
    implementation: "composite_ellipse_hook_highlight"
  };
}

async function createBadgeShapeLayers(params, state) {
  const cx = numberParam(params.center_x == null ? params.x : params.center_x, 300, -100000, 100000);
  const cy = numberParam(params.center_y == null ? params.y : params.center_y, 300, -100000, 100000);
  const radius = numberParam(params.radius, 70, 1, 30000);
  const name = safeLayerName(params.name, "Badge");
  const style = String(params.style || "burst").toLowerCase();
  const layerIds = [];
  let base;
  if (style === "circle" || style === "round") {
    base = await createEllipseShapeLayer({
      x: cx - radius,
      y: cy - radius,
      width: radius * 2,
      height: radius * 2,
      fill: params.fill || params.color || { r: 255, g: 210, b: 65 },
      name: `${name} Base`
    });
  } else {
    base = await createStarburstShapeLayer(Object.assign({}, params, {
      center_x: cx,
      center_y: cy,
      outer_radius: radius,
      inner_radius: numberParam(params.inner_radius, radius * 0.78, 1, 30000),
      points: Math.round(numberParam(params.points || params.point_count, 18, 6, 96)),
      name: `${name} Base`
    }), state);
  }
  layerIds.push(base.layer_id);
  const inner = await createEllipseShapeLayer({
    x: cx - radius * 0.66,
    y: cy - radius * 0.66,
    width: radius * 1.32,
    height: radius * 1.32,
    fill: params.inner_fill || params.stroke_fill || { r: 255, g: 255, b: 255 },
    opacity: numberParam(params.inner_opacity, 22, 0, 100),
    name: `${name} Inner`
  });
  layerIds.push(inner.layer_id);
  const groupId = await groupLayerIdsIfRequested(layerIds, name, params.group !== false);
  return {
    group_id: groupId,
    group_name: groupId == null ? null : name,
    layer_ids: layerIds,
    implementation: "composite_badge"
  };
}

async function createFoldedCornerShapeLayers(params, state) {
  const name = safeLayerName(params.name, "Folded Corner");
  const generated = foldedCornerPoints(params);
  const layerIds = [];
  const body = await createGeneratedPolygonShapeLayer(Object.assign({}, params, { name: `${name} Body` }), state, generated.body, "Folded Card Body", "generated_folded_corner_body");
  layerIds.push(body.layer_id);
  const fold = await createGeneratedPolygonShapeLayer(Object.assign({}, params, {
    name: `${name} Fold`,
    fill: params.fold_fill || { r: 230, g: 236, b: 245 },
    color: params.fold_fill || params.color
  }), state, generated.fold, "Folded Card Fold", "generated_folded_corner_fold");
  layerIds.push(fold.layer_id);
  const groupId = await groupLayerIdsIfRequested(layerIds, name, params.group !== false);
  return {
    group_id: groupId,
    group_name: groupId == null ? null : name,
    layer_ids: layerIds,
    body_layer_id: body.layer_id,
    fold_layer_id: fold.layer_id,
    implementation: "composite_folded_corner"
  };
}

function starPoints(params, state) {
  const docWidth = Math.max(1, Math.round(state && state.width || 1600));
  const docHeight = Math.max(1, Math.round(state && state.height || 1600));
  const cx = numberParam(params.x == null ? params.center_x : params.x, docWidth / 2, -100000, 100000);
  const cy = numberParam(params.y == null ? params.center_y : params.y, docHeight / 2, -100000, 100000);
  const pointCount = Math.round(numberParam(params.points || params.point_count, 5, 3, 64));
  const outerRadius = numberParam(params.outer_radius || params.radius, 120, 1, 30000);
  const innerRadius = numberParam(params.inner_radius, outerRadius * 0.42, 1, 30000);
  const rotation = numberParam(params.rotation, -90, -3600, 3600) * Math.PI / 180;
  const points = [];
  for (let index = 0; index < pointCount * 2; index += 1) {
    const radius = index % 2 === 0 ? outerRadius : innerRadius;
    const angle = rotation + index * Math.PI / pointCount;
    points.push({
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius
    });
  }
  return points;
}

function segmentPolygonPoints(start, end, width) {
  const x1 = Number(start && start.x);
  const y1 = Number(start && start.y);
  const x2 = Number(end && end.x);
  const y2 = Number(end && end.y);
  if (![x1, y1, x2, y2].every(Number.isFinite)) {
    throw codedError("invalid_shape_line_points", "shape.line/polyline points must contain numeric x/y values.");
  }
  const dx = x2 - x1;
  const dy = y2 - y1;
  const length = Math.sqrt(dx * dx + dy * dy);
  if (length < 0.01) {
    throw codedError("invalid_shape_line_points", "shape.line/polyline segment length is too small.");
  }
  const half = Math.max(0.5, Number(width) / 2);
  const px = -dy / length * half;
  const py = dx / length * half;
  return [
    { x: x1 + px, y: y1 + py },
    { x: x2 + px, y: y2 + py },
    { x: x2 - px, y: y2 - py },
    { x: x1 - px, y: y1 - py }
  ];
}

function polylineInputPoints(params) {
  const raw = Array.isArray(params.points) ? params.points : [];
  return raw.map((point) => {
    if (Array.isArray(point)) {
      return { x: Number(point[0]), y: Number(point[1]) };
    }
    return { x: Number(point && point.x), y: Number(point && point.y) };
  });
}

async function createSolidColorLayerFromSelection(params, fallbackName) {
  if (!(await hasActiveSelection())) {
    throw codedError("selection_empty", "A shape selection must be active before creating a solid color shape layer.");
  }
  const name = safeLayerName(params.name, fallbackName);
  await playAction([
    {
      _obj: "make",
      _target: [{ _ref: "contentLayer" }],
      using: {
        _obj: "contentLayer",
        name,
        type: {
          _obj: "solidColorLayer",
          color: rgbColor(params.fill || params.color, [255, 255, 255])
        }
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await setActiveLayerProperties({
    name,
    opacity: numberParam(params.opacity, 100, 0, 100),
    blendMode: blendModeValue(params.blend_mode || "normal")
  });
  const layerId = await getActiveLayerId();
  if (params.clear_selection !== false) {
    await clearSelection();
  }
  return { layer_id: layerId, layer_name: name, implementation: "solid_color_layer_from_selection" };
}

async function createPolygonShapeLayer(params, state) {
  const points = normalizedPolygonPoints({ points: params.points }, state);
  await selectPolygonPoints(points, { points, feather: params.feather }, state, "replace", "shape_polygon_selection_failed");
  const result = await createSolidColorLayerFromSelection(params, "Polygon");
  result.point_count = points.length;
  return result;
}

async function createStarShapeLayer(params, state) {
  const points = starPoints(params, state);
  await selectPolygonPoints(points, { points, feather: params.feather }, state, "replace", "shape_star_selection_failed");
  const result = await createSolidColorLayerFromSelection(params, "Star");
  result.point_count = points.length;
  result.star_points = Math.round(numberParam(params.points || params.point_count, 5, 3, 64));
  return result;
}

async function createLineShapeLayer(params, state) {
  const start = {
    x: params.x1 == null ? params.start && params.start.x : params.x1,
    y: params.y1 == null ? params.start && params.start.y : params.y1
  };
  const end = {
    x: params.x2 == null ? params.end && params.end.x : params.x2,
    y: params.y2 == null ? params.end && params.end.y : params.y2
  };
  const width = numberParam(params.width || params.stroke_width, 8, 0.5, 3000);
  const points = segmentPolygonPoints(start, end, width);
  await selectPolygonPoints(points, { points, feather: params.feather }, state, "replace", "shape_line_selection_failed");
  const result = await createSolidColorLayerFromSelection(params, "Line");
  result.width = width;
  result.point_count = points.length;
  result.cap = "butt";
  return result;
}

async function createPolylineShapeLayer(params, state) {
  const inputPoints = polylineInputPoints(params);
  if (inputPoints.length < 2 || inputPoints.length > 128) {
    throw codedError("invalid_shape_polyline_points", "shape.polyline points must contain 2-128 points.");
  }
  const width = numberParam(params.width || params.stroke_width, 8, 0.5, 3000);
  for (let index = 0; index < inputPoints.length - 1; index += 1) {
    const points = segmentPolygonPoints(inputPoints[index], inputPoints[index + 1], width);
    await selectPolygonPoints(points, { points, feather: params.feather }, state, index === 0 ? "replace" : "add", "shape_polyline_selection_failed");
  }
  const result = await createSolidColorLayerFromSelection(params, "Polyline");
  result.width = width;
  result.segment_count = inputPoints.length - 1;
  result.cap = "butt";
  result.join = "miter_union";
  return result;
}

function pathCoordinate(point, axis, fallback) {
  if (Array.isArray(point)) {
    const index = axis === "x" ? 0 : 1;
    const parsed = Number(point[index]);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  const parsed = Number(point && point[axis]);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function pathHandleFromInput(input, keys) {
  for (const key of keys) {
    const raw = input[key];
    if (!raw) {
      continue;
    }
    const hx = pathCoordinate(raw, "x", NaN);
    const hy = pathCoordinate(raw, "y", NaN);
    if (Number.isFinite(hx) && Number.isFinite(hy)) {
      return { x: hx, y: hy };
    }
  }
  return null;
}

function normalizedPathPoint(point, index) {
  const x = pathCoordinate(point, "x", NaN);
  const y = pathCoordinate(point, "y", NaN);
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    throw codedError("invalid_path_points", `path point ${index} must contain numeric x/y values.`);
  }
  const input = point && typeof point === "object" && !Array.isArray(point) ? point : {};
  const backward = pathHandleFromInput(input, ["backward", "back", "in", "in_handle"]);
  const forward = pathHandleFromInput(input, ["forward", "out", "out_handle"]);
  const rawKind = typeof input.kind === "string" ? input.kind.toLowerCase() : null;
  if (rawKind && rawKind !== "smooth" && rawKind !== "corner") {
    throw codedError("invalid_path_points", `path point ${index}.kind must be smooth or corner.`);
  }
  if (rawKind === "corner" && input.smooth === true) {
    throw codedError("invalid_path_points", `path point ${index} cannot set kind=corner and smooth=true at the same time.`);
  }
  const kind = rawKind || (input.smooth === true ? "smooth" : "corner");
  return {
    x,
    y,
    backward: backward || { x, y },
    forward: forward || { x, y },
    has_backward: Boolean(backward),
    has_forward: Boolean(forward),
    smooth: kind === "smooth",
    kind
  };
}

function normalizedPathSubpaths(params) {
  const rawSubpaths = Array.isArray(params.subpaths) ? params.subpaths : null;
  const subpaths = rawSubpaths || [{
    points: Array.isArray(params.points) ? params.points : [],
    closed: params.closed !== false,
    operation: params.operation || "add"
  }];
  if (!subpaths.length) {
    throw codedError("invalid_path_points", "path.create_work_path requires points or subpaths.");
  }
  return subpaths.map((subpath, subpathIndex) => {
    const rawPoints = Array.isArray(subpath.points) ? subpath.points : [];
    const closed = subpath.closed !== false;
    if (rawPoints.length < (closed ? 3 : 2) || rawPoints.length > 512) {
      throw codedError(
        "invalid_path_points",
        `path subpath ${subpathIndex} requires ${closed ? "3-512" : "2-512"} points.`
      );
    }
    const points = rawPoints.map((point, pointIndex) => normalizedPathPoint(point, pointIndex));
    const operation = String(subpath.operation || "add").toLowerCase();
    const operationMap = {
      add: "add",
      subtract: "subtract",
      intersect: "intersect",
      exclude: "xor"
    };
    return {
      closed,
      operation: operationMap[operation] || "add",
      points: normalizeBezierSubpathHandles(points, closed, subpath, params)
    };
  });
}

function pointDistance(a, b) {
  const dx = Number(a.x) - Number(b.x);
  const dy = Number(a.y) - Number(b.y);
  return Math.sqrt(dx * dx + dy * dy);
}

function vectorLength(vector) {
  return Math.sqrt(vector.x * vector.x + vector.y * vector.y);
}

function dotProduct(a, b) {
  return a.x * b.x + a.y * b.y;
}

function crossProduct(a, b) {
  return a.x * b.y - a.y * b.x;
}

function vectorFrom(a, b) {
  return { x: Number(b.x) - Number(a.x), y: Number(b.y) - Number(a.y) };
}

function normalizedBezierHandleMode(subpathParams, params) {
  const raw = String(
    (subpathParams && (subpathParams.handle_mode || subpathParams.handles || subpathParams.smoothing)) ||
    params.handle_mode ||
    params.handles ||
    "manual"
  ).toLowerCase().replace(/[\s-]+/g, "_");
  if (raw === "auto" || raw === "auto_smooth" || raw === "catmullrom" || raw === "catmull_rom") {
    return "catmull_rom";
  }
  if (raw === "geometric" || raw === "geometry") {
    return "geometric";
  }
  return "manual";
}

function bezierHandleScale(subpathParams, params) {
  return numberParam(
    (subpathParams && subpathParams.handle_scale != null ? subpathParams.handle_scale : params.handle_scale),
    1,
    0.05,
    2
  );
}

function autoBezierTurnAngle(previous, point, next) {
  const incoming = vectorFrom(previous, point);
  const outgoing = vectorFrom(point, next);
  const incomingLength = Math.max(vectorLength(incoming), 0.001);
  const outgoingLength = Math.max(vectorLength(outgoing), 0.001);
  const cosine = dotProduct(incoming, outgoing) / Math.max(incomingLength * outgoingLength, 0.001);
  return Math.acos(Math.max(-1, Math.min(1, cosine))) * 180 / Math.PI;
}

function shouldKeepBezierCorner(point, turnAngle, minDistance, params = {}) {
  if (point.kind === "corner" && point.has_backward === false && point.has_forward === false) {
    return true;
  }
  const threshold = numberParam(params.corner_angle_threshold, 118, 45, 175);
  const minSegment = numberParam(params.min_smooth_segment_length, 12, 0, 1000);
  return turnAngle >= threshold || minDistance <= minSegment;
}

function autoBezierHandles(points, closed, mode, scale, params = {}) {
  if (mode === "manual") {
    return points.map((point) => Object.assign({}, point));
  }
  const count = points.length;
  return points.map((point, index) => {
    const previous = points[index - 1] || (closed ? points[(index - 1 + count) % count] : null);
    const next = points[index + 1] || (closed ? points[(index + 1) % count] : null);
    const updated = Object.assign({}, point);
    if (!previous || !next) {
      updated.backward = { x: point.x, y: point.y };
      updated.forward = { x: point.x, y: point.y };
      updated.has_backward = false;
      updated.has_forward = false;
      updated.smooth = false;
      updated.kind = "corner";
      return updated;
    }
    const prevDistance = Math.max(pointDistance(previous, point), 0.001);
    const nextDistance = Math.max(pointDistance(point, next), 0.001);
    const minDistance = Math.min(prevDistance, nextDistance);
    const turnAngle = autoBezierTurnAngle(previous, point, next);
    const cornerThreshold = numberParam(params.corner_angle_threshold, 118, 45, 175);
    if (shouldKeepBezierCorner(point, turnAngle, minDistance, params)) {
      updated.backward = { x: point.x, y: point.y };
      updated.forward = { x: point.x, y: point.y };
      updated.has_backward = false;
      updated.has_forward = false;
      updated.smooth = false;
      updated.kind = "corner";
      updated.corner_reason = turnAngle >= cornerThreshold ? "sharp_turn" : "short_segment";
      return updated;
    }
    const tangent = vectorFrom(previous, next);
    const tangentLength = Math.max(vectorLength(tangent), 0.001);
    const ux = tangent.x / tangentLength;
    const uy = tangent.y / tangentLength;
    const baseFactor = mode === "geometric" ? 0.28 : 0.30;
    const angleDamping = Math.max(0.08, Math.cos((turnAngle * Math.PI / 180) / 2));
    const loopSafeCap = Math.max(4, minDistance * 0.36 * angleDamping);
    const outLength = Math.min(nextDistance * baseFactor * angleDamping, loopSafeCap) * scale;
    const inLength = Math.min(prevDistance * baseFactor * angleDamping, loopSafeCap) * scale;
    updated.backward = { x: point.x - ux * inLength, y: point.y - uy * inLength };
    updated.forward = { x: point.x + ux * outLength, y: point.y + uy * outLength };
    updated.has_backward = true;
    updated.has_forward = true;
    updated.smooth = true;
    updated.kind = "smooth";
    updated.turn_angle = Number(turnAngle.toFixed(2));
    return updated;
  });
}

function repairManualBezierHandles(points, closed, scale, params = {}) {
  const generated = autoBezierHandles(points, closed, "catmull_rom", scale, params);
  return points.map((point, index) => {
    if (point.has_backward && point.has_forward) {
      return point;
    }
    const fallback = generated[index];
    return Object.assign({}, point, {
      backward: point.has_backward ? point.backward : fallback.backward,
      forward: point.has_forward ? point.forward : fallback.forward,
      has_backward: true,
      has_forward: true,
      smooth: point.smooth || point.kind === "smooth",
      kind: point.kind || (point.smooth ? "smooth" : "corner")
    });
  });
}

function normalizeBezierSubpathHandles(points, closed, subpathParams, params) {
  const mode = normalizedBezierHandleMode(subpathParams, params);
  const scale = bezierHandleScale(subpathParams, params);
  if (mode !== "manual") {
    return autoBezierHandles(points, closed, mode, scale, params);
  }
  if (params.auto_repair_handles === true || (subpathParams && subpathParams.auto_repair_handles === true)) {
    return repairManualBezierHandles(points, closed, scale, params);
  }
  return points;
}

function auditBezierSubpath(subpath, subpathIndex, options = {}) {
  const tolerance = numberParam(options.tolerance, 12, 0.5, 180);
  const minRatio = numberParam(options.min_handle_ratio, 0.03, 0, 1);
  const maxRatio = numberParam(options.max_handle_ratio, 0.75, 0.05, 3);
  const points = subpath.points || [];
  const warnings = [];
  const metrics = [];
  const count = points.length;
  points.forEach((point, index) => {
    const previous = points[index - 1] || (subpath.closed ? points[(index - 1 + count) % count] : null);
    const next = points[index + 1] || (subpath.closed ? points[(index + 1) % count] : null);
    const inVector = vectorFrom(point.backward || point, point);
    const outVector = vectorFrom(point, point.forward || point);
    const inLength = vectorLength(inVector);
    const outLength = vectorLength(outVector);
    const item = {
      subpath_index: subpathIndex,
      point_index: index,
      anchor: { x: point.x, y: point.y },
      in_length: Number(inLength.toFixed(3)),
      out_length: Number(outLength.toFixed(3)),
      flags: []
    };
    if (previous) {
      const prevLength = Math.max(pointDistance(previous, point), 0.001);
      const ratio = inLength / prevLength;
      item.in_ratio = Number(ratio.toFixed(3));
      const towardPrevious = dotProduct(vectorFrom(point, previous), vectorFrom(point, point.backward || point));
      if (inLength > 0.001 && towardPrevious <= 0) {
        item.flags.push("in_wrong_direction");
      }
      if (inLength > 0.001 && (ratio < minRatio || ratio > maxRatio)) {
        item.flags.push("in_ratio_out_of_range");
      }
    }
    if (next) {
      const nextLength = Math.max(pointDistance(point, next), 0.001);
      const ratio = outLength / nextLength;
      item.out_ratio = Number(ratio.toFixed(3));
      const towardNext = dotProduct(vectorFrom(point, next), vectorFrom(point, point.forward || point));
      if (outLength > 0.001 && towardNext <= 0) {
        item.flags.push("out_wrong_direction");
      }
      if (outLength > 0.001 && (ratio < minRatio || ratio > maxRatio)) {
        item.flags.push("out_ratio_out_of_range");
      }
    }
    if (point.kind === "smooth" || point.smooth === true) {
      if (!point.has_backward || !point.has_forward || inLength <= 0.001 || outLength <= 0.001) {
        item.flags.push("smooth_missing_handle");
      } else {
        const cross = Math.abs(crossProduct(inVector, outVector));
        const sinAngle = cross / Math.max(inLength * outLength, 0.001);
        const angle = Math.asin(Math.min(1, sinAngle)) * 180 / Math.PI;
        item.smooth_collinear_error = Number(angle.toFixed(2));
        if (angle > tolerance) {
          item.flags.push("not_smooth_collinear");
        }
      }
    } else if (point.kind === "corner" && (inLength > 0.001 || outLength > 0.001)) {
      item.intentional_corner = true;
    }
    if (item.flags.length) {
      warnings.push({
        subpath_index: subpathIndex,
        point_index: index,
        flags: item.flags.slice()
      });
    }
    metrics.push(item);
  });
  return {
    subpath_index: subpathIndex,
    closed: Boolean(subpath.closed),
    point_count: count,
    warning_count: warnings.length,
    warnings,
    metrics
  };
}

function cubicBezierPoint(p0, p1, p2, p3, t) {
  const mt = 1 - t;
  const mt2 = mt * mt;
  const t2 = t * t;
  return {
    x: mt2 * mt * p0.x + 3 * mt2 * t * p1.x + 3 * mt * t2 * p2.x + t2 * t * p3.x,
    y: mt2 * mt * p0.y + 3 * mt2 * t * p1.y + 3 * mt * t2 * p2.y + t2 * t * p3.y
  };
}

function orientation(a, b, c) {
  const value = (b.y - a.y) * (c.x - b.x) - (b.x - a.x) * (c.y - b.y);
  if (Math.abs(value) < 0.0001) {
    return 0;
  }
  return value > 0 ? 1 : 2;
}

function onSegment(a, b, c) {
  return b.x <= Math.max(a.x, c.x) + 0.0001 && b.x + 0.0001 >= Math.min(a.x, c.x) &&
    b.y <= Math.max(a.y, c.y) + 0.0001 && b.y + 0.0001 >= Math.min(a.y, c.y);
}

function segmentsIntersect(a1, a2, b1, b2) {
  const o1 = orientation(a1, a2, b1);
  const o2 = orientation(a1, a2, b2);
  const o3 = orientation(b1, b2, a1);
  const o4 = orientation(b1, b2, a2);
  if (o1 !== o2 && o3 !== o4) {
    return true;
  }
  return (o1 === 0 && onSegment(a1, b1, a2)) ||
    (o2 === 0 && onSegment(a1, b2, a2)) ||
    (o3 === 0 && onSegment(b1, a1, b2)) ||
    (o4 === 0 && onSegment(b1, a2, b2));
}

function detectBezierSelfIntersections(subpath, subpathIndex, options = {}) {
  const samplesPerCurve = Math.max(6, Math.min(48, Math.round(numberParam(options.samples_per_curve, 16, 4, 64))));
  const points = subpath.points || [];
  const curveCount = subpath.closed ? points.length : Math.max(0, points.length - 1);
  const segments = [];
  for (let curveIndex = 0; curveIndex < curveCount; curveIndex += 1) {
    const current = points[curveIndex];
    const next = points[(curveIndex + 1) % points.length];
    const p0 = current;
    const p1 = current.forward || current;
    const p2 = next.backward || next;
    const p3 = next;
    let previous = cubicBezierPoint(p0, p1, p2, p3, 0);
    for (let sampleIndex = 1; sampleIndex <= samplesPerCurve; sampleIndex += 1) {
      const currentSample = cubicBezierPoint(p0, p1, p2, p3, sampleIndex / samplesPerCurve);
      segments.push({ curve_index: curveIndex, sample_index: sampleIndex - 1, a: previous, b: currentSample });
      previous = currentSample;
    }
  }
  const warnings = [];
  const maxWarnings = Math.max(1, Math.min(16, Math.round(numberParam(options.max_loop_warnings, 6, 1, 64))));
  for (let i = 0; i < segments.length; i += 1) {
    for (let j = i + 1; j < segments.length; j += 1) {
      if (j - i <= 1) {
        continue;
      }
      if (subpath.closed && i === 0 && j === segments.length - 1) {
        continue;
      }
      if (segments[i].curve_index === segments[j].curve_index && Math.abs(segments[i].sample_index - segments[j].sample_index) <= 1) {
        continue;
      }
      if (segmentsIntersect(segments[i].a, segments[i].b, segments[j].a, segments[j].b)) {
        warnings.push({
          subpath_index: subpathIndex,
          point_index: null,
          flags: ["self_intersection"],
          segment_a: { curve_index: segments[i].curve_index, sample_index: segments[i].sample_index },
          segment_b: { curve_index: segments[j].curve_index, sample_index: segments[j].sample_index }
        });
        if (warnings.length >= maxWarnings) {
          return warnings;
        }
      }
    }
  }
  return warnings;
}

function auditBezierSubpaths(subpaths, options = {}) {
  const subpathAudits = subpaths.map((subpath, index) => auditBezierSubpath(subpath, index, options));
  subpathAudits.forEach((audit, index) => {
    const loopWarnings = detectBezierSelfIntersections(subpaths[index], index, options);
    if (loopWarnings.length) {
      audit.warnings = audit.warnings.concat(loopWarnings);
      audit.warning_count = audit.warnings.length;
      audit.loop_warning_count = loopWarnings.length;
    } else {
      audit.loop_warning_count = 0;
    }
  });
  const warnings = subpathAudits.reduce((items, audit) => items.concat(audit.warnings), []);
  const loopWarningCount = warnings.filter((warning) => Array.isArray(warning.flags) && warning.flags.indexOf("self_intersection") >= 0).length;
  return {
    status: warnings.length ? "warning" : "ok",
    subpath_count: subpaths.length,
    point_count: subpaths.reduce((total, subpath) => total + subpath.points.length, 0),
    warning_count: warnings.length,
    loop_warning_count: loopWarningCount,
    warnings,
    subpaths: subpathAudits
  };
}

function domPathPointKind(point) {
  const pointKind = constants.PointKind || {};
  const isSmooth = point && (point.kind === "smooth" || (point.kind == null && point.smooth === true));
  const key = isSmooth ? "SMOOTHPOINT" : "CORNERPOINT";
  const camelKey = isSmooth ? "smoothPoint" : "cornerPoint";
  return pointKind[key] || pointKind[camelKey] || (isSmooth ? "smoothPoint" : "cornerPoint");
}

function domShapeOperation(operation, preferXor) {
  const shapeOperation = constants.ShapeOperation || {};
  const normalized = preferXor ? "xor" : String(operation || "add").toLowerCase();
  const keyMap = {
    add: ["SHAPEADD", "shapeAdd", "ADD", "add"],
    subtract: ["SHAPESUBTRACT", "shapeSubtract", "SUBTRACT", "subtract"],
    intersect: ["SHAPEINTERSECT", "shapeIntersect", "INTERSECT", "intersect"],
    xor: ["SHAPEXOR", "shapeXOR", "XOR", "xor"]
  };
  const keys = keyMap[normalized] || keyMap.add;
  for (const key of keys) {
    if (shapeOperation[key] != null) {
      return shapeOperation[key];
    }
  }
  return preferXor ? "xor" : "add";
}

function domPointArray(point) {
  return [Number(point.x), Number(point.y)];
}

function createPlainDomPathPoint(point, options = {}) {
  const plain = {
    anchor: domPointArray(point),
    leftDirection: domPointArray(point.backward || point),
    rightDirection: domPointArray(point.forward || point)
  };
  if (options.includeKind !== false) {
    const kind = domPathPointKind(point);
    if (kind != null) {
      plain.kind = kind;
    }
  }
  return plain;
}

function createPlainDomSubPathInfo(subpath, options = {}) {
  const plain = {
    closed: Boolean(subpath.closed),
    entireSubPath: subpath.points.map((point) => createPlainDomPathPoint(point, options))
  };
  if (options.includeOperation !== false) {
    plain.operation = domShapeOperation(subpath.operation, options.preferXorOperation === true);
  }
  return plain;
}

function domPathPointDiagnostic(point) {
  if (!point) {
    return null;
  }
  const firstPoint = Array.isArray(point.entireSubPath) ? point.entireSubPath[0] : point;
  return {
    constructor_name: firstPoint && firstPoint.constructor ? firstPoint.constructor.name : null,
    keys: firstPoint && typeof firstPoint === "object" ? Object.keys(firstPoint) : [],
    anchor: firstPoint && firstPoint.anchor ? firstPoint.anchor : null,
    leftDirection: firstPoint && firstPoint.leftDirection ? firstPoint.leftDirection : null,
    rightDirection: firstPoint && firstPoint.rightDirection ? firstPoint.rightDirection : null,
    kind: firstPoint && firstPoint.kind != null ? String(firstPoint.kind) : null,
    kind_type: firstPoint && firstPoint.kind != null ? typeof firstPoint.kind : null
  };
}

function domPathCandidateDiagnostic(candidate) {
  const firstSubpath = candidate && candidate.subpaths && candidate.subpaths[0] ? candidate.subpaths[0] : null;
  return {
    implementation: candidate ? candidate.implementation : null,
    subpath_constructor_name: firstSubpath && firstSubpath.constructor ? firstSubpath.constructor.name : null,
    subpath_keys: firstSubpath && typeof firstSubpath === "object" ? Object.keys(firstSubpath) : [],
    closed: firstSubpath ? firstSubpath.closed : null,
    operation: firstSubpath && firstSubpath.operation != null ? String(firstSubpath.operation) : null,
    operation_type: firstSubpath && firstSubpath.operation != null ? typeof firstSubpath.operation : null,
    point_count: firstSubpath && Array.isArray(firstSubpath.entireSubPath) ? firstSubpath.entireSubPath.length : null,
    first_point: domPathPointDiagnostic(firstSubpath)
  };
}

function domPathSubpathCandidates(subpaths) {
  return [
    {
      implementation: "dom_pathitems_add_plain_with_kind",
      subpaths: subpaths.map((subpath) => createPlainDomSubPathInfo(subpath, { preferXorOperation: false }))
    },
    {
      implementation: "dom_pathitems_add_plain_no_operation_with_kind",
      subpaths: subpaths.map((subpath) => createPlainDomSubPathInfo(subpath, { includeOperation: false }))
    },
    {
      implementation: "dom_pathitems_add_plain_no_operation_no_kind",
      subpaths: subpaths.map((subpath) => createPlainDomSubPathInfo(subpath, { includeOperation: false, includeKind: false }))
    },
    {
      implementation: "dom_pathitems_add_plain_add_operation_no_kind",
      subpaths: subpaths.map((subpath) => createPlainDomSubPathInfo(subpath, { includeKind: false, preferXorOperation: false }))
    },
    {
      implementation: "dom_pathitems_add_plain_xor_operation_no_kind",
      subpaths: subpaths.map((subpath) => createPlainDomSubPathInfo(subpath, { includeKind: false, preferXorOperation: true }))
    }
  ];
}

function domPathPublicInfo(pathRef) {
  return {
    path_kind: "path_item",
    mode: "dom_pathitems_add",
    path_id: pathRef.path_id,
    path_name: pathRef.path_name,
    subpath_count: pathRef.subpath_count,
    point_count: pathRef.point_count,
    closed: pathRef.closed,
    fallback_used: false,
    implementation: pathRef.implementation || "dom_pathitems_add",
    kind_fallback_used: Boolean(pathRef.kind_fallback_used),
    rejected_candidate_count: Array.isArray(pathRef.rejected_candidate_attempts) ? pathRef.rejected_candidate_attempts.length : 0,
    rejected_candidate_attempts: Array.isArray(pathRef.rejected_candidate_attempts) ? pathRef.rejected_candidate_attempts : []
  };
}

async function createDomPathItemFromSubpaths(subpaths, params = {}) {
  const documentRef = app.activeDocument;
  if (!documentRef || !documentRef.pathItems || typeof documentRef.pathItems.add !== "function") {
    throw codedError(
      "path_dom_unavailable",
      "Photoshop document.pathItems.add is unavailable; reload Photoshop/UXP before creating native Bezier paths."
    );
  }
  const pathName = safeLayerName(params.path_name || params.name, "Native Bezier Path");
  let pathItem = null;
  const attempts = [];
  for (const candidate of domPathSubpathCandidates(subpaths)) {
    try {
      pathItem = await documentRef.pathItems.add(pathName, candidate.subpaths);
      return {
        path_item: pathItem,
        path_kind: "path_item",
        path_id: pathItem && pathItem.id != null ? pathItem.id : null,
        path_name: pathItem && pathItem.name ? pathItem.name : pathName,
        subpath_count: subpaths.length,
        point_count: subpaths.reduce((total, subpath) => total + subpath.points.length, 0),
        closed: subpaths.every((subpath) => subpath.closed),
        implementation: candidate.implementation,
        kind_fallback_used: candidate.implementation.indexOf("no_kind") >= 0,
        rejected_candidate_attempts: attempts.slice(),
        warnings: candidate.implementation.indexOf("no_kind") >= 0 ? ["dom_pathitems_add accepted no-kind fallback; point.kind was not written to Photoshop."] : []
      };
    } catch (error) {
      attempts.push({
        implementation: candidate.implementation,
        message: error && error.message ? error.message : String(error),
        name: error && error.name ? String(error.name) : null,
        number: error && error.number != null ? String(error.number) : null,
        stack: error && error.stack ? String(error.stack).slice(0, 1000) : null,
        diagnostic: domPathCandidateDiagnostic(candidate)
      });
    }
  }
  throw codedError(
    "path_dom_create_failed",
    `Photoshop rejected document.pathItems.add for all DOM PathItem variants: ${attempts.map((item) => item.implementation + " => " + item.message).join(" | ")}`,
    {
      attempts,
      has_path_items_add: Boolean(documentRef.pathItems && typeof documentRef.pathItems.add === "function"),
      class_route_available: false,
      class_route_removed: true,
      point_kind_keys: Object.keys(constants.PointKind || {}),
      shape_operation_keys: Object.keys(constants.ShapeOperation || {}),
      subpath_count: subpaths.length,
      point_count: subpaths.reduce((total, subpath) => total + subpath.points.length, 0)
    }
  );
}
async function makeWorkPathFromSelection(tolerance) {
  const value = numberParam(tolerance, 2, 0.5, 100);
  const descriptors = [
    {
      _obj: "make",
      _target: [{ _ref: "path" }],
      from: { _ref: "channel", _property: "selection" },
      tolerance: pixelUnit(value),
      _options: { dialogOptions: "dontDisplay" }
    },
    {
      _obj: "make",
      new: { _class: "path" },
      from: { _ref: "channel", _property: "selection" },
      tolerance: pixelUnit(value),
      _options: { dialogOptions: "dontDisplay" }
    },
    {
      _obj: "make",
      _target: [{ _ref: "path" }],
      from: { _ref: "selectionClass" },
      tolerance: pixelUnit(value),
      _options: { dialogOptions: "dontDisplay" }
    }
  ];
  let lastError = null;
  for (const descriptor of descriptors) {
    try {
      await playAction([descriptor]);
      return { tolerance: value, implementation: "selection_to_work_path" };
    } catch (error) {
      lastError = error;
    }
  }
  throw codedError(
    "path_create_failed",
    "Photoshop rejected Make Work Path from selection descriptors.",
    { message: lastError && lastError.message ? lastError.message : String(lastError) }
  );
}

async function createWorkPath(params, state) {
  const subpaths = normalizedPathSubpaths(params);
  const canSelectionPath = subpaths.every((subpath) => subpath.closed && subpath.operation === "add");
  const hasBezierHandles = subpaths.some((subpath) => subpath.points.some((point) =>
    Math.abs(point.backward.x - point.x) > 0.001 ||
    Math.abs(point.backward.y - point.y) > 0.001 ||
    Math.abs(point.forward.x - point.x) > 0.001 ||
    Math.abs(point.forward.y - point.y) > 0.001
  ));
  const requestedModeKey = String(params.path_mode || params.mode || "").toLowerCase().replace(/[\s-]+/g, "_");
  const requestedMode = requestedModeKey === "stable"
    ? "stable"
    : (requestedModeKey === "calibrated_bezier" || requestedModeKey === "bezier" || requestedModeKey === "direct" || requestedModeKey === "dom")
      ? "dom_path_item"
      : "auto";

  if (requestedMode === "stable" && (!canSelectionPath || hasBezierHandles)) {
    throw codedError(
      "path_dom_required",
      "path.create_work_path mode=stable only supports closed polygon-style subpaths without Bezier handles. Use path_mode=dom for native Bezier paths.",
      { mode: requestedMode, can_selection_path: canSelectionPath, has_bezier_handles: hasBezierHandles }
    );
  }

  const useSelectionWorkPath = canSelectionPath && params.direct !== true && requestedMode !== "dom_path_item" && !hasBezierHandles;
  if (useSelectionWorkPath) {
    await clearSelection();
    for (let index = 0; index < subpaths.length; index += 1) {
      const points = subpaths[index].points.map((point) => ({ x: point.x, y: point.y }));
      await selectPolygonPoints(points, { points, feather: params.feather }, state, index === 0 ? "replace" : "add", "path_selection_fallback_failed");
    }
    await makeWorkPathFromSelection(params.tolerance);
    if (state) {
      state.last_path_item_ref = null;
    }
    if (params.clear_selection !== false) {
      await clearSelection();
    }
    return {
      path_kind: "work_path",
      mode: requestedMode,
      subpath_count: subpaths.length,
      point_count: subpaths.reduce((total, subpath) => total + subpath.points.length, 0),
      closed: subpaths.every((subpath) => subpath.closed),
      fallback_used: true,
      implementation: "selection_to_work_path"
    };
  }

  const pathRef = await createDomPathItemFromSubpaths(subpaths, params);
  if (state) {
    state.last_path_item_ref = pathRef;
  }
  if (params.clear_selection !== false) {
    await clearSelection();
  }
  return domPathPublicInfo(pathRef);
}

async function createBezierWorkPath(params, state) {
  const subpaths = normalizedPathSubpaths(params);
  const closedOnly = params.closed_only !== false;
  if (closedOnly && !subpaths.every((subpath) => subpath.closed)) {
    throw codedError(
      "invalid_path_points",
      "path.bezier_work_path requires closed subpaths unless params.closed_only is false."
    );
  }
  const pathRef = await createDomPathItemFromSubpaths(subpaths, params);
  if (state) {
    state.last_path_item_ref = pathRef;
  }
  if (params.clear_selection !== false) {
    await clearSelection();
  }
  const info = domPathPublicInfo(pathRef);
  info.path_audit = auditBezierSubpaths(subpaths, params.audit || params);
  info.handle_mode = normalizedBezierHandleMode(params, params);
  return info;
}

function auditBezierPathHandles(params) {
  const subpaths = normalizedPathSubpaths(params);
  const audit = auditBezierSubpaths(subpaths, params.audit || params);
  return {
    path_audit: audit,
    handle_mode: normalizedBezierHandleMode(params, params),
    subpath_count: subpaths.length,
    point_count: subpaths.reduce((total, subpath) => total + subpath.points.length, 0)
  };
}

function rotatePointAround(point, center, angleRadians) {
  if (!angleRadians) {
    return { x: point.x, y: point.y };
  }
  const dx = point.x - center.x;
  const dy = point.y - center.y;
  const cos = Math.cos(angleRadians);
  const sin = Math.sin(angleRadians);
  return {
    x: center.x + dx * cos - dy * sin,
    y: center.y + dx * sin + dy * cos
  };
}

function createBezierEllipseSubpath(params) {
  const x = numberParam(params.x, 0, -100000, 100000);
  const y = numberParam(params.y, 0, -100000, 100000);
  const width = numberParam(params.width, params.diameter || 100, 1, 30000);
  const height = numberParam(params.height, params.diameter || 100, 1, 30000);
  const rotation = numberParam(params.rotation, 0, -3600, 3600);
  const angleRadians = rotation * Math.PI / 180;
  const kappa = 0.5522847498307936;
  const cx = x + width / 2;
  const cy = y + height / 2;
  const rx = width / 2;
  const ry = height / 2;
  const center = { x: cx, y: cy };
  const rawPoints = [
    {
      anchor: { x: cx, y: cy - ry },
      backward: { x: cx - kappa * rx, y: cy - ry },
      forward: { x: cx + kappa * rx, y: cy - ry }
    },
    {
      anchor: { x: cx + rx, y: cy },
      backward: { x: cx + rx, y: cy - kappa * ry },
      forward: { x: cx + rx, y: cy + kappa * ry }
    },
    {
      anchor: { x: cx, y: cy + ry },
      backward: { x: cx + kappa * rx, y: cy + ry },
      forward: { x: cx - kappa * rx, y: cy + ry }
    },
    {
      anchor: { x: cx - rx, y: cy },
      backward: { x: cx - rx, y: cy + kappa * ry },
      forward: { x: cx - rx, y: cy - kappa * ry }
    }
  ];
  return {
    closed: true,
    operation: params.operation || "add",
    points: rawPoints.map((item) => {
      const anchor = rotatePointAround(item.anchor, center, angleRadians);
      return {
        x: anchor.x,
        y: anchor.y,
        backward: rotatePointAround(item.backward, center, angleRadians),
        forward: rotatePointAround(item.forward, center, angleRadians),
        has_backward: true,
        has_forward: true,
        smooth: true,
        kind: "smooth"
      };
    }),
    ellipse: { x, y, width, height, rotation, kappa }
  };
}

async function createBezierEllipseShapeLayer(params, state) {
  const subpath = createBezierEllipseSubpath(params);
  const subpaths = [{ closed: subpath.closed, operation: subpath.operation, points: subpath.points }];
  const pathRef = await createDomPathItemFromSubpaths(subpaths, Object.assign({}, params, { path_name: params.path_name || params.name || "Bezier Ellipse Path" }));
  if (state) {
    state.last_path_item_ref = pathRef;
  }
  await pathToSelection(Object.assign({}, params, { operation: "replace", __path_item: pathRef }), state);
  const fillInfo = await createSolidColorLayerFromSelection(params, "Bezier Ellipse");
  fillInfo.path = domPathPublicInfo(pathRef);
  fillInfo.path_audit = auditBezierSubpaths(subpaths, params.audit || params);
  fillInfo.kappa = subpath.ellipse.kappa;
  fillInfo.rotation = subpath.ellipse.rotation;
  fillInfo.implementation = "dom_pathitems_add_bezier_ellipse_to_selection_fill_layer";
  return fillInfo;
}

async function createBezierFillShapeLayer(params, state) {
  const subpaths = normalizedPathSubpaths(params);
  const closedOnly = params.closed_only !== false;
  if (closedOnly && !subpaths.every((subpath) => subpath.closed)) {
    throw codedError(
      "invalid_path_points",
      "shape.bezier_fill requires closed subpaths unless params.closed_only is false."
    );
  }
  const pathRef = await createDomPathItemFromSubpaths(subpaths, params);
  if (state) {
    state.last_path_item_ref = pathRef;
  }
  await pathToSelection(Object.assign({}, params, { operation: "replace", __path_item: pathRef }), state);
  const fillInfo = await createSolidColorLayerFromSelection(params, "Native Bezier Fill");
  fillInfo.path = domPathPublicInfo(pathRef);
  fillInfo.path_audit = auditBezierSubpaths(subpaths, params.audit || params);
  fillInfo.handle_mode = normalizedBezierHandleMode(params, params);
  fillInfo.implementation = "dom_pathitems_add_to_selection_fill_layer";
  return fillInfo;
}

function pathItemDebugInfo(pathRef) {
  const pathItem = pathRef && pathRef.path_item;
  return {
    path_name: pathRef && pathRef.path_name ? pathRef.path_name : null,
    path_id: pathRef && pathRef.path_id != null ? pathRef.path_id : null,
    path_item_type: pathItem == null ? null : typeof pathItem,
    path_item_keys: pathItem && typeof pathItem === "object" ? Object.keys(pathItem) : [],
    has_make_selection: Boolean(pathItem && typeof pathItem.makeSelection === "function"),
    has_select: Boolean(pathItem && typeof pathItem.select === "function"),
    has_fill_path: Boolean(pathItem && typeof pathItem.fillPath === "function"),
    has_stroke_path: Boolean(pathItem && typeof pathItem.strokePath === "function")
  };
}

function namedPathSelectionDescriptors(pathRef, feather, operation, antiAlias) {
  const refs = [];
  if (pathRef && pathRef.path_id != null) {
    refs.push({ label: "id_object", ref: { _ref: "path", _id: pathRef.path_id } });
    refs.push({ label: "id_array", ref: [{ _ref: "path", _id: pathRef.path_id }] });
  }
  if (pathRef && pathRef.path_name) {
    refs.push({ label: "name_object", ref: { _ref: "path", _name: pathRef.path_name } });
    refs.push({ label: "name_array", ref: [{ _ref: "path", _name: pathRef.path_name }] });
  }
  refs.push({ label: "target_object", ref: { _ref: "path", _enum: "ordinal", _value: "targetEnum" } });
  refs.push({ label: "target_array", ref: [{ _ref: "path", _enum: "ordinal", _value: "targetEnum" }] });
  return refs.map((entry) => {
    const descriptor = {
      _obj: "set",
      _target: [{ _ref: "channel", _property: "selection" }],
      to: entry.ref,
      feather: pixelUnit(feather),
      antiAlias,
      _options: { dialogOptions: "dontDisplay" }
    };
    if (operation !== "replace") {
      descriptor.selectionModifier = {
        _enum: "selectionModifierType",
        _value: CHANNEL_SELECTION_MODIFIER_BY_OPERATION[operation]
      };
    }
    return { label: entry.label, descriptor };
  });
}

async function selectDomPathItemIfPossible(pathRef) {
  const pathItem = pathRef && pathRef.path_item;
  if (pathItem && typeof pathItem.select === "function") {
    try {
      await pathItem.select();
      return true;
    } catch (error) {
      return false;
    }
  }
  return false;
}

async function makeNamedPathSelection(pathRef, params, operation, feather) {
  const attempts = [];
  const antiAlias = params.anti_alias !== false;
  await selectDomPathItemIfPossible(pathRef);
  for (const entry of namedPathSelectionDescriptors(pathRef, feather, operation, antiAlias)) {
    try {
      await playAction([entry.descriptor]);
      if (await hasActiveSelection()) {
        return Object.assign(domPathPublicInfo(pathRef), {
          has_active_selection: true,
          operation,
          feather,
          selection_method: `batchplay_named_path_${entry.label}`
        });
      }
      attempts.push({ label: entry.label, message: "descriptor completed but selection is empty" });
    } catch (error) {
      attempts.push({ label: entry.label, message: error && error.message ? error.message : String(error) });
    }
  }
  throw codedError(
    "path_to_selection_failed",
    `Photoshop rejected converting native PathItem to selection: ${attempts.map((item) => item.label + " => " + item.message).join(" | ")}`,
    Object.assign(pathItemDebugInfo(pathRef), { attempts })
  );
}

async function makePathItemSelection(pathRef, params) {
  const pathItem = pathRef && pathRef.path_item;
  const feather = numberParam(params.feather, 0, 0, 500);
  const operation = normalizedSelectionOperation(params.operation, "replace");
  if (operation !== "replace") {
    await ensureSelectionOperationHasBase(operation);
  }
  if (!pathItem) {
    throw codedError("path_dom_unavailable", "The current native PathItem is unavailable.", pathItemDebugInfo(pathRef));
  }
  if (typeof pathItem.makeSelection === "function") {
    try {
      await pathItem.makeSelection(feather, params.anti_alias !== false, selectionTypeForOperation(operation));
    } catch (error) {
      if (operation === "replace") {
        try {
          await pathItem.makeSelection(feather, params.anti_alias !== false);
        } catch (fallbackError) {
          return makeNamedPathSelection(pathRef, params, operation, feather);
        }
      } else {
        return makeNamedPathSelection(pathRef, params, operation, feather);
      }
    }
    if (await hasActiveSelection()) {
      return Object.assign(domPathPublicInfo(pathRef), {
        has_active_selection: true,
        operation,
        feather,
        selection_method: "path_item_make_selection"
      });
    }
  }
  return makeNamedPathSelection(pathRef, params, operation, feather);
}

async function pathToSelection(params, state) {
  const pathRef = params.__path_item || params.path_item || (state && state.last_path_item_ref) || null;
  if (pathRef && pathRef.path_kind === "path_item") {
    return makePathItemSelection(pathRef, params);
  }

  const feather = numberParam(params.feather, 0, 0, 500);
  const operation = normalizedSelectionOperation(params.operation, "replace");
  if (operation !== "replace") {
    await ensureSelectionOperationHasBase(operation);
  }
  const descriptor = {
    _obj: "set",
    _target: [{ _ref: "channel", _property: "selection" }],
    to: { _ref: "path", _property: "workPath" },
    feather: pixelUnit(feather),
    antiAlias: params.anti_alias !== false,
    _options: { dialogOptions: "dontDisplay" }
  };
  if (operation !== "replace") {
    descriptor.selectionModifier = {
      _enum: "selectionModifierType",
      _value: CHANNEL_SELECTION_MODIFIER_BY_OPERATION[operation]
    };
  }
  try {
    await playAction([descriptor]);
  } catch (error) {
    throw codedError(
      "path_to_selection_failed",
      "Photoshop rejected converting the active work path to a selection.",
      { message: error && error.message ? error.message : String(error), operation }
    );
  }
  if (!(await hasActiveSelection())) {
    throw codedError("selection_empty", "path.to_selection did not create a usable selection.");
  }
  return {
    path_kind: "work_path",
    has_active_selection: true,
    operation,
    feather
  };
}
async function createEmptyPixelLayer(name) {
  const layerName = safeLayerName(name, "Codex Layer");
  await playAction([
    {
      _obj: "make",
      _target: [{ _ref: "layer" }],
      using: {
        _obj: "layer",
        name: layerName
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await setActiveLayerProperties({ name: layerName, opacity: 100, blendMode: "normal" });
  return { layer_id: await getActiveLayerId(), layer_name: layerName };
}

async function strokeActiveSelection(params) {
  const width = numberParam(params.width || params.stroke_width, 8, 0.5, 3000);
  const location = String(params.location || "center").toLowerCase();
  const locationMap = {
    center: "center",
    inside: "inside",
    outside: "outside"
  };
  const descriptor = {
    _obj: "stroke",
    width: pixelUnit(width),
    location: {
      _enum: "strokeLocation",
      _value: locationMap[location] || "center"
    },
    color: rgbColor(params.color || params.fill, [0, 0, 0]),
    opacity: percentUnit(numberParam(params.opacity, 100, 0, 100)),
    mode: {
      _enum: "blendMode",
      _value: blendModeValue(params.blend_mode || params.mode || "normal")
    },
    _options: { dialogOptions: "dontDisplay" }
  };
  try {
    await playAction([descriptor]);
  } catch (error) {
    throw codedError(
      "path_stroke_failed",
      "Photoshop rejected stroking the path-derived selection.",
      { message: error && error.message ? error.message : String(error), descriptor }
    );
  }
  return { width, location: locationMap[location] || "center" };
}

async function strokeWorkPath(params, state) {
  await pathToSelection(Object.assign({}, params, { operation: "replace" }), state);
  let layerId = params.target_layer_id || params.layer_id || null;
  let layerName = null;
  if (layerId != null) {
    await selectLayer(layerId);
  } else if (params.create_layer !== false) {
    const layer = await createEmptyPixelLayer(params.name || "Path Stroke");
    layerId = layer.layer_id;
    layerName = layer.layer_name;
  }
  const stroke = await strokeActiveSelection(params);
  if (params.clear_selection !== false) {
    await clearSelection();
  }
  return {
    layer_id: layerId || await getActiveLayerId(),
    layer_name: layerName,
    stroke,
    implementation: "work_path_to_selection_stroke"
  };
}

async function fillWorkPath(params, state) {
  await pathToSelection(Object.assign({}, params, { operation: "replace" }), state);
  const result = await createSolidColorLayerFromSelection(params, "Path Fill");
  result.implementation = "work_path_to_selection_fill_layer";
  return result;
}

async function applyDropShadowLayerStyle(layerId, params) {
  if (layerId != null) {
    await selectLayer(layerId);
  }
  const enabled = params.enabled !== false;
  const color = rgbColor(params.color || params.fill, [0, 0, 0]);
  const opacity = numberParam(params.opacity, 35, 0, 100);
  const distance = numberParam(params.distance, 12, 0, 30000);
  const size = numberParam(params.size || params.blur, 24, 0, 30000);
  const spread = numberParam(params.spread || params.choke, 0, 0, 100);
  const angle = numberParam(params.angle, 120, -360, 360);
  const mode = blendModeValue(params.blend_mode || params.mode || "multiply");
  await playAction([
    {
      _obj: "set",
      _target: [
        { _ref: "property", _property: "layerEffects" },
        activeLayerRef()
      ],
      to: {
        _obj: "layerEffects",
        scale: percentUnit(100),
        dropShadow: {
          _obj: "dropShadow",
          enabled,
          present: true,
          showInDialog: false,
          mode: { _enum: "blendMode", _value: mode },
          color,
          opacity: percentUnit(opacity),
          useGlobalAngle: params.use_global_angle === true,
          localLightingAngle: angleUnit(angle),
          distance: pixelUnit(distance),
          chokeMatte: percentUnit(spread),
          blur: pixelUnit(size),
          noise: percentUnit(numberParam(params.noise, 0, 0, 100)),
          antiAlias: params.anti_alias === true
        }
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  return {
    layer_id: await getActiveLayerId(),
    effect: {
      type: "drop_shadow",
      enabled,
      opacity,
      distance,
      size,
      spread,
      angle,
      blend_mode: mode
    }
  };
}

function layerEffectBase() {
  return {
    _obj: "layerEffects",
    scale: percentUnit(100)
  };
}

async function setLayerEffects(layerId, effects) {
  if (layerId != null) {
    await selectLayer(layerId);
  }
  await playAction([
    {
      _obj: "set",
      _target: [
        { _ref: "property", _property: "layerEffects" },
        activeLayerRef()
      ],
      to: Object.assign(layerEffectBase(), effects),
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  return getActiveLayerId();
}

async function applyOuterGlowLayerStyle(layerId, params) {
  const opacity = numberParam(params.opacity, 45, 0, 100);
  const size = numberParam(params.size || params.blur, 32, 0, 30000);
  const spread = numberParam(params.spread || params.choke, 0, 0, 100);
  const mode = blendModeValue(params.blend_mode || params.mode || "screen");
  const activeId = await setLayerEffects(layerId, {
    outerGlow: {
      _obj: "outerGlow",
      enabled: params.enabled !== false,
      present: true,
      showInDialog: false,
      mode: { _enum: "blendMode", _value: mode },
      color: rgbColor(params.color || params.fill, [255, 255, 255]),
      opacity: percentUnit(opacity),
      glowTechnique: { _enum: "matteTechnique", _value: "softMatte" },
      chokeMatte: percentUnit(spread),
      blur: pixelUnit(size),
      noise: percentUnit(numberParam(params.noise, 0, 0, 100)),
      antiAlias: params.anti_alias !== false,
      inputRange: percentUnit(numberParam(params.range, 50, 0, 100))
    }
  });
  return {
    layer_id: activeId,
    effect: { type: "outer_glow", opacity, size, spread, blend_mode: mode }
  };
}

async function applyStrokeLayerStyle(layerId, params) {
  const size = numberParam(params.size || params.width || params.stroke_width, 6, 1, 1000);
  const opacity = numberParam(params.opacity, 100, 0, 100);
  const mode = blendModeValue(params.blend_mode || params.mode || "normal");
  const position = String(params.position || params.location || "outside").toLowerCase();
  const positionMap = { inside: "inside", center: "center", outside: "outside" };
  const activeId = await setLayerEffects(layerId, {
    frameFX: {
      _obj: "frameFX",
      enabled: params.enabled !== false,
      present: true,
      showInDialog: false,
      style: { _enum: "frameStyle", _value: positionMap[position] || "outside" },
      paintType: { _enum: "frameFill", _value: "solidColor" },
      mode: { _enum: "blendMode", _value: mode },
      opacity: percentUnit(opacity),
      size: pixelUnit(size),
      color: rgbColor(params.color || params.fill, [255, 255, 255])
    }
  });
  return {
    layer_id: activeId,
    effect: { type: "stroke", size, opacity, position: positionMap[position] || "outside", blend_mode: mode }
  };
}

function gradientTypeValue(value) {
  const key = String(value || "linear").toLowerCase().replace(/[\s-]+/g, "_");
  const map = {
    linear: "linear",
    radial: "radial",
    angle: "angle",
    reflected: "reflected",
    diamond: "diamond"
  };
  return map[key] || "linear";
}

async function applyGradientOverlayLayerStyle(layerId, params) {
  const opacity = numberParam(params.opacity, 35, 0, 100);
  const mode = blendModeValue(params.blend_mode || params.mode || "soft_light");
  const angle = numberParam(params.angle, 90, -360, 360);
  const scale = numberParam(params.scale, 100, 1, 1000);
  const activeId = await setLayerEffects(layerId, {
    gradientFill: {
      _obj: "gradientFill",
      enabled: params.enabled !== false,
      present: true,
      showInDialog: false,
      mode: { _enum: "blendMode", _value: mode },
      opacity: percentUnit(opacity),
      gradient: gradientDescriptor(params, [
        { location: 0, color: { rgb: [0, 0, 0] } },
        { location: 100, color: { rgb: [255, 255, 255] } }
      ]),
      type: { _enum: "gradientType", _value: gradientTypeValue(params.style || params.type) },
      angle: angleUnit(angle),
      scale: percentUnit(scale),
      reverse: params.reverse === true,
      dither: params.dither !== false,
      align: params.align !== false
    }
  });
  return {
    layer_id: activeId,
    effect: { type: "gradient_overlay", opacity, angle, scale, blend_mode: mode }
  };
}

async function createGradientFillLayer(params) {
  const name = safeLayerName(params.name, "Gradient Fill");
  const angle = numberParam(params.angle, 90, -360, 360);
  const scale = numberParam(params.scale, 100, 1, 1000);
  await playAction([
    {
      _obj: "make",
      _target: [{ _ref: "contentLayer" }],
      using: {
        _obj: "contentLayer",
        name,
        type: {
          _obj: "gradientLayer",
          gradient: gradientDescriptor(params, [
            { location: 0, color: { rgb: [255, 255, 255] } },
            { location: 100, color: { rgb: [255, 255, 255] } }
          ]),
          type: { _enum: "gradientType", _value: gradientTypeValue(params.style || params.type) },
          angle: angleUnit(angle),
          scale: percentUnit(scale),
          reverse: params.reverse === true,
          dither: params.dither !== false,
          align: params.align !== false
        }
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await setActiveLayerProperties({
    name,
    opacity: numberParam(params.opacity, 100, 0, 100),
    blendMode: blendModeValue(params.blend_mode || "normal")
  });
  return {
    layer_id: await getActiveLayerId(),
    layer_name: name,
    gradient: {
      style: gradientTypeValue(params.style || params.type),
      angle,
      scale
    }
  };
}

function tonalPreset(value) {
  const key = String(value || "highlights").toLowerCase();
  if (["shadow", "shadows", "dark", "darks"].includes(key)) {
    return "shadows";
  }
  if (["midtone", "midtones", "mid", "mids"].includes(key)) {
    return "midtones";
  }
  return "highlights";
}

async function extractLuminosityRangeLayer(params) {
  const sourceLayerId = params.source_layer_id == null ? params.layer_id : params.source_layer_id;
  if (sourceLayerId != null) {
    await selectLayer(sourceLayerId);
  }
  const range = tonalPreset(params.range || params.preset);
  const name = safeLayerName(params.name, `${range[0].toUpperCase()}${range.slice(1)} Extract`);
  await duplicateActiveLayer(name);
  const layerId = await getActiveLayerId();
  await selectColorRange({ preset: range, fuzziness: numberParam(params.fuzziness, 60, 0, 200) });
  if (params.invert === true) {
    await invertSelection();
  }
  if (params.expand != null && Number(params.expand) > 0) {
    await expandSelection(params.expand);
  }
  if (params.feather != null && Number(params.feather) > 0) {
    await featherSelection(params.feather);
  }
  if (!(await hasActiveSelection())) {
    throw codedError("selection_empty", "layer.extract_luminosity_range did not create a usable tonal selection.");
  }
  await selectLayer(layerId);
  await makeLayerMaskFromSelection();
  await setActiveLayerProperties({
    name,
    opacity: numberParam(params.opacity, 100, 0, 100),
    blendMode: blendModeValue(params.blend_mode || "normal")
  });
  await clearSelection();
  return {
    layer_id: layerId,
    layer_name: name,
    range,
    mask_applied: true
  };
}

async function createBloomLayer(params) {
  const sourceLayerId = params.source_layer_id == null ? params.layer_id : params.source_layer_id;
  if (sourceLayerId != null) {
    await selectLayer(sourceLayerId);
  }
  const range = tonalPreset(params.range || params.preset);
  const name = safeLayerName(params.name, "Bloom Layer");
  await duplicateActiveLayer(name);
  const layerId = await getActiveLayerId();
  await selectColorRange({ preset: range, fuzziness: numberParam(params.fuzziness, 70, 0, 200) });
  if (params.feather != null && Number(params.feather) > 0) {
    await featherSelection(params.feather);
  }
  if (!(await hasActiveSelection())) {
    throw codedError("selection_empty", "effect.bloom_layer did not create a usable tonal selection.");
  }
  await selectLayer(layerId);
  await makeLayerMaskFromSelection();
  const radius = numberParam(params.blur_radius || params.radius, 36, 0.1, 500);
  await applyGaussianBlurToLayer(layerId, radius);
  await setActiveLayerProperties({
    name,
    opacity: numberParam(params.opacity, 28, 0, 100),
    blendMode: blendModeValue(params.blend_mode || "screen")
  });
  await clearSelection();
  return {
    layer_id: layerId,
    layer_name: name,
    range,
    blur_radius: radius
  };
}

function pointParam(value, fallback) {
  const source = value && typeof value === "object" ? value : {};
  return {
    x: numberParam(source.x, fallback.x, -100000, 100000),
    y: numberParam(source.y, fallback.y, -100000, 100000)
  };
}

function generatedLightRayPolygons(params, state) {
  if (Array.isArray(params.rays) && params.rays.length) {
    return params.rays.map((ray) => {
      if (Array.isArray(ray.points) && ray.points.length >= 3) {
        return ray.points;
      }
      const origin = pointParam(ray.origin || params.origin, { x: (state.width || 1600) / 2, y: 0 });
      const end = pointParam(ray.end || ray.target, { x: origin.x, y: (state.height || 1600) * 0.8 });
      const width = numberParam(ray.width || params.width, 120, 1, 30000);
      const angle = Math.atan2(end.y - origin.y, end.x - origin.x) + Math.PI / 2;
      const dx = Math.cos(angle) * width / 2;
      const dy = Math.sin(angle) * width / 2;
      return [origin, { x: end.x + dx, y: end.y + dy }, { x: end.x - dx, y: end.y - dy }];
    });
  }

  const width = Math.max(1, Number(state && state.width || 1600));
  const height = Math.max(1, Number(state && state.height || 1600));
  const origin = pointParam(params.origin, { x: width * 0.18, y: height * 0.02 });
  const count = Math.round(numberParam(params.count, 5, 1, 24));
  const length = numberParam(params.length, height * 0.9, 1, 100000);
  const spread = numberParam(params.spread, 45, 1, 180) * Math.PI / 180;
  const baseAngle = numberParam(params.angle, 68, -360, 360) * Math.PI / 180;
  const rayWidth = numberParam(params.width, Math.max(width, height) * 0.08, 1, 30000);
  const polygons = [];
  for (let index = 0; index < count; index += 1) {
    const t = count <= 1 ? 0.5 : index / (count - 1);
    const angle = baseAngle - spread / 2 + spread * t;
    const end = {
      x: origin.x + Math.cos(angle) * length,
      y: origin.y + Math.sin(angle) * length
    };
    const perp = angle + Math.PI / 2;
    const taper = rayWidth * (0.65 + 0.35 * Math.sin((index + 1) * 1.7));
    const dx = Math.cos(perp) * taper / 2;
    const dy = Math.sin(perp) * taper / 2;
    polygons.push([origin, { x: end.x + dx, y: end.y + dy }, { x: end.x - dx, y: end.y - dy }]);
  }
  return polygons;
}

async function createLightRays(params, state) {
  const polygons = generatedLightRayPolygons(params, state);
  const layerIds = [];
  const baseName = safeLayerName(params.name, "Light Rays");
  for (let index = 0; index < polygons.length; index += 1) {
    const opacity = numberParam(params.opacity, 18, 0, 100) * (1 - index / Math.max(1, polygons.length) * 0.28);
    const result = await createPolygonShapeLayer({
      points: polygons[index],
      name: `${baseName} ${index + 1}`,
      color: params.color || params.fill || { rgb: [255, 248, 220] },
      opacity,
      blend_mode: params.blend_mode || "screen",
      feather: params.feather
    }, state);
    layerIds.push(result.layer_id);
    const blur = numberParam(params.blur_radius || params.radius, 18, 0, 500);
    if (blur > 0.1) {
      await selectLayer(result.layer_id);
      try {
        await rasterizeActiveLayerForRetouch();
      } catch (error) {
      }
      await applyGaussianBlurToLayer(result.layer_id, blur);
    }
  }
  await selectLayerIds(layerIds);
  const groupName = safeLayerName(params.group_name || params.name, "Codex Light Rays");
  const groupId = await groupSelectedLayers(groupName);
  return {
    group_id: groupId,
    group_name: groupName,
    ray_count: layerIds.length,
    layer_ids: layerIds
  };
}

async function copyActiveSelectionToNewLayer() {
  const copyDescriptors = [
    { _obj: "copyEvent", _options: { dialogOptions: "dontDisplay" } },
    { _obj: "copy", _options: { dialogOptions: "dontDisplay" } }
  ];
  let copied = false;
  let lastError = null;
  for (const descriptor of copyDescriptors) {
    try {
      await playAction([descriptor]);
      copied = true;
      break;
    } catch (error) {
      lastError = error;
    }
  }
  if (!copied) {
    throw codedError("clone_patch_failed", "Photoshop rejected copying the source patch selection.", {
      message: lastError && lastError.message ? lastError.message : String(lastError)
    });
  }
  await playAction([{ _obj: "paste", _options: { dialogOptions: "dontDisplay" } }]);
  return getActiveLayerId();
}

function patchPointBounds(value, fallbackRadius, state) {
  const source = value && typeof value === "object" ? value : {};
  return normalizedEllipseBoundsFromCenter({
    x: source.x,
    y: source.y,
    radius: source.radius == null ? fallbackRadius : source.radius,
    width: source.width,
    height: source.height
  }, state);
}

async function retouchClonePatch(params, state) {
  const sourceLayerId = params.source_layer_id == null ? params.layer_id : params.source_layer_id;
  const patches = Array.isArray(params.patches) && params.patches.length
    ? params.patches
    : [{ source: params.source, target: params.target, radius: params.radius, feather: params.feather }];
  const fallbackRadius = numberParam(params.radius, 24, 1, 500);
  const feather = numberParam(params.feather, 2, 0, 200);
  const layerIds = [];
  for (let index = 0; index < patches.length; index += 1) {
    const patch = patches[index] || {};
    if (!patch.source || !patch.target) {
      throw codedError("invalid_clone_patch", "Each clone patch requires source and target points.", { index });
    }
    if (sourceLayerId != null) {
      await selectLayer(sourceLayerId);
    }
    const sourceBounds = patchPointBounds(patch.source, patch.radius || fallbackRadius, state);
    const targetBounds = patchPointBounds(patch.target, patch.radius || fallbackRadius, state);
    await selectEllipseReplace(sourceBounds, patch.feather == null ? feather : patch.feather);
    if (!(await hasActiveSelection())) {
      throw codedError("selection_empty", "Clone patch source selection is empty.", { index });
    }
    const pastedLayerId = await copyActiveSelectionToNewLayer();
    const patchName = safeLayerName(patch.name || params.name, `Clone Patch ${index + 1}`);
    await setActiveLayerProperties({
      name: patchName,
      opacity: numberParam(patch.opacity == null ? params.opacity : patch.opacity, 100, 0, 100),
      blendMode: blendModeValue(patch.blend_mode || params.blend_mode || "normal")
    });
    const pastedBounds = await getLayerBoundsById(pastedLayerId);
    await transformLayerById(pastedLayerId, {
      offset_x: targetBounds.center_x - pastedBounds.centerX,
      offset_y: targetBounds.center_y - pastedBounds.centerY
    });
    layerIds.push(await getActiveLayerId());
  }
  await clearSelection();
  let groupId = null;
  let groupName = null;
  if (layerIds.length > 1 || params.group !== false) {
    await selectLayerIds(layerIds);
    groupName = safeLayerName(params.group_name || params.name, "Codex Clone Patch");
    groupId = await groupSelectedLayers(groupName);
  }
  return {
    layer_ids: layerIds,
    patch_count: layerIds.length,
    group_id: groupId,
    group_name: groupName
  };
}

function adjustmentDescriptorForOperation(op) {
  const params = op.params || {};
  if (op.op === "adjust_exposure") {
    return {
      _obj: "exposure",
      exposure: numberParam(params.exposure, 0, -5, 5),
      offset: numberParam(params.offset, 0, -1, 1),
      gammaCorrection: numberParam(params.gamma, 1, 0.01, 9.99)
    };
  }

  if (op.op === "adjust_vibrance") {
    return {
      _obj: "vibrance",
      vibrance: Math.round(numberParam(params.vibrance, 0, -100, 100)),
      saturation: Math.round(numberParam(params.saturation, 0, -100, 100))
    };
  }

  if (op.op === "adjust_color_balance") {
    return {
      _obj: "colorBalance",
      shadowLevels: colorBalanceTriplet(params.shadows),
      midtoneLevels: colorBalanceTriplet(params.midtones),
      highlightLevels: colorBalanceTriplet(params.highlights),
      preserveLuminosity: params.preserve_luminosity !== false
    };
  }

  if (op.op === "adjust_hue_saturation") {
    const adjustment = {
      _obj: "hueSatAdjustmentV2",
      hue: Math.round(numberParam(params.hue, 0, -180, 180)),
      saturation: Math.round(numberParam(params.saturation, 0, -100, 100)),
      lightness: Math.round(numberParam(params.lightness, 0, -100, 100))
    };
    const rangeDescriptor = hueSaturationRangeDescriptor(params.range);
    if (rangeDescriptor) {
      Object.assign(adjustment, rangeDescriptor);
    }
    return {
      _obj: "hueSaturation",
      adjustment: [adjustment]
    };
  }

  if (op.op === "adjust_curves") {
    return curvesAdjustmentDescriptor(params);
  }

  if (op.op === "adjust_levels") {
    return levelsAdjustmentDescriptor(params);
  }

  if (op.op === "adjust_selective_color") {
    return selectiveColorDescriptor(params);
  }

  if (op.op === "adjust_gradient_map") {
    return gradientMapDescriptor(params);
  }

  if (op.op === "adjust_color_lookup") {
    return colorLookupDescriptor(params);
  }

  throw new Error(`Unsupported apply op: ${op.op}`);
}

function cameraRawParam(params, groupName, key) {
  const group = params && params[groupName] && typeof params[groupName] === "object"
    ? params[groupName]
    : null;
  if (group && group[key] != null) {
    return group[key];
  }
  return params ? params[key] : undefined;
}

function setCameraRawNumber(descriptor, descriptorKey, value, fallback, min, max, roundValue) {
  if (value == null) {
    return;
  }
  const parsed = numberParam(value, fallback, min, max);
  descriptor[descriptorKey] = roundValue ? Math.round(parsed) : parsed;
}

function cameraRawDescriptorForOperation(op) {
  const params = op.params || {};
  const descriptor = {
    _obj: "Adobe Camera Raw Filter",
    CrVe: 167772160,
    PrVN: 6,
    PrVe: 184549376,
    _options: { dialogOptions: "dontDisplay" }
  };

  setCameraRawNumber(descriptor, "Temp", cameraRawParam(params, "basic", "temperature"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Tint", cameraRawParam(params, "basic", "tint"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Exposure2012", cameraRawParam(params, "basic", "exposure"), 0, -5, 5, false);
  setCameraRawNumber(descriptor, "Contrast2012", cameraRawParam(params, "basic", "contrast"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Highlights2012", cameraRawParam(params, "basic", "highlights"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Shadows2012", cameraRawParam(params, "basic", "shadows"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Whites2012", cameraRawParam(params, "basic", "whites"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Blacks2012", cameraRawParam(params, "basic", "blacks"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Vibrance", cameraRawParam(params, "color", "vibrance"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Saturation", cameraRawParam(params, "color", "saturation"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Texture", cameraRawParam(params, "presence", "texture"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Clarity2012", cameraRawParam(params, "presence", "clarity"), 0, -100, 100, true);
  setCameraRawNumber(descriptor, "Dehaze", cameraRawParam(params, "presence", "dehaze"), 0, -100, 100, true);
  setCameraRawNumber(
    descriptor,
    "LuminanceSmoothing",
    cameraRawParam(params, "detail", "luminance_noise_reduction"),
    0,
    0,
    100,
    true
  );
  setCameraRawNumber(
    descriptor,
    "ColorNoiseReduction",
    cameraRawParam(params, "detail", "color_noise_reduction"),
    0,
    0,
    100,
    true
  );
  setCameraRawNumber(descriptor, "Sharpness", cameraRawParam(params, "detail", "sharpening"), 0, 0, 150, true);

  return descriptor;
}

function acrAiMaskSpec(target) {
  const spec = target && target.acr_ai_mask && typeof target.acr_ai_mask === "object"
    ? target.acr_ai_mask
    : {};
  const engine = String(spec.engine || "camera_raw_internal");
  const maskType = String(spec.mask_type || "");
  if (!ACR_AI_MASK_INTERNAL_TYPES.has(maskType)) {
    throw codedError(
      "unsupported_acr_ai_mask_type",
      `Unsupported ACR AI mask type: ${maskType}`,
      { mask_type: maskType }
    );
  }
  return Object.assign({}, spec, { engine, mask_type: maskType });
}

async function assertCameraRawInternalAiMaskAvailable(spec) {
  throw codedError(
    "acr_ai_mask_internal_unavailable",
    "Camera Raw internal AI masks are not exposed through a stable UXP batchPlay descriptor in this build.",
    {
      requested_engine: spec.engine,
      mask_type: spec.mask_type,
      parts: spec.parts || null,
      combine: spec.combine || null,
      hint: "Use engine=photoshop_selection_fallback for subject/background/sky, or provide a calibrated internal descriptor in a future build."
    }
  );
}

async function selectPhotoshopFallbackAiMask(spec) {
  const maskType = spec.mask_type;
  if (!ACR_SELECTION_FALLBACK_TYPES.has(maskType)) {
    throw codedError(
      "acr_ai_mask_fallback_unsupported",
      "photoshop_selection_fallback supports only subject, background, and sky in this build.",
      {
        mask_type: maskType,
        supported_mask_types: Array.from(ACR_SELECTION_FALLBACK_TYPES)
      }
    );
  }

  if (maskType === "sky") {
    await selectSky();
  } else {
    await selectSubject();
    if (maskType === "background") {
      await invertSelection();
    }
  }

  if (!(await hasActiveSelection())) {
    throw codedError(
      "acr_ai_mask_fallback_failed",
      `Photoshop fallback selection did not create a usable ${maskType} selection.`
    );
  }

  await featherSelection(spec.feather);
  if (spec.invert === true) {
    await invertSelection();
  }
}

async function applyPhotoshopSelectionFallbackCameraRaw(op, index, baseActiveLayerId, state, jobId, spec) {
  const target = op.target || {};
  const layer = op.layer || {};
  const layerName = safeLayerName(layer.name, `Codex ACR Mask - ${jobId || `op-${index + 1}`}`);
  const opacity = numberParam(layer.opacity, 100, 0, 100);
  const blendMode = blendModeValue(layer.blend_mode);
  const targetLayerId = target.layer_id == null ? baseActiveLayerId : target.layer_id;

  if (targetLayerId == null) {
    throw codedError("no_target_layer", "acr_ai_mask camera_raw_filter requires an active layer or target.layer_id.");
  }
  if (Array.isArray(spec.combine) && spec.combine.length > 0) {
    throw codedError(
      "acr_ai_mask_combine_unavailable",
      "photoshop_selection_fallback does not support add/subtract/intersect yet.",
      { combine_count: spec.combine.length }
    );
  }

  const targetDescriptor = await assertCameraRawTargetLayer(targetLayerId);
  await selectLayer(targetLayerId);
  await duplicateActiveLayer(layerName);
  await convertActiveLayerToSmartObject();
  await playAction([cameraRawDescriptorForOperation(op)]);
  await selectPhotoshopFallbackAiMask(spec);
  await makeLayerMaskFromSelection();
  await setActiveLayerProperties({ name: layerName, opacity, blendMode });
  const layerId = await getActiveLayerId();
  await clearSelection();

  return {
    op: op.op,
    layer_name: layerName,
    layer_id: layerId,
    opacity,
    blend_mode: blendMode,
    target_type: "acr_ai_mask",
    target_layer_id: targetDescriptor.layerID == null ? targetDescriptor.id || targetLayerId : targetDescriptor.layerID,
    target_layer_name: targetDescriptor.name || null,
    smart_object: true,
    smart_filter: "camera_raw_filter",
    acr_ai_mask_engine: "photoshop_selection_fallback",
    acr_ai_mask_type: spec.mask_type,
    acr_ai_mask_note: "ACR params were applied as a smart filter and constrained by a Photoshop AI-generated layer mask."
  };
}

function defaultLayerName(op, index) {
  const names = {
    adjust_exposure: "Exposure",
    adjust_vibrance: "Vibrance",
    adjust_color_balance: "Color Balance",
    adjust_hue_saturation: "Hue Saturation",
    adjust_curves: "Curves",
    adjust_levels: "Levels",
    adjust_selective_color: "Selective Color",
    adjust_gradient_map: "Gradient Map",
    adjust_color_lookup: "Color Lookup",
    camera_raw_filter: "Camera Raw"
  };
  return `Codex - ${names[op.op] || `Adjustment ${index + 1}`}`;
}

async function createAdjustmentLayer(op, index) {
  const layer = op.layer || {};
  const layerName = safeLayerName(layer.name, defaultLayerName(op, index));
  const opacity = numberParam(layer.opacity, 100, 0, 100);
  const blendMode = blendModeValue(layer.blend_mode);
  const adjustment = adjustmentDescriptorForOperation(op);

  await playAction([
    {
      _obj: "make",
      _target: [{ _ref: "contentLayer" }],
      using: {
        _obj: "contentLayer",
        name: layerName,
        type: adjustment
      },
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
  await setActiveLayerProperties({ name: layerName, opacity, blendMode });
  const layerId = await getActiveLayerId();
  return {
    op: op.op,
    layer_name: layerName,
    layer_id: layerId,
    opacity,
    blend_mode: blendMode
  };
}

async function applyCameraRawFilter(op, index, baseActiveLayerId, state, jobId) {
  const target = op.target || {};
  const layer = op.layer || {};
  if (target.type === "acr_ai_mask") {
    const spec = acrAiMaskSpec(target);
    if (spec.engine === "photoshop_selection_fallback") {
      return applyPhotoshopSelectionFallbackCameraRaw(op, index, baseActiveLayerId, state, jobId, spec);
    }
    await assertCameraRawInternalAiMaskAvailable(spec);
  }
  if (target.type && target.type !== "global") {
    throw codedError(
      "unsupported_camera_raw_target",
      "camera_raw_filter supports target.type=global or target.type=acr_ai_mask."
    );
  }
  const layerName = safeLayerName(layer.name, `Codex ACR - ${jobId || `op-${index + 1}`}`);
  const opacity = numberParam(layer.opacity, 100, 0, 100);
  const blendMode = blendModeValue(layer.blend_mode);
  const targetLayerId = target.layer_id == null ? baseActiveLayerId : target.layer_id;

  if (targetLayerId == null) {
    throw codedError("no_target_layer", "camera_raw_filter requires an active layer or target.layer_id.");
  }

  const targetDescriptor = await assertCameraRawTargetLayer(targetLayerId);
  await selectLayer(targetLayerId);
  await duplicateActiveLayer(layerName);
  await convertActiveLayerToSmartObject();
  await playAction([cameraRawDescriptorForOperation(op)]);
  await setActiveLayerProperties({ name: layerName, opacity, blendMode });
  const layerId = await getActiveLayerId();
  await clearSelection();

  return {
    op: op.op,
    layer_name: layerName,
    layer_id: layerId,
    opacity,
    blend_mode: blendMode,
    target_type: target.type || "global",
    target_layer_id: targetDescriptor.layerID == null ? targetDescriptor.id || targetLayerId : targetDescriptor.layerID,
    target_layer_name: targetDescriptor.name || null,
    smart_object: true,
    smart_filter: "camera_raw_filter"
  };
}

async function applyAdjustmentOperation(op, index, state) {
  const target = op.target || {};
  const maskInfo = await prepareSelectionMask(target, state);
  const applied = await createAdjustmentLayer(op, index);
  await clearSelection();
  applied.target_type = target.type || "global";
  applied.mask_source = maskInfo ? maskInfo.source : null;
  applied.mask_operation = maskInfo ? maskInfo.operation : null;
  applied.mask_label = maskInfo ? maskInfo.label : null;
  applied.mask_point_count = maskInfo ? maskInfo.point_count : null;
  applied.mask_feather = maskInfo ? maskInfo.feather : null;
  applied.mask_invert = maskInfo ? maskInfo.invert : false;
  applied.mask_item_count = maskInfo && maskInfo.item_count ? maskInfo.item_count : null;
  applied.mask_items = maskInfo && maskInfo.items ? maskInfo.items : null;
  return applied;
}

async function applyOperation(op, index, baseActiveLayerId, state, jobId) {
  if (op.op === "camera_raw_filter") {
    return applyCameraRawFilter(op, index, baseActiveLayerId, state, jobId);
  }
  return applyAdjustmentOperation(op, index, state);
}

async function appendReviewPacket(job, plan, result, warnings) {
  const review = plan.review || {};
  if (review.export_global !== false) {
    try {
      const preview = await exportPreview({
        job_id: job.job_id,
        payload: {
          format: "jpeg",
          max_side: clampNumber(review.max_side, 256, 4096, 1600),
          quality: 8
        }
      });
      if (preview.status === "ok" && preview.global_preview) {
        result.global_preview = preview.global_preview;
      } else if (preview.error) {
        warnings.push(`global review export failed: ${preview.error.message}`);
      }
    } catch (error) {
      warnings.push(`global review export failed: ${error && error.message ? error.message : error}`);
    }
  }

  if (Array.isArray(review.regions) && review.regions.length > 0) {
    try {
      const regions = await exportRegions({
        job_id: job.job_id,
        payload: {
          regions: review.regions,
          format: "jpeg",
          max_side: clampNumber(review.max_side, 128, 2048, DEFAULT_REGION_MAX_SIDE),
          quality: 8,
          upscale_small_regions: true
        }
      });
      if (regions.status === "ok" && regions.regions) {
        result.regions = regions.regions;
      } else if (regions.error) {
        warnings.push(`region review export failed: ${regions.error.message}`);
      }
    } catch (error) {
      warnings.push(`region review export failed: ${error && error.message ? error.message : error}`);
    }
  }
}

async function applyPlan(job) {
  const payload = job.payload || {};
  const plan = payload.plan || {};
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }

  const ops = Array.isArray(plan.ops) ? plan.ops : [];
  if (ops.length === 0) {
    return errorResult(job, "invalid_plan", "plan.ops must contain at least one operation.");
  }

  const workflowId = payload.workflow_id ? safeIdentifier(payload.workflow_id, "workflow") : null;
  const stageId = payload.stage_id ? safeIdentifier(payload.stage_id, "stage") : null;
  const historyName = workflowId && stageId
    ? `Codex Agent - ${workflowId} - ${stageId} - ${job.job_id}`
    : `Codex Agent - ${job.job_id}`;
  const warnings = [];
  const appliedOps = [];
  let createdGroupId = null;

  try {
    await core.executeAsModal(async (executionContext) => {
      const doc = app.activeDocument;
      let suspensionId = null;
      try {
        if (doc && doc.id != null && (!plan.safety || plan.safety.create_history_state !== false)) {
          suspensionId = await executionContext.hostControl.suspendHistory({
            documentID: doc.id,
            name: historyName
          });
        }
      } catch (error) {
        warnings.push(`history suspension failed: ${error && error.message ? error.message : error}`);
      }

      try {
        if (executionContext.isCancelled) {
          throw new Error("User cancelled before applying the plan.");
        }
        const baseActiveLayerId = await getActiveLayerId();
        for (let index = 0; index < ops.length; index += 1) {
          if (executionContext.isCancelled) {
            throw new Error("User cancelled while applying the plan.");
          }
          const applied = await applyOperation(ops[index], index, baseActiveLayerId, state, job.job_id);
          appliedOps.push(applied);
        }
        const createdLayerIds = appliedOps.map((op) => op.layer_id).filter((value) => value != null);
        if (createdLayerIds.length > 0) {
          await selectLayerIds(createdLayerIds);
          createdGroupId = await groupSelectedLayers(historyName);
        } else {
          await createLayerGroup(historyName);
          createdGroupId = await getActiveLayerId();
        }
        if (suspensionId) {
          suspensionId.finalName = historyName;
          await executionContext.hostControl.resumeHistory(suspensionId, true);
          suspensionId = null;
        }
      } catch (error) {
        if (suspensionId) {
          try {
            await executionContext.hostControl.resumeHistory(suspensionId, false);
          } catch (resumeError) {
            warnings.push(`history rollback failed: ${resumeError && resumeError.message ? resumeError.message : resumeError}`);
          }
        }
        throw error;
      }
    }, { commandName: historyName, timeOut: 30000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "apply_failed";
    const details = { warnings };
    if (error && error.details !== undefined) {
      details.details = error.details;
    }
    return errorResult(job, code, message, details);
  }

  const afterState = await readDocumentState(false);
  runtime.lastAgentEdit = {
    job_id: job.job_id,
    history_name: historyName,
    document_name: afterState.name || state.name,
    active_layer_name: afterState.active_layer_name || null,
    revision: afterState.revision || null,
    completed_at: new Date().toISOString()
  };

  const result = {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    document: afterState,
    applied_ops: appliedOps.map((op) => `${op.op}:${op.layer_name}`),
    agent_group_name: historyName,
    agent_group_id: createdGroupId,
    workflow_id: payload.workflow_id || (plan.metadata && plan.metadata.workflow_id) || null,
    stage_id: payload.stage_id || (plan.metadata && plan.metadata.stage_id) || null,
    stage_group_name: historyName,
    warnings
  };
  await appendReviewPacket(job, plan, result, warnings);
  return result;
}

async function currentHistoryStateName() {
  try {
    const result = await playAction([
      {
        _obj: "get",
        _target: [{ _ref: "historyState", _enum: "ordinal", _value: "targetEnum" }],
        _options: { dialogOptions: "dontDisplay" }
      }
    ]);
    return result && result[0] ? result[0].name || result[0].title || null : null;
  } catch (error) {
    return null;
  }
}

async function recoverAgentEditFromBackend(payload, prefix) {
  const jobId = payload && payload.job_id ? String(payload.job_id) : "";
  if (!jobId) {
    return null;
  }
  try {
    const response = await getJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    const record = response && response.job ? response.job : null;
    const result = record && record.result ? record.result : null;
    const document = result && result.document ? result.document : {};
    if (!record || record.job_type !== "apply_plan" || record.status !== "done" || !result) {
      return null;
    }
    return {
      job_id: jobId,
      history_name: result.agent_group_name || `${prefix} - ${jobId}`,
      document_name: document.name || null,
      active_layer_name: document.active_layer_name || null,
      revision: document.revision || null,
      recovered: true
    };
  } catch (error) {
    return null;
  }
}

async function undoLastAgentEdit(job) {
  const payload = job.payload || {};
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }

  const prefix = String(payload.history_name_prefix || "Codex Agent");
  const lastEdit = runtime.lastAgentEdit || await recoverAgentEditFromBackend(payload, prefix);
  if (!lastEdit) {
    return errorResult(
      job,
      "no_confirmed_agent_edit",
      "No confirmed agent edit is available in this plugin session. Retry with a specific apply job_id after reloading the plugin."
    );
  }
  if (payload.job_id && payload.job_id !== lastEdit.job_id) {
    return errorResult(job, "job_mismatch", `Latest agent edit is ${lastEdit.job_id}, not ${payload.job_id}.`);
  }
  if (!lastEdit.history_name || !lastEdit.history_name.startsWith(prefix)) {
    return errorResult(job, "history_prefix_mismatch", "Latest agent edit does not match the requested history prefix.");
  }
  if (lastEdit.document_name && state.name && state.name !== lastEdit.document_name) {
    return errorResult(
      job,
      "document_mismatch",
      "The active document does not match the latest confirmed agent edit.",
      { active_document: state.name, latest_agent_edit: lastEdit }
    );
  }
  if (lastEdit.active_layer_name && state.active_layer_name !== lastEdit.active_layer_name) {
    return errorResult(
      job,
      "cannot_confirm_latest_agent_layer",
      "The active layer no longer matches the latest confirmed agent edit; refusing to undo blindly.",
      { active_layer_name: state.active_layer_name, latest_agent_edit: lastEdit }
    );
  }

  try {
    await core.executeAsModal(async () => {
      await playAction([{ _obj: "undo", _options: { dialogOptions: "dontDisplay" } }]);
    }, { commandName: `Undo ${lastEdit.history_name}`, timeOut: 5000 });
  } catch (error) {
    return errorResult(job, "undo_failed", String(error && error.message ? error.message : error));
  }

  runtime.lastAgentEdit = null;
  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    undone_job_id: lastEdit.job_id,
    document: await readDocumentState(false),
    warnings: []
  };
}

async function makeSelection(job) {
  const payload = job.payload || {};
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }

  const mask = payload.selection_mask || payload.mask || null;
  if (!mask || typeof mask !== "object") {
    return errorResult(job, "invalid_selection_mask", "payload.selection_mask must be an object.");
  }

  let selectionInfo = null;
  try {
    await core.executeAsModal(async () => {
      selectionInfo = await prepareSelectionMask(
        {
          type: "selection_mask",
          selection_mask: mask
        },
        state
      );
    }, { commandName: `Codex Selection - ${job.job_id}`, timeOut: 30000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "make_selection_failed";
    return errorResult(job, code, message, error && error.details !== undefined ? error.details : null);
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    document: await readDocumentState(false),
    selection: selectionInfo,
    warnings: []
  };
}

async function selectionCommand(job) {
  const payload = job.payload || {};
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }

  const actionName = String(payload.action || "");
  let resultInfo = null;
  try {
    await core.executeAsModal(async () => {
      if (actionName === "select_subject") {
        await selectSubject();
        await featherSelection(payload.feather);
      } else if (actionName === "select_sky") {
        await selectSky();
        await featherSelection(payload.feather);
      } else if (actionName === "select_all") {
        await selectAll();
      } else if (actionName === "deselect") {
        await clearSelection();
      } else if (actionName === "inverse") {
        if (!(await hasActiveSelection())) {
          throw codedError("no_active_selection", "Cannot inverse selection because no active selection exists.");
        }
        await invertSelection();
      } else if (actionName === "color_range") {
        await selectColorRange(payload);
        await featherSelection(payload.feather);
      } else if (actionName === "focus_area") {
        await selectFocusArea(payload);
        await featherSelection(payload.feather);
      } else if (actionName === "modify") {
        if (!(await hasActiveSelection())) {
          throw codedError("no_active_selection", "Cannot modify selection because no active selection exists.");
        }
        const operation = String(payload.operation || "");
        if (operation === "feather") {
          await featherSelection(payload.amount);
        } else if (operation === "expand") {
          await expandSelection(payload.amount);
        } else if (operation === "contract") {
          await contractSelection(payload.amount);
        } else if (operation === "smooth") {
          await smoothSelection(payload.amount);
        } else if (operation === "border") {
          await borderSelection(payload.amount);
        } else {
          throw codedError("invalid_selection_modify_operation", `Unsupported selection modify operation: ${operation}`);
        }
      } else if (actionName === "save_selection") {
        await saveSelectionChannel(payload.channel_name);
      } else if (actionName === "load_selection") {
        await loadSelectionChannel(payload.channel_name);
      } else {
        throw codedError("unsupported_selection_command", `Unsupported selection command: ${actionName}`);
      }

      if (payload.invert === true && actionName !== "inverse") {
        if (!(await hasActiveSelection())) {
          throw codedError("no_active_selection", "Cannot invert after command because no active selection exists.");
        }
        await invertSelection();
      }

      resultInfo = {
        action: actionName,
        operation: payload.operation || null,
        amount: payload.amount == null ? null : Number(payload.amount),
        feather: payload.feather == null ? 0 : Number(payload.feather),
        channel_name: payload.channel_name || null,
        has_active_selection: await hasActiveSelection()
      };
    }, { commandName: `Codex Selection Command - ${actionName}`, timeOut: 30000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "selection_command_failed";
    return errorResult(job, code, message, error && error.details !== undefined ? error.details : null);
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    document: await readDocumentState(false),
    selection: resultInfo,
    warnings: []
  };
}

async function deleteLayerById(layerId) {
  await playAction([
    {
      _obj: "delete",
      _target: [{ _ref: "layer", _id: layerId }],
      _options: { dialogOptions: "dontDisplay" }
    }
  ]);
}

async function executeCapability(job) {
  const payload = job.payload || {};
  const capabilityId = String(payload.capability_id || "");
  const params = payload.params && typeof payload.params === "object" ? payload.params : {};
  const state = await readDocumentState(false);
  const warnings = [];
  let resultInfo = null;

  if (capabilityId !== "descriptor.raw_batchplay" && !state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }

  try {
    await core.executeAsModal(async () => {
      if (capabilityId === "layer.create_group") {
        const name = safeLayerName(params.name, `Codex Group - ${job.job_id}`);
        await createLayerGroup(name);
        resultInfo = { capability_id: capabilityId, group_name: name };
      } else if (capabilityId === "layer.duplicate_active") {
        const name = safeLayerName(params.name, `Codex Duplicate - ${job.job_id}`);
        await duplicateActiveLayer(name);
        await setActiveLayerProperties({
          name,
          opacity: numberParam(params.opacity, 100, 0, 100),
          blendMode: blendModeValue(params.blend_mode || "normal")
        });
        resultInfo = { capability_id: capabilityId, layer_name: name, layer_id: await getActiveLayerId() };
      } else if (capabilityId === "filter.gaussian_blur_duplicate") {
        const name = safeLayerName(params.name, `Codex Blur - ${job.job_id}`);
        const radius = numberParam(params.radius, 24, 0.1, 500);
        await duplicateActiveLayer(name);
        await playAction([
          {
            _obj: "gaussianBlur",
            radius: { _unit: "pixelsUnit", _value: radius },
            _options: { dialogOptions: "dontDisplay" }
          }
        ]);
        await setActiveLayerProperties({
          name,
          opacity: numberParam(params.opacity, 40, 0, 100),
          blendMode: blendModeValue(params.blend_mode || "screen")
        });
        resultInfo = { capability_id: capabilityId, layer_name: name, layer_id: await getActiveLayerId(), radius };
      } else if (capabilityId === "descriptor.raw_batchplay") {
        if (payload.user_confirmed !== true || payload.risk_acknowledged !== true) {
          throw codedError("raw_descriptor_not_confirmed", "Raw batchPlay requires user_confirmed=true and risk_acknowledged=true.");
        }
        const descriptors = Array.isArray(params.descriptors) ? params.descriptors : [];
        if (!descriptors.length) {
          throw codedError("invalid_raw_descriptor", "params.descriptors must be a non-empty array.");
        }
        const rawResult = await playAction(descriptors);
        resultInfo = { capability_id: capabilityId, descriptor_count: descriptors.length, raw_result: rawResult };
      } else {
        throw codedError("unsupported_capability", `Unsupported capability_id: ${capabilityId}`);
      }
    }, { commandName: `Codex Capability - ${capabilityId}`, timeOut: 30000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "capability_failed";
    return errorResult(job, code, message, error && error.details !== undefined ? error.details : null);
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    workflow_id: payload.workflow_id || null,
    stage_id: payload.stage_id || null,
    capability: resultInfo,
    document: await readDocumentState(false),
    warnings
  };
}

async function executeOperationRecipe(job) {
  const payload = job.payload || {};
  const recipe = payload.operation_recipe || payload.recipe || {};
  const steps = Array.isArray(recipe.steps) ? recipe.steps : [];
  let state = await readDocumentState(false);
  const warnings = [];
  const stepResults = { byId: {}, byIndex: [] };
  const canCreateDocument = steps.length > 0 && steps[0] && steps[0].atom_id === "document.create";
  const svgObjectLayers = {};
  let activeSvgObjectId = null;
  let activeSvgPartId = null;

  if (!state.has_active_document && !canCreateDocument) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }
  if (!steps.length) {
    return errorResult(job, "invalid_operation_recipe", "operation_recipe.steps must be a non-empty array.");
  }

  try {
    await core.executeAsModal(async () => {
      for (let index = 0; index < steps.length; index += 1) {
        const step = steps[index] || {};
        const stepId = String(step.step_id || `step_${index + 1}`);
        const atomId = String(step.atom_id || "");
        const params = resolveRecipeValue(step.params || {}, stepResults);
        activeSvgObjectId = params.object_id || null;
        activeSvgPartId = params.part_id || null;
        let resultInfo = {
          step_id: stepId,
          atom_id: atomId
        };

        if (atomId === "document.create") {
          resultInfo.document = await createDesignDocument(params);
          state = await readDocumentState(false);
        } else if (atomId === "document.set_canvas_size") {
          resultInfo.document = await setCanvasSize(params, state);
          state = await readDocumentState(false);
        } else if (atomId === "asset.place_embedded") {
          resultInfo = Object.assign(resultInfo, await placeEmbeddedAsset(params));
        } else if (atomId === "shape.svg_asset_place") {
          resultInfo = Object.assign(resultInfo, await placeSvgAssetLayer(params));
        } else if (atomId === "asset.replace_contents") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo = Object.assign(resultInfo, await replaceSmartObjectContents(Object.assign({}, params, { layer_id: layerId })));
        } else if (atomId === "layer.transform") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo = Object.assign(resultInfo, await transformLayerById(layerId, params));
        } else if (atomId === "layer.move_to_top") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo.layer_id = await moveLayerToTop(layerId);
        } else if (atomId === "layer.move_above") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          const referenceLayerId = resolveRecipeValue(params.reference_layer_id || params.above_layer_id, stepResults);
          resultInfo.layer_id = await moveLayerRelative(layerId, referenceLayerId, "above");
          resultInfo.reference_layer_id = referenceLayerId;
          resultInfo.position = "above";
        } else if (atomId === "layer.move_below") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          const referenceLayerId = resolveRecipeValue(params.reference_layer_id || params.below_layer_id, stepResults);
          resultInfo.layer_id = await moveLayerRelative(layerId, referenceLayerId, "below");
          resultInfo.reference_layer_id = referenceLayerId;
          resultInfo.position = "below";
        } else if (atomId === "layer.reorder") {
          resultInfo = Object.assign(resultInfo, await reorderLayer(params, step, stepResults));
        } else if (atomId === "layer.align") {
          resultInfo = Object.assign(resultInfo, await alignLayers(params, state));
        } else if (atomId === "layer.distribute") {
          resultInfo = Object.assign(resultInfo, await distributeLayers(params));
        } else if (atomId === "layer.create_clipping_mask") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo.layer_id = await createClippingMask(layerId);
          resultInfo.clipping_mask = true;
        } else if (atomId === "layer.release_clipping_mask") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo.layer_id = await releaseClippingMask(layerId);
          resultInfo.clipping_mask = false;
        } else if (atomId === "text.create") {
          resultInfo = Object.assign(resultInfo, await createTextLayer(params));
        } else if (atomId === "text.fit_to_box") {
          const textLayerId = resolveRecipeValue(
            step.target != null ? step.target : (params.text_layer_id || params.layer_id || params.target_layer_id),
            stepResults
          );
          const boxLayerId = resolveRecipeValue(
            params.box_layer_id || params.container_layer_id || params.reference_layer_id,
            stepResults
          );
          resultInfo = Object.assign(resultInfo, await fitTextLayerToBox(textLayerId, boxLayerId, params));
        } else if (atomId === "shape.rectangle") {
          resultInfo = Object.assign(resultInfo, await createRectangleShapeLayer(params));
        } else if (atomId === "shape.rounded_rectangle") {
          resultInfo = Object.assign(resultInfo, await createRoundedRectangleShapeLayer(params, state));
        } else if (atomId === "shape.capsule") {
          resultInfo = Object.assign(resultInfo, await createCapsuleShapeLayer(params, state));
        } else if (atomId === "shape.cut_corner_rect") {
          resultInfo = Object.assign(resultInfo, await createCutCornerRectShapeLayer(params, state));
        } else if (atomId === "shape.ellipse") {
          resultInfo = Object.assign(resultInfo, await createEllipseShapeLayer(params));
        } else if (atomId === "shape.ribbon") {
          resultInfo = Object.assign(resultInfo, await createRibbonShapeLayer(params, state));
        } else if (atomId === "shape.arc_band") {
          resultInfo = Object.assign(resultInfo, await createArcBandShapeLayer(params, state));
        } else if (atomId === "shape.chevron") {
          resultInfo = Object.assign(resultInfo, await createChevronShapeLayer(params, state));
        } else if (atomId === "shape.bracket") {
          resultInfo = Object.assign(resultInfo, await createBracketShapeLayer(params, state));
        } else if (atomId === "shape.scalloped_triangle") {
          resultInfo = Object.assign(resultInfo, await createScallopedTriangleShapeLayer(params, state));
        } else if (atomId === "shape.blob") {
          resultInfo = Object.assign(resultInfo, await createBlobShapeLayer(params, state));
        } else if (atomId === "shape.wavy_band") {
          resultInfo = Object.assign(resultInfo, await createWavyBandShapeLayer(params, state));
        } else if (atomId === "shape.starburst") {
          resultInfo = Object.assign(resultInfo, await createStarburstShapeLayer(params, state));
        } else if (atomId === "shape.beads_on_path") {
          resultInfo = Object.assign(resultInfo, await createBeadsOnPathShapeLayers(params, state));
        } else if (atomId === "shape.dashed_path") {
          resultInfo = Object.assign(resultInfo, await createDashedPathShapeLayers(params, state));
        } else if (atomId === "shape.arrow_path") {
          resultInfo = Object.assign(resultInfo, await createArrowPathShapeLayers(params, state));
        } else if (atomId === "shape.bauble") {
          resultInfo = Object.assign(resultInfo, await createBaubleShapeLayers(params, state));
        } else if (atomId === "shape.badge") {
          resultInfo = Object.assign(resultInfo, await createBadgeShapeLayers(params, state));
        } else if (atomId === "shape.callout") {
          resultInfo = Object.assign(resultInfo, await createCalloutShapeLayer(params, state));
        } else if (atomId === "shape.ticket_card") {
          resultInfo = Object.assign(resultInfo, await createTicketCardShapeLayer(params, state));
        } else if (atomId === "shape.notched_panel") {
          resultInfo = Object.assign(resultInfo, await createNotchedPanelShapeLayer(params, state));
        } else if (atomId === "shape.folded_corner") {
          resultInfo = Object.assign(resultInfo, await createFoldedCornerShapeLayers(params, state));
        } else if (atomId === "shape.polygon") {
          resultInfo = Object.assign(resultInfo, await createPolygonShapeLayer(params, state));
        } else if (atomId === "shape.star") {
          resultInfo = Object.assign(resultInfo, await createStarShapeLayer(params, state));
        } else if (atomId === "shape.line") {
          resultInfo = Object.assign(resultInfo, await createLineShapeLayer(params, state));
        } else if (atomId === "shape.polyline") {
          resultInfo = Object.assign(resultInfo, await createPolylineShapeLayer(params, state));
        } else if (atomId === "path.create_work_path") {
          resultInfo = Object.assign(resultInfo, await createWorkPath(params, state));
        } else if (atomId === "path.bezier_work_path") {
          resultInfo = Object.assign(resultInfo, await createBezierWorkPath(params, state));
        } else if (atomId === "path.audit_bezier_handles") {
          resultInfo = Object.assign(resultInfo, auditBezierPathHandles(params));
        } else if (atomId === "path.dom_runtime_diagnostics") {
          resultInfo = Object.assign(resultInfo, pathDomRuntimeDiagnostics(params));
        } else if (atomId === "path.to_selection") {
          resultInfo = Object.assign(resultInfo, await pathToSelection(params, state));
        } else if (atomId === "path.stroke") {
          resultInfo = Object.assign(resultInfo, await strokeWorkPath(params, state));
        } else if (atomId === "shape.path_fill") {
          resultInfo = Object.assign(resultInfo, await fillWorkPath(params, state));
        } else if (atomId === "shape.bezier_fill") {
          resultInfo = Object.assign(resultInfo, await createBezierFillShapeLayer(params, state));
        } else if (atomId === "shape.bezier_ellipse") {
          resultInfo = Object.assign(resultInfo, await createBezierEllipseShapeLayer(params, state));
        } else if (atomId === "layer.create_group") {
          const groupName = safeLayerName(params.name, `Codex Agent - ${job.job_id}`);
          await createLayerGroup(groupName);
          resultInfo.group_id = await getActiveLayerId();
          resultInfo.group_name = groupName;
        } else if (atomId === "layer.duplicate") {
          const sourceLayerId = resolveRecipeValue(params.source_layer_id, stepResults);
          if (sourceLayerId != null) {
            await selectLayer(sourceLayerId);
          }
          const name = safeLayerName(params.name, `Codex Duplicate - ${index + 1}`);
          await duplicateActiveLayer(name);
          resultInfo.layer_id = await getActiveLayerId();
          resultInfo.layer_name = name;
        } else if (atomId === "layer.select") {
          const ids = params.layer_ids ? resolveRecipeValue(params.layer_ids, stepResults) : null;
          if (Array.isArray(ids) && ids.length) {
            await selectLayerIds(ids);
            resultInfo.active_layer_ids = ids;
          } else {
            const layerId = resolveRecipeValue(params.layer_id, stepResults);
            await selectLayer(layerId);
            resultInfo.active_layer_ids = [layerId];
          }
        } else if (atomId === "layer.set_properties") {
          const targetLayerId = resolveLayerTarget(step, params, stepResults);
          resultInfo.layer_id = await setLayerPropertiesById(targetLayerId, params);
        } else if (atomId === "layer.effect_shadow") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo = Object.assign(resultInfo, await applyDropShadowLayerStyle(layerId, params));
        } else if (atomId === "layer.effect_outer_glow") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo = Object.assign(resultInfo, await applyOuterGlowLayerStyle(layerId, params));
        } else if (atomId === "layer.effect_stroke") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo = Object.assign(resultInfo, await applyStrokeLayerStyle(layerId, params));
        } else if (atomId === "layer.effect_gradient_overlay") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo = Object.assign(resultInfo, await applyGradientOverlayLayerStyle(layerId, params));
        } else if (atomId === "gradient.fill") {
          resultInfo = Object.assign(resultInfo, await createGradientFillLayer(params));
        } else if (atomId === "layer.extract_luminosity_range") {
          resultInfo = Object.assign(resultInfo, await extractLuminosityRangeLayer(params));
        } else if (atomId === "effect.bloom_layer") {
          resultInfo = Object.assign(resultInfo, await createBloomLayer(params));
        } else if (atomId === "effect.light_rays") {
          resultInfo = Object.assign(resultInfo, await createLightRays(params, state));
        } else if (atomId === "layer.group") {
          const layerIds = resolveRecipeValue(params.layer_ids || [], stepResults);
          if (!Array.isArray(layerIds) || !layerIds.length) {
            throw codedError("invalid_operation_recipe", "layer.group params.layer_ids must be a non-empty array.");
          }
          await selectLayerIds(layerIds);
          const groupName = safeLayerName(params.name, `Codex Agent - ${job.job_id}`);
          resultInfo.group_id = await groupSelectedLayers(groupName);
          resultInfo.group_name = groupName;
          resultInfo.layer_ids = layerIds;
        } else if (atomId === "layer.delete") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          await deleteLayerById(layerId);
          resultInfo.deleted_layer_id = layerId;
        } else if (atomId === "filter.gaussian_blur") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          resultInfo = Object.assign(resultInfo, await applyGaussianBlurToLayer(layerId, params.radius));
        } else if (atomId === "retouch.spot_heal_points") {
          resultInfo = Object.assign(resultInfo, await retouchSpotHealPoints(params, state));
        } else if (atomId === "retouch.healing_brush_points") {
          resultInfo = Object.assign(resultInfo, await retouchSpotHealPoints(params, state));
        } else if (atomId === "retouch.content_aware_fill_selection") {
          resultInfo = Object.assign(resultInfo, await retouchContentAwareFillSelection(params));
        } else if (atomId === "retouch.clone_patch") {
          resultInfo = Object.assign(resultInfo, await retouchClonePatch(params, state));
        } else if (atomId === "adjustment.hue_saturation") {
          const targetLayerId = resolveLayerTarget(step, params, stepResults);
          const adjustmentParams = {
            op: "adjust_hue_saturation",
            target_layer_id: targetLayerId,
            clipping_mask: params.clipping_mask === true,
            params: {
              range: params.range || "master",
              hue: params.hue,
              saturation: params.saturation,
              lightness: params.lightness
            },
            layer: {
              name: params.name,
              opacity: params.opacity,
              blend_mode: params.blend_mode
            }
          };
          resultInfo = Object.assign(resultInfo, await createAdjustmentLayerFromAtom(
            adjustmentParams,
            index,
            state,
            job.job_id
          ));
        } else if (atomId === "adjustment.create") {
          resultInfo = Object.assign(resultInfo, await createAdjustmentLayerFromAtom(params, index, state, job.job_id));
        } else if (atomId === "mask.apply_current_selection") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          if (!(await hasActiveSelection())) {
            throw codedError("no_active_selection", "mask.apply_current_selection requires an active Photoshop selection.");
          }
          await selectLayer(layerId);
          await makeLayerMaskFromSelection();
          resultInfo.layer_id = layerId;
          resultInfo.mask_applied = true;
        } else if (atomId === "mask.apply_alpha") {
          const layerId = resolveLayerTarget(step, params, stepResults);
          const mask = params.selection_mask || params.mask || {
            source: "alpha_mask",
            asset_path: params.asset_path,
            asset_uri: params.asset_uri || params.uri,
            threshold: params.threshold,
            feather: params.feather,
            invert: params.invert,
            label: params.label
          };
          resultInfo = Object.assign(resultInfo, await applySelectionMaskToLayer(layerId, mask, state));
        } else if (atomId === "selection.clear") {
          await clearSelection();
          resultInfo.has_active_selection = await hasActiveSelection();
        } else {
          throw codedError("unsupported_operation_atom", `Unsupported operation atom: ${atomId}`);
        }

        if (params.object_id) {
          const objectId = String(params.object_id);
          if (!svgObjectLayers[objectId]) {
            svgObjectLayers[objectId] = { layer_ids: [], group_id: null };
          }
          if (atomId === "shape.svg_asset_place" && resultInfo.layer_id != null) {
            svgObjectLayers[objectId].layer_ids.push(resultInfo.layer_id);
          }
          if (atomId === "layer.group" && resultInfo.group_id != null) {
            svgObjectLayers[objectId].group_id = resultInfo.group_id;
          }
        }
        stepResults.byId[stepId] = resultInfo;
        stepResults.byIndex[index] = resultInfo;
      }
    }, { commandName: `Codex Operation Recipe - ${recipe.recipe_id || job.job_id}`, timeOut: 120000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "operation_recipe_failed";
    const cleanup = { attempted: false, cleaned: false, error: null };
    const objectRecord = activeSvgObjectId ? svgObjectLayers[String(activeSvgObjectId)] : null;
    if (objectRecord && (objectRecord.group_id != null || objectRecord.layer_ids.length)) {
      cleanup.attempted = true;
      try {
        await core.executeAsModal(async () => {
          if (objectRecord.group_id != null) {
            await deleteLayerById(objectRecord.group_id);
          } else {
            for (const layerId of objectRecord.layer_ids.slice().reverse()) {
              await deleteLayerById(layerId);
            }
          }
        }, { commandName: `Cleanup SVG Object - ${activeSvgObjectId}`, timeOut: 30000 });
        cleanup.cleaned = true;
      } catch (cleanupError) {
        cleanup.error = String(cleanupError && cleanupError.message ? cleanupError.message : cleanupError);
      }
    }
    const originalDetails = error && error.details !== undefined ? error.details : null;
    const details = {
      original: originalDetails,
      object_id: activeSvgObjectId,
      part_id: activeSvgPartId,
      svg_object_cleanup: cleanup
    };
    return errorResult(job, code, message, details);
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    workflow_id: recipe.workflow_id || payload.workflow_id || null,
    stage_id: recipe.stage_id || payload.stage_id || null,
    operation_recipe: {
      recipe_id: recipe.recipe_id || null,
      goal: recipe.goal || null,
      steps: stepResults.byIndex
    },
    document: await readDocumentState(false),
    warnings
  };
}

function colorRangeParamsFromSeed(params) {
  const copy = Object.assign({}, params || {});
  if (copy.preset || copy.color) {
    return copy;
  }
  const seed = String(copy.seed_profile || "").toLowerCase();
  if (seed.includes("highlight") || seed.includes("light")) {
    copy.preset = "highlights";
  } else if (seed.includes("shadow") || seed.includes("dark")) {
    copy.preset = "shadows";
  } else if (seed.includes("midtone")) {
    copy.preset = "midtones";
  } else if (seed.includes("skin")) {
    copy.preset = "skin_tones";
  } else if (seed.includes("cyan") || seed.includes("blue")) {
    copy.preset = "cyans";
  } else if (seed.includes("green")) {
    copy.preset = "greens";
  } else if (seed.includes("yellow")) {
    copy.preset = "yellows";
  } else if (seed.includes("red")) {
    copy.preset = "reds";
  } else {
    copy.preset = "midtones";
  }
  return copy;
}

function selectionMaskFromCandidate(candidate) {
  const atomId = String(candidate.atom_id || "");
  const params = Object.assign({}, candidate.params || {});
  const label = candidate.candidate_id || atomId;
  if (atomId === "selection.select_subject") {
    return Object.assign({ source: "select_subject", label }, params);
  }
  if (atomId === "selection.select_sky") {
    return Object.assign({ source: "select_sky", label }, params);
  }
  if (atomId === "selection.color_range") {
    return Object.assign({ source: "color_range", label }, colorRangeParamsFromSeed(params));
  }
  if (atomId === "selection.tonal_range") {
    const tonal = colorRangeParamsFromSeed(Object.assign({}, params, { seed_profile: params.seed_profile || params.preset }));
    if (params.preset && ["highlights", "midtones", "shadows"].includes(params.preset)) {
      tonal.preset = params.preset;
    }
    return Object.assign({ source: "color_range", label }, tonal);
  }
  if (atomId === "selection.bbox") {
    return Object.assign({ source: "bbox", label }, params);
  }
  if (atomId === "selection.polygon") {
    return Object.assign({ source: "polygon", label }, params);
  }
  if (atomId === "selection.alpha_mask") {
    return Object.assign({ source: "alpha_mask", label }, params.selection_mask || params);
  }
  if (atomId === "selection.object_selection") {
    if (params.selection_mask && typeof params.selection_mask === "object") {
      return Object.assign({ label }, params.selection_mask);
    }
    if (Array.isArray(params.points)) {
      return Object.assign({ source: "polygon", label }, params);
    }
    if (params.bbox && typeof params.bbox === "object") {
      return Object.assign({ source: "bbox", label }, params);
    }
    if (String(params.mode || params.source || "").toLowerCase() === "select_subject") {
      return Object.assign({ source: "select_subject", label }, params);
    }
    return null;
  }
  if (atomId === "selection.channel_load") {
    return null;
  }
  if (atomId === "selection.current") {
    return { source: "current_selection", label };
  }
  return null;
}

async function refineActiveSelectionFromParams(params) {
  if (!(await hasActiveSelection())) {
    throw codedError("no_active_selection", "selection.refine_edge requires an active selection.");
  }
  const smooth = params.smooth == null ? null : numberParam(params.smooth, 0, 0, 200);
  const expand = params.expand == null ? null : numberParam(params.expand, 0, 0, 200);
  const contract = params.contract == null ? null : numberParam(params.contract, 0, 0, 200);
  const feather = params.feather == null ? null : numberParam(params.feather, 0, 0, 500);
  const border = params.border == null ? null : numberParam(params.border, 0, 0, 200);
  if (smooth && smooth > 0) {
    await smoothSelection(smooth);
  }
  if (expand && expand > 0) {
    await expandSelection(expand);
  }
  if (contract && contract > 0) {
    await contractSelection(contract);
  }
  if (feather && feather > 0) {
    await featherSelection(feather);
  }
  if (border && border > 0) {
    await borderSelection(border);
  }
  if (params.invert === true) {
    await invertSelection();
  }
}

async function runSelectionCandidate(candidate, state, recipeId) {
  const atomId = String(candidate.atom_id || "");
  const candidateId = safeIdentifier(candidate.candidate_id, `candidate_${Date.now()}`);
  const params = candidate.params || {};
  const channelName = safeLayerName(params.channel_name || `Codex ${recipeId || "selection"} ${candidateId}`, `Codex ${candidateId}`);

  if (!["selection.current", "selection.refine", "selection.refine_edge"].includes(atomId)) {
    await clearSelection();
  }

  if (atomId === "selection.focus_area") {
    await selectGeneratedSelectionMask(params, "replace", "focus_area", async () => selectFocusArea(params));
  } else if (atomId === "selection.channel_load") {
    await loadSelectionChannel(params.channel_name, "replace");
  } else if (atomId === "selection.refine_edge") {
    await refineActiveSelectionFromParams(params);
  } else if (atomId === "selection.refine") {
    const operation = String(params.operation || "").toLowerCase();
    if (operation === "feather") {
      await featherSelection(params.amount);
    } else if (operation === "expand") {
      await expandSelection(params.amount);
    } else if (operation === "contract") {
      await contractSelection(params.amount);
    } else if (operation === "smooth") {
      await smoothSelection(params.amount);
    } else if (operation === "border") {
      await borderSelection(params.amount);
    } else if (params.invert === true) {
      await invertSelection();
    } else {
      throw codedError("invalid_selection_modify_operation", `Unsupported selection.refine operation: ${operation}`);
    }
  } else {
    const mask = selectionMaskFromCandidate(candidate);
    if (!mask) {
      throw codedError("unsupported_selection_atom", `Unsupported executable selection atom: ${atomId}`);
    }
    await applySingleSelectionMask(mask, state, "replace");
  }

  if (!(await hasActiveSelection())) {
    throw codedError("selection_empty", `Selection candidate ${candidateId} produced no usable selection.`);
  }
  await saveSelectionChannel(channelName);
  return {
    candidate_id: candidateId,
    atom_id: atomId,
    channel_name: channelName,
    has_active_selection: true
  };
}

async function executeSelectionRecipe(job) {
  const payload = job.payload || {};
  const recipe = payload.selection_recipe || payload.recipe || {};
  const candidates = Array.isArray(recipe.candidates) ? recipe.candidates : [];
  const mergePlan = recipe.merge_plan || {};
  const state = await readDocumentState(false);
  const candidateResults = {};
  const warnings = [];

  if (!state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }
  if (mergePlan.mode === "soft_alpha") {
    return errorResult(
      job,
      "soft_alpha_recipe_backend_only",
      "selection_recipe merge_plan.mode=soft_alpha must be composited by the Python backend before Photoshop receives one final alpha mask."
    );
  }

  try {
    await core.executeAsModal(async () => {
      for (const candidate of candidates) {
        const result = await runSelectionCandidate(candidate, state, recipe.recipe_id || job.job_id);
        candidateResults[result.candidate_id] = result;
      }

      const items = Array.isArray(mergePlan.items) ? mergePlan.items : [];
      if (!items.length) {
        throw codedError("invalid_selection_recipe", "selection_recipe.merge_plan.items must be a non-empty array.");
      }
      await clearSelection();
      for (let index = 0; index < items.length; index += 1) {
        const item = items[index] || {};
        const candidateId = String(item.candidate_id || "");
        const result = candidateResults[candidateId];
        if (!result) {
          throw codedError("selection_candidate_missing", `Merge item candidate_id not found: ${candidateId}`);
        }
        const operation = String(item.operation || (index === 0 ? "replace" : "add"));
        if (index === 0 && operation !== "replace") {
          throw codedError("invalid_selection_recipe", "First merge_plan item must use operation=replace.");
        }
        await loadSelectionChannel(result.channel_name, operation);
      }

      if (mergePlan.feather != null) {
        await featherSelection(mergePlan.feather);
      }
      if (mergePlan.invert === true) {
        await invertSelection();
      }
      if (!(await hasActiveSelection())) {
        throw codedError("selection_empty", "Selection recipe merge produced no usable final selection.");
      }
      const finalChannelName = safeLayerName(mergePlan.channel_name || `Codex ${recipe.recipe_id || job.job_id} final`, `Codex ${job.job_id}`);
      await saveSelectionChannel(finalChannelName);
      candidateResults.__final__ = {
        channel_name: finalChannelName,
        has_active_selection: true
      };
    }, { commandName: `Codex Selection Recipe - ${recipe.recipe_id || job.job_id}`, timeOut: 120000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "selection_recipe_failed";
    return errorResult(job, code, message, error && error.details !== undefined ? error.details : null);
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    workflow_id: recipe.workflow_id || payload.workflow_id || null,
    stage_id: recipe.stage_id || payload.stage_id || null,
    selection_recipe: {
      recipe_id: recipe.recipe_id || null,
      goal: recipe.goal || null,
      candidates: Object.values(candidateResults).filter((item) => item && item.candidate_id),
      final_channel_name: candidateResults.__final__ ? candidateResults.__final__.channel_name : null,
      selection_mask: { source: "current_selection" }
    },
    document: await readDocumentState(false),
    warnings
  };
}

async function applyMaskToLayer(job) {
  const payload = job.payload || {};
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }
  const selectionMask = payload.selection_mask || payload.mask || null;
  if (!selectionMask || typeof selectionMask !== "object") {
    return errorResult(job, "invalid_selection_mask", "payload.selection_mask must be an object.");
  }
  const targetLayerId = payload.layer_id || payload.target_layer_id || null;
  let maskInfo = null;
  try {
    await core.executeAsModal(async () => {
      maskInfo = await applySelectionMaskToLayer(targetLayerId, selectionMask, state);
    }, { commandName: `Codex Apply Mask - ${job.job_id}`, timeOut: 60000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "apply_mask_to_layer_failed";
    return errorResult(job, code, message, error && error.details !== undefined ? error.details : null);
  }
  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    mask: maskInfo,
    document: await readDocumentState(false),
    warnings: []
  };
}

async function deleteAgentGroup(job) {
  const payload = job.payload || {};
  const requestedGroupName = typeof payload.group_name === "string" ? payload.group_name.trim() : "";
  const targetJobId = safeJobId(payload.job_id);
  if (!requestedGroupName && !targetJobId) {
    return errorResult(job, "invalid_delete_target", "payload.job_id or payload.group_name is required.");
  }
  if (requestedGroupName && (!requestedGroupName.startsWith("Codex Agent - ") || requestedGroupName.length > 160)) {
    return errorResult(job, "invalid_group_name", "payload.group_name must start with 'Codex Agent - ' and be 160 characters or fewer.");
  }

  const groupName = requestedGroupName || `Codex Agent - ${targetJobId}`;
  const state = await readDocumentState(false);
  if (!state.has_active_document) {
    return errorResult(job, "no_active_document", "No active Photoshop document is available.", state.error || null);
  }

  let deletedLayerId = null;
  try {
    await core.executeAsModal(async () => {
      const doc = app.activeDocument;
      const layer = findLayerGroupByName(doc && doc.layers, groupName, 0);
      if (!layer) {
        throw codedError("agent_group_not_found", `Could not find layer group: ${groupName}.`);
      }
      if (!isLayerGroup(layer)) {
        throw codedError("agent_target_not_group", `Matched layer is not a layer group: ${groupName}.`);
      }
      const layerId = layerIdValue(layer);
      if (layerId == null) {
        throw codedError("agent_group_id_unavailable", `Layer group id is unavailable: ${groupName}.`);
      }
      deletedLayerId = layerId;
      await deleteLayerById(layerId);
    }, { commandName: `Delete ${groupName}`, timeOut: 5000 });
  } catch (error) {
    const message = String(error && error.message ? error.message : error);
    const code = error && error.code
      ? error.code
      : message.toLowerCase().includes("modal") ? "modal_busy" : "delete_agent_group_failed";
    return errorResult(job, code, message, error && error.details !== undefined ? error.details : null);
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "ok",
    deleted_job_id: targetJobId,
    deleted_group_name: groupName,
    workflow_id: payload.workflow_id || null,
    stage_id: payload.stage_id || null,
    deleted_layer_id: deletedLayerId,
    document: await readDocumentState(false),
    warnings: []
  };
}

async function executeJob(job) {
  if (job.job_type === "ping") {
    return {
      schema_version: "ps-agent/v1",
      job_id: job.job_id,
      status: "ok",
      message: "pong",
      document: await readDocumentState(false)
    };
  }

  if (job.job_type === "get_state") {
    const includeLayers = !job.payload || job.payload.include_layers !== false;
    const state = await readDocumentState(includeLayers);
    if (!state.has_active_document) {
      return {
        schema_version: "ps-agent/v1",
        job_id: job.job_id,
        status: "error",
        state,
        error: state.error || {
          code: "no_active_document",
          message: "No active Photoshop document is available."
        }
      };
    }
    return {
      schema_version: "ps-agent/v1",
      job_id: job.job_id,
      status: "ok",
      state,
      document: state,
      warnings: []
    };
  }

  if (job.job_type === "export_preview") {
    return exportPreview(job);
  }

  if (job.job_type === "export_regions") {
    return exportRegions(job);
  }

  if (job.job_type === "apply_plan") {
    return applyPlan(job);
  }

  if (job.job_type === "undo_last_agent_edit") {
    return undoLastAgentEdit(job);
  }

  if (job.job_type === "make_selection") {
    return makeSelection(job);
  }

  if (job.job_type === "selection_command") {
    return selectionCommand(job);
  }

  if (job.job_type === "execute_capability") {
    return executeCapability(job);
  }

  if (job.job_type === "operation_recipe") {
    return executeOperationRecipe(job);
  }

  if (job.job_type === "selection_recipe") {
    return executeSelectionRecipe(job);
  }

  if (job.job_type === "apply_mask_to_layer") {
    return applyMaskToLayer(job);
  }

  if (job.job_type === "delete_agent_group") {
    return deleteAgentGroup(job);
  }

  return {
    schema_version: "ps-agent/v1",
    job_id: job.job_id,
    status: "error",
    error: {
      code: "unsupported_job",
      message: `Job type ${job.job_type} is registered but not implemented in the phase-one UXP plugin.`
    }
  };
}

async function flushPendingResult() {
  if (!runtime.pendingResult) {
    return true;
  }
  const pending = runtime.pendingResult;
  await postJson(`/uxp/jobs/${encodeURIComponent(pending.job_id)}/result`, pending.result);
  runtime.pendingResult = null;
  runtime.lastError = null;
  return true;
}

async function pollJob() {
  if (!runtime.backendConnected || runtime.currentJob || runtime.pollInFlight) {
    return;
  }
  if (Date.now() < runtime.nextPollAt) {
    return;
  }

  runtime.pollInFlight = true;
  try {
    await flushPendingResult();
    const claimedBy = `uxp:${PLUGIN_VERSION}`;
    const response = await getJson(`/uxp/jobs/next?claimed_by=${encodeURIComponent(claimedBy)}`);
    runtime.queue = response.queue || runtime.queue;
    if (!response.job) {
      render();
      return;
    }

    runtime.currentJob = response.job;
    render();
    let result;
    try {
      result = await executeJob(response.job);
    } catch (error) {
      result = errorResult(
        response.job,
        "job_execution_failed",
        String(error && error.message ? error.message : error)
      );
    }
    try {
      await postJson(`/uxp/jobs/${encodeURIComponent(response.job.job_id)}/result`, result);
      runtime.pendingResult = null;
    } catch (postError) {
      runtime.pendingResult = {
        job_id: response.job.job_id,
        result
      };
      throw postError;
    }
    runtime.lastError = result.status === "error" && result.error ? result.error.message : null;
  } catch (error) {
    runtime.lastError = `poll failed: ${error && error.message ? error.message : error}`;
    setBackendStatus(runtime.pendingResult ? "waiting" : "error");
  } finally {
    runtime.currentJob = null;
    runtime.pollInFlight = false;
    render();
  }
}

async function refreshNow() {
  await sendHeartbeat();
  await pollJob();
}

async function copyStartCommand() {
  const command = START_COMMAND;
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      runtime.lastError = `copy unavailable; command: ${command}`;
      render();
      return;
    }
    await navigator.clipboard.writeText(command);
    runtime.lastError = "Start command copied.";
  } catch (error) {
    runtime.lastError = `copy failed; command: ${command}`;
  }
  render();
}

async function loadDiagnostics() {
  try {
    runtime.diagnostics = await getJson("/api/diagnostics");
    runtime.lastError = null;
    setBackendStatus("connected");
  } catch (error) {
    runtime.diagnostics = {
      status: "error",
      start_command: START_COMMAND,
      error: String(error && error.message ? error.message : error)
    };
    runtime.lastError = `diagnostics failed: ${error && error.message ? error.message : error}`;
    setBackendStatus("error");
  }
  render();
}

function startLoops() {
  if (runtime.initialized) {
    render();
    return;
  }
  runtime.initialized = true;
  const refreshButton = $("btnRefresh");
  if (refreshButton) {
    refreshButton.addEventListener("click", refreshNow);
  }
  const copyButton = $("btnCopyStart");
  if (copyButton) {
    copyButton.addEventListener("click", copyStartCommand);
  }
  const diagnosticsButton = $("btnDiagnostics");
  if (diagnosticsButton) {
    diagnosticsButton.addEventListener("click", loadDiagnostics);
  }
  render();
  setTimeout(refreshNow, 1000);
  runtime.heartbeatTimer = setInterval(sendHeartbeat, HEARTBEAT_MS);
  runtime.pollTimer = setInterval(pollJob, POLL_MS);
}

entrypoints.setup({
  commands: {
    refreshStatus: refreshNow
  },
  panels: {
    agentPanel: {
      show() {
        startLoops();
      }
    }
  }
});

