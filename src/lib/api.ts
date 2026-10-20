/**
 * Medarina backend API client.
 *
 * All functions return the parsed JSON body on success.
 * They throw an Error with a human-readable message on failure.
 *
 * The base URL is read from the VITE_API_URL env variable.
 * If not set it falls back to http://localhost:8000 (local dev).
 *
 * Usage:
 *   import * as api from '../lib/api'
 *   const inv = await api.createInvestigation({ ... })
 */

const BASE = (import.meta.env.VITE_API_URL ?? 'http://localhost:8000').replace(/\/$/, '')

async function request<T = unknown>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  if (body !== undefined) opts.body = JSON.stringify(body)

  const res = await fetch(`${BASE}${path}`, opts)

  if (!res.ok) {
    let msg = `API error ${res.status}`
    try {
      const data = await res.json()
      msg = data.detail ?? data.message ?? msg
    } catch {
      // ignore
    }
    throw new Error(msg)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export const getHealth = () => request<{ status: string; timestamp: string }>('GET', '/health')

// ---------------------------------------------------------------------------
// Investigations
// ---------------------------------------------------------------------------

export interface InvestigationListParams {
  archived?: boolean
  status?: string
  category?: string
  q?: string
  limit?: number
  offset?: number
}

export const listInvestigations = (params: InvestigationListParams = {}) => {
  const sp = new URLSearchParams()
  if (params.archived !== undefined) sp.set('archived', String(params.archived))
  if (params.status) sp.set('status', params.status)
  if (params.category) sp.set('category', params.category)
  if (params.q) sp.set('q', params.q)
  if (params.limit !== undefined) sp.set('limit', String(params.limit))
  if (params.offset !== undefined) sp.set('offset', String(params.offset))
  const qs = sp.toString()
  return request<{ items: unknown[]; total: number }>('GET', `/api/investigations${qs ? `?${qs}` : ''}`)
}

export const getInvestigation = (id: string) =>
  request<unknown>('GET', `/api/investigations/${id}`)

export const createInvestigation = (body: unknown) =>
  request<unknown>('POST', '/api/investigations', body)

export const updateInvestigation = (id: string, patch: unknown) =>
  request<unknown>('PATCH', `/api/investigations/${id}`, patch)

export const deleteInvestigation = (id: string) =>
  request<void>('DELETE', `/api/investigations/${id}`)

// ---------------------------------------------------------------------------
// Evidence
// ---------------------------------------------------------------------------

export const addEvidence = (invId: string, body: unknown) =>
  request<unknown>('POST', `/api/investigations/${invId}/evidence`, body)

// ---------------------------------------------------------------------------
// Retrieval
// ---------------------------------------------------------------------------

export const triggerRetrieval = (invId: string, opts?: { tinyfishEndpoint?: string; tinyfishApiKey?: string }) =>
  request<{ message: string; investigationId: string }>('POST', `/api/investigations/${invId}/retrieve`, opts ?? {})

// ---------------------------------------------------------------------------
// Analysis
// ---------------------------------------------------------------------------

export const analyseInvestigation = (invId: string) =>
  request<{
    verificationQuestions: string[]
    summary?: string
    riskIndicators: string[]
    recommendation?: string
  }>('POST', `/api/investigations/${invId}/analyse`)

// ---------------------------------------------------------------------------
// Reports
// ---------------------------------------------------------------------------

export const saveReport = (invId: string) =>
  request<unknown>('POST', `/api/investigations/${invId}/reports`)

export const listReports = (invId: string) =>
  request<{ items: unknown[] }>('GET', `/api/investigations/${invId}/reports`)

export const listAllReports = () =>
  request<{ items: unknown[] }>('GET', '/api/reports')

export const getReport = (reportId: string) =>
  request<unknown>('GET', `/api/reports/${reportId}`)

// ---------------------------------------------------------------------------
// TinyFish proxy test
// ---------------------------------------------------------------------------

export const testTinyfishConnection = (endpoint: string, apiKey = '') =>
  request<{ ok: boolean; message: string }>('POST', '/api/tinyfish/test', {
    endpoint,
    apiKey,
  })

// ---------------------------------------------------------------------------
// Backend liveness (used by Settings page)
// ---------------------------------------------------------------------------

/** Returns true when the backend is reachable. */
export async function isBackendReachable(): Promise<boolean> {
  try {
    const h = await getHealth()
    return h.status === 'ok'
  } catch {
    return false
  }
}
