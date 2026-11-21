<script setup>
import { ref, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { fmtDate, fmtNum, timeAgo } from '../format'
import { useGroupsStore } from '../stores/groups'
import { useUiStore } from '../stores/ui'

const store = useGroupsStore()
const ui = useUiStore()
const rows = ref([])
const loading = ref(true)
const q = ref('')
const editing = ref(null) // group đang sửa
const form = ref({ name: '', is_active: true })

async function load() {
  loading.value = true
  try {
    rows.value = await api.groups()
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    loading.value = false
  }
}

async function toggleActive(g) {
  try {
    const r = await api.updateGroup(g.id, { is_active: !g.is_active })
    Object.assign(g, r)
    ui.toast(`${g.name}: ${r.is_active ? 'bật quét' : 'tạm dừng quét'}`, 'ok')
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

function startEdit(g) {
  editing.value = g
  form.value = { name: g.name || '', is_active: g.is_active }
}

async function saveEdit() {
  if (!form.value.name.trim()) {
    ui.toast('Tên nhóm không được để trống', 'warn')
    return
  }
  try {
    const r = await api.updateGroup(editing.value.id, { name: form.value.name.trim() })
    Object.assign(editing.value, r)
    ui.toast('Đã lưu', 'ok')
    editing.value = null
    store.fetchGroups(true)
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

async function scanNow(g) {
  try {
    await api.createScan({ group_id: g.id, days: 7, max_posts: 0, reset_cursor: false })
    ui.toast(`Đã đặt yêu cầu quét "${g.name}" — xem ở mục Quét dữ liệu`, 'ok')
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

function filtered() {
  const kw = q.value.trim().toLowerCase()
  if (!kw) return rows.value
  return rows.value.filter(
    (g) => (g.name || '').toLowerCase().includes(kw) || (g.fb_group_id || '').includes(kw) || (g.slug || '').includes(kw),
  )
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-head">
      <h1>Nhóm</h1>
      <span class="sub">{{ rows.length }} nhóm · {{ rows.filter((g) => g.is_active).length }} đang bật</span>
      <div class="spacer"></div>
      <input v-model="q" class="input" placeholder="Tìm nhóm…" style="width: 220px" />
      <button class="btn" @click="load">↻ Tải lại</button>
    </div>

    <div v-if="loading" class="loading">Đang tải…</div>
    <div v-else class="table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>Nhóm</th>
            <th>FB group id</th>
            <th class="num">Bài</th>
            <th class="num">Chưa đọc</th>
            <th>Bài mới nhất</th>
            <th>Quét gần nhất</th>
            <th>Trạng thái</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in filtered()" :key="g.id">
            <td style="max-width: 360px">
              <div style="font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{ g.name }}</div>
              <div style="color: var(--text-dim); font-size: 12px">
                <a v-if="g.group_url" :href="g.group_url" target="_blank" rel="noopener">Facebook ↗</a>
                <span v-else>{{ g.privacy || '' }} {{ g.member_count_txt ? '· ' + g.member_count_txt : '' }}</span>
              </div>
            </td>
            <td><code style="font-size: 12px; color: var(--text-dim)">{{ g.fb_group_id }}</code></td>
            <td class="num">
              <RouterLink :to="{ name: 'posts', query: { group_id: g.id } }">{{ fmtNum(g.post_count) }}</RouterLink>
            </td>
            <td class="num">
              <span v-if="g.unread_count" class="tag info">{{ fmtNum(g.unread_count) }}</span>
              <span v-else style="color: var(--text-dim)">0</span>
            </td>
            <td style="white-space: nowrap">{{ timeAgo(g.latest_post_at) }}</td>
            <td style="white-space: nowrap; color: var(--text-dim)">
              {{ g.last_scanned_at ? fmtDate(g.last_scanned_at) : 'chưa quét' }}
            </td>
            <td>
              <span class="tag" :class="g.is_active ? 'ok' : ''">{{ g.is_active ? 'bật' : 'tắt' }}</span>
            </td>
            <td style="white-space: nowrap; text-align: right">
              <button class="btn ghost sm" @click="scanNow(g)">Quét</button>
              <button class="btn ghost sm" @click="startEdit(g)">Sửa</button>
              <button class="btn ghost sm" @click="toggleActive(g)">{{ g.is_active ? 'Tắt' : 'Bật' }}</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="editing" class="modal-backdrop" @click.self="editing = null">
      <div class="modal">
        <h2>Sửa nhóm #{{ editing.id }}</h2>
        <div class="field" style="margin-top: 14px">
          <span>Tên hiển thị</span>
          <input v-model="form.name" class="input" @keyup.enter="saveEdit" />
        </div>
        <div style="margin-top: 10px; font-size: 12px; color: var(--text-dim)">
          Lần quét tới (discover) có thể ghi đè tên từ Facebook.
        </div>
        <div class="modal-foot">
          <button class="btn" @click="editing = null">Huỷ</button>
          <button class="btn primary" @click="saveEdit">Lưu</button>
        </div>
      </div>
    </div>
  </div>
</template>
