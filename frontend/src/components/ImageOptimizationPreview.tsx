import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Settings } from "../api/types";
import { formatBytes } from "./common";

interface PreviewImage {
  data_url: string;
  size: number;
  width: number;
  height: number;
  original_kept: boolean;
}

export function ImageOptimizationPreview({ form }: { form: Settings }) {
  const enabled = form.image_optimization_enabled === "true";
  const format = form.image_optimization_format ?? "webp";
  const quality = form.image_optimization_quality ?? "85";
  const maxWidth = form.image_optimization_max_width ?? "0";
  const key = JSON.stringify([format, quality, maxWidth]);
  const valid = [quality, maxWidth].every((s) => /^\d+$/.test(s)) &&
    Number(quality) >= 1 && Number(quality) <= 100 && Number(maxWidth) <= 10000;
  const [sample, setSample] = useState<PreviewImage | null>(null);
  const [preview, setPreview] = useState<{ key: string; image: PreviewImage } | null>(null);
  const [sampleError, setSampleError] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [actualSize, setActualSize] = useState(false);
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const queue = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    if (sample) return;
    let active = true;
    setSampleError("");
    api.get<PreviewImage>("/settings/image-optimization/sample")
      .then((image) => { if (active) setSample(image); })
      .catch((reason: Error) => { if (active) setSampleError(reason.message); });
    return () => { active = false; };
  }, [sample, retry]);

  useEffect(() => {
    const token = ++generation.current;
    setError("");
    if (!enabled || !valid || !sample) {
      setLoading(false);
      return;
    }
    setLoading(true);
    const timer = window.setTimeout(() => {
      // Serialize requests, skip superseded work, and ignore old responses.
      queue.current = queue.current.catch(() => {}).then(async () => {
        if (generation.current !== token) return;
        try {
          const image = await api.post<PreviewImage>("/settings/image-optimization/preview", {
            image_optimization_enabled: "true", image_optimization_format: format,
            image_optimization_quality: quality, image_optimization_max_width: maxWidth,
          });
          if (generation.current === token) setPreview({ key, image });
        } catch (reason) {
          if (generation.current === token) setError((reason as Error).message);
        } finally {
          if (generation.current === token) setLoading(false);
        }
      });
    }, 300);
    return () => {
      window.clearTimeout(timer);
      if (generation.current === token) generation.current++;
    };
  }, [enabled, valid, sample, format, quality, maxWidth, key, retry]);

  const current = !enabled ? sample : preview?.key === key ? preview.image : null;
  const stale = enabled && !current && !!preview;
  const shown = current ?? preview?.image;
  const savings = sample && current ? 100 * (1 - current.size / sample.size) : 0;
  const sizeChange = savings > 0 ? `${savings.toFixed(1)}% smaller`
    : savings < 0 ? `${Math.abs(savings).toFixed(1)}% larger` : "Original size";
  const describe = (image: PreviewImage) => `${formatBytes(image.size)} · ${image.width} × ${image.height} px`;

  return (
    <div className="image-preview">
      <div className="image-preview-toolbar">
        <h4>Live quality preview</h4>
        <button className="btn" type="button" aria-pressed={actualSize}
          onClick={() => setActualSize(!actualSize)}>
          {actualSize ? "Fit to panel" : "View at actual size (100%)"}
        </button>
      </div>
      <p className="section-hint">
        Compare this sample's original image with the result of your selected settings.
        Changes update automatically without saving. This sample includes fine text,
        line art, shading and color; results vary between chapter pages.
      </p>
      {(sampleError || error) && <div className="error-banner" role="alert">
        {sampleError || error}{" "}
        <button className="btn" type="button" onClick={() => setRetry(retry + 1)}>Retry preview</button>
      </div>}
      <div className="image-preview-grid">
        <figure>
          <figcaption><strong>Original quality</strong><span>{sample ? describe(sample) : "Loading sample…"}</span></figcaption>
          <div className={`image-preview-frame${actualSize ? " actual-size" : ""}`}>
            {sample && <img src={sample.data_url} width={sample.width} height={sample.height}
              alt="Original sample with fine text, line art, halftones and color gradients" />}
          </div>
        </figure>
        <figure aria-busy={loading}>
          <figcaption><strong>{enabled ? "Optimized preview" : "Original quality (default)"}</strong>
            <span role="status" aria-live="polite">
              {!enabled ? "Optimization is off — original images are preserved."
                : !valid ? "Enter a valid quality and maximum width to preview."
                : error ? "Preview unavailable. Try again below."
                : loading || stale ? "Updating preview…"
                : current ? `${describe(current)} · ${sizeChange}` : "Preparing preview…"}
            </span>
          </figcaption>
          <div className={`image-preview-frame${actualSize ? " actual-size" : ""}${stale ? " stale" : ""}`}>
            {shown && <img src={shown.data_url} width={shown.width} height={shown.height}
              alt={enabled ? "Sample processed with the image optimization settings" : "Unchanged original sample"} />}
          </div>
        </figure>
      </div>
      {enabled && current?.original_kept && <p className="section-hint">
        Original retained: encoding this sample would not reduce its size.
      </p>}
    </div>
  );
}
