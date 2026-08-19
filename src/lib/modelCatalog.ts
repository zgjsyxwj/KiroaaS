import { useCallback, useEffect, useRef, useState } from 'react';
import type { TranslationKey } from '@/lib/i18n';

export interface ModelCatalogEntry {
  id: string;
  description?: string;
}

export type ModelCatalogStatus = 'offline' | 'loading' | 'ready' | 'empty' | 'error';

export interface ModelCatalogState {
  models: ModelCatalogEntry[];
  status: ModelCatalogStatus;
  error: string | null;
}

export const MODEL_CATALOG_STATUS_KEYS: Record<Exclude<ModelCatalogStatus, 'ready'>, TranslationKey> = {
  offline: 'modelsServerOffline',
  loading: 'modelsLoading',
  empty: 'modelsEmpty',
  error: 'modelsLoadFailed',
};

const MODEL_CATALOG_REFRESH_INTERVAL_MS = 10 * 60 * 1000;

type ModelCatalogPayload = {
  data?: unknown;
};

export type ModelCatalogFetcher = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export function modelCatalogUrl(host: string, port: number): string {
  const fetchHost = host === '0.0.0.0' ? '127.0.0.1' : host;
  return `http://${fetchHost}:${port}/v1/models`;
}

export async function fetchModelCatalog(
  host: string,
  port: number,
  apiKey: string,
  fetcher: ModelCatalogFetcher = globalThis.fetch,
): Promise<ModelCatalogEntry[]> {
  const response = await fetcher(modelCatalogUrl(host, port), {
    headers: { Authorization: `Bearer ${apiKey}` },
  });

  if (!response.ok) {
    throw new Error(`Model Catalog request failed with HTTP ${response.status}`);
  }

  const payload = (await response.json()) as ModelCatalogPayload;
  if (!Array.isArray(payload.data)) {
    throw new Error('Model Catalog response did not contain a data array');
  }

  return payload.data.flatMap((entry): ModelCatalogEntry[] => {
    if (!entry || typeof entry !== 'object') return [];
    const candidate = entry as { id?: unknown; description?: unknown };
    if (typeof candidate.id !== 'string' || candidate.id.trim() === '') return [];
    return [{
      id: candidate.id,
      ...(typeof candidate.description === 'string'
        ? { description: candidate.description }
        : {}),
    }];
  });
}

const OFFLINE_MODEL_CATALOG_STATE: ModelCatalogState = {
  models: [],
  status: 'offline',
  error: null,
};

export function useModelCatalog(
  host: string,
  port: number,
  apiKey: string,
  isRunning: boolean,
): ModelCatalogState & { refresh: () => Promise<void> } {
  const [state, setState] = useState<ModelCatalogState>(OFFLINE_MODEL_CATALOG_STATE);
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestIdRef.current;

    if (!isRunning || !apiKey) {
      setState(OFFLINE_MODEL_CATALOG_STATE);
      return;
    }

    // A refresh replaces the complete snapshot. It must never retain a
    // previous catalog while a new backend snapshot is being resolved.
    setState({ models: [], status: 'loading', error: null });

    try {
      const models = await fetchModelCatalog(host, port, apiKey);
      if (requestId !== requestIdRef.current) return;
      setState({
        models,
        status: models.length > 0 ? 'ready' : 'empty',
        error: null,
      });
    } catch (error) {
      if (requestId !== requestIdRef.current) return;
      setState({
        models: [],
        status: 'error',
        error: error instanceof Error ? error.message : 'Unknown Model Catalog error',
      });
    }
  }, [apiKey, host, isRunning, port]);

  useEffect(() => {
    if (!isRunning || !apiKey) {
      requestIdRef.current += 1;
      setState(OFFLINE_MODEL_CATALOG_STATE);
      return;
    }

    void refresh();
    const intervalId = window.setInterval(() => {
      void refresh();
    }, MODEL_CATALOG_REFRESH_INTERVAL_MS);

    return () => {
      requestIdRef.current += 1;
      window.clearInterval(intervalId);
    };
  }, [apiKey, isRunning, refresh]);

  return { ...state, refresh };
}
