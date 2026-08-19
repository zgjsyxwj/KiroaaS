import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  fetchModelCatalog,
  useModelCatalog,
  type ModelCatalogFetcher,
} from './modelCatalog';

function modelResponse(ids: string[]): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ data: ids.map((id) => ({ id })) }),
  } as Response;
}

describe('Model Catalog network boundary', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('keeps the backend order and sends the configured bearer key', async () => {
    const fetcher = vi.fn<ModelCatalogFetcher>().mockResolvedValue(
      modelResponse(['gpt-5.6-terra', 'gpt-5.6-sol', 'runtime-special']),
    );

    await expect(fetchModelCatalog('0.0.0.0', 8123, 'secret', fetcher)).resolves.toEqual([
      { id: 'gpt-5.6-terra' },
      { id: 'gpt-5.6-sol' },
      { id: 'runtime-special' },
    ]);
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:8123/v1/models',
      { headers: { Authorization: 'Bearer secret' } },
    );
  });

  it('rejects a failed backend response instead of fabricating models', async () => {
    const fetcher = vi.fn<ModelCatalogFetcher>().mockResolvedValue({
      ok: false,
      status: 503,
    } as Response);

    await expect(fetchModelCatalog('127.0.0.1', 8000, 'secret', fetcher)).rejects.toThrow('503');
  });
});

describe('useModelCatalog', () => {
  const fetchMock = vi.fn<ModelCatalogFetcher>();

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('reports offline without calling the backend when the server is stopped', () => {
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() => useModelCatalog('127.0.0.1', 8000, 'secret', false));

    expect(result.current.status).toBe('offline');
    expect(result.current.models).toEqual([]);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('exposes the exact online catalog returned by the backend', async () => {
    fetchMock.mockResolvedValueOnce(modelResponse(['gpt-5.6-sol', 'claude-sonnet-4.6']));
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() => useModelCatalog('127.0.0.1', 8000, 'secret', true));

    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.models.map((model) => model.id)).toEqual([
      'gpt-5.6-sol',
      'claude-sonnet-4.6',
    ]);
  });

  it('reports an empty backend catalog without adding a fallback', async () => {
    fetchMock.mockResolvedValueOnce(modelResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() => useModelCatalog('127.0.0.1', 8000, 'secret', true));

    await waitFor(() => expect(result.current.status).toBe('empty'));
    expect(result.current.models).toEqual([]);
  });

  it('reports a failed backend catalog without retaining stale IDs after refresh', async () => {
    fetchMock
      .mockResolvedValueOnce(modelResponse(['old-model']))
      .mockResolvedValueOnce({ ok: false, status: 503 } as Response);
    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() => useModelCatalog('127.0.0.1', 8000, 'secret', true));

    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.status).toBe('error');
    expect(result.current.models).toEqual([]);
  });
});
