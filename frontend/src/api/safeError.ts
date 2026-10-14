import { ApiError } from './http'

export function safeError(cause: unknown): string {
  return cause instanceof ApiError ? `${cause.code}${cause.requestId ? ` (${cause.requestId})` : ''}` : 'Service unavailable'
}
