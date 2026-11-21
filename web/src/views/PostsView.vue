<script setup>
import { ref, watch, onMounted, reactive } from 'vue'
import { useRoute } from 'vue-router'
import { api, mediaUrl } from '../api'
import { fmtDate, fmtNum, initials, toIsoDate } from '../format'
import { useGroupsStore } from '../stores/groups'
import { useUiStore } from '../stores/ui'
import PostDetail from '../components/PostDetail.vue'

const groups = useGroupsStore()
const ui = useUiStore()

const filters = reactive({
  group_id: '',
  q: '',
  author: '',
  date_from: '',
  date_to: '',
  has_media: '',
  is_read: '',
})
const sort = ref('posted_at')
const order = ref('desc')
const size = ref(20)
const page = ref(1)

const data = ref({ items: [], total: 0, pages: 0 })
const loading = ref(false)
const error = ref('')
const selectedId = ref(null)
const selected = ref(null)
const selectedLoading = ref(false)
const checked = reactive(new Set())

function buildParams() {
  return {
    group_id: filters.group_id || undefined,
    q: filters.q || undefined,
    author: filters.author || undefined,
    date_from: filters.date_from ? `${filters.date_from}T00:00:00` : undefined,
    date_to: filters.date_to ? `${filters.date_to}T23:59:59` : undefined,
    has_media: filters.has_media === '' ? undefined : filters.has_media,
    is_read: filters.is_read === '' ? undefined : filters.is_read,
    sort: sort.value,
    order: order.value,
    page: page.value,
    size: size.value,
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.posts(buildParams())
  } catch (e) {
    error.value = e.message
    data.value = { items: [], total: 0, pages: 0 }
  } finally {
    loading.value = false
  }
}

let timer = null
function reloadDebounced() {
  clearTimeout(timer)
  timer = setTimeout(() => {
    page.value = 1
    load()
  }, 350)
}

function applyFilters() {
  page.value = 1
  load()
}

function resetFilters() {
  Object.assign(filters, {
    group_id: '',
    q: '',
    author: '',
    date_from: '',
    date_to: '',
    has_media: '',
    is_read: '',
  })
  sort.value = 'posted_at'
  order.value = 'desc'
  applyFilters()
}

function setRange(days) {
  const to = new Date()
  const from = new Date()
  from.setDate(from.getDate() - days)
  filters.date_from = toIsoDate(from)
  filters.date_to = toIsoDate(to)
  applyFilters()
}

/** Lọc nhanh theo 1 năm (hoặc khoảng năm) dựa trên bài đã lưu. */
const yearFilter = ref('')
const yearOptions = ref([])

async function loadYears() {
  try {
    const st = await api.stats({ days: 3650 })
    const years = new Set((st.by_day || []).map((d) => d.day.slice(0, 4)))
    yearOptions.value = [...years].sort().reverse()
  } catch {
    yearOptions.value = []
  }
}

function setYear(year) {
  if (!year) {
    filters.date_from = ''
    filters.date_to = ''
  } else {
    filters.date_from = `${year}-01-01`
    filters.date_to = `${year}-12-31`
  }
  applyFilters()
}

function changeSort(e) {
  sort.value = e.target.value
  load()
}

function changePage(p) {
  if (p < 1 || (data.value.pages && p > data.value.pages)) return
  page.value = p
  load()
}

async function openPost(p) {
  selectedId.value = p.id
  selectedLoading.value = true
  try {
    selected.value = await api.post(p.id)
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    selectedLoading.value = false
  }
}

function updateInList(updated) {
  const item = data.value.items.find((x) => x.id === updated.id)
  if (item) Object.assign(item, updated)
  if (selected.value?.id === updated.id) Object.assign(selected.value, updated)
}

