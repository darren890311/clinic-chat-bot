<script setup lang="ts">
/**
 * Push-to-talk, and the review step that makes it safe.
 *
 * Every rule here comes from one fact: speech is lossy and cannot be re-read.
 * A patient who mishears a time can ask again; a patient whose *name* was
 * misheard finds out when nobody can reach them.
 *
 * So nothing recognised is ever sent on the patient's behalf. The transcript
 * appears in an editable box and they send it, which makes a misrecognition a
 * two-second correction instead of a wrong appointment. Booking itself is
 * still the confirmation card, unreachable from here — the same guarantee as
 * the chat path, because this deliberately *is* the chat path.
 */
import { computed, onUnmounted, ref } from 'vue'
import { speak, transcribe } from '../api'

const emit = defineEmits<{ submit: [text: string]; error: [message: string] }>()

type Stage = 'idle' | 'recording' | 'thinking' | 'review'

const stage = ref<Stage>('idle')
const heard = ref('')
const seconds = ref(0)
const muted = ref(false)
const playing = ref(false)

let recorder: MediaRecorder | null = null
let chunks: Blob[] = []
let ticker: number | undefined
let objectUrl: string | null = null

/**
 * One audio element, unlocked by a user gesture, reused for every reply.
 *
 * Safari will not play audio from an element that has never been started by a
 * gesture, and it counts the element rather than the page. Building a fresh
 * `new Audio()` for each reply therefore worked in Chrome and silently did
 * nothing in Safari: the recognised text appeared, the written reply appeared,
 * and the patient heard nothing.
 *
 * It cannot be fixed at playback time either. By then the turn has been
 * through recognition, the assistant and synthesis, and several seconds of
 * awaiting have passed since the button was pressed. So the element is primed
 * during the press itself, with a fraction of a second of silence, and then
 * only has its source swapped afterwards.
 */
const audio = new Audio()
let unlocked = false

// 44 bytes: a WAV header describing no samples at all.
const SILENCE =
  'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA='

function unlock() {
  if (unlocked) return
  unlocked = true
  audio.src = SILENCE
  audio.play().then(
    () => audio.pause(),
    () => {
      // Refused anyway. The written reply is always on screen, so the turn is
      // not lost; the patient simply reads instead of listening.
      unlocked = false
    },
  )
}

// A patient thinking out loud is not a transcription bill. Sixty seconds is
// far longer than anyone spends saying which afternoon suits them.
const MAX_SECONDS = 60

const elapsed = computed(() => `0:${String(seconds.value).padStart(2, '0')}`)

/** Chrome records webm/opus, Safari mp4, Firefox ogg. None of them asks. */
function pickMimeType(): string | undefined {
  const preferred = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']
  return preferred.find((type) => MediaRecorder.isTypeSupported(type))
}

async function start() {
  // The press is the gesture. Everything after it is too late for Safari.
  unlock()
  stopPlayback()
  let stream: MediaStream
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  } catch {
    emit('error', 'I could not reach your microphone. You can type instead.')
    return
  }

  const mimeType = pickMimeType()
  recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
  chunks = []
  recorder.ondataavailable = (event) => event.data.size && chunks.push(event.data)
  recorder.onstop = () => {
    // Releasing the tracks stops the browser's recording indicator. Left
    // running it looks like the page is still listening, which it is not.
    stream.getTracks().forEach((track) => track.stop())
    void finish()
  }

  recorder.start()
  stage.value = 'recording'
  seconds.value = 0
  ticker = window.setInterval(() => {
    seconds.value += 1
    if (seconds.value >= MAX_SECONDS) stop()
  }, 1000)
}

function stop() {
  window.clearInterval(ticker)
  if (recorder?.state === 'recording') recorder.stop()
}

/** Abandon the recording without transcribing it — a misfired button. */
function discard() {
  window.clearInterval(ticker)
  if (recorder?.state === 'recording') {
    recorder.onstop = null
    recorder.stop()
    recorder.stream.getTracks().forEach((track) => track.stop())
  }
  recorder = null
  chunks = []
  heard.value = ''
  stage.value = 'idle'
}

async function finish() {
  const blob = new Blob(chunks, { type: recorder?.mimeType || 'audio/webm' })
  recorder = null
  chunks = []
  stage.value = 'thinking'

  try {
    const text = await transcribe(blob)
    if (!text) {
      // Silence is the commonest outcome of a push-to-talk button, not a
      // failure worth an error banner.
      emit('error', 'I did not hear anything. Try again, or type it.')
      stage.value = 'idle'
      return
    }
    heard.value = text
    stage.value = 'review'
  } catch (e) {
    emit('error', (e as Error).message)
    stage.value = 'idle'
  }
}

