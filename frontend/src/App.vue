<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import ConfirmationCard from './components/ConfirmationCard.vue'
import {
  formatWhen,
  listServices,
  sendMessage,
  type BookedAppointment,
  type PendingHold,
  type Service,
} from './api'

type Turn = { role: 'patient' | 'assistant'; text: string }

const services = ref<Service[]>([])
const turns = ref<Turn[]>([])
const draft = ref('')
const sending = ref(false)
const error = ref('')

const conversationId = ref<string | null>(null)
const hold = ref<PendingHold | null>(null)
const booked = ref<BookedAppointment[]>([])
const escalated = ref(false)
const escalationReason = ref<string | null>(null)

// Shown because being able to watch the vendor change while the behaviour does
// not is the point of the provider abstraction.
const provider = ref('')
const model = ref('')
const cachedTokens = ref(0)

const log = ref<HTMLElement | null>(null)
const canSend = computed(() => draft.value.trim().length > 0 && !sending.value && !escalated.value)

const urgent = computed(() => escalationReason.value === 'urgent_symptoms')

async function scroll() {
  await nextTick()
  log.value?.scrollTo({ top: log.value.scrollHeight, behavior: 'smooth' })
}

watch(turns, scroll, { deep: true })
watch(hold, scroll)

async function send() {
  if (!canSend.value) return
  const text = draft.value.trim()
  draft.value = ''
  turns.value.push({ role: 'patient', text })
  sending.value = true
  error.value = ''

  try {
    const reply = await sendMessage(text, conversationId.value)
    conversationId.value = reply.conversation_id
    turns.value.push({ role: 'assistant', text: reply.reply })
    hold.value = reply.pending_hold
    booked.value = reply.booked
    escalated.value = reply.escalated
    escalationReason.value = reply.escalation_reason
    provider.value = reply.provider
    model.value = reply.model
    cachedTokens.value = reply.cached_tokens
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    sending.value = false
  }
}

function onBooked() {
  hold.value = null
  turns.value.push({
    role: 'assistant',
    text: 'That is booked. You will find the details above.',
  })
  // Ask the assistant to pick the conversation back up, so the transcript
  // stays coherent rather than ending on a form submission.
  draft.value = 'Thanks, that is booked.'
  void send()
}

onMounted(async () => {
  try {
    services.value = await listServices()
  } catch (e) {
    error.value = (e as Error).message
  }
})
</script>

<template>
  <div class="wrap">
    <header class="top">
      <div>
        <h1>Darren Dental</h1>
        <p class="sub">Book an appointment</p>
      </div>
      <p v-if="provider" class="engine" :title="`${provider} / ${model}`">
        {{ provider }} · {{ model }}
        <span v-if="cachedTokens" class="cache">{{ cachedTokens }} cached</span>
      </p>
    </header>

    <section v-if="services.length" class="services" aria-label="Treatments offered">
      <span v-for="s in services" :key="s.code" class="chip">
        {{ s.name }} · {{ s.duration_minutes }} min
      </span>
    </section>

    <div ref="log" class="log" role="log" aria-live="polite">
      <p v-if="!turns.length" class="hint">
        Try “I need a cleaning next week” or “what do you offer?”
      </p>

      <div v-for="(turn, i) in turns" :key="i" class="turn" :class="turn.role">
        <span class="who">{{ turn.role === 'patient' ? 'You' : 'Assistant' }}</span>
        <p>{{ turn.text }}</p>
      </div>

      <p v-if="sending" class="thinking">…</p>

      <!-- The assistant cannot book. This is what books. -->
      <ConfirmationCard
        v-if="hold"
        :hold="hold"
        @booked="onBooked"
        @expired="hold = null"
      />

      <div v-for="a in booked" :key="a.appointment_id" class="booked">
        <strong>Booked</strong>
        {{ a.service_name }} with {{ a.practitioner_name }}, {{ formatWhen(a.starts_at) }}
      </div>
    </div>

    <div v-if="escalated" class="escalated" :class="{ urgent }" role="alert">
      <strong v-if="urgent">Please call the practice now.</strong>
      <strong v-else>Passed to the practice.</strong>
      <span v-if="urgent">
        If you cannot reach anyone, or your symptoms are severe, go to an emergency
        department.
      </span>
      <span v-else>Someone will follow up with you.</span>
    </div>

    <form class="composer" @submit.prevent="send">
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
.services {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 14px 0 6px;
}
.chip {
  font-size: 0.75rem;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px 10px;
}
.log {
  flex: 1;
  overflow-y: auto;
  padding: 12px 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 16rem;
}
.hint {
  color: var(--muted);
  font-size: 0.9rem;
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
.thinking {
  color: var(--muted);
  margin: 0;
  letter-spacing: 0.2em;
}
.booked {
  border-left: 3px solid var(--accent);
  padding: 8px 12px;
  font-size: 0.9rem;
  background: var(--surface);
}
.booked strong {
  color: var(--accent);
  margin-right: 6px;
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
