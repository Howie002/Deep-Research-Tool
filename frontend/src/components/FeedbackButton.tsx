'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { MessageSquarePlus, X, Camera, LoaderCircle } from 'lucide-react';

import { createPortal } from 'react-dom';
const BASE = '/DeepResearch';

// Keep the compressed screenshot comfortably under the ~1MB proxy body cap.
const SHOT_MAX_DIM = 1600;
const SHOT_MAX_CHARS = 600_000;

/**
 * Grab one frame of the current tab as a data URL. Uses getDisplayMedia with
 * preferCurrentTab (Chromium/Edge; one click in the share dialog), the same
 * pure-web capture path the SOP tool uses. The stream is stopped immediately
 * after the single frame is drawn.
 */
async function captureTabFrame(): Promise<string> {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: true,
    audio: false,
    // Non-standard Chromium hints: pre-select the current tab in the picker.
    preferCurrentTab: true,
    selfBrowserSurface: 'include',
  } as unknown as DisplayMediaStreamOptions);
  try {
    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;
    await video.play();
    // Give the compositor a frame so width/height are real.
    await new Promise((r) => setTimeout(r, 120));
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 1280;
    canvas.height = video.videoHeight || 720;
    canvas.getContext('2d')!.drawImage(video, 0, 0);
    return canvas.toDataURL('image/png');
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
}

/** Crop the source image to the given rect and compress to fit the size cap. */
function cropAndCompress(
  src: HTMLImageElement,
  sx: number,
  sy: number,
  sw: number,
  sh: number
): string {
  const scale = Math.min(1, SHOT_MAX_DIM / Math.max(sw, sh));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(sw * scale));
  canvas.height = Math.max(1, Math.round(sh * scale));
  canvas.getContext('2d')!.drawImage(src, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
  let quality = 0.85;
  let out = canvas.toDataURL('image/jpeg', quality);
  while (out.length > SHOT_MAX_CHARS && quality > 0.4) {
    quality -= 0.1;
    out = canvas.toDataURL('image/jpeg', quality);
  }
  return out;
}

/**
 * Snipping-tool crop overlay: shows the frozen tab frame full-screen; the user
 * drags a rectangle (dimmed outside the selection) and the crop is returned.
 * Escape cancels.
 */
function CropOverlay({
  frame,
  onDone,
  onCancel,
}: {
  frame: string;
  onDone: (dataUrl: string) => void;
  onCancel: () => void;
}) {
  const imgRef = useRef<HTMLImageElement>(null);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const rect = drag && {
    left: Math.min(drag.x0, drag.x1),
    top: Math.min(drag.y0, drag.y1),
    width: Math.abs(drag.x1 - drag.x0),
    height: Math.abs(drag.y1 - drag.y0),
  };

  const finish = () => {
    const img = imgRef.current;
    if (!img || !rect || rect.width < 10 || rect.height < 10) {
      setDrag(null);
      return;
    }
    const box = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / box.width;
    const scaleY = img.naturalHeight / box.height;
    const sx = Math.max(0, (rect.left - box.left) * scaleX);
    const sy = Math.max(0, (rect.top - box.top) * scaleY);
    const sw = Math.min(img.naturalWidth - sx, rect.width * scaleX);
    const sh = Math.min(img.naturalHeight - sy, rect.height * scaleY);
    if (sw < 4 || sh < 4) {
      setDrag(null);
      return;
    }
    onDone(cropAndCompress(img, sx, sy, sw, sh));
  };

  return (
    <div
      className="fixed inset-0 z-[100] bg-[#0a0a0a] cursor-crosshair select-none"
      onMouseDown={(e) => setDrag({ x0: e.clientX, y0: e.clientY, x1: e.clientX, y1: e.clientY })}
      onMouseMove={(e) => drag && setDrag({ ...drag, x1: e.clientX, y1: e.clientY })}
      onMouseUp={finish}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        ref={imgRef}
        src={frame}
        alt="Captured screen"
        draggable={false}
        className="absolute inset-0 m-auto max-w-full max-h-full"
      />
      {rect ? (
        <div
          className="absolute border-2 border-[#f0b323] pointer-events-none"
          style={{
            left: rect.left,
            top: rect.top,
            width: rect.width,
            height: rect.height,
            boxShadow: '0 0 0 100vmax rgba(0,0,0,0.55)',
          }}
        />
      ) : (
        <div className="absolute inset-0 bg-black/40 pointer-events-none" />
      )}
      <div className="absolute top-4 left-1/2 -translate-x-1/2 bg-black/75 text-white text-xs px-4 py-2 rounded-md pointer-events-none">
        Drag to select the area to include · Esc to cancel
      </div>
    </div>
  );
}

/**
 * Sidebar Feedback button (fleet standard). Collapses with the rail like the
 * nav items; opens a small modal that posts a free-form note (optionally with
 * a cropped screenshot of the tab) to /api/feedback, which forwards it to the
 * dashboard's Feedback panel.
 */