function send() {
  const text = heard.value.trim()
  if (!text) return
  emit('submit', text)
  heard.value = ''
  stage.value = 'idle'
}

/**
 * Speak a reply. Called by the parent, which also renders the same text.
 *
 * Audio never replaces the written reply; it accompanies it. If synthesis
 * fails the patient loses nothing they can see.
 */
async function speakReply(text: string) {
  if (muted.value) return
  stopPlayback()
  const blob = await speak(text)
  if (!blob || muted.value) return

  objectUrl = URL.createObjectURL(blob)
  audio.src = objectUrl
  audio.onended = stopPlayback
  audio.onerror = stopPlayback
  playing.value = true
  void audio.play().catch(stopPlayback)
}

/**
 * Stop the audio mid-sentence.
 *
 * Distinct from mute, which is a standing preference. This is "the second
 * time you offered was the right one and I do not need the other six" — the
 * fastest way to lose a patient is to make them listen to the whole list.
 */
function stopPlayback() {
  audio.pause()
  audio.onended = null
  audio.onerror = null
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl)
    objectUrl = null
  }
  playing.value = false
}

function toggleMute() {
  muted.value = !muted.value
  if (muted.value) stopPlayback()
}

onUnmounted(() => {
  window.clearInterval(ticker)
  discard()
  stopPlayback()
})

defineExpose({ speakReply, stopPlayback })
</script>

<template>
  <div class="voice">
    <div class="bar">
      <button
        v-if="stage === 'idle'"
        type="button"
        class="mic"
        @click="start"
        aria-label="Speak instead of typing"
      >
        <span aria-hidden="true">🎙</span> Speak
      </button>

      <template v-else-if="stage === 'recording'">
        <button type="button" class="mic recording" @click="stop">
          <span class="dot" aria-hidden="true"></span> Stop · {{ elapsed }}
        </button>
        <button type="button" class="ghost" @click="discard">Cancel</button>
      </template>

      <span v-else-if="stage === 'thinking'" class="status">Listening…</span>

      <button
        v-if="playing"
        type="button"
        class="ghost"
        @click="stopPlayback"
        aria-label="Stop the assistant speaking"
      >
        ⏹ Stop speaking
      </button>

      <button
        type="button"
        class="ghost mute"
        :aria-pressed="muted"
        @click="toggleMute"
      >
        {{ muted ? '🔇 Voice off' : '🔊 Voice on' }}
      </button>
    </div>

    <!-- What was heard, before anything acts on it. Editable because the
         alternative to fixing a misheard word is saying the whole thing
         again. -->
    <form v-if="stage === 'review'" class="review" @submit.prevent="send">
      <label>
        I heard
        <input v-model="heard" type="text" aria-label="What was heard. Edit it if it is wrong" />
      </label>
      <div class="actions">
        <button type="submit" :disabled="!heard.trim()">Send</button>
        <button type="button" class="ghost" @click="discard">Discard</button>
      </div>
      <p class="hint">Not right? Edit it before sending, or discard and say it again.</p>
    </form>
  </div>
</template>

<style scoped>
.voice {
  display: grid;
  gap: 10px;
}
.bar {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
button {
  font: inherit;
  font-size: 0.85rem;
  padding: 8px 14px;
  border-radius: 999px;
  border: 1px solid var(--accent);
  background: var(--accent);
  color: #fff;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.ghost {
  background: var(--surface);
  color: var(--muted);
  border-color: var(--border);
}
.ghost:hover {
  color: var(--text);
}
.mic.recording {
  background: #a11;
  border-color: #a11;
}
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #fff;
  margin-right: 6px;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  50% { opacity: 0.25; }
}
.status {
  color: var(--muted);
  font-size: 0.85rem;
}
.mute {
  margin-left: auto;
}
.review {
  border: 1px solid var(--accent);
  border-radius: 10px;
  padding: 12px 14px;
  background: var(--surface);
  display: grid;
  gap: 8px;
}
.review label {
  display: grid;
  gap: 4px;
  font-size: 0.8rem;
  color: var(--muted);
}
.review input {
  font: inherit;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  color: var(--text);
}
.actions {
  display: flex;
  gap: 8px;
}
.hint {
  margin: 0;
  font-size: 0.78rem;
  color: var(--muted);
}
</style>
