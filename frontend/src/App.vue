<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import ConfirmationCard from './components/ConfirmationCard.vue'
import VoiceControls from './components/VoiceControls.vue'
import {
  formatWhen,
  getClinic,
  getVoiceStatus,
  listBookings,
  listServices,
  sendMessage,
  setClinicTimeZone,
  type BookedAppointment,
  type PendingHold,
  type Service,
} from './api'

type Turn = { role: 'patient' | 'assistant'; text: string }

/**
 * Strip the emphasis the model occasionally adds.
 *
 * Replies are rendered as plain text, so a model that decides to write
 * **9:00 AM this morning** puts literal asterisks in front of a patient. It
 * does this in maybe one reply in ten, which is exactly often enough to be
 * seen and too rare to notice while building. Asking it not to would work most
 * of the time; removing them works every time.
 */
function plain(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])/g, '$1')
    .replace(/`(.+?)`/g, '$1')
}

const clinicName = ref('')
const contactPhone = ref<string | null>(null)
const services = ref<Service[]>([])

const turns = ref<Turn[]>([])
const draft = ref('')
const sending = ref(false)
const error = ref('')

const conversationId = ref<string | null>(null)
const hold = ref<PendingHold | null>(null)
const bookings = ref<BookedAppointment[]>([])
const escalated = ref(false)
const escalationReason = ref<string | null>(null)

/**
 * Which engine answered, and how much of the prompt came from cache.
 *
 * A developer's instrument, not a patient's. It is the only way to see that
 * prompt caching is still working, because a cache that has silently stopped
 * produces no error, only a larger bill and a slower reply. It also names the
 * vendor and the model, which a patient booking a filling has no use for and
 * an attacker would rather not have to guess.
 *
 * So it is shown in development, and on demand anywhere with ?debug in the
 * address, which is how it gets demonstrated on a deployed copy.
 */
const showDiagnostics = ref(false)

const provider = ref('')
const model = ref('')
const cachedTokens = ref(0)

const voice = ref<InstanceType<typeof VoiceControls> | null>(null)
const voiceAvailable = ref(false)

const log = ref<HTMLElement | null>(null)
const canSend = computed(() => draft.value.trim().length > 0 && !sending.value && !escalated.value)
const urgent = computed(() => escalationReason.value === 'urgent_symptoms')

/**
 * Starting suggestions.
 *
 * A blank box asking an open question is the worst opening a booking flow can
 * have — the patient has to guess what the assistant understands. These are
 * ordinary sentences, not commands, so tapping one and typing one reach the
 * same place. They disappear once the conversation has started.
 */
const suggestions = computed(() =>
  services.value.slice(0, 4).map((s) => `I'd like to book ${s.name.toLowerCase()}`),
)

async function scroll() {
  await nextTick()
  log.value?.scrollTo({ top: log.value.scrollHeight, behavior: 'smooth' })
}

watch(turns, scroll, { deep: true })
watch(hold, scroll)

async function send(text?: string, spoken = false) {
  const message = (text ?? draft.value).trim()
  if (!message || sending.value || escalated.value) return
  draft.value = ''
  turns.value.push({ role: 'patient', text: message })
  sending.value = true
  error.value = ''

  try {
    const reply = await sendMessage(message, conversationId.value, spoken ? 'voice' : 'chat')
    conversationId.value = reply.conversation_id
    turns.value.push({ role: 'assistant', text: plain(reply.reply) })
    hold.value = reply.pending_hold
    escalated.value = reply.escalated
    escalationReason.value = reply.escalation_reason
    provider.value = reply.provider
    model.value = reply.model
    cachedTokens.value = reply.cached_tokens

    bookings.value = reply.booked

    // Spoken in, spoken out. A patient who typed is not read aloud to; a
    // patient who spoke gets the reply both ways, never only as audio.
    if (spoken) void voice.value?.speakReply(reply.reply)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    sending.value = false
  }
}

