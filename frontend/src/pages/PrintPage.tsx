/**
 * One-click print flow (docs/auto-print-pipeline-spec.md).
 *
 * The user picks a file, a filament type and a colour (quality + layer
 * height default from what's actually loaded in the fleet). Everything
 * else -- printer choice, slicing profiles, orientation, slicing, queueing
 * -- happens automatically on the backend's `/auto-print/` flow.
 *
 * This is a separate, additional surface: the existing SliceModal /
 * PrintModal expert flow is untouched.
 */

import { useEffect, useMemo, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  Clock,
  Disc3,
  Eye,
  File as FileIcon,
  FolderUp,
  Gauge,
  Info,
  Layers,
  Loader2,
  Palette,
  PlayCircle,
  Printer as PrinterIcon,
  RotateCcw,
  Weight,
  XCircle,
  type LucideIcon,
} from 'lucide-react';
import { api } from '../api/client';
import type { AutoPrintFlow, AutoPrintRequest, AutoPrintStage } from '../api/client';
import { Card, CardContent, CardHeader } from '../components/Card';
import { Button } from '../components/Button';
import { Toggle } from '../components/Toggle';
import { Collapsible } from '../components/Collapsible';
import { ModelViewer } from '../components/ModelViewer';
import { GcodeToolpathViewer } from '../components/GcodeToolpathViewer';
import { getColorName } from '../utils/colors';

const ACCEPTED_EXTENSIONS = ['.gcode', '.3mf', '.stl', '.obj', '.amf'];
// Formats ModelViewer can actually render (see ModelViewer.tsx) -- .gcode is
// handled by the toolpath viewer once sliced, .obj/.amf have no preview yet.
const MESH_PREVIEW_EXTENSIONS = new Set(['stl', '3mf']);

// UI-facing stage order: "upload -> analyse -> printer -> slice -> review ->
// queued". `pending` (the flow row exists but the worker hasn't started)
// collapses into the same "upload" step -- there is nothing else to show the
// user yet. `review` is the new `awaiting_approval` pause: sliced, nothing
// queued, waiting on the user.
const STAGE_STEPS: { key: string; stage: AutoPrintStage }[] = [
  { key: 'upload', stage: 'uploading' },
  { key: 'analyse', stage: 'analysing' },
  { key: 'printer', stage: 'printer_selected' },
  { key: 'slice', stage: 'slicing' },
  { key: 'review', stage: 'awaiting_approval' },
  { key: 'queued', stage: 'queued' },
];
const STAGE_INDEX: Record<AutoPrintStage, number> = {
  pending: 0,
  uploading: 0,
  analysing: 1,
  printer_selected: 2,
  slicing: 3,
  awaiting_approval: 4,
  queued: 5,
  // Stopped at the review step and never queued -- same position as
  // awaiting_approval, just a different terminal outcome.
  discarded: 4,
  failed: -1,
};

// Stages that end polling: two true terminal outcomes (queued, failed) plus
// discarded (also terminal -- nothing further happens to this flow) and
// awaiting_approval, which is not terminal but *is* a pause -- it sits there
// until the user acts, so there is nothing to poll for either.
const POLL_STOP_STAGES: ReadonlySet<AutoPrintStage> = new Set([
  'queued',
  'failed',
  'discarded',
  'awaiting_approval',
]);

// Stages where the flow is genuinely done and the form should unlock for a
// new submission. awaiting_approval is deliberately excluded -- the user
// must approve or discard first.
const FORM_UNLOCK_STAGES: ReadonlySet<AutoPrintStage> = new Set(['queued', 'failed', 'discarded']);

const BRIM_STORAGE_KEY = 'autoPrintBrim';
const BRIM_WIDTH_STORAGE_KEY = 'autoPrintBrimWidth';

function loadStoredBrim(): boolean {
  try {
    const stored = localStorage.getItem(BRIM_STORAGE_KEY);
    return stored == null ? true : stored === 'true';
  } catch {
    return true;
  }
}

function loadStoredBrimWidth(): number {
  try {
    const stored = localStorage.getItem(BRIM_WIDTH_STORAGE_KEY);
    const n = stored != null ? Number(stored) : NaN;
    return Number.isFinite(n) && n >= 0 ? n : 5;
  } catch {
    return 5;
  }
}

