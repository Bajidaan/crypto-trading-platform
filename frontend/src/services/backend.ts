// Port of the FastAPI backend when the dashboard runs on the Vite dev server (:5173).
// 8010 rather than 8000, which other local apps (e.g. Prospector) use by default.
// Override with VITE_BACKEND_PORT in frontend/.env.local. Docker/nginx builds ignore this:
// they reach the backend on the same origin through the reverse proxy.
export const BACKEND_PORT: string = import.meta.env.VITE_BACKEND_PORT || '8010';
