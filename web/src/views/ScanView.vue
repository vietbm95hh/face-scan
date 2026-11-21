<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { fmtDate, fmtNum } from '../format'
import { useGroupsStore } from '../stores/groups'
import { useUiStore } from '../stores/ui'

const groups = useGroupsStore()
const ui = useUiStore()

const form = reactive({
  group_id: '',
  keyword: '',
  timeMode: 'days', // days | today | year | range
  days: 7,
  from_year: new Date().getFullYear(),
  to_year: '',
  from_date: '',
  to_date: '',
  max_posts: 0,
  reset_cursor: true,
  download_media: true,
  search_all: true,
  keyword_match: 'strict',
  note: '',
})

/** Tham số thời gian gửi lên API tuỳ chế độ đã chọn. */
function timeParams() {
  if (form.timeMode === 'today') {
    return { date: '' } // trống = hôm nay theo giờ Việt Nam
  }
  if (form.timeMode === 'year') {
    return {
      from_year: Number(form.from_year) || undefined,
      to_year: form.to_year ? Number(form.to_year) : undefined,
    }
  }
  if (form.timeMode === 'range') {
    return {
      from_date: form.from_date || undefined,
      to_date: form.to_date || undefined,
    }
  }
  return { days: Number(form.days) || 0 }
}

function timeError() {
  if (form.timeMode === 'year') {
    if (!form.from_year) return 'Nhập năm cần lấy'
    if (form.to_year && Number(form.to_year) < Number(form.from_year)) {
      return 'Đến năm phải >= từ năm'
    }
  }
  if (form.timeMode === 'range') {
    if (!form.from_date && !form.to_date) return 'Nhập ít nhất 1 ngày'
    if (form.from_date && form.to_date && form.to_date < form.from_date) {
      return 'Đến ngày phải sau từ ngày'
    }
  }
  return ''
}
const submitting = ref(false)
const requests = ref([])
const status = ref({ queued: 0, running: 0, last_request: null })
const jobs = ref([])
const loading = ref(true)
const filterStatus = ref('')
const bulkScope = ref('active') // active | selected
let timer = null

async function loadAll() {
  loading.value = true
  try {
    const [s, r, j] = await Promise.all([
      api.scanStatus(),
      api.scanRequests({ limit: 100, status: filterStatus.value || undefined }),
      api.jobs({ limit: 30 }),
    ])
    status.value = s
    requests.value = r
    jobs.value = j
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    loading.value = false
  }
}

async function refreshLive() {
  try {
    const [s, r] = await Promise.all([
      api.scanStatus(),
      api.scanRequests({ limit: 100, status: filterStatus.value || undefined }),
    ])
    status.value = s
    requests.value = r
  } catch {
    /* im lặng khi poll */
  }
}

async function submit() {
  if (!form.group_id) {
    ui.toast('Chọn nhóm cần quét', 'warn')
    return
  }
  submitting.value = true
  try {
    const r = await api.createScan({
      group_id: Number(form.group_id),
      ...timeParams(),
      max_posts: Number(form.max_posts) || 0,
      reset_cursor: form.reset_cursor,
      download_media: form.download_media,
      note: form.note || undefined,
      requested_by: 'web-ui',
    })
    ui.toast(`Đã xếp hàng yêu cầu #${r.id} cho "${r.group_name}"`, 'ok')
    form.note = ''
    await refreshLive()
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    submitting.value = false
  }
}

async function submitAllActive() {
  const list = groups.groups.filter((g) => g.is_active)
  if (!list.length) {
    ui.toast('Không có nhóm nào đang bật', 'warn')
    return
  }
  const ok = await ui.confirm({
    title: `Quét ${list.length} nhóm đang bật?`,
    message: `Sẽ tạo ${list.length} yêu cầu quét (${form.days || 'mặc định'} ngày gần nhất). Worker sẽ chạy lần lượt.`,
    okText: 'Đặt yêu cầu',
  })
  if (!ok) return
  let done = 0
  for (const g of list) {
    try {
      await api.createScan({
        group_id: g.id,
        ...timeParams(),
        max_posts: Number(form.max_posts) || 0,
        reset_cursor: form.reset_cursor,
        download_media: form.download_media,
        requested_by: 'web-ui',
      })
      done++
    } catch (e) {
      ui.toast(`${g.name}: ${e.message}`, 'error')
    }
  }
  ui.toast(`Đã đặt ${done}/${list.length} yêu cầu quét`, done ? 'ok' : 'error')
  await refreshLive()
}

async function submitKeyword() {
  const kw = form.keyword.trim()
  if (!kw) {
    ui.toast('Nhập từ khóa cần tìm', 'warn')
    return
  }
  const err = timeError()
  if (err) {
    ui.toast(err, 'warn')
    return
  }
  submitting.value = true
  try {
    const r = await api.createScan({
      keyword: kw,
      ...timeParams(),
      max_posts: Number(form.max_posts) || 0,
      download_media: form.download_media,
      search_all: form.search_all,
      keyword_match: form.keyword_match,
      note: form.note || undefined,
      requested_by: 'web-ui',
    })
    ui.toast(`Đã xếp hàng yêu cầu #${r.id} tìm "${r.keyword}"`, 'ok')
    form.note = ''
    await refreshLive()
  } catch (e) {
    ui.toast(e.message, 'error')
  } finally {
    submitting.value = false
  }
}

