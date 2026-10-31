ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

# zbar: the C library pyzbar wraps via ctypes, for barcode decoding.
RUN apk add --no-cache \
        python3 \
        py3-pip \
        zbar \
        curl

COPY app/requirements.txt /app/requirements.txt
# --break-system-packages: Alpine's python3 marks itself "externally managed"
# (PEP 668); this is an add-on container, not a shared system, so it's safe.
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

COPY app /app
COPY rootfs /

RUN set -e; \
    mkdir -p /tmp/ocr-ort /tmp/ocr-models /app/static/ocr; \
    curl -fsSL https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-1.31.1.tgz -o /tmp/ort.tgz; \
    curl -fsSL https://registry.npmjs.org/@gutenye/ocr-models/-/ocr-models-1.4.2.tgz -o /tmp/ocr-models.tgz; \
    printf '%s  %s\n' bdccdb9fbb9ab537247c0c734e9b1d2b1b338b4f2a09faa58f0dc1cfa27c312c /tmp/ort.tgz 9b6666eae0b51936f186bcf4d581c403b63232c8d2d256308d2259dee3afd14e /tmp/ocr-models.tgz | sha256sum -c -; \
    tar -xzf /tmp/ort.tgz -C /tmp/ocr-ort package/dist/ort.wasm.min.js package/dist/ort-wasm-simd-threaded.mjs package/dist/ort-wasm-simd-threaded.wasm; \
    tar -xzf /tmp/ocr-models.tgz -C /tmp/ocr-models package/assets/ch_PP-OCRv4_det_infer.onnx package/assets/ch_PP-OCRv4_rec_infer.onnx package/assets/ppocr_keys_v1.txt; \
    cp /tmp/ocr-ort/package/dist/ort.wasm.min.js /app/static/ocr/; \
    cp /tmp/ocr-ort/package/dist/ort-wasm-simd-threaded.mjs /app/static/ocr/; \
    cp /tmp/ocr-ort/package/dist/ort-wasm-simd-threaded.wasm /app/static/ocr/; \
    cp /tmp/ocr-models/package/assets/ch_PP-OCRv4_det_infer.onnx /app/static/ocr/; \
    cp /tmp/ocr-models/package/assets/ch_PP-OCRv4_rec_infer.onnx /app/static/ocr/; \
    cp /tmp/ocr-models/package/assets/ppocr_keys_v1.txt /app/static/ocr/; \
    rm -rf /tmp/ocr-ort /tmp/ocr-models /tmp/ort.tgz /tmp/ocr-models.tgz; \
    for file in /app/static/ocr/ort.wasm.min.js /app/static/ocr/ort-wasm-simd-threaded.mjs /app/static/ocr/ort-wasm-simd-threaded.wasm /app/static/ocr/ch_PP-OCRv4_det_infer.onnx /app/static/ocr/ch_PP-OCRv4_rec_infer.onnx /app/static/ocr/ppocr_keys_v1.txt; do test -s "$file"; done

RUN chmod a+x /etc/services.d/pantry_tracker/run

WORKDIR /app
ENV PYTHONUNBUFFERED=1

LABEL \
    org.opencontainers.image.title="Pantry Tracker" \
    org.opencontainers.image.description="Scan grocery barcodes to track your pantry and get notified before food expires" \
    org.opencontainers.image.source="https://github.com/nict41/nict41-s-HA-Apps" \
    org.opencontainers.image.licenses="MIT"
