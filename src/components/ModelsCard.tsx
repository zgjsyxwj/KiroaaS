import { useState } from 'react';
import type { MouseEvent } from 'react';
import { Sparkles, Loader2, ChevronDown, Copy, Check, RefreshCw } from 'lucide-react';
import { useI18n } from '@/hooks/useI18n';
import {
  MODEL_CATALOG_STATUS_KEYS,
  type ModelCatalogState,
} from '@/lib/modelCatalog';

interface ModelsCardProps {
  catalog: ModelCatalogState;
  isRunning: boolean;
  onRefresh: () => Promise<void>;
}

export function ModelsCard({ catalog, isRunning, onRefresh }: ModelsCardProps) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const { models, status } = catalog;
  const canToggle = status === 'ready' && models.length > 0;
  const statusMessage = status === 'ready' ? null : t(MODEL_CATALOG_STATUS_KEYS[status]);

  const copyId = (event: MouseEvent, id: string) => {
    event.stopPropagation();
    navigator.clipboard?.writeText(id).then(() => {
      setCopiedId(id);
      setTimeout(() => setCopiedId((current) => (current === id ? null : current)), 1200);
    });
  };

  const toggle = () => {
    if (canToggle) setExpanded((value) => !value);
  };

  const handleRefresh = (event: MouseEvent) => {
    event.stopPropagation();
    void onRefresh();
  };

  return (
    <div className="relative flex-1 min-h-[92px]">
      <div
        className={`absolute inset-x-0 top-0 bg-[#111] text-white border-0 rounded-[32px] overflow-hidden origin-top transition-all duration-300 ease-out ${
          expanded
            ? 'shadow-2xl shadow-black/40 z-20 scale-[1.02] ring-1 ring-white/10'
            : 'shadow-sm hover:shadow-lg hover:bg-[#1a1a1a]'
        }`}
      >
        <div className="absolute top-0 right-0 w-24 h-24 bg-stone-800 rounded-full blur-2xl -translate-y-1/2 translate-x-1/2 opacity-50 pointer-events-none" />

        <div className="px-6 py-4 flex items-stretch justify-center min-h-[92px] relative z-10">
          <button
            type="button"
            onClick={toggle}
            disabled={!canToggle}
            aria-expanded={canToggle ? expanded : undefined}
            className={`flex-1 min-w-0 text-left disabled:cursor-default ${canToggle ? 'cursor-pointer' : ''}`}
          >
            <div className="flex items-center gap-2">
              <Sparkles className="h-3 w-3 text-stone-400" aria-hidden="true" />
              <span className="text-stone-400 font-semibold text-[10px] tracking-wider uppercase">
                {t('supportedModels')}
              </span>
            </div>
            <div className="flex items-baseline gap-2 mt-1">
              {status === 'loading' ? (
                <div className="flex items-center gap-2" role="status" aria-live="polite">
                  <Loader2 className="h-5 w-5 animate-spin text-stone-400" aria-hidden="true" />
                  <span className="text-xs text-stone-400">{statusMessage}</span>
                </div>
              ) : status === 'ready' ? (
                <span className="text-2xl font-bold tracking-tight" role="status" aria-live="polite">
                  {models.length}
                </span>
              ) : (
                <span className="text-xs text-stone-400" role="status" aria-live="polite">
                  {statusMessage}
                </span>
              )}
            </div>
            {status === 'ready' && models.length > 0 && !expanded && (
              <span className="text-[10px] text-stone-400 font-medium mt-1 block">
                {t('modelsClickToView')}
              </span>
            )}
          </button>
          <div className="flex items-start gap-1">
            {isRunning && status !== 'loading' && (
              <button
                type="button"
                onClick={handleRefresh}
                aria-label={t('modelsRefresh')}
                title={t('modelsRefresh')}
                className="h-7 w-7 rounded-full flex items-center justify-center text-stone-400 hover:text-white hover:bg-white/10 transition-colors"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            )}
            {canToggle && (
              <ChevronDown
                className={`h-4 w-4 mt-1 text-stone-400 transition-transform duration-300 ${
                  expanded ? 'rotate-180' : ''
                }`}
                aria-hidden="true"
              />
            )}
          </div>
        </div>

        <div
          className="grid transition-[grid-template-rows] duration-300 ease-out relative z-10"
          style={{ gridTemplateRows: expanded ? '1fr' : '0fr' }}
          aria-hidden={!expanded}
        >
          <div className="overflow-hidden">
            <div
              className="px-6 pb-5 pt-1 space-y-1.5 max-h-[320px] overflow-y-auto"
              onClick={(event) => event.stopPropagation()}
              role="list"
            >
              {models.map((model, index) => (
                <div
                  key={model.id}
                  className={`group flex items-center gap-2 px-3 py-1 bg-white/5 hover:bg-white/10 border border-white/5 rounded-lg transition-all ${
                    expanded ? 'opacity-100 translate-y-0' : 'opacity-0 -translate-y-1'
                  }`}
                  style={{
                    transitionDuration: '250ms',
                    transitionDelay: expanded ? `${Math.min(index * 20, 200)}ms` : '0ms',
                  }}
                  title={model.description || model.id}
                  role="listitem"
                >
                  <span className="text-xs font-mono text-white truncate flex-1 min-w-0">{model.id}</span>
                  <button
                    type="button"
                    onClick={(event) => copyId(event, model.id)}
                    tabIndex={expanded ? 0 : -1}
                    aria-label={`Copy ${model.id}`}
                    className={`h-6 w-6 rounded-md flex items-center justify-center shrink-0 transition-all ${
                      copiedId === model.id
                        ? 'bg-lime-500/20 text-lime-300 opacity-100'
                        : 'text-stone-400 hover:text-white hover:bg-white/10 opacity-0 group-hover:opacity-100'
                    }`}
                  >
                    {copiedId === model.id ? (
                      <Check className="h-3.5 w-3.5" aria-hidden="true" />
                    ) : (
                      <Copy className="h-3 w-3" aria-hidden="true" />
                    )}
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
