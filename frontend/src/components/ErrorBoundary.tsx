import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
    errorInfo: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error, errorInfo: null };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('Uncaught error in React component tree:', error, errorInfo);
    this.setState({ error, errorInfo });
  }

  public render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: 30, color: '#f87171', background: '#0f172a', minHeight: '100vh', fontFamily: 'monospace' }}>
          <h2 style={{ fontSize: 20, fontWeight: 'bold', marginBottom: 12 }}>⚠️ React Render Error</h2>
          <div style={{ padding: 16, background: '#1e293b', borderRadius: 8, border: '1px solid #ef4444', marginBottom: 16 }}>
            <strong>{this.state.error?.toString()}</strong>
          </div>
          <pre style={{ fontSize: 12, color: '#94a3b8', whiteSpace: 'pre-wrap' }}>
            {this.state.errorInfo?.componentStack}
          </pre>
          <button
            onClick={() => window.location.reload()}
            style={{ marginTop: 16, padding: '8px 16px', background: '#3b82f6', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}
          >
            Reload Page
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