/**
 * The card booked it.
 *
 * The list is re-read from the server rather than patched locally. Merging the
 * new appointment into what was already on screen left a replaced booking
 * sitting beside its replacement after a reschedule, and kept showing a name
 * that had since been corrected.
 *
 * Nothing is sent to the assistant. It learns about the booking from the turn
 * context, which reads the same database. Faking a patient message to tell it
 * would put words in their mouth and spend a model call on something the
 * server already knows.
 */
function onVoice(text: string) {
  void send(text, true)
}

async function onBooked() {
  // Whatever the assistant was saying is now out of date.
  voice.value?.stopPlayback()
  hold.value = null
  await refreshBookings()
}

async function refreshBookings() {
  if (!conversationId.value) return
  try {
    bookings.value = await listBookings(conversationId.value)
  } catch {
    // Leave what is on screen; the next reply will correct it.
  }
}

onMounted(async () => {
  try {
    const [clinic, list, voiceStatus] = await Promise.all([
      getClinic(),
      listServices(),
      getVoiceStatus().catch(() => ({ available: false, stt: '', tts: '' })),
    ])
    voiceAvailable.value = voiceStatus.available
    showDiagnostics.value =
      clinic.environment !== 'production' ||
      new URLSearchParams(window.location.search).has('debug')
    clinicName.value = clinic.name
    contactPhone.value = clinic.contact_phone
    setClinicTimeZone(clinic.timezone)
    services.value = list
    turns.value.push({
      role: 'assistant',
      text: `Hello, I book appointments for ${clinic.name}. What do you need to come in for?`,
    })
  } catch (e) {
    error.value = (e as Error).message
  }
})
</script>

<template>
  <div class="wrap">
    <header class="top">
      <div>
        <h1>{{ clinicName || 'Loading…' }}</h1>
        <p class="sub">Book an appointment</p>
      </div>
      <p v-if="contactPhone" class="urgent-line">
        In an emergency, call <strong>{{ contactPhone }}</strong>
      </p>
      <p v-if="showDiagnostics && provider" class="engine">
        {{ provider }} · {{ model }}
        <span v-if="cachedTokens" class="cache">{{ cachedTokens }} cached</span>
      </p>
    </header>

    <div ref="log" class="log" role="log" aria-live="polite">
      <div v-for="(turn, i) in turns" :key="i" class="turn" :class="turn.role">
        <span class="who">{{ turn.role === 'patient' ? 'You' : 'Assistant' }}</span>
        <p>{{ turn.text }}</p>
      </div>

      <!-- Only before the patient has said anything. After that the assistant
           is asking its own questions and a menu would talk over it. -->
      <div v-if="turns.length === 1 && suggestions.length" class="suggestions">
        <button v-for="s in suggestions" :key="s" type="button" @click="send(s)">
          {{ s }}
        </button>
        <button type="button" class="ghost" @click="send('What do you offer?')">
          Something else
        </button>
      </div>

      <p v-if="sending" class="thinking">…</p>

      <!-- Deliberately not removed when it expires. The card's own expired
           state was unreachable: destroying it here meant the countdown hit
           zero, the form vanished, and the patient was left looking at a
           conversation that said a form was on their screen. It stays,
           says it has expired, and disappears on the next turn when the
           server reports no hold. -->
      <ConfirmationCard v-if="hold" :hold="hold" @booked="onBooked" />

      <div v-for="b in bookings" :key="b.appointment_id" class="booked">
        <p class="booked-head"><strong>Booked</strong> {{ b.service_name }}</p>
        <dl>
          <div><dt>With</dt><dd>{{ b.practitioner_name }}</dd></div>
          <div><dt>When</dt><dd>{{ formatWhen(b.starts_at) }}</dd></div>
          <!-- What the practice has recorded, not what was typed here, so a
               correction made in conversation is visible immediately. -->
          <div v-if="b.patient_name"><dt>Name</dt><dd>{{ b.patient_name }}</dd></div>
          <div v-if="b.patient_phone"><dt>Phone</dt><dd>{{ b.patient_phone }}</dd></div>
        </dl>
        <p class="check">
          If anything here is wrong, tell me below and I will sort it out.
        </p>
      </div>
    </div>

    <div v-if="escalated" class="escalated" :class="{ urgent }" role="alert">
      <strong v-if="urgent">
        Please call the practice now{{ contactPhone ? ` on ${contactPhone}` : '' }}.
      </strong>
      <strong v-else>Passed to the practice.</strong>
      <span v-if="urgent">
        If you cannot reach anyone, or your symptoms are severe, go to an emergency
        department.
      </span>
      <span v-else>Someone will follow up with you.</span>
    </div>

    <VoiceControls
      v-if="voiceAvailable && !escalated"
      ref="voice"
      @submit="onVoice"
      @error="error = $event"
    />

    <form class="composer" @submit.prevent="send()">
      <input
        v-model="draft"
        type="text"
        :disabled="escalated"
        :placeholder="escalated ? 'The practice will be in touch' : 'Type your message'"
        aria-label="Message"
      />
      <button type="submit" :disabled="!canSend">Send</button>
    </form>

    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.wrap {
  max-width: 44rem;
  margin: 0 auto;
  padding: 24px 16px 32px;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}
