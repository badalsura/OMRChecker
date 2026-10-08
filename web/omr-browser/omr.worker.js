/*
 * omr.worker.js - runs the OMR engine off the main thread.
 *
 * Created by OMR.createWorkerEngine(template, {workerUrl: "omr.worker.js"}); you do
 * not post messages to it yourself. The page decodes the photo (canvas access is
 * main-thread only on older Safari) and transfers the gray pixels here, or the
 * RGBA pixels when the template has a colour dropout (info.needsColor).
 *
 * Messages: {id, type: "init" | "scan" | "preview" | "setColorDropout", payload}
 *   -> {id, result} or {id, error}
 */
/* global importScripts, OMR */
(function () {
  "use strict";
  var engine = null;

  function reply(id, result, transfer) {
    self.postMessage({ id: id, result: result }, transfer || []);
  }
  function fail(id, error) {
    self.postMessage({ id: id, error: String((error && error.message) || error) });
  }

  function init(payload) {
    var options = payload.options || {};
    if (typeof OMR === "undefined") importScripts(options.omrScriptUrl || "omr.js");
    // Extension scripts, e.g. one that calls OMR.registerZoneReader("ocr", fn)
    (options.extraScripts || []).forEach(function (url) {
      importScripts(url);
    });
    var ready = Promise.resolve();
    if (options.barcodes) ready = ready.then(function () {
      return OMR.enableBarcodes(options.barcodes);
    });
    return ready
      .then(function () {
        return OMR.loadTemplate(payload.template, {
          config: options.config,
          assets: options.assets,
          assetsBaseUrl: options.assetsBaseUrl,
          maxDimension: options.maxDimension,
          templateOverrides: options.templateOverrides,
        });
      })
      .then(function (created) {
        engine = created;
        if (options.bubbleModel) return engine.loadBubbleModel(options.bubbleModel);
        return null;
      })
      .then(function () {
        return {
          version: OMR.VERSION,
          fields: engine.template.fieldBlocks.reduce(function (n, b) {
            return n + b.fields.length;
          }, 0),
          zones: engine.template.zones.length,
          preProcessors: engine.preProcessors.map(function (p) {
            return p.name;
          }),
          barcodes: OMR.barcodesAvailable(),
          bubbleModel: !!engine.bubbleModel,
          needsColor: engine.needsColor,
        };
      });
  }

  function scan(payload) {
    if (!engine) return Promise.reject(new Error("Worker engine is not initialised"));
    var image = { width: payload.width, height: payload.height, data: new Uint8Array(payload.data) };
    if (payload.channels > 1) image.channels = payload.channels;
    return engine.scan(image, { fileId: payload.fileId }).then(function (result) {
      var out = { result: result, registration: result.registration || null };
      var transfer = [];
      if (payload.returnImage && result.alignedImage) {
        var data = result.alignedImage.data;
        var buffer = data.byteOffset === 0 && data.byteLength === data.buffer.byteLength ? data.buffer : data.slice().buffer;
        out.image = { width: result.alignedImage.width, height: result.alignedImage.height, data: buffer };
        transfer.push(buffer);
      }
      return { value: out, transfer: transfer };
    });
  }

  self.onmessage = function (event) {
    var message = event.data || {};
    var work;
    try {
      if (message.type === "init") work = init(message.payload).then(function (v) {
        return { value: v };
      });
      else if (message.type === "scan") work = scan(message.payload);
      else if (message.type === "preview") {
        if (!engine) throw new Error("Worker engine is not initialised");
        var frame = message.payload;
        work = Promise.resolve({ value: engine.preview({ width: frame.width, height: frame.height, data: new Uint8Array(frame.data) }) });
      }
      else if (message.type === "setColorDropout") {
        if (!engine) throw new Error("Worker engine is not initialised");
        engine.setColorDropout(message.payload.spec);
        work = Promise.resolve({ value: engine.needsColor });
      }
      else throw new Error("Unknown message type: " + message.type);
    } catch (error) {
      fail(message.id, error);
      return;
    }
    work.then(
      function (out) {
        reply(message.id, out.value, out.transfer);
      },
      function (error) {
        fail(message.id, error);
      }
    );
  };
})();