export default function FeedbackButton() {
  const [open, setOpen] = useState(false);
  // Portal mount gate: document.body only exists client-side.
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shot, setShot] = useState<string | null>(null);
  const [frame, setFrame] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);

  useEffect(() => {
    if (open) {
      setMessage('');
      setSent(false);
      setSending(false);
      setError(null);
      setShot(null);
      setFrame(null);
      setCapturing(false);
    }
  }, [open]);

  const capture = async () => {
    if (capturing) return;
    setError(null);
    setCapturing(true); // hides the modal so it isn't in the shot
    try {
      const png = await captureTabFrame();
      setFrame(png); // opens the crop overlay
    } catch {
      setError('Screen capture was cancelled or blocked.');
      setCapturing(false);
    }
  };

  const onCrop = useCallback((dataUrl: string) => {
    setShot(dataUrl);
    setFrame(null);
    setCapturing(false);
  }, []);

  const onCropCancel = useCallback(() => {
    setFrame(null);
    setCapturing(false);
  }, []);

  const submit = async () => {
    const text = message.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    try {
      const res = await fetch(`${BASE}/api/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ comment: text, screenshot: shot ?? undefined }),
      });
      if (res.ok) setSent(true);
      else setError('Could not send feedback');
    } catch {
      setError('Could not send feedback');
    }
    setSending(false);
  };

  if (!mounted) return null;
  return createPortal(
    <>
      {/* Floating feedback trigger: fixed bottom-right so it is reachable on
          every page without scrolling, and rendered through a portal so a
          transformed/collapsed sidebar ancestor can't reposition it. */}
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="Send feedback to the AI team"
        aria-label="Send feedback"
        className="fixed bottom-5 right-5 z-40 flex items-center justify-center w-12 h-12 rounded-full bg-[#500000] text-white shadow-lg shadow-black/30 ring-1 ring-white/25 hover:bg-[#3c001c] hover:scale-105 transition-all duration-150"
      >
        <MessageSquarePlus size={21} strokeWidth={1.9} />
      </button>

      {frame && <CropOverlay frame={frame} onDone={onCrop} onCancel={onCropCancel} />}

      {open && !frame && (
        <div
          className={`fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 ${capturing ? 'invisible' : ''}`}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          <div className="bg-[#FFFFFF] w-full max-w-lg rounded-xl border border-[#D6D3C4] shadow-xl overflow-hidden">
            <div className="flex items-center justify-between px-5 py-4 border-b border-[#D6D3C4]">
              <div className="flex items-center gap-3">
                <span className="w-[3px] h-5 bg-[#500000] rounded-full" />
                <span className="text-[11px] font-bold tracking-[0.16em] uppercase text-[#500000]">
                  Send Feedback
                </span>
              </div>
              <button
                onClick={() => setOpen(false)}
                className="text-[#707070] hover:text-[#500000] transition-colors duration-150"
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>

            {sent ? (
              <div className="px-5 py-8 text-center space-y-3">
                <MessageSquarePlus size={28} className="mx-auto text-[#500000]" />
                <p className="text-sm text-[#1A1A1A]">Thanks, your feedback was recorded.</p>
                <div className="flex justify-center pt-1">
                  <button
                    onClick={() => setOpen(false)}
                    className="bg-[#500000] text-white text-xs font-bold tracking-[0.12em] uppercase px-5 py-2 rounded-md hover:bg-[#3c001c] transition-colors duration-150"
                  >
                    Done
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="px-5 py-5 space-y-3">
                  <p className="text-xs text-[#707070] leading-relaxed">
                    Tell us what is working, what is missing, or anything we could improve
                    about the Deep Research Agent. This goes to the AI team.
                  </p>
                  <textarea
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    rows={6}
                    autoFocus
                    placeholder="Your feedback…"
                    className="w-full resize-none border border-[#D6D3C4] bg-[#FFFFFF] text-[#1A1A1A] text-sm px-4 py-3 rounded-md focus:outline-none focus:border-[#500000] transition-colors duration-150 leading-relaxed"
                  />

                  {shot ? (
                    <div className="relative inline-block">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={shot}
                        alt="Attached screenshot"
                        className="max-h-28 rounded-md border border-[#D6D3C4]"
                      />
                      <button
                        onClick={() => setShot(null)}
                        title="Remove screenshot"
                        className="absolute -top-2 -right-2 bg-[#500000] text-white rounded-full p-1 hover:bg-[#3c001c]"
                      >
                        <X size={11} />
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={capture}
                      disabled={capturing}
                      className="inline-flex items-center gap-2 text-xs text-[#500000] border border-[#D6D3C4] hover:border-[#500000] rounded-md px-3 py-2 transition-colors duration-150 disabled:opacity-50"
                    >
                      {capturing ? <LoaderCircle size={13} className="animate-spin" /> : <Camera size={13} />}
                      Attach screenshot
                    </button>
                  )}

                  {error && <p className="text-xs text-[#B91C1C]">{error}</p>}
                </div>
                <div className="flex justify-end gap-3 px-5 pb-5">
                  <button
                    onClick={() => setOpen(false)}
                    className="text-xs font-bold tracking-[0.12em] uppercase text-[#707070] hover:text-[#1A1A1A] transition-colors duration-150 px-3 py-2"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={submit}
                    disabled={!message.trim() || sending}
                    className="bg-[#500000] text-white text-xs font-bold tracking-[0.12em] uppercase px-5 py-2 rounded-md hover:bg-[#3c001c] disabled:opacity-40 transition-colors duration-150"
                  >
                    {sending ? 'Sending…' : 'Send'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>,
    document.body
  );
}