.top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
h1 {
  font-size: 1.4rem;
  margin: 0;
}
.sub {
  margin: 2px 0 0;
  color: var(--muted);
  font-size: 0.9rem;
}
/* Always on screen, whatever the assistant happens to say.
   The number is the one thing on this page that has to be there when somebody
   has knocked a tooth out, and the model offered it in two conversations out
   of three. Two out of three is not a safety instruction. */
.urgent-line {
  margin: 0 0 4px;
  font-size: 0.8rem;
  color: var(--muted);
  text-align: right;
}
.urgent-line strong {
  color: #a11;
  font-variant-numeric: tabular-nums;
}
.engine {
  margin: 0;
  font-size: 0.75rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.cache {
  display: block;
  opacity: 0.7;
}
.log {
  flex: 1;
  overflow-y: auto;
  padding: 16px 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 18rem;
}
.turn {
  display: grid;
  gap: 2px;
}
.turn .who {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
}
.turn p {
  margin: 0;
  padding: 10px 12px;
  border-radius: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  white-space: pre-wrap;
  line-height: 1.45;
}
.turn.patient {
  justify-items: end;
  text-align: right;
}
.turn.patient p {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.suggestions button {
  font: inherit;
  font-size: 0.85rem;
  padding: 7px 13px;
  border-radius: 999px;
  border: 1px solid var(--accent);
  background: var(--surface);
  color: var(--accent);
  cursor: pointer;
}
.suggestions button:hover {
  background: var(--accent);
  color: #fff;
}
.suggestions .ghost {
  border-color: var(--border);
  color: var(--muted);
}
.suggestions .ghost:hover {
  background: var(--border);
  color: var(--text);
}
.thinking {
  color: var(--muted);
  margin: 0;
  letter-spacing: 0.2em;
}
.booked {
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--surface);
}
.booked-head {
  margin: 0 0 8px;
  font-size: 0.95rem;
}
.booked-head strong {
  color: var(--accent);
  margin-right: 6px;
}
.booked dl {
  margin: 0;
  display: grid;
  gap: 3px;
}
.booked dl > div {
  display: flex;
  gap: 8px;
  font-size: 0.9rem;
}
.booked dt {
  color: var(--muted);
  min-width: 4rem;
}
.booked dd {
  margin: 0;
}
.check {
  margin: 10px 0 0;
  font-size: 0.82rem;
  color: var(--muted);
}
.escalated {
  border: 1px solid var(--border);
  border-left: 3px solid var(--muted);
  padding: 10px 12px;
  font-size: 0.9rem;
  margin-bottom: 10px;
  display: grid;
  gap: 2px;
}
.escalated.urgent {
  border-left-color: #a11;
  background: #fff5f5;
}
.escalated.urgent strong {
  color: #a11;
}
.composer {
  display: flex;
  gap: 8px;
}
.composer input {
  flex: 1;
  font: inherit;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
}
.composer button {
  font: inherit;
  padding: 10px 18px;
  border-radius: 8px;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: #fff;
  cursor: pointer;
}
.composer button:disabled {
  opacity: 0.5;
  cursor: default;
}
.error {
  color: #a11;
  font-size: 0.9rem;
}
</style>
