<script setup>
import { ref, computed, onMounted } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { fmtDate, fmtNum } from '../format'
import { useGroupsStore } from '../stores/groups'
import { useUiStore } from '../stores/ui'

const groups = useGroupsStore()
const ui = useUiStore()

const groupId = ref('')
const days = ref(7)
const stats = ref(null)
const loading = ref(false)

const maxDay = computed(() => Math.max(1, ...(stats.value?.by_day || []).map((d) => d.posts)))

async function load() {
  loading.value = true
  try {
    stats.value = await api.stats({ group_id: groupId.value || undefined, days: days.value })
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    loading.value = false
  }
}

async function quickScan() {
  const g = groups.groups.find((x) => x.id === Number(groupId.value))
  if (!g) {
    ui.toast('Chọn nhóm trước', 'warn')
    return
  }
  try {
    await api.createScan({ group_id: g.id, days: days.value, reset_cursor: false, requested_by: 'web-ui' })
    ui.toast(`Đã đặt yêu cầu quét "${g.name}"`, 'ok')
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

onMounted(async () => {
  await groups.fetchGroups()
  await load()
})
</script>

<template>
  <div>
    <div class="page-head">
      <h1>Thống kê</h1>
      <div class="spacer"></div>
      <select v-model="groupId" class="select" style="max-width: 280px" @change="load">
        <option value="">Tất cả nhóm</option>
        <option v-for="g in groups.groups" :key="g.id" :value="g.id">{{ g.name }}</option>
      </select>
      <select v-model.number="days" class="select" style="width: 130px" @change="load">
        <option :value="1">1 ngày</option>
        <option :value="7">7 ngày</option>
        <option :value="30">30 ngày</option>
        <option :value="90">90 ngày</option>
        <option :value="365">365 ngày</option>
      </select>
      <button class="btn" @click="load">↻</button>
      <button v-if="groupId" class="btn primary sm" @click="quickScan">Quét nhóm này</button>
    </div>

    <div v-if="loading" class="loading">Đang tải…</div>
    <template v-else-if="stats">
      <div class="stat-grid">
        <div class="stat">
          <div class="label">Bài viết ({{ stats.days }} ngày)</div>
          <div class="value">{{ fmtNum(stats.total) }}</div>
        </div>
        <div class="stat">
          <div class="label">Tổng like</div>
          <div class="value">{{ fmtNum(stats.by_day.reduce((s, d) => s + (d.likes || 0), 0)) }}</div>
        </div>
        <div class="stat">
          <div class="label">Tổng bình luận</div>
          <div class="value">{{ fmtNum(stats.by_day.reduce((s, d) => s + (d.comments || 0), 0)) }}</div>
        </div>
        <div class="stat">
          <div class="label">Nhóm đang bật</div>
          <div class="value">{{ groups.groups.filter((g) => g.is_active).length }}</div>
        </div>
      </div>

      <div class="card" style="margin-top: 16px">
        <h2>Bài theo ngày</h2>
        <div v-if="!stats.by_day.length" class="empty">Không có dữ liệu trong khoảng này.</div>
        <div v-else class="bars">
          <div v-for="d in stats.by_day" :key="d.day" class="bar-col" :title="`${d.day}: ${d.posts} bài, ${d.likes} like`">
            <div style="font-size: 11px; color: var(--text-dim)">{{ d.posts }}</div>
            <div class="bar" :style="{ height: `${(d.posts / maxDay) * 120}px` }"></div>
            <div class="bar-label">{{ d.day.slice(5) }}</div>
          </div>
        </div>
      </div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-top: 16px">
        <div class="card">
          <h2>Tác giả tích cực</h2>
          <div v-if="!stats.top_authors.length" class="empty">—</div>
          <table v-else class="grid" style="margin-top: 10px">
            <thead>
              <tr><th>Tác giả</th><th class="num">Bài</th><th class="num">Like</th></tr>
            </thead>
            <tbody>
              <tr v-for="(a, i) in stats.top_authors" :key="i">
                <td>
                  <RouterLink :to="{ name: 'posts', query: { author: a.author_name || '', group_id: groupId || undefined } }">
                    {{ a.author_name || 'Ẩn danh' }}
                  </RouterLink>
                </td>
                <td class="num">{{ fmtNum(a.posts) }}</td>
                <td class="num">{{ fmtNum(a.likes) }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="card">
          <h2>Bài nhiều like nhất</h2>
          <div v-if="!stats.top_posts.length" class="empty">—</div>
          <div v-for="p in stats.top_posts" :key="p.id" style="padding: 8px 0; border-bottom: 1px solid var(--line)">
            <RouterLink :to="{ name: 'posts', query: { group_id: p.group_id } }" style="font-weight: 500">
              {{ p.title || `#${p.id}` }}
            </RouterLink>
            <div style="font-size: 12px; color: var(--text-dim)">
              {{ p.author_name || 'Ẩn danh' }} · 👍 {{ fmtNum(p.like_count) }} · 💬 {{ fmtNum(p.comment_count) }} ·
              {{ fmtDate(p.posted_at) }}
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
