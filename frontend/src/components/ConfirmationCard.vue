<script setup lang="ts">
/**
 * The step that actually books an appointment.
 *
 * The assistant has no tool that can confirm a booking. It offers times and
 * reserves one; this card is where the reservation becomes an appointment,
 * and only when the patient acts on it.
 *
 * The name and phone number are typed rather than spoken, because digits are
 * where speech recognition fails most often and a wrong number is a patient
 * nobody can reach.
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { confirmBooking, formatTime, formatWhen, type PendingHold } from '../api'

const props = defineProps<{ hold: PendingHold }>()
const emit = defineEmits<{ booked: [id: string]; expired: [] }>()

const fullName = ref('')
const phone = ref('')
const email = ref('')
const submitting = ref(false)
const error = ref('')

const remaining = ref(0)
let ticker: number | undefined

/** The hold has a real TTL; showing it is how the patient knows it is real. */
function tick() {
  const left = Math.max(0, new Date(props.hold.expires_at).getTime() - Date.now())
  remaining.value = Math.floor(left / 1000)
  if (left === 0) emit('expired')
}

watch(
  () => props.hold.hold_id,
  () => {
    tick()
    window.clearInterval(ticker)
    ticker = window.setInterval(tick, 1000)
  },
  { immediate: true },
)

onUnmounted(() => window.clearInterval(ticker))

const countdown = computed(() => {
  const m = Math.floor(remaining.value / 60)
  const s = remaining.value % 60
  return `${m}:${String(s).padStart(2, '0')}`
})

const runningOut = computed(() => remaining.value > 0 && remaining.value < 60)
const canSubmit = computed(
  () => fullName.value.trim().length > 1 && phone.value.trim().length > 5 && remaining.value > 0,
)

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ''
  try {
    const appointment = await confirmBooking(props.hold.hold_id, {
      full_name: fullName.value.trim(),
      phone: phone.value.trim(),
      email: email.value.trim() || null,
    })
    emit('booked', appointment.appointment_id)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="card" aria-labelledby="confirm-heading">
    <header class="head">
      <h2 id="confirm-heading">Confirm your appointment</h2>
      <span class="hold" :class="{ urgent: runningOut }" role="timer">
        Held for {{ countdown }}
      </span>
    </header>

    <dl class="details">
      <div><dt>Treatment</dt><dd>{{ hold.service_name }}</dd></div>
      <div><dt>With</dt><dd>{{ hold.practitioner_name }}</dd></div>
      <div><dt>When</dt><dd>{{ formatWhen(hold.starts_at) }}</dd></div>
      <div><dt>Until</dt><dd>{{ formatTime(hold.ends_at) }}</dd></div>
    </dl>

    <form class="form" @submit.prevent="submit">
      <label>
        Your name
        <input v-model="fullName" type="text" autocomplete="name" required />
      </label>
      <label>
        Phone number
        <!-- Typed, never transcribed: a misheard digit is a patient we cannot reach. -->
        <input v-model="phone" type="tel" inputmode="tel" autocomplete="tel" required />
      </label>
      <label>
        Email <span class="optional">(optional)</span>
        <input v-model="email" type="email" autocomplete="email" />
      </label>

      <p v-if="error" class="error" role="alert">{{ error }}</p>

      <button type="submit" :disabled="!canSubmit || submitting">
        {{ submitting ? 'Booking…' : 'Confirm booking' }}
      </button>
      <p v-if="remaining === 0" class="error" role="alert">
        This reservation has expired. Ask for another time.
      </p>
    </form>
  </section>
</template>

<style scoped>
.card {
  border: 1px solid var(--accent);
  border-radius: 10px;
  padding: 16px;
  background: var(--surface);
  margin: 12px 0;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 12px;
}
h2 {
  font-size: 1rem;
  margin: 0;
  color: var(--accent);
}
.hold {
  font-variant-numeric: tabular-nums;
  font-size: 0.85rem;
  color: var(--muted);
}
.hold.urgent {
  color: #a11;
  font-weight: 600;
}
.details {
  margin: 0 0 14px;
  display: grid;
  gap: 4px;
}
.details > div {
  display: flex;
  gap: 8px;
  font-size: 0.95rem;
}
dt {
  color: var(--muted);
  min-width: 5.5rem;
}
dd {
  margin: 0;
  font-weight: 500;
}
.form {
  display: grid;
  gap: 10px;
}
label {
  display: grid;
  gap: 4px;
  font-size: 0.85rem;
  color: var(--muted);
}
input {
  font: inherit;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  color: var(--text);
}
.optional {
  font-weight: 400;
}
.error {
  color: #a11;
  font-size: 0.9rem;
  margin: 0;
}
button {
  font: inherit;
  padding: 10px;
  border-radius: 7px;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: #fff;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
