<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '../api'
import { fmtDate, fmtNum } from '../format'
import { useUiStore } from '../stores/ui'

const ui = useUiStore()

const form = reactive({
  mode: 'keyword', // keyword | page
  keyword: '',
  page: '',
  resume: true, // Page: quét tiếp từ mốc lần trước
  timeMode: 'today', // today | year | range | all
  keyword_match: 'strict', // strict | normal | loose | off
  from_year: new Date().getFullYear(),
  to_year: '',
  from_date: '',
  to_date: '',
  lookback: 1000,
  max_posts: 0,
  search_all: true,
  excluded_terms: '',
  exclude_sources: '',
  download_media: true,
})

const previewing = ref(false)
const elapsed = ref(0)
let elapsedTimer = null
const reviewStarting = ref(false)
const preview = ref(null)
const reviews = ref([])
const review = ref(null)
const reviewLoading = ref(false)
const showSkipped = ref(false)
const excludedIds = ref([]) // fb_post_id người dùng đã chọn "đừng lấy lại"
let pollTimer = null
let pollTries = 0

const sourceLabel = { group: 'nhóm', page: 'Page', user: 'trang cá nhân', unknown: '?' }

const isPage = computed(() => form.mode === 'page')

/** Mốc quét của Page đang nhập (bài mới nhất đã quét + lần quét gần nhất). */
const pageCursor = ref(null)
let cursorTimer = null