async function cancel(r) {
  const ok = await ui.confirm({ title: `Huỷ yêu cầu #${r.id}?`, message: r.group_name, okText: 'Huỷ yêu cầu', danger: true })
  if (!ok) return
  try {
    await api.cancelScan(r.id)
    ui.toast('Đã huỷ', 'ok')
    await refreshLive()
  } catch (e) {
    ui.toast(e.message, 'error')
  }
}

async function changeFilter() {
  requests.value = await api.scanRequests({ limit: 100, status: filterStatus.value || undefined }).catch(() => [])
}

/** Ngày cuối của khoảng [date_from, date_to) để hiển thị cho người dùng. */
function lastDay(dateTo) {
  if (!dateTo) return null
  return new Date(new Date(dateTo).getTime() - 86400000).toISOString()
}

const statusTag = {
  queued: 'info',
  running: 'warn',
  done: 'ok',
  error: 'bad',
  canceled: '',
}

onMounted(async () => {
  await groups.fetchGroups()
  await loadAll()
  timer = setInterval(refreshLive, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="page-head">
      <h1>Quét dữ liệu</h1>
      <span class="sub">
        Đang chờ <strong>{{ status.queued }}</strong> · đang chạy <strong>{{ status.running }}</strong>
      </span>
      <div class="spacer"></div>
      <button class="btn" @click="loadAll">↻ Tải lại</button>
    </div>

    <div class="posts-layout">
      <div>
        <div class="card" style="margin-bottom: 16px">
          <h2 style="margin-bottom: 12px">Đặt yêu cầu quét</h2>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px">
            <label class="field" style="grid-column: span 2">
              <span>Nhóm</span>
              <select v-model="form.group_id" class="select">
                <option value="">— chọn nhóm —</option>
                <option v-for="g in groups.groups" :key="g.id" :value="g.id">
                  {{ g.is_active ? '' : '[tắt] ' }}{{ g.name }}
                </option>
              </select>
            </label>
            <label class="field">
              <span>Lọc thời gian</span>
              <select v-model="form.timeMode" class="select">
                <option value="days">N ngày gần nhất</option>
                <option value="today">Chỉ hôm nay</option>
                <option value="year">Theo năm</option>
                <option value="range">Theo ngày</option>
              </select>
            </label>
            <label v-if="form.timeMode === 'days'" class="field">
              <span>Số ngày gần nhất (0 = mặc định)</span>
              <input v-model.number="form.days" type="number" min="0" max="365" class="input" />
            </label>
            <template v-else-if="form.timeMode === 'year'">
              <label class="field">
                <span>Từ năm</span>
                <input v-model.number="form.from_year" type="number" min="2004" max="2100" class="input" />
              </label>
              <label class="field">
                <span>Đến năm (trống = cùng năm)</span>
                <input v-model.number="form.to_year" type="number" min="2004" max="2100" class="input" placeholder="—" />
              </label>
            </template>
            <template v-else>
              <label class="field">
                <span>Từ ngày</span>
                <input v-model="form.from_date" type="date" class="input" />
              </label>
              <label class="field">
                <span>Đến ngày</span>
                <input v-model="form.to_date" type="date" class="input" />
              </label>
            </template>
            <label class="field">
              <span>Tối đa bài (0 = không giới hạn)</span>
              <input v-model.number="form.max_posts" type="number" min="0" max="5000" class="input" />
            </label>
          </div>
          <div style="display: flex; gap: 18px; margin-top: 12px; flex-wrap: wrap">
            <label class="switch"><input v-model="form.reset_cursor" type="checkbox" /> Quét lại từ đầu (reset cursor)</label>
            <label class="switch"><input v-model="form.download_media" type="checkbox" /> Tải ảnh</label>
          </div>
          <label class="field" style="margin-top: 12px">
            <span>Ghi chú</span>
            <input v-model="form.note" class="input" placeholder="VD: quét sau đợt tuyển dụng, chạy tay…" />
          </label>
          <div style="display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap">
            <button class="btn primary" :disabled="submitting || !form.group_id" @click="submit">
              {{ submitting ? 'Đang gửi…' : 'Đặt yêu cầu quét' }}
            </button>
            <button class="btn" :disabled="submitting" @click="submitAllActive">Quét tất cả nhóm đang bật</button>
          </div>

          <div style="border-top: 1px solid var(--line); margin-top: 16px; padding-top: 14px">
            <h3 style="margin-bottom: 10px">Hoặc quét theo từ khóa (ô tìm kiếm Facebook)</h3>
            <label class="field">
              <span>Từ khóa</span>
              <input
                v-model="form.keyword"
                class="input"
                placeholder="VD: tuyển dụng kế toán"
                @keyup.enter="submitKeyword"
              />
            </label>
            <div style="display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; align-items: center">
              <button class="btn primary" :disabled="submitting || !form.keyword.trim()" @click="submitKeyword">
                Quét từ khóa
              </button>
              <label class="switch">
                <input v-model="form.search_all" type="checkbox" />
                Lấy mọi nguồn (group, Page, trang cá nhân)
              </label>
              <label class="field" style="min-width: 220px">
                <span>Mức khớp từ khóa (tiêu đề/nội dung)</span>
                <select v-model="form.keyword_match" class="select">
                  <option value="strict">Chặt: đủ ý từ khóa (khuyên dùng)</option>
                  <option value="normal">Vừa: thêm bài trùng 1 từ chính</option>
                  <option value="loose">Rộng: có bất kỳ từ nào</option>
                  <option value="off">Không kiểm tra từ khóa</option>
                </select>
              </label>
            </div>
           
          </div>
          <div v-if="form.timeMode !== 'days'" style="margin-top: 10px; font-size: 12px; color: var(--warn)">
            ⚠ Lọc theo năm/ngày chỉ áp dụng cho <strong>quét từ khóa</strong> (ô tìm kiếm Facebook). Quét feed
            của nhóm chỉ đi từ bài mới về cũ nên không tới được bài cũ.
          </div>
          
        </div>

        <div class="card">
          <div class="page-head" style="margin-bottom: 10px">
            <h2>Hàng đợi yêu cầu</h2>
            <div class="spacer"></div>
            <select v-model="filterStatus" class="select" style="width: 150px" @change="changeFilter">
              <option value="">Tất cả</option>
              <option value="queued">queued</option>
              <option value="running">running</option>
              <option value="done">done</option>
              <option value="error">error</option>
              <option value="canceled">canceled</option>
            </select>
          </div>
          <div v-if="!requests.length" class="empty">Chưa có yêu cầu nào.</div>
          <div v-else class="table-wrap" style="max-height: 480px">
            <table class="grid">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Nhóm</th>
                  <th>Ngày / bài</th>
                  <th>Trạng thái</th>
                  <th>Tạo</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in requests" :key="r.id">
                  <td>{{ r.id }}</td>
                  <td style="max-width: 260px">
                    <template v-if="r.keyword_id">
                      <span class="tag" style="margin-right: 4px">từ khóa</span>{{ r.keyword }}
                      <span v-if="r.search_all" class="tag ok" style="margin-left: 4px" title="Lấy cả bài Page / trang cá nhân">mọi nguồn</span>
                    </template>
                    <template v-else>{{ r.group_name }}</template>
                  </td>
                  <td style="white-space: nowrap">
                    <template v-if="r.date_from || r.date_to">
                      {{ fmtDate(r.date_from, false) }} → {{ fmtDate(lastDay(r.date_to), false) }}
                    </template>
                    <template v-else>{{ r.days || 'mặc định' }} ngày</template>
                    · {{ r.max_posts || '∞' }} bài
                    <span v-if="r.reset_cursor && !r.keyword_id" class="tag" style="margin-left: 4px">reset</span>
                    <span v-if="r.keyword_id && r.keyword_match && r.keyword_match !== 'strict'" class="tag" style="margin-left: 4px">
                      khớp: {{ r.keyword_match }}
                    </span>
                    <span v-if="r.review_count" class="tag info" style="margin-left: 4px">review ×{{ r.review_count }}</span>
                  </td>
                  <td>
                    <span class="tag" :class="statusTag[r.status]">{{ r.status }}</span>
                    <div v-if="r.error" style="font-size: 11px; color: #ff97a1; max-width: 240px" :title="r.error">
                      {{ r.error.slice(0, 80) }}
                    </div>
                  </td>
                  <td style="white-space: nowrap; font-size: 12px; color: var(--text-dim)">{{ fmtDate(r.created_at) }}</td>
                  <td style="text-align: right">
                    <button v-if="r.status === 'queued'" class="btn danger sm" @click="cancel(r)">Huỷ</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="card detail">
        <h2>Lịch sử quét (jobs)</h2>
        <div v-if="loading" class="loading">Đang tải…</div>
        <div v-else-if="!jobs.length" class="empty">Chưa có lần quét nào.</div>
        <div v-for="j in jobs" :key="j.id" style="padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 13px">
          <div style="display: flex; gap: 8px; align-items: center">
            <span class="tag" :class="statusTag[j.status]">{{ j.status }}</span>
            <strong style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{ j.group_name || '—' }}</strong>
            <span style="color: var(--text-dim); font-size: 12px">{{ fmtDate(j.started_at) }}</span>
          </div>
          <div style="color: var(--text-dim); font-size: 12px; margin-top: 2px">
            tìm thấy {{ fmtNum(j.posts_found) }} · mới {{ fmtNum(j.posts_new) }} · cập nhật {{ fmtNum(j.posts_updated) }}
            <span v-if="j.trigger_source"> · {{ j.trigger_source }}</span>
          </div>
          <div v-if="j.error" style="color: #ff97a1; font-size: 12px">{{ j.error }}</div>
        </div>
      </div>
    </div>
  </div>
</template>
