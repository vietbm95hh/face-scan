<script setup>
import { onMounted, computed, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { useGroupsStore } from './stores/groups'
import { useUiStore } from './stores/ui'
import { api } from './api'

const groups = useGroupsStore()
const ui = useUiStore()
const queue = ref({ queued: 0, running: 0 })

const links = [
  { to: '/posts', icon: '📝', label: 'Bài viết' },
  { to: '/groups', icon: '👥', label: 'Nhóm' },
  { to: '/scan', icon: '📡', label: 'Quét dữ liệu' },
  { to: '/verify', icon: '🔍', label: 'Đối chiếu' },
  { to: '/dashboard', icon: '📊', label: 'Thống kê' },
]

const healthy = computed(() => groups.health?.ok)

async function refreshQueue() {
  try {
    queue.value = await api.scanStatus()
  } catch {
    /* bỏ qua: chỉ là badge phụ */
  }
}

onMounted(async () => {
  groups.checkHealth()
  groups.fetchGroups()
  refreshQueue()
  setInterval(refreshQueue, 30000)
})
</script>

<template>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <span class="logo">📷</span>
        <span>FB Scanner</span>
      </div>
      <RouterLink v-for="l in links" :key="l.to" :to="l.to" class="nav-link">
        <span class="nav-icon">{{ l.icon }}</span>
        <span>{{ l.label }}</span>
        <span
          v-if="l.to === '/scan' && (queue.queued || queue.running)"
          class="tag info"
          style="margin-left: auto"
          >{{ queue.queued + queue.running }}</span
        >
      </RouterLink>
      <div class="sidebar-foot">
        <div>
          <span class="dot" :class="healthy ? 'ok' : 'bad'"></span>
          {{ healthy ? 'API + DB OK' : 'Không kết nối được API' }}
        </div>
      </div>
    </aside>

    <main class="main">
      <RouterView />
    </main>

    <!-- toast -->
    <div class="toasts">
      <div v-for="t in ui.toasts" :key="t.id" class="toast" :class="t.type" @click="ui.dismiss(t.id)">
        {{ t.message }}
      </div>
    </div>

    <!-- confirm dialog -->
    <div v-if="ui.dialog" class="modal-backdrop" @click.self="ui.answerDialog(false)">
      <div class="modal">
        <h2>{{ ui.dialog.title }}</h2>
        <p style="color: var(--text-dim); white-space: pre-wrap">{{ ui.dialog.message }}</p>
        <div class="modal-foot">
          <button class="btn" @click="ui.answerDialog(false)">Huỷ</button>
          <button class="btn" :class="ui.dialog.danger ? 'danger' : 'primary'" @click="ui.answerDialog(true)">
            {{ ui.dialog.okText }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
