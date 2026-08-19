import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ModelsCard } from './ModelsCard';
import { ChatView, type Message } from './ChatView';
import type { ModelCatalogState } from '@/lib/modelCatalog';

vi.mock('@/hooks/useI18n', () => ({
  useI18n: () => ({
    t: (key: string) => ({
      supportedModels: 'Supported Models',
      modelsServerOffline: 'Start the server to view models',
      modelsLoadFailed: 'Failed to load models',
      modelsEmpty: 'No models available',
      modelsLoading: 'Loading models...',
      modelsRefresh: 'Refresh model list',
      modelsClickToView: 'Click to view',
      chatModelSelection: 'Select a model',
      chatServerOffline: 'Server offline',
      chatServerOfflineDesc: 'Please start the server first',
      chatWelcome: 'Start a conversation',
      chatWelcomeDesc: 'Send a message to start chatting with AI',
      chatPlaceholder: 'Type a message...',
      chatError: 'Failed to send message',
      clear: 'Clear',
      models: 'models',
    }[key] ?? key),
  }),
}));

vi.mock('@/components/ConversationSidebar', () => ({
  ConversationSidebar: () => <aside data-testid="conversation-sidebar" />,
}));

const readyCatalog = (ids: string[]): ModelCatalogState => ({
  models: ids.map((id) => ({ id })),
  status: 'ready',
  error: null,
});

const baseChatProps = {
  host: '127.0.0.1',
  port: 8000,
  apiKey: 'secret',
  messages: [] as Message[],
  onMessagesChange: vi.fn(),
  conversations: [],
  currentConversationId: null,
  onSelectConversation: vi.fn(),
  onNewChat: vi.fn(),
  onDeleteConversation: vi.fn(),
  onRenameConversation: vi.fn(),
  onRefreshModelCatalog: vi.fn(async () => {}),
};

describe('ModelsCard', () => {
  it('shows backend IDs in backend order when the catalog is online', () => {
    render(
      <ModelsCard
        catalog={readyCatalog(['gpt-5.6-sol', 'runtime-special'])}
        isRunning
        onRefresh={vi.fn(async () => {})}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Supported Models/ }));

    expect(screen.getByText('gpt-5.6-sol')).toBeInTheDocument();
    expect(screen.getByText('runtime-special')).toBeInTheDocument();
    expect(screen.getByText('gpt-5.6-sol').compareDocumentPosition(screen.getByText('runtime-special'))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it('shows an explicit offline state and no model capability when stopped', () => {
    render(
      <ModelsCard
        catalog={{ models: [], status: 'offline', error: null }}
        isRunning={false}
        onRefresh={vi.fn(async () => {})}
      />,
    );

    expect(screen.getByText('Start the server to view models')).toBeInTheDocument();
    expect(screen.queryByText('gpt-5.6-sol')).not.toBeInTheDocument();
  });

  it('shows an explicit empty state instead of a static fallback', () => {
    render(
      <ModelsCard
        catalog={{ models: [], status: 'empty', error: null }}
        isRunning
        onRefresh={vi.fn(async () => {})}
      />,
    );

    expect(screen.getByText('No models available')).toBeInTheDocument();
    expect(screen.queryByText('claude-sonnet-4-6')).not.toBeInTheDocument();
  });

  it('refreshes only through the backend catalog callback', () => {
    const onRefresh = vi.fn(async () => {});
    render(
      <ModelsCard
        catalog={readyCatalog(['gpt-5.6-sol'])}
        isRunning
        onRefresh={onRefresh}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));

    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

describe('ChatView model selection', () => {
  it('selects the first backend model and does not render a static Claude fallback', async () => {
    render(
      <ChatView
        {...baseChatProps}
        isRunning
        modelCatalog={readyCatalog(['gpt-5.6-terra', 'runtime-special'])}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole('combobox', { name: 'Select a model' })).toHaveTextContent('gpt-5.6-terra');
    });
    expect(screen.queryByText('claude-sonnet-4-6')).not.toBeInTheDocument();
  });

  it('disables model selection and chat input when the backend is offline', () => {
    render(
      <ChatView
        {...baseChatProps}
        isRunning={false}
        modelCatalog={{ models: [], status: 'offline', error: null }}
      />,
    );

    expect(screen.getByRole('combobox', { name: 'Select a model' })).toBeDisabled();
    expect(screen.getByPlaceholderText('Start the server to view models')).toBeDisabled();
  });
});