function fileExtension(name: string): string {
  const idx = name.lastIndexOf('.');
  return idx >= 0 ? name.slice(idx + 1).toLowerCase() : '';
}

function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds == null) return null;
  const totalMinutes = Math.max(1, Math.round(seconds / 60));
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

/** Faint perspective bed grid shown before any preview is available. */
function EmptyPreview({ label }: { label: string }) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-3 bg-bambu-dark/40">
      <div
        className="h-24 w-40 opacity-25"
        style={{
          backgroundImage:
            'linear-gradient(rgba(148,163,184,0.7) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,0.7) 1px, transparent 1px)',
          backgroundSize: '14px 14px',
          transform: 'perspective(360px) rotateX(55deg)',
        }}
        aria-hidden
      />
      <span className="text-xs text-bambu-gray">{label}</span>
    </div>
  );
}

function SummaryRow({
  icon: Icon,
  label,
  value,
  swatchColor,
  rendering,
  renderingLabel,
}: {
  icon: LucideIcon;
  label: string;
  value: string | null;
  swatchColor?: string | null;
  /** When set, shows `renderingLabel` in place of `value` (see PrintPage's
   * gcodeReady tracking -- the sliced numbers wait on the toolpath viewer). */
  rendering?: boolean;
  renderingLabel?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="flex items-center gap-2 text-bambu-gray">
        <Icon className="h-4 w-4 shrink-0" />
        {label}
      </span>
      {rendering ? (
        <span className="flex shrink-0 items-center gap-1.5 text-xs text-bambu-gray">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {renderingLabel}
        </span>
      ) : (
        <span className="flex min-w-0 items-center gap-1.5 truncate text-white">
          {swatchColor && value && (
            <span
              className="h-3 w-3 shrink-0 rounded-full border border-white/20"
              style={{ backgroundColor: swatchColor }}
              aria-hidden
            />
          )}
          <span className="truncate">{value ?? '—'}</span>
        </span>
      )}
    </div>
  );
}

function StageIndicator({ flow }: { flow: AutoPrintFlow }) {
  const { t } = useTranslation();
  const activeIndex = STAGE_INDEX[flow.stage];
  return (
    <div className="space-y-2" role="status">
      <div className="flex items-center gap-1.5">
        {STAGE_STEPS.map((step, i) => (
          <div
            key={step.key}
            className={`h-1.5 flex-1 rounded-full transition-colors ${
              flow.stage === 'failed'
                ? 'bg-red-900/50'
                : flow.stage === 'discarded'
                  ? i <= activeIndex
                    ? 'bg-bambu-gray/50'
                    : 'bg-bambu-dark-tertiary'
                  : i <= activeIndex
                    ? 'bg-bambu-green'
                    : 'bg-bambu-dark-tertiary'
            }`}
          />
        ))}
      </div>
      <div className="flex items-center justify-between text-[0.65rem] text-bambu-gray">
        {STAGE_STEPS.map((step, i) => (
          <span
            key={step.key}
            className={i === activeIndex && flow.stage !== 'failed' ? 'text-white' : ''}
          >
            {t(`print.status.stage.${step.key}`)}
          </span>
        ))}
      </div>
      {!POLL_STOP_STAGES.has(flow.stage) && (
        <div className="flex items-center gap-2 text-xs text-bambu-gray">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <span>{flow.stage_detail || t(`print.status.stage.${STAGE_STEPS[Math.max(0, activeIndex)].key}`)}</span>
        </div>
      )}
    </div>
  );
}

const selectClassName =
  'w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-sm text-white focus:border-bambu-green focus:outline-none focus:ring-1 focus:ring-bambu-green disabled:cursor-not-allowed disabled:opacity-50';

