export type Clinic = {
  name: string
  timezone: string
  contact_phone: string | null
}

export type Service = {
  code: string
  name: string
  duration_minutes: number
  description: string
}

export type PendingHold = {
  hold_id: string
  service_code: string
  service_name: string
  practitioner_name: string
  starts_at: string
  ends_at: string
  expires_at: string
}

export type BookedAppointment = {
  appointment_id: string
  service_name: string
  practitioner_name: string
  starts_at: string
  ends_at: string
  // From the database, not from what was typed into the card. A name corrected
  // in conversation has to show up on the card the patient is asked to check.
  patient_name: string | null
  patient_phone: string | null
}

export type ChatReply = {
  conversation_id: string
  reply: string
  escalated: boolean
  escalation_reason: string | null
  tools_used: string[]
  provider: string
  model: string
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  pending_hold: PendingHold | null
  booked: BookedAppointment[]
}

export type Appointment = {
  appointment_id: string
  status: string
  service_code: string
  practitioner_slug: string
  practitioner_name: string
  starts_at: string
  ends_at: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    // FastAPI puts the message the patient should read in `detail`, and the
    // booking errors are written to be readable rather than diagnostic.
    throw new Error(body.detail ?? `Request failed (${response.status})`)
  }
  return body as T
}

export const getClinic = () => request<Clinic>('/api/clinic')

export const listServices = () => request<Service[]>('/api/services')

export const listBookings = (conversationId: string) =>
  request<BookedAppointment[]>(`/api/conversations/${conversationId}/bookings`)

export const sendMessage = (message: string, conversationId: string | null) =>
  request<ChatReply>('/api/chat', {
    method: 'POST',
    body: JSON.stringify({ message, conversation_id: conversationId, channel: 'chat' }),
  })

export const confirmBooking = (
  holdId: string,
  patient: { full_name: string; phone: string; email?: string | null },
) =>
  request<Appointment>('/api/appointments', {
    method: 'POST',
    // Same key for a retried confirmation, so a dropped response cannot
    // produce a second appointment.
    headers: { 'Idempotency-Key': `card:${holdId}` },
    body: JSON.stringify({ hold_id: holdId, patient }),
  })

/**
 * Times are rendered in the *practice's* timezone, never the viewer's.
 *
 * A patient is walking into a building and the building has one clock. Using
 * the browser's zone produced an assistant saying 11:45 AM beside a
 * confirmation saying 11:45 PM for the same appointment, because the server
 * formats in clinic time and the client was formatting in Taipei time.
 */
let clinicTimeZone = 'UTC'

export function setClinicTimeZone(tz: string) {
  clinicTimeZone = tz
}

export function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString('en-GB', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
    timeZone: clinicTimeZone,
  })
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-GB', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
    timeZone: clinicTimeZone,
  })
}
