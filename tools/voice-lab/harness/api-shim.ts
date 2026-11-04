/**
 * Harness-only implementation of the three realtime API calls the production
 * client imports from `@/lib/api`.  It keeps the client code untouched while
 * avoiding the full app shell (router, better-auth session, React providers).
 */

export interface RealtimeIceConfigResponse {
  ice_servers: Array<{
    urls: string | string[];
    username?: string | null;
    credential?: string | null;
  }>;
  credential_ttl_seconds: number;
}

interface RealtimeSessionCreateRequest {
  chat_id: string;
  character_id: string;
  observation_id?: string | null;
  sdp: { type: string; sdp: string };
}

interface RealtimeSessionCreateResponse {
  session_id: string;
  chat_id: string;
  character_id: string;
  sdp: { type: string; sdp: string };
  ice_servers: RealtimeIceConfigResponse["ice_servers"];
  credential_ttl_seconds: number;
}

function token(): string {
  return (window as unknown as { __VOICE_LAB_TOKEN__?: string }).__VOICE_LAB_TOKEN__ ?? "";
}

function observationId(): string | null {
  return (window as unknown as { __VOICE_LAB_OBSERVATION__?: string }).__VOICE_LAB_OBSERVATION__ ?? null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers ?? {});
  headers.set("Authorization", `Bearer ${token()}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message =
      payload && typeof payload === "object" && "message" in payload
        ? String((payload as { message?: unknown }).message)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload && typeof payload === "object" && "data" in payload
    ? ((payload as { data: T }).data)
    : (payload as T);
}

export async function getRealtimeIceConfig(options?: {
  signal?: AbortSignal;
}): Promise<RealtimeIceConfigResponse> {
  return request<RealtimeIceConfigResponse>("/v1/realtime/config", {
    signal: options?.signal,
  });
}

export async function createRealtimeSession(
  body: RealtimeSessionCreateRequest,
  options?: { signal?: AbortSignal },
): Promise<RealtimeSessionCreateResponse> {
  const observation = observationId();
  return request<RealtimeSessionCreateResponse>("/v1/realtime/session", {
    method: "POST",
    signal: options?.signal,
    body: JSON.stringify({ ...body, observation_id: observation ?? undefined }),
  });
}

export async function deleteRealtimeSession(
  sessionId: string,
  options?: { signal?: AbortSignal },
): Promise<void> {
  await request<void>(`/v1/realtime/session/${sessionId}`, {
    method: "DELETE",
    signal: options?.signal,
  });
}