export function PrintPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [filamentType, setFilamentType] = useState('');
  const [colorHex, setColorHex] = useState('');
  const [quality, setQuality] = useState('');
  const [layerHeight, setLayerHeight] = useState<number | null>(null);
  const [autoOrient, setAutoOrient] = useState(true);
  const [autoArrange, setAutoArrange] = useState(true);
  const [printerOverride, setPrinterOverride] = useState<number | null>(null);
  // Inner + outer brim, default on at 5mm; remembered across visits per the
  // spec's "standing behaviour" requirement.
  const [brim, setBrim] = useState<boolean>(() => loadStoredBrim());
  const [brimWidth, setBrimWidth] = useState<number>(() => loadStoredBrimWidth());
  useEffect(() => {
    try {
      localStorage.setItem(BRIM_STORAGE_KEY, String(brim));
    } catch {
      // Private mode / quota failures shouldn't break the page.
    }
  }, [brim]);
  useEffect(() => {
    try {
      localStorage.setItem(BRIM_WIDTH_STORAGE_KEY, String(brimWidth));
    } catch {
      // Private mode / quota failures shouldn't break the page.
    }
  }, [brimWidth]);

  const [flowId, setFlowId] = useState<number | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  // Set once GcodeToolpathViewer's onReady fires for the current preview, so
  // the summary can withhold the sliced numbers until the viewer showing
  // them is actually up (see GcodeToolpathViewer's onReady prop).
  const [gcodeReady, setGcodeReady] = useState(false);

  const optionsQuery = useQuery({
    queryKey: ['auto-print-options'],
    queryFn: () => api.getAutoPrintOptions(),
    staleTime: 30_000,
  });

  const printersQuery = useQuery({
    queryKey: ['printers'],
    queryFn: () => api.getPrinters(),
    staleTime: 60_000,
  });

  // Distinct filament types actually loaded somewhere in the fleet.
  const filamentTypes = useMemo(() => {
    const types = new Set<string>();
    for (const f of optionsQuery.data?.filaments ?? []) types.add(f.filament_type);
    return Array.from(types).sort();
  }, [optionsQuery.data]);

  // Colours loaded for the selected filament type.
  const colorOptions = useMemo(
    () => (optionsQuery.data?.filaments ?? []).filter((f) => f.filament_type === filamentType),
    [optionsQuery.data, filamentType],
  );

  // Default the filament type once options arrive; keep a manual pick.
  useEffect(() => {
    if (!filamentType && filamentTypes.length > 0) {
      setFilamentType(filamentTypes[0]);
    }
  }, [filamentTypes, filamentType]);

  // Keep the colour valid for the selected type -- re-picks on first load and
  // whenever the type changes out from under the current colour.
  useEffect(() => {
    if (colorOptions.length === 0) {
      setColorHex('');
      return;
    }
    if (!colorOptions.some((c) => c.color_hex === colorHex)) {
      setColorHex(colorOptions[0].color_hex);
    }
  }, [colorOptions, colorHex]);

  // Quality / layer height default from the options endpoint, applied once.
  const defaultsAppliedRef = useRef(false);
  useEffect(() => {
    if (defaultsAppliedRef.current || !optionsQuery.data) return;
    defaultsAppliedRef.current = true;
    setQuality(optionsQuery.data.defaults.quality);
    setLayerHeight(optionsQuery.data.defaults.layer_height);
  }, [optionsQuery.data]);

  const layerHeightOptions = useMemo(
    () => optionsQuery.data?.quality_tiers.find((qt) => qt.tier === quality)?.layer_heights ?? [],
    [optionsQuery.data, quality],
  );

  // Keep the layer height valid for the selected tier.
  useEffect(() => {
    if (layerHeightOptions.length === 0) return;
    if (layerHeight == null || !layerHeightOptions.includes(layerHeight)) {
      setLayerHeight(layerHeightOptions[0]);
    }
  }, [layerHeightOptions, layerHeight]);

  // Local (pre-upload) mesh preview for stl/3mf, so the viewport isn't empty
  // while the user is still filling in the rest of the form.
  const [localPreviewUrl, setLocalPreviewUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!file || !MESH_PREVIEW_EXTENSIONS.has(fileExtension(file.name))) {
      setLocalPreviewUrl(null);
      return;
    }
    // Not implemented in jsdom (test environment); guard rather than crash --
    // the backend-served model_preview_url still covers the preview once the
    // flow reaches "analysing" there.
    if (typeof URL.createObjectURL !== 'function') {
      setLocalPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setLocalPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const flowQuery = useQuery({
    queryKey: ['auto-print-flow', flowId],
    queryFn: () => api.getAutoPrintFlow(flowId as number),
    enabled: flowId != null,
    // ~1 Hz while the flow is actively progressing; stop once it lands on a
    // terminal stage (queued/failed/discarded) or pauses at awaiting_approval
    // -- a paused flow has nothing new to report until the user acts.
    refetchInterval: (query) => {
      const stage = query.state.data?.stage;
      return stage != null && POLL_STOP_STAGES.has(stage) ? false : 1000;
    },
  });
  const flow = flowQuery.data ?? null;

  useEffect(() => {
    if (flow?.stage === 'queued') {
      queryClient.invalidateQueries({ queryKey: ['queue'] });
    }
  }, [flow?.stage, queryClient]);

  // The slice-derived summary rows (time, filament usage) hold off on the
  // gcode viewer's onReady, not on the flow reaching a stage -- reset
  // whenever the preview URL changes so a new flow doesn't show stale-fast
  // numbers off the old parse.
  useEffect(() => {
    setGcodeReady(false);
  }, [flow?.gcode_preview_url]);

  const printerStatusQuery = useQuery({
    queryKey: ['printerStatus', flow?.printer?.id],
    queryFn: () => api.getPrinterStatus(flow!.printer!.id),
    enabled: flow?.printer?.id != null,
    staleTime: 5_000,
  });

  const startMutation = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error(t('print.errors.noFile'));
      if (!filamentType) throw new Error(t('print.errors.noFilament'));
      const body: AutoPrintRequest = {
        filament_type: filamentType,
        color_hex: colorHex || null,
        quality: quality || 'Standard',
        layer_height: layerHeight,
        printer_id: printerOverride,
        auto_orient: autoOrient,
        auto_arrange: autoArrange,
        brim,
        brim_width: brimWidth,
      };
      return api.startAutoPrint(file, body);
    },
    onSuccess: (res) => {
      setSubmitError(null);
      setFlowId(res.id);
    },
    onError: (err: unknown) => {
      setSubmitError(err instanceof Error ? err.message : String(err));
    },
  });

  // Approve/discard only ever act on the flow currently on screen, which is
  // only reachable once flowId is set -- both write the response straight
  // into the flow's query cache so the UI (and refetchInterval, which reads
  // that same cache) updates immediately rather than waiting on a poll tick.
  const approveMutation = useMutation({
    mutationFn: () => api.approveAutoPrint(flowId as number),
    onSuccess: (res) => {
      queryClient.setQueryData(['auto-print-flow', flowId], res);
    },
  });
  const discardMutation = useMutation({
    mutationFn: () => api.discardAutoPrint(flowId as number),
    onSuccess: (res) => {
      queryClient.setQueryData(['auto-print-flow', flowId], res);
    },
  });

  // In flight between submit and a genuinely finished stage. The form stays
  // enabled once `failed`/`discarded` is reached (retry / start-over) or
  // `queued` (so the user can immediately start another print) -- but not at
  // `awaiting_approval`, which still needs a decision.
  const isRunning = flowId != null && flow != null && !FORM_UNLOCK_STAGES.has(flow.stage);
  const formDisabled = isRunning || startMutation.isPending;
  const canSubmit = !!file && !!filamentType && !formDisabled;

  const handleFileSelect = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0];
    if (picked) setFile(picked);
    e.target.value = '';
  };
  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (!formDisabled) setIsDragging(true);
  };
  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  };
  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    if (formDisabled) return;
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  };

  const handleRetry = () => {
    setFlowId(null);
    setSubmitError(null);
  };

  // Preview: gcode toolpath once slicing has produced one, else the mesh
  // (backend-served once uploaded, or a local object-URL before that), else
  // the empty bed placeholder.
  const meshPreviewUrl = flow?.model_preview_url ?? localPreviewUrl;
  const meshFileType = file ? fileExtension(file.name) : undefined;
  const canPreviewMesh = meshFileType != null && MESH_PREVIEW_EXTENSIONS.has(meshFileType);
  const filamentColors = colorHex ? [colorHex] : undefined;

  let previewNode: React.ReactNode;
  if (flow?.gcode_preview_url) {
    previewNode = (
      <GcodeToolpathViewer
        gcodeUrl={flow.gcode_preview_url}
        filamentColors={filamentColors}
        className="h-full w-full"
        onReady={() => setGcodeReady(true)}
      />
    );
  } else if (meshPreviewUrl && canPreviewMesh) {
    previewNode = (
      <ModelViewer
        url={meshPreviewUrl}
        fileType={meshFileType}
        filamentColors={filamentColors}
        className="h-full w-full"
      />
    );
  } else {
    previewNode = (
      <EmptyPreview label={file ? t('print.summary.noPreviewAvailable') : t('print.summary.noPreview')} />
    );
  }

  const selectedColor = colorOptions.find((c) => c.color_hex === colorHex);
  const colorName = selectedColor ? selectedColor.color_name || getColorName(selectedColor.color_hex, filamentType) : null;

  // The sliced numbers (time, filament usage) come from the same G-code the
  // toolpath viewer is parsing -- show them only once that parse is done, so
  // they don't appear ahead of the preview that visualises them.
  const waitingOnGcodePreview = flow?.gcode_preview_url != null && !gcodeReady;

  return (
    <div className="mx-auto max-w-7xl p-4 md:p-6">
      <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        {/* Left column */}
        <div className="space-y-6">
          <div>
            <h1 className="text-2xl font-semibold text-white">{t('print.title')}</h1>
            <p className="mt-1 text-sm text-bambu-gray">{t('print.subtitle')}</p>
          </div>

          {/* Dropzone */}
          <Card>
            <CardContent>
              <div
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => !formDisabled && fileInputRef.current?.click()}
                className={`rounded-lg border-2 border-dashed p-10 text-center transition-colors ${
                  formDisabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'
                } ${
                  isDragging
                    ? 'border-bambu-green bg-bambu-green/10'
                    : 'border-bambu-dark-tertiary hover:border-bambu-green/50'
                }`}
              >
                <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-lg bg-blue-500/15 text-blue-400">
                  <FolderUp className="h-6 w-6" />
                </div>
                <p className="font-medium text-white">{t('print.dropzone.title')}</p>
                <p className="mt-1 text-xs text-bambu-gray">{t('print.dropzone.supports')}</p>
                <Button
                  type="button"
                  size="sm"
                  className="mt-4"
                  disabled={formDisabled}
                  onClick={(e) => {
                    e.stopPropagation();
                    fileInputRef.current?.click();
                  }}
                >
                  {t('print.dropzone.chooseFile')}
                </Button>
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPTED_EXTENSIONS.join(',')}
                className="hidden"
                onChange={handleFileSelect}
                aria-label={t('print.dropzone.chooseFile')}
              />
              <p className="mt-3 truncate text-sm text-bambu-gray">
                {file ? file.name : t('print.dropzone.noFileSelected')}
              </p>
            </CardContent>
          </Card>

          {/* Filament + Print Options + Advanced + Print button */}
          <Card>
            <CardContent className="space-y-6">
              {/* Filament */}
              <div>
                <h2 className="font-medium text-white">{t('print.filament.title')}</h2>
                <p className="mt-1 text-sm text-bambu-gray">{t('print.filament.subtitle')}</p>
                {optionsQuery.isError && (
                  <p className="mt-2 text-sm text-red-400">{t('print.errors.optionsLoadFailed')}</p>
                )}
                <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label className="mb-1.5 flex items-center gap-1.5 text-xs text-bambu-gray">
                      <Disc3 className="h-3.5 w-3.5" />
                      {t('print.filament.typeLabel')}
                    </label>
                    <select
                      value={filamentType}
                      onChange={(e) => setFilamentType(e.target.value)}
                      disabled={formDisabled || optionsQuery.isLoading || filamentTypes.length === 0}
                      className={selectClassName}
                      aria-label={t('print.filament.typeLabel')}
                    >
                      {filamentTypes.length === 0 ? (
                        <option value="">{t('print.filament.noneLoaded')}</option>
                      ) : (
                        filamentTypes.map((ftype) => (
                          <option key={ftype} value={ftype}>
                            {ftype}
                          </option>
                        ))
                      )}
                    </select>
                  </div>
                  <div>
                    <label className="mb-1.5 flex items-center gap-1.5 text-xs text-bambu-gray">
                      <Palette className="h-3.5 w-3.5" />
                      {t('print.filament.colorLabel')}
                    </label>
                    <div className="relative">
                      {colorHex && (
                        <span
                          className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 rounded-full border border-white/20"
                          style={{ backgroundColor: colorHex }}
                          aria-hidden
                        />
                      )}
                      <select
                        value={colorHex}
                        onChange={(e) => setColorHex(e.target.value)}
                        disabled={formDisabled || colorOptions.length === 0}
                        className={`${selectClassName} ${colorHex ? 'pl-8' : ''}`}
                        aria-label={t('print.filament.colorLabel')}
                      >
                        {colorOptions.length === 0 ? (
                          <option value="">{t('print.filament.noColors')}</option>
                        ) : (
                          colorOptions.map((c) => (
                            <option key={c.color_hex} value={c.color_hex}>
                              {c.color_name || getColorName(c.color_hex, filamentType)}
                            </option>
                          ))
                        )}
                      </select>
                    </div>
                  </div>
                </div>
              </div>

              {/* Print Options */}
              <div>
                <h2 className="font-medium text-white">{t('print.options.title')}</h2>
                <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2">
                    <label className="flex items-center gap-1.5 text-xs text-bambu-gray">
                      <Gauge className="h-3.5 w-3.5" />
                      {t('print.options.quality')}
                    </label>
                    <div className="relative mt-1">
                      <select
                        value={quality}
                        onChange={(e) => setQuality(e.target.value)}
                        disabled={formDisabled || (optionsQuery.data?.quality_tiers.length ?? 0) === 0}
                        className="w-full appearance-none bg-transparent pr-5 text-sm font-medium text-white focus:outline-none focus:ring-1 focus:ring-bambu-green rounded disabled:opacity-50"
                        aria-label={t('print.options.quality')}
                      >
                        {(optionsQuery.data?.quality_tiers ?? []).map((qt) => (
                          <option key={qt.tier} value={qt.tier}>
                            {qt.tier}
                          </option>
                        ))}
                      </select>
                      <ChevronDown className="pointer-events-none absolute right-0 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-bambu-gray" />
                    </div>
                  </div>
                  <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2">
                    <label className="flex items-center gap-1.5 text-xs text-bambu-gray">
                      <Layers className="h-3.5 w-3.5" />
                      {t('print.options.layerHeight')}
                    </label>
                    <div className="relative mt-1">
                      <select
                        value={layerHeight ?? ''}
                        onChange={(e) => setLayerHeight(Number(e.target.value))}
                        disabled={formDisabled || layerHeightOptions.length === 0}
                        className="w-full appearance-none bg-transparent pr-5 text-sm font-medium text-white focus:outline-none focus:ring-1 focus:ring-bambu-green rounded disabled:opacity-50"
                        aria-label={t('print.options.layerHeight')}
                      >
                        {layerHeightOptions.map((lh) => (
                          <option key={lh} value={lh}>
                            {lh.toFixed(2)} mm
                          </option>
                        ))}
                      </select>
                      <ChevronDown className="pointer-events-none absolute right-0 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-bambu-gray" />
                    </div>
                  </div>
                </div>
              </div>

              {/* Advanced Settings */}
              <Collapsible
                summary={<span className="text-sm font-medium text-white">{t('print.advanced.title')}</span>}
                defaultOpen={false}
              >
                <div className="space-y-4 border-t border-bambu-dark-tertiary pt-4">
                  <label className="flex items-center justify-between gap-3">
                    <span className="text-sm text-bambu-gray">{t('print.advanced.autoOrient')}</span>
                    <Toggle checked={autoOrient} onChange={setAutoOrient} disabled={formDisabled} />
                  </label>
                  <label className="flex items-center justify-between gap-3">
                    <span className="text-sm text-bambu-gray">{t('print.advanced.autoArrange')}</span>
                    <Toggle checked={autoArrange} onChange={setAutoArrange} disabled={formDisabled} />
                  </label>
                  <div>
                    <label className="flex items-center justify-between gap-3">
                      <span className="text-sm text-bambu-gray">{t('print.advanced.brim')}</span>
                      <Toggle checked={brim} onChange={setBrim} disabled={formDisabled} />
                    </label>
                    <div className="mt-2 flex items-center justify-between gap-3">
                      <label htmlFor="print-brim-width" className="text-xs text-bambu-gray">
                        {t('print.advanced.brimWidth')}
                      </label>
                      <div className="flex items-center gap-1.5">
                        <input
                          id="print-brim-width"
                          type="number"
                          min={0}
                          max={50}
                          step={0.5}
                          value={brimWidth}
                          onChange={(e) => setBrimWidth(Number(e.target.value))}
                          disabled={formDisabled || !brim}
                          aria-label={t('print.advanced.brimWidth')}
                          className="w-20 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-2 py-1 text-sm text-white focus:border-bambu-green focus:outline-none focus:ring-1 focus:ring-bambu-green disabled:cursor-not-allowed disabled:opacity-50"
                        />
                        <span className="text-xs text-bambu-gray">{t('print.advanced.brimWidthUnit')}</span>
                      </div>
                    </div>
                  </div>
                  <div>
                    <label className="mb-1.5 block text-xs text-bambu-gray">
                      {t('print.advanced.printerOverride')}
                    </label>
                    <select
                      value={printerOverride ?? ''}
                      onChange={(e) => setPrinterOverride(e.target.value ? Number(e.target.value) : null)}
                      disabled={formDisabled}
                      className={selectClassName}
                      aria-label={t('print.advanced.printerOverride')}
                    >
                      <option value="">{t('print.advanced.printerAuto')}</option>
                      {(printersQuery.data ?? []).map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
              </Collapsible>

              {submitError && (
                <div className="flex items-start gap-2 rounded-lg border border-red-900/40 bg-red-900/20 p-3 text-sm text-red-400" role="alert">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{submitError}</span>
                </div>
              )}

              <Button
                type="button"
                size="lg"
                className="w-full"
                disabled={!canSubmit}
                onClick={() => startMutation.mutate()}
              >
                {/* At `awaiting_approval` the pipeline is not working, it is
                    waiting on the user — a spinner reading "Starting..." there
                    reads as "still busy" and competes with the Approve button
                    that is actually live. Show a settled label instead. */}
                {flow?.stage === 'awaiting_approval' ? (
                  <>
                    <Eye className="h-4 w-4" />
                    {t('print.actions.awaitingReview')}
                  </>
                ) : startMutation.isPending || isRunning ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    {t('print.actions.starting')}
                  </>
                ) : (
                  <>
                    <PlayCircle className="h-4 w-4" />
                    {t('print.actions.print')}
                  </>
                )}
              </Button>

              {flow && (
                <div className="space-y-3">
                  <StageIndicator flow={flow} />
                  {flow.stage === 'failed' && (
                    <div className="flex items-start gap-2 rounded-lg border border-red-900/40 bg-red-900/20 p-3 text-sm text-red-400" role="alert">
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                      <div>
                        <p>{flow.error || t('print.status.failedGeneric')}</p>
                        <button
                          type="button"
                          onClick={handleRetry}
                          className="mt-1 text-xs text-red-300 underline hover:text-red-200"
                        >
                          {t('print.status.retry')}
                        </button>
                      </div>
                    </div>
                  )}
                  {flow.stage === 'awaiting_approval' && (
                    <div
                      className="space-y-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-300"
                      role="alert"
                    >
                      <div className="flex items-start gap-2">
                        <Eye className="mt-0.5 h-4 w-4 shrink-0" />
                        <span>{t('print.status.awaitingApproval')}</span>
                      </div>
                      {(approveMutation.isError || discardMutation.isError) && (
                        <p className="text-xs text-red-400">
                          {approveMutation.error instanceof Error
                            ? approveMutation.error.message
                            : discardMutation.error instanceof Error
                              ? discardMutation.error.message
                              : t('print.status.approvalActionFailed')}
                        </p>
                      )}
                      <div className="flex gap-2">
                        <Button
                          type="button"
                          size="sm"
                          disabled={approveMutation.isPending || discardMutation.isPending}
                          onClick={() => approveMutation.mutate()}
                        >
                          {approveMutation.isPending ? (
                            <>
                              <Loader2 className="h-4 w-4 animate-spin" />
                              {t('print.actions.approving')}
                            </>
                          ) : (
                            <>
                              <CheckCircle2 className="h-4 w-4" />
                              {t('print.actions.approve')}
                            </>
                          )}
                        </Button>
                        <Button
                          type="button"
                          variant="secondary"
                          size="sm"
                          disabled={approveMutation.isPending || discardMutation.isPending}
                          onClick={() => discardMutation.mutate()}
                        >
                          {discardMutation.isPending ? (
                            <>
                              <Loader2 className="h-4 w-4 animate-spin" />
                              {t('print.actions.discarding')}
                            </>
                          ) : (
                            <>
                              <XCircle className="h-4 w-4" />
                              {t('print.actions.discard')}
                            </>
                          )}
                        </Button>
                      </div>
                    </div>
                  )}
                  {flow.stage === 'discarded' && (
                    <div className="flex items-center gap-2 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3 text-sm text-bambu-gray">
                      <Info className="h-4 w-4 shrink-0" />
                      <span>{t('print.status.discarded')}</span>
                      <button
                        type="button"
                        onClick={handleRetry}
                        className="ml-auto inline-flex shrink-0 items-center gap-1 text-xs text-white underline hover:no-underline"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        {t('print.status.startOver')}
                      </button>
                    </div>
                  )}
                  {flow.stage === 'queued' && (
                    <div className="flex items-center gap-2 rounded-lg border border-bambu-green/40 bg-bambu-green/10 p-3 text-sm text-bambu-green">
                      <CheckCircle2 className="h-4 w-4 shrink-0" />
                      <span>{t('print.status.queuedSuccess')}</span>
                      <Link to="/queue" className="ml-auto inline-flex shrink-0 items-center gap-1 underline hover:no-underline">
                        {t('print.status.viewQueue')}
                        <ArrowRight className="h-3.5 w-3.5" />
                      </Link>
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right column */}
        <div className="space-y-6">
          <Card>
            <CardHeader className="flex items-center justify-between">
              <h2 className="font-medium text-white">{t('print.summary.title')}</h2>
              <span title={t('print.summary.infoTooltip')}>
                <Info className="h-4 w-4 text-bambu-gray" />
              </span>
            </CardHeader>
            <CardContent className="space-y-4">
              {flow?.stage === 'awaiting_approval' && (
                <div className="flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs font-medium text-amber-300">
                  <Eye className="h-3.5 w-3.5 shrink-0" />
                  {t('print.status.notQueuedYet')}
                </div>
              )}
              <div
                className={`relative overflow-hidden rounded-lg border border-bambu-dark-tertiary transition-all ${
                  flow?.stage === 'awaiting_approval' ? 'h-80' : 'h-56'
                }`}
              >
                {previewNode}
              </div>
              <dl className="space-y-2.5">
                <SummaryRow icon={FileIcon} label={t('print.summary.file')} value={file?.name ?? null} />
                <SummaryRow icon={Disc3} label={t('print.summary.filament')} value={filamentType || null} />
                <SummaryRow
                  icon={Palette}
                  label={t('print.summary.color')}
                  value={colorName}
                  swatchColor={colorHex || null}
                />
                <SummaryRow
                  icon={Clock}
                  label={t('print.summary.estimatedTime')}
                  value={formatDuration(flow?.estimate.print_time_seconds)}
                  rendering={waitingOnGcodePreview}
                  renderingLabel={t('print.summary.renderingPreview')}
                />
                <SummaryRow
                  icon={Weight}
                  label={t('print.summary.filamentUsage')}
                  value={
                    flow?.estimate.filament_used_g != null
                      ? `${flow.estimate.filament_used_g.toFixed(1)} g`
                      : null
                  }
                  rendering={waitingOnGcodePreview}
                  renderingLabel={t('print.summary.renderingPreview')}
                />
                <SummaryRow
                  icon={Layers}
                  label={t('print.summary.layerHeight')}
                  value={layerHeight != null ? `${layerHeight.toFixed(2)} mm` : null}
                />
                <SummaryRow icon={PrinterIcon} label={t('print.summary.printer')} value={flow?.printer?.name ?? null} />
              </dl>
            </CardContent>
          </Card>

          {flow?.printer && (
            <Card>
              <CardHeader>
                <h2 className="font-medium text-white">{t('print.printerStatus.title')}</h2>
              </CardHeader>
              <CardContent>
                <div className="flex items-center gap-3">
                  <div className="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-bambu-dark-tertiary bg-bambu-dark">
                    {printerStatusQuery.data?.cover_url ? (
                      <img src={printerStatusQuery.data.cover_url} alt="" className="h-full w-full object-cover" />
                    ) : (
                      <PrinterIcon className="h-6 w-6 text-bambu-gray" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span
                        className={`h-2 w-2 rounded-full ${
                          printerStatusQuery.data?.connected ? 'bg-bambu-green' : 'bg-red-500'
                        }`}
                        aria-hidden
                      />
                      <span className="text-xs text-bambu-gray">
                        {printerStatusQuery.data?.connected
                          ? t('print.printerStatus.online')
                          : t('print.printerStatus.offline')}
                      </span>
                    </div>
                    <p className="truncate font-medium text-white">{flow.printer.name}</p>
                    <p className="text-xs text-bambu-gray">{flow.printer.model}</p>
                  </div>
                </div>
                <p className="mt-3 text-xs text-bambu-gray">{flow.printer.reason}</p>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
