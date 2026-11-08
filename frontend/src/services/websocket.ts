import type { WsMessage } from '../types';

export type WsHandler = (msg: WsMessage) => void;
export type ConnectionStatusHandler = (status: 'CONNECTED' | 'CONNECTING' | 'DISCONNECTED') => void;

const getWsUrl = (): string => {
  if (typeof window === 'undefined') return 'ws://localhost:8000/ws/stream';
  const { protocol, hostname, port } = window.location;
  const wsProto = protocol === 'https:' ? 'wss:' : 'ws:';
  if (port === '5173') {
    return `${wsProto}//${hostname}:8000/ws/stream`;
  }
  return `${wsProto}//${window.location.host}/ws/stream`;
};

class WebSocketClient {
  private ws: WebSocket | null = null;
  private url: string = getWsUrl();
  private subscribers: Map<string, Set<WsHandler>> = new Map();
  private statusListeners: Set<ConnectionStatusHandler> = new Set();
  private subscribedTopics: Set<string> = new Set(['ticker', 'candle', 'position', 'order', 'risk_alert']);
  private reconnectTimer: any = null;
  private reconnectInterval: number = 2000;
  private shouldReconnect: boolean = true;
  private status: 'CONNECTED' | 'CONNECTING' | 'DISCONNECTED' = 'DISCONNECTED';

  constructor() {
    this.subscribers.set('*', new Set());
  }

  public connect(url?: string) {
    if (url) this.url = url;
    this.shouldReconnect = true;
    this.setStatus('CONNECTING');

    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        this.setStatus('CONNECTED');
        this.reconnectInterval = 2000;
        this.sendSubscriptions();
      };

      this.ws.onmessage = (event) => {
        try {
          const msg: WsMessage = JSON.parse(event.data);
          this.notifySubscribers(msg);
        } catch (e) {
          console.warn('[WS] Failed to parse message:', event.data, e);
        }
      };

      this.ws.onerror = (err) => {
        console.error('[WS] Socket error:', err);
      };

      this.ws.onclose = () => {
        this.setStatus('DISCONNECTED');
        if (this.shouldReconnect) {
          this.scheduleReconnect();
        }
      };
    } catch (e) {
      this.setStatus('DISCONNECTED');
      if (this.shouldReconnect) {
        this.scheduleReconnect();
      }
    }
  }

  private setStatus(s: 'CONNECTED' | 'CONNECTING' | 'DISCONNECTED') {
    this.status = s;
    this.statusListeners.forEach((fn) => fn(s));
  }

  public onStatusChange(fn: ConnectionStatusHandler) {
    this.statusListeners.add(fn);
    fn(this.status);
    return () => this.statusListeners.delete(fn);
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.reconnectInterval = Math.min(this.reconnectInterval * 1.5, 15000);
      this.connect();
    }, this.reconnectInterval);
  }

  private sendSubscriptions() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      const payload = {
        action: 'subscribe',
        topics: Array.from(this.subscribedTopics),
      };
      this.ws.send(JSON.stringify(payload));
    }
  }

  public subscribeTopic(topic: string) {
    this.subscribedTopics.add(topic);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action: 'subscribe', topics: [topic] }));
    }
  }

  public subscribe(topic: string, handler: WsHandler): () => void {
    if (!this.subscribers.has(topic)) {
      this.subscribers.set(topic, new Set());
    }
    this.subscribers.get(topic)!.add(handler);
    this.subscribeTopic(topic);

    return () => {
      const set = this.subscribers.get(topic);
      if (set) {
        set.delete(handler);
      }
    };
  }

  private notifySubscribers(msg: WsMessage) {
    // Notify specific topic listeners
    const topicSet = this.subscribers.get(msg.topic);
    if (topicSet) {
      topicSet.forEach((fn) => fn(msg));
    }
    // Notify wildcard listeners
    const starSet = this.subscribers.get('*');
    if (starSet) {
      starSet.forEach((fn) => fn(msg));
    }
  }

  public disconnect() {
    this.shouldReconnect = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.setStatus('DISCONNECTED');
  }
}

export const wsService = new WebSocketClient();
