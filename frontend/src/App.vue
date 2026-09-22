<script setup lang="ts">
import { onMounted, ref } from 'vue'

type Service = { code: string; name: string; duration_minutes: number; description: string }
type Slot = { start: string; end: string; practitioner_slug: string; practitioner_name: string }

const services = ref<Service[]>([])
const selected = ref('A')
const slots = ref<Slot[]>([])
const loading = ref(false)
const error = ref('')

async function load<T>(url: string): Promise<T> {
  const res = await fetch(url)
  const body = await res.json()
  if (!res.ok) throw new Error(body.detail ?? res.statusText)
  return body as T
}

async function search() {
  loading.value = true
  error.value = ''
  try {
    const data = await load<{ slots: Slot[] }>(
      `/api/availability?service=${encodeURIComponent(selected.value)}&days=14&limit=40`,
    )
    slots.value = data.slots
  } catch (e) {
    error.value = (e as Error).message
    slots.value = []
  } finally {
    loading.value = false
  }
}

function when(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

onMounted(async () => {
  try {
    services.value = await load<Service[]>('/api/services')
    await search()
  } catch (e) {
    error.value = (e as Error).message
  }
})
</script>

<template>
  <div class="wrap">
    <h1>Darren Dental</h1>
    <p class="sub">Appointment availability — the chat and voice assistant book against this same engine.</p>

    <div class="card">
      <div class="row">
        <select v-model="selected" @change="search">
          <option v-for="s in services" :key="s.code" :value="s.code">
            {{ s.code }} — {{ s.name }} ({{ s.duration_minutes }} min)
          </option>
        </select>
        <button :disabled="loading" @click="search">{{ loading ? 'Searching…' : 'Find times' }}</button>
      </div>
      <p v-if="error" class="err">{{ error }}</p>
    </div>

    <div class="card">
      <table v-if="slots.length">
        <thead><tr><th>When</th><th>Practitioner</th></tr></thead>
        <tbody>
          <tr v-for="s in slots" :key="s.start + s.practitioner_slug">
            <td>{{ when(s.start) }}</td>
            <td>{{ s.practitioner_name }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!loading" class="empty">No availability in the next 14 days for this service.</p>
    </div>
  </div>
</template>