async function loadCursor() {
  clearTimeout(cursorTimer)
  pageCursor.value = null
  if (!isPage.value || !form.page.trim()) return
  cursorTimer = setTimeout(async () => {
    try {
      const rows = await api.pageCursors({ limit: 200 })
      const key = form.page.trim().replace(/^https?:\/\/(www\.)?facebook\.com\//i, '').split(/[/?#]/)[0].replace(/^@/, '').toLowerCase()
      pageCursor.value = rows.find((r) => r.source_key === key) || null
    } catch {
      /* không quan trọng */
    }
  }, 400)
}

async function forgetCursor() {
  if (!pageCursor.value) return
  const ok = await ui.confirm({
    title: `Xoá mốc quét của “${pageCursor.value.page_name || pageCursor.value.source_key}”?`,
    message:
      'Lần quét sau sẽ quét lại toàn bộ khoảng thời gian đã chọn thay vì quét tiếp. ' +
      'Bài đã lưu trong DB vẫn được giữ và nhận ra là "đã có", nhưng sẽ chậm hơn.',
    okText: 'Xoá mốc',
  })
  if (!ok) return
  try {
    await api.forgetPageCursor(pageCursor.value.source_key)
    pageCursor.value = null
    ui.toast('Đã xoá mốc quét', 'ok')
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

/** Từ khóa/Page đang dùng — dùng cho tiêu đề và so khớp lịch sử review. */
function termValue() {
  return (isPage.value ? form.page : form.keyword).trim()
}

/** Số bài Facebook trả về nhưng KHÔNG liên quan / sai ngày (chỉ để tham khảo, không hiện). */
const hiddenTotal = computed(() =>
  Object.values(preview.value?.hidden_counts || {}).reduce((a, b) => a + b, 0),
)

function splitList(s) {
  return (s || '')
    .split(/[,;\n]/)
    .map((x) => x.trim())
    .filter(Boolean)
}

/** Tham số lọc thời gian chung cho preview + review. */
function filters() {
  const base = {
    lookback: Number(form.lookback) || undefined,
    keyword_match: form.keyword_match,
    excluded_terms: splitList(form.excluded_terms),
    exclude_sources: splitList(form.exclude_sources),
    exclusions: excludedIds.value,
  }
  if (form.timeMode === 'today') {
    return { ...base, date: '' } // trống = hôm nay theo giờ Việt Nam
  }
  if (form.timeMode === 'year') {
    return { ...base, from_year: Number(form.from_year) || undefined, to_year: form.to_year ? Number(form.to_year) : undefined }
  }
  if (form.timeMode === 'range') {
    return { ...base, from_date: form.from_date || undefined, to_date: form.to_date || undefined }
  }
  return base
}

function validate() {
  if (isPage.value) {
    if (!form.page.trim()) return 'Nhập link hoặc tên Page cần quét'
  } else if (!form.keyword.trim()) {
    return 'Nhập từ khóa cần kiểm tra'
  }
  if (form.timeMode === 'year') {
    if (!form.from_year) return 'Nhập năm cần lấy'
    if (form.to_year && Number(form.to_year) < Number(form.from_year)) return 'Đến năm phải >= từ năm'
  }
  if (form.timeMode === 'range' && form.from_date && form.to_date && form.to_date < form.from_date) {
    return 'Đến ngày phải sau từ ngày'
  }
  return ''
}

async function runPreview() {
  const err = validate()
  if (err) {
    ui.toast(err, 'warn')
    return
  }
  previewing.value = true
  preview.value = null
  elapsed.value = 0
  clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => (elapsed.value += 1), 1000)
  try {
    const base = {
      ...filters(),
      max_posts: Number(form.max_posts) || 0,
      sample: 40,
    }
    preview.value = isPage.value
      ? await api.searchPreview({ page: form.page.trim(), resume: form.resume, ...base })
      : await api.searchPreview({
          keyword: form.keyword.trim(),
          ...base,
          search_all: form.search_all,
        })
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    clearInterval(elapsedTimer)
    previewing.value = false
  }
}

async function startReview() {
  const err = validate()
  if (err) {
    ui.toast(err, 'warn')
    return
  }
  const term = termValue()
  const ok = await ui.confirm({
    title: isPage.value ? `Quét Page "${term}"?` : `Review "${term}"?`,
    message:
      (isPage.value
        ? 'Sẽ mở feed Page trên Facebook, LƯU các bài trong khoảng thời gian đã chọn vào DB.'
        : 'Sẽ tìm trên Facebook, LƯU bài mới vào DB (như quét) và ghi lại kết quả để đối chiếu.') +
      ' Bài đã có trong DB sẽ không tải lại.' +
      (excludedIds.value.length ? `\nLoại trừ thêm ${excludedIds.value.length} bài đã chọn.` : ''),
    okText: isPage.value ? 'Quét Page' : 'Chạy review',
  })
  if (!ok) return
  reviewStarting.value = true
  try {
    const base = {
      ...filters(),
      max_posts: Number(form.max_posts) || 0,
      download_media: form.download_media,
      created_by: 'web-ui',
    }
    if (isPage.value) {
      await api.searchReview({ page: term, resume: form.resume, ...base })
    } else {
      await api.searchReview({
        keyword: term,
        ...base,
        search_all: form.search_all,
      })
    }
    ui.toast('Đã bắt đầu — đang chờ kết quả…', 'ok')
    pollTries = 0
    startPolling(term)
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    reviewStarting.value = false
  }
}

/** Review chạy nền: cứ 5s hỏi lại danh sách xem có bản ghi mới chưa. */
function startPolling(term) {
  stopPolling()
  pollTimer = setInterval(async () => {
    pollTries++
    try {
      await loadReviews()
      const latest = reviews.value[0]
      if (latest && latest.term === term) {
        stopPolling()
        await openReview(latest.id)
        ui.toast(`Review xong: ${latest.new_count} bài mới, ${latest.existing_count} đã có`, 'ok')
      } else if (pollTries > 120) {
        stopPolling()
        ui.toast('Review chưa trả về sau 10 phút — kiểm tra worker/API log', 'warn')
      }
    } catch {
      /* im lặng khi poll */
    }
  }, 5000)
}

function stopPolling() {
  clearInterval(pollTimer)
  pollTimer = null
}

async function loadReviews() {
  reviews.value = await api.reviews({ limit: 50 })
}

async function openReview(id) {
  reviewLoading.value = true
  showSkipped.value = false
  try {
    review.value = await api.review(id, { with_posts: true, sample: 200 })
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    reviewLoading.value = false
  }
}

function excludeFromPreview(item) {
  if (!excludedIds.value.includes(item.fb_post_id)) {
    excludedIds.value.push(item.fb_post_id)
    ui.toast(`Đã loại trừ ${item.fb_post_id} — lần review tới sẽ bỏ qua`, 'ok', 2000)
  }
}

function clearExclusions() {
  excludedIds.value = []
}

const reasonTag = (reason) => {
  if (reason === 'already_in_db') return 'ok'
  if (reason === 'out_of_range' || reason === 'excluded_id' || reason === 'excluded_source') return 'warn'
  return ''
}

watch(() => [form.mode, form.page], loadCursor)

onMounted(async () => {
  try {
    await loadReviews()
  } catch (e) {
    ui.toast(e.message, 'error')
  }
})
onUnmounted(() => {
  stopPolling()
  clearInterval(elapsedTimer)
  clearTimeout(cursorTimer)
})
</script>

<template>
  <div>
    <div class="page-head">
      <h1>Đối chiếu với Facebook</h1>
    </div>

    <div class="posts-layout">
      <div>
        <div class="card" style="margin-bottom: 16px">
          <h2 style="margin-bottom: 4px">1. Kiểm tra trước</h2>

          <div style="display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap">
            <button
              class="btn sm"
              :class="{ primary: form.mode === 'keyword' }"
              @click="form.mode = 'keyword'"
            >
              Theo từ khóa
            </button>
            <button
              class="btn sm"
              :class="{ primary: form.mode === 'page' }"
              @click="form.mode = 'page'"
            >
              Theo Page
            </button>
          </div>

          <label v-if="!isPage" class="field">
            <span>Từ khóa</span>
            <input
              v-model="form.keyword"
              class="input"
              placeholder="VD: tuyển dụng kế toán"
              @keyup.enter="runPreview"
            />
          </label>
          <label v-else class="field">
            <span>Link hoặc tên Page cần quét</span>
            <input
              v-model="form.page"
              class="input"
              placeholder="VD: https://facebook.com/thanhnien"
              @keyup.enter="runPreview"
            />
            <div
              v-if="pageCursor"
              class="tag ok"
              style="margin-top: 8px; display: inline-flex; gap: 6px; align-items: center"
            >
              Lần quét gần nhất {{ pageCursor.last_scan_text }} — mốc: bài mới nhất đã quét
              {{ pageCursor.last_posted_text }}
              <button class="btn sm" style="padding: 0 6px" @click="forgetCursor">✕ xoá mốc</button>
            </div>
          </label>

          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 10px">
            <label class="field">
              <span>Lọc thời gian</span>
              <select v-model="form.timeMode" class="select">
                <option value="today">Chỉ hôm nay</option>
                <option value="year">Theo năm</option>
                <option value="range">Theo ngày</option>
                <option value="all">Tất cả (mới nhất trước)</option>
              </select>
            </label>
            <template v-if="form.timeMode === 'year'">
              <label class="field">
                <span>Từ năm</span>
                <input v-model.number="form.from_year" type="number" min="2004" max="2100" class="input" />
              </label>
              <label class="field">
                <span>Đến năm (trống = cùng năm)</span>
                <input v-model.number="form.to_year" type="number" min="2004" max="2100" class="input" placeholder="—" />
              </label>
            </template>
            <template v-if="form.timeMode === 'range'">
              <label class="field">
                <span>Từ ngày</span>
                <input v-model="form.from_date" type="date" class="input" />
              </label>
              <label class="field">
                <span>Đến ngày</span>
                <input v-model="form.to_date" type="date" class="input" />
              </label>
            </template>
            <label v-if="!isPage" class="field">
              <span>Mức khớp từ khóa (trong tiêu đề/nội dung)</span>
              <select v-model="form.keyword_match" class="select">
                <option value="strict">Chặt: đủ ý từ khóa (khuyên dùng)</option>
                <option value="normal">Vừa: thêm bài trùng 1 từ chính</option>
                <option value="loose">Rộng: có bất kỳ từ nào</option>
                <option value="off">Không kiểm tra từ khóa</option>
              </select>
            </label>
            <label class="field">
              <span>Đọc tối đa N bài kết quả</span>
              <input v-model.number="form.lookback" type="number" min="0" max="20000" class="input" />
            </label>
            <label class="field">
              <span>Tối đa bài lưu (0 = không giới hạn)</span>
              <input v-model.number="form.max_posts" type="number" min="0" max="5000" class="input" />
            </label>
          </div>

          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin-top: 10px">
            <label class="field">
              <span>Cụm từ loại trừ (cách nhau bằng dấu phẩy)</span>
              <input v-model="form.excluded_terms" class="input" placeholder="VD: đã tuyển, hết hạn" />
            </label>
            <label class="field">
              <span>Loại trừ nguồn (id Page/trang cá nhân)</span>
              <input v-model="form.exclude_sources" class="input" placeholder="VD: shopdienmay, minh.abc" />
            </label>
          </div>

          <div style="display: flex; gap: 18px; margin-top: 12px; flex-wrap: wrap; align-items: center">
            <label v-if="!isPage" class="switch">
              <input v-model="form.search_all" type="checkbox" />
              Lấy mọi nguồn (nhóm + Page + trang cá nhân)
            </label>
            <label v-if="isPage" class="switch" :title="'Chỉ quét bài mới hơn mốc lần trước — nhanh hơn nhiều'">
              <input v-model="form.resume" type="checkbox" />
              Quét tiếp từ mốc lần trước (bỏ qua bài đã quét)
            </label>
            <label class="switch">
              <input v-model="form.download_media" type="checkbox" />
              Tải ảnh khi lưu (review)
            </label>
          </div>

          <div style="display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; align-items: center">
            <button class="btn primary" :disabled="previewing" @click="runPreview">
              {{ previewing ? `Đang kiểm tra… ${elapsed}s` : '🔍 Kiểm tra trước' }}
            </button>
            <button class="btn" :disabled="reviewStarting" @click="startReview">
              {{ reviewStarting ? 'Đang gửi…' : 'Review & lưu kết quả' }}
            </button>
            <button
              v-if="excludedIds.length"
              class="btn ghost sm"
              title="Bỏ danh sách bài đang loại trừ"
              @click="clearExclusions"
            >
              Bỏ loại trừ ({{ excludedIds.length }})
            </button>
          </div>

          <div v-if="excludedIds.length" class="tag warn" style="margin-top: 10px">
            Đang loại trừ {{ excludedIds.length }} bài: {{ excludedIds.slice(0, 5).join(', ') }}{{ excludedIds.length > 5 ? '…' : '' }}
          </div>
        </div>

        <!-- Kết quả preview -->
        <div v-if="preview" class="card" style="margin-bottom: 16px">
          <div class="page-head" style="margin-bottom: 8px">
            <h2>Kết quả {{ preview.page ? 'quét Page' : 'kiểm tra' }}: “{{ preview.page_name || preview.term }}”</h2>
            <span v-if="preview.page" class="tag info">Page</span>
            <span class="tag info">{{ preview.range }}</span>
            <span v-if="preview.resume" class="tag ok" :title="`Chỉ quét bài mới hơn ${preview.since_text}`">
              quét tiếp từ {{ preview.since_text }}
            </span>
          </div>
          <div class="stat-grid">
            <div class="stat"><div class="label">Facebook trả về</div><div class="value">{{ fmtNum(preview.found) }}</div></div>
            <div class="stat"><div class="label">Đúng khoảng thời gian</div><div class="value">{{ fmtNum(preview.in_range) }}</div></div>
            <div class="stat"><div class="label">Sẽ lưu mới</div><div class="value" style="color: var(--ok)">{{ fmtNum(preview.would_save) }}</div></div>
            <div class="stat">
              <div class="label">Hiện trên tool</div>
              <div class="value" style="color: var(--ok)">{{ fmtNum(preview.items.length) }}</div>
            </div>
          </div>

          <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px">
            <span v-if="preview.would_save" class="tag ok">Sẽ lưu mới: {{ fmtNum(preview.would_save) }}</span>
            <span v-if="preview.existing" class="tag">Đã có trong DB: {{ fmtNum(preview.existing) }}</span>
            <span
              v-if="hiddenTotal"
              class="tag"
              style="opacity: 0.65"
              title="Bài Facebook trả về nhưng không chứa từ khóa hoặc ngoài khoảng thời gian đã lọc — không hiển thị và không lưu."
            >
              Đã ẩn (không liên quan / sai ngày): {{ fmtNum(hiddenTotal) }}
            </span>
          </div>
          <div style="font-size: 12px; color: var(--text-dim); margin-top: 8px">
            Dừng vì: {{ preview.stopped_reason }}
            <template v-if="preview.items.length >= preview.sample_size">
              · chỉ hiện {{ preview.sample_size }} bài đầu
            </template>
          </div>
          <div v-if="!preview.page && preview.match_counts" style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px">
            <span style="font-size: 12px; color: var(--text-dim)">Mức khớp từ khóa:</span>
            <span v-for="(v, k) in preview.match_counts" :key="k" class="tag">{{ k }}: {{ fmtNum(v) }}</span>
          </div>
          <div v-if="preview.note" class="error-box" style="margin-top: 10px; font-size: 12px; background: rgba(240,168,0,.1); border-color: rgba(240,168,0,.4); color: #ffd477">
            ⚠ {{ preview.note }}
          </div>

          <div v-if="preview.stopped_reason?.startsWith('lỗi:')" class="error-box" style="margin-top: 10px; font-size: 12px">
            ❌ Không phải "không có bài" — lỗi khi đọc kết quả tìm kiếm (xem chi tiết bên dưới).
            Nếu là lỗi phiên: export lại <code>facebook_cookies.json</code> (đủ <code>c_user</code> +
            <code>xs</code>) rồi thử lại.
          </div>
          <div v-if="preview.page" class="tag info" style="margin-top: 8px">
            Đọc feed Page bằng Chromium — bài chỉ có ảnh/video có thể không có nội dung chữ
          </div>
          <div v-else-if="preview.engine === 'browser'" class="tag info" style="margin-top: 8px">
            Đọc bằng browser (GraphQL bị Facebook chặn) — nội dung/ngày có thể thiếu so với GraphQL
          </div>
          <div v-if="preview.errors?.length" class="error-box" style="margin-top: 10px; font-size: 12px">
            {{ preview.errors.join(' · ') }}
          </div>

          <div class="post-list" style="margin-top: 12px">
            <div v-for="it in preview.items" :key="it.fb_post_id" class="post-card" style="cursor: default">
              <div class="body">
                <div class="title">{{ it.snippet || '(không có nội dung)' }}</div>
                <div class="meta">
                  <span class="avatar" style="width: 20px; height: 20px; font-size: 9px">👤</span>
                  <span>{{ it.author_name || 'Ẩn danh' }}</span>
                  <span>·</span>
                  <span>
                    {{ sourceLabel[it.source_type] || 'bài viết' }}
                    <template v-if="it.source_name && it.source_name !== it.author_name">
                      · {{ it.source_name }}
                    </template>
                    <template v-else-if="!it.source_name && it.source_fb_id">· {{ it.source_fb_id }}</template>
                  </span>
                  <span>·</span>
                  <span>{{ fmtDate(it.posted_at) }}</span>
                </div>
                <div class="meta">
                  <span class="tag" :class="it.keep ? 'ok' : reasonTag(it.reason)">
                    {{ it.keep ? 'sẽ lưu' : it.reason_text }}
                  </span>
                  <span v-if="!preview.page" class="tag" :class="it.match_accepted ? 'ok' : 'warn'" :title="'Mức khớp từ khóa trong nội dung bài hoặc tên group/Page'">
                    khớp: {{ it.level_text || it.match }}
                  </span>
                  <a v-if="it.url" :href="it.url" target="_blank" rel="noopener" @click.stop>Mở Facebook ↗</a>
                  <span v-if="it.existing_id" class="tag ok">đã có trong DB #{{ it.existing_id }}</span>
                  <button v-if="!it.existing_id" class="btn ghost sm" @click="excludeFromPreview(it)">Loại trừ</button>
                </div>
              </div>
            </div>
            <div v-if="!preview.items.length" class="empty">
              <template v-if="preview.page">
                Page không có bài nào trong khoảng thời gian đã chọn.
              </template>
              <template v-else>
                Không có bài nào khớp từ khóa trong khoảng thời gian đã chọn
                (bài Facebook trả về nhưng không liên quan hoặc sai ngày đã được ẩn).
              </template>
            </div>
          </div>
        </div>

        <!-- Chi tiết review đã chọn -->
        <div v-if="review" class="card">
          <div class="page-head" style="margin-bottom: 8px">
            <h2>Báo cáo review #{{ review.id }}: “{{ review.page_name || review.term }}”</h2>
            <span v-if="review.page" class="tag info">Page</span>
            <span class="tag info">{{ review.range }}</span>
            <span v-if="review.search_all" class="tag ok">mọi nguồn</span>
          </div>
          <div class="stat-grid">
            <div class="stat"><div class="label">Facebook trả về</div><div class="value">{{ fmtNum(review.found) }}</div></div>
            <div class="stat"><div class="label">Bài mới lưu</div><div class="value" style="color: var(--ok)">{{ fmtNum(review.new_count) }}</div></div>
            <div class="stat"><div class="label">Đã có trong DB</div><div class="value">{{ fmtNum(review.existing_count) }}</div></div>
            <div class="stat"><div class="label">Bị bỏ qua</div><div class="value" style="color: var(--warn)">{{ fmtNum(review.skipped_count) }}</div></div>
          </div>

          <div v-if="review.note" class="error-box" style="margin-top: 10px; font-size: 12px; background: rgba(240,168,0,.1); border-color: rgba(240,168,0,.4); color: #ffd477">
            ⚠ {{ review.note }}
          </div>

          <div v-if="review.engine === 'browser'" class="tag info" style="margin-top: 8px">
            Lần review này đọc bằng browser (GraphQL bị Facebook chặn) — nội dung/ngày có thể thiếu.
          </div>
          <h3 style="margin-top: 16px; margin-bottom: 8px">Bài mới lưu ({{ review.new_posts?.length || 0 }})</h3>
          <div v-if="!review.new_posts?.length" class="empty" style="padding: 16px">
            Không có bài mới — mọi bài đúng khoảng thời gian đã có trong DB.
            <RouterLink :to="{ name: 'posts', query: { q: review.term } }">Xem bài đã lưu →</RouterLink>
          </div>
          <div v-else class="post-list">
            <div v-for="p in review.new_posts" :key="p.id" class="post-card" style="cursor: default">
              <div class="body">
                <div class="title">{{ p.title || (p.content || '').slice(0, 120) }}</div>
                <div class="meta">
                  <span class="avatar" style="width: 20px; height: 20px; font-size: 9px">👤</span>
                  <span>{{ p.author_name || 'Ẩn danh' }}</span>
                  <span>·</span>
                  <span>{{ sourceLabel[p.source_type] || p.source_type }}<template
                    v-if="(p.source_name || p.group_name) && (p.source_name || p.group_name) !== p.author_name"
                  > {{ p.source_name || p.group_name }}</template></span>
                  <span>·</span>
                  <span>{{ fmtDate(p.posted_at) }}</span>
                  <span v-if="p.media_count" class="tag">🖼 {{ p.media_count }}</span>
                </div>
                <div class="meta">
                  <a v-if="p.permalink" :href="p.permalink" target="_blank" rel="noopener">Mở Facebook ↗</a>
                  <RouterLink :to="{ name: 'posts', query: { q: review.term } }">Bài cùng từ khóa →</RouterLink>
                </div>
              </div>
            </div>
          </div>

          <div style="margin-top: 16px">
            <button class="btn ghost sm" @click="showSkipped = !showSkipped">
              {{ showSkipped ? '▾' : '▸' }} Bài bị bỏ qua ({{ review.skipped_count }})
            </button>
            <div v-if="showSkipped" class="table-wrap" style="margin-top: 8px; max-height: 360px">
              <table class="grid">
                <thead>
                  <tr><th>fb_post_id</th><th>Lý do</th><th>Nội dung</th><th>Ngày</th><th></th></tr>
                </thead>
                <tbody>
                  <tr v-for="s in review.skipped" :key="s.fb_post_id">
                    <td><code style="font-size: 11px; color: var(--text-dim)">{{ s.fb_post_id }}</code></td>
                    <td><span class="tag" :class="reasonTag(s.reason)">{{ s.reason_text || s.reason }}</span></td>
                    <td style="max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
                      {{ s.snippet || '—' }}
                    </td>
                    <td style="white-space: nowrap">{{ fmtDate(s.posted_at) }}</td>
                    <td>
                      <a v-if="s.url" :href="s.url" target="_blank" rel="noopener" style="font-size: 12px">FB ↗</a>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div class="card detail">
        <div class="page-head" style="margin-bottom: 10px">
          <h2>Lịch sử review</h2>
          <div class="spacer"></div>
          <button class="btn ghost sm" @click="loadReviews">↻</button>
        </div>
        <div v-if="!reviews.length" class="empty" style="padding: 16px">Chưa có lần review nào.</div>
        <div
          v-for="r in reviews"
          :key="r.id"
          style="padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 13px; cursor: pointer"
          @click="openReview(r.id)"
        >
          <div style="display: flex; gap: 8px; align-items: center">
            <strong style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">#{{ r.id }} · {{ r.term }}</strong>
            <span style="color: var(--text-dim); font-size: 12px">{{ fmtDate(r.created_at) }}</span>
          </div>
          <div style="color: var(--text-dim); font-size: 12px; margin-top: 2px">
            {{ r.range }} · FB {{ fmtNum(r.found) }} · <span style="color: var(--ok)">mới {{ fmtNum(r.new_count) }}</span> ·
            đã có {{ fmtNum(r.existing_count) }} · bỏ {{ fmtNum(r.skipped_count) }}
          </div>
        </div>
        <div v-if="reviewLoading" class="loading">Đang tải báo cáo…</div>
      </div>
    </div>
  </div>
</template>