async function toggleRead(p, evt) {
  evt?.stopPropagation()
  const next = !p.is_read
  try {
    const r = await api.updatePost(p.id, { is_read: next })
    updateInList(r)
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

async function saveNote(p, note) {
  try {
    const r = await api.updatePost(p.id, { note })
    updateInList(r)
    ui.toast('Đã lưu ghi chú', 'ok')
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

async function removePost(p) {
  let preview = null
  try {
    preview = await api.deletePreview(p.id)
  } catch (e) {
    ui.toast(e.message, 'error')
    return
  }
  const ok = await ui.confirm({
    title: 'Xoá bài viết?',
    message: `Sẽ xoá bài #${p.id} của "${p.author_name || 'ẩn danh'}" khỏi DB${
      preview.media_files ? ` và ${preview.media_files} file ảnh trên đĩa` : ''
    }. Lần quét sau Facebook trả về bài này thì nó sẽ được thêm lại.`,
    okText: 'Xoá',
    danger: true,
  })
  if (!ok) return
  try {
    const r = await api.deletePost(p.id)
    ui.toast(`Đã xoá ${r.deleted} bài, ${r.files_deleted} file ảnh`, 'ok')
    if (selectedId.value === p.id) {
      selectedId.value = null
      selected.value = null
    }
    data.value.items = data.value.items.filter((x) => x.id !== p.id)
    data.value.total = Math.max(0, data.value.total - 1)
    groups.fetchGroups(true)
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

function toggleCheck(p) {
  if (checked.has(p.id)) checked.delete(p.id)
  else checked.add(p.id)
}

function toggleCheckAll() {
  const allChecked = data.value.items.length > 0 && data.value.items.every((p) => checked.has(p.id))
  checked.clear()
  if (!allChecked) data.value.items.forEach((p) => checked.add(p.id))
}

async function bulkRead() {
  const ids = [...checked]
  if (!ids.length) return
  try {
    const r = await api.bulkRead(ids)
    data.value.items.forEach((p) => {
      if (checked.has(p.id)) p.is_read = true
    })
    if (selected.value && checked.has(selected.value.id)) selected.value.is_read = true
    ui.toast(`Đã đánh dấu ${r.updated} bài đã đọc`, 'ok')
    checked.clear()
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

async function bulkDelete() {
  const ids = [...checked]
  if (!ids.length) return
  const ok = await ui.confirm({
    title: `Xoá ${ids.length} bài?`,
    message: 'Các bài đã chọn sẽ bị xoá khỏi DB kèm ảnh đã tải. Thao tác không hoàn tác được.',
    okText: 'Xoá tất cả',
    danger: true,
  })
  if (!ok) return
  try {
    const r = await api.deletePosts(ids)
    ui.toast(`Đã xoá ${r.deleted} bài, ${r.files_deleted} file ảnh`, 'ok')
    checked.clear()
    if (ids.includes(selectedId.value)) {
      selectedId.value = null
      selected.value = null
    }
    load()
    groups.fetchGroups(true)
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ui.toast('Đã copy', 'ok', 1500)
  } catch {
    ui.toast('Không copy được', 'error')
  }
}

watch(() => filters.group_id, () => applyFilters())

const route = useRoute()
onMounted(async () => {
  // deep-link từ Thống kê/Nhóm: /posts?group_id=1&author=...
  if (route.query.group_id) filters.group_id = String(route.query.group_id)
  if (route.query.author) filters.author = String(route.query.author)
  if (route.query.q) filters.q = String(route.query.q)
  await groups.fetchGroups()
  loadYears()
  await load()
})
</script>

<template>
  <div>
    <div class="page-head">
      <h1>Bài viết</h1>
      <span class="sub">{{ fmtNum(data.total) }} bài</span>
      <div class="spacer"></div>
      <button class="btn" @click="load">↻ Tải lại</button>
    </div>

    <div class="toolbar">
      <input v-model="filters.q" class="input" placeholder="Tìm nội dung…" style="width: 240px" @input="reloadDebounced" />
      <input v-model="filters.author" class="input" placeholder="Tác giả…" style="width: 160px" @input="reloadDebounced" />
      <select v-model="filters.group_id" class="select" style="max-width: 260px">
        <option value="">Tất cả nhóm</option>
        <option v-for="g in groups.groups" :key="g.id" :value="g.id">{{ g.name }}</option>
      </select>
      <input v-model="filters.date_from" type="date" class="input" />
      <span style="color: var(--text-dim)">→</span>
      <input v-model="filters.date_to" type="date" class="input" @change="applyFilters" />
      <select v-model="filters.has_media" class="select" @change="applyFilters">
        <option value="">Ảnh: tất cả</option>
        <option :value="'true'">Có ảnh</option>
        <option :value="'false'">Không ảnh</option>
      </select>
      <select v-model="filters.is_read" class="select" @change="applyFilters">
        <option value="">Đọc: tất cả</option>
        <option :value="'false'">Chưa đọc</option>
        <option :value="'true'">Đã đọc</option>
      </select>
      <select :value="sort" class="select" @change="changeSort">
        <option value="posted_at">Mới nhất</option>
        <option value="like_count">Nhiều like</option>
        <option value="comment_count">Nhiều bình luận</option>
        <option value="scraped_at">Vừa quét</option>
      </select>
      <select v-if="yearOptions.length" :value="yearFilter" class="select" style="width: 110px" @change="yearFilter = $event.target.value; setYear($event.target.value)">
        <option value="">Năm: tất cả</option>
        <option v-for="y in yearOptions" :key="y" :value="y">{{ y }}</option>
      </select>
      <button class="btn ghost sm" @click="setRange(1)">Hôm nay</button>
      <button class="btn ghost sm" @click="setRange(7)">7 ngày</button>
      <button class="btn ghost sm" @click="yearFilter = ''; resetFilters()">Xoá lọc</button>
    </div>

    <div v-if="checked.size" class="toolbar card" style="padding: 8px 12px">
      <strong>{{ checked.size }} bài đã chọn</strong>
      <button class="btn sm" @click="bulkRead">Đánh dấu đã đọc</button>
      <button class="btn danger sm" @click="bulkDelete">Xoá</button>
      <button class="btn ghost sm" @click="checked.clear()">Bỏ chọn</button>
    </div>

    <div v-if="error" class="error-box" style="margin-bottom: 12px">{{ error }}</div>

    <div class="posts-layout">
      <div>
        <div v-if="loading" class="loading">Đang tải…</div>
        <div v-else-if="!data.items.length" class="card empty">
          Không có bài nào khớp bộ lọc. Thử nới khoảng ngày hoặc <RouterLink to="/scan">đặt yêu cầu quét</RouterLink>.
        </div>
        <div v-else class="post-list">
          <div class="toolbar" style="margin: 0 0 2px">
            <label class="switch" style="font-size: 12px; color: var(--text-dim)">
              <input
                type="checkbox"
                :checked="data.items.length && data.items.every((p) => checked.has(p.id))"
                @change="toggleCheckAll"
              />
              Chọn trang này
            </label>
          </div>
          <div
            v-for="p in data.items"
            :key="p.id"
            class="post-card"
            :class="{ active: selectedId === p.id, unread: !p.is_read }"
            @click="openPost(p)"
          >
            <input type="checkbox" :checked="checked.has(p.id)" @click.stop="toggleCheck(p)" />
            <img v-if="p.cover_url" :src="mediaUrl(p.cover_url)" class="thumb" loading="lazy" alt="" />
            <div v-else class="thumb" style="display: grid; place-items: center; color: #4a5178">🖼</div>
            <div class="body">
              <div class="title">{{ p.title || p.summary || '(không tiêu đề)' }}</div>
              <div v-if="p.content && p.content !== p.title" class="snippet">{{ p.content }}</div>
              <div class="meta">
                <span class="avatar" style="width: 20px; height: 20px; font-size: 9px">{{ initials(p.author_name) }}</span>
                <span>{{ p.author_name || 'Ẩn danh' }}</span>
                <span>·</span>
                <span>
                  <template v-if="p.source_type === 'page'">📄 {{ p.source_name || p.source_fb_id }}</template>
                  <template v-else>{{ p.group_name || p.source_name }}</template>
                </span>
                <span>·</span>
                <span>{{ fmtDate(p.posted_at) }}</span>
              </div>
              <div class="meta">
                <span>👍 {{ fmtNum(p.like_count) }}</span>
                <span>💬 {{ fmtNum(p.comment_count) }}</span>
                <span>↗ {{ fmtNum(p.share_count) }}</span>
                <span v-if="p.note" class="tag warn">📌 ghi chú</span>
                <span v-if="p.is_read" class="tag ok">đã đọc</span>
              </div>
            </div>
            <div class="actions" @click.stop>
              <button class="btn ghost sm" :title="p.is_read ? 'Đánh dấu chưa đọc' : 'Đánh dấu đã đọc'" @click="toggleRead(p, $event)">
                {{ p.is_read ? '↺' : '✓' }}
              </button>
              <button class="btn ghost sm" title="Xoá bài" @click="removePost(p)">🗑</button>
            </div>
          </div>
        </div>

        <div class="pager">
          <button class="btn sm" :disabled="page <= 1" @click="changePage(page - 1)">‹ Trước</button>
          <span>Trang {{ page }} / {{ data.pages || 1 }}</span>
          <button class="btn sm" :disabled="page >= data.pages" @click="changePage(page + 1)">Sau ›</button>
          <select v-model.number="size" class="select sm" @change="applyFilters">
            <option :value="10">10/trang</option>
            <option :value="20">20/trang</option>
            <option :value="50">50/trang</option>
            <option :value="100">100/trang</option>
          </select>
        </div>
      </div>

      <div class="detail">
        <div v-if="selectedLoading" class="card loading">Đang tải chi tiết…</div>
        <PostDetail
          v-else-if="selected"
          :post="selected"
          @toggle-read="toggleRead"
          @save-note="saveNote"
          @delete="removePost"
          @copy="copyText"
          @close="selected = null; selectedId = null"
        />
        <div v-else class="card empty" style="font-size: 13px">Chọn một bài để xem chi tiết, ảnh, ghi chú.</div>
      </div>
    </div>
  </div>
</template>
