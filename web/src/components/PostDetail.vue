<script setup>
import { ref, watch } from 'vue'
import { mediaUrl } from '../api'
import { fmtDate, fmtNum, initials } from '../format'

const props = defineProps({ post: { type: Object, required: true } })
const emit = defineEmits(['toggle-read', 'save-note', 'delete', 'copy', 'close'])

const noteDraft = ref('')
const lightbox = ref(null)

watch(
  () => props.post,
  (p) => {
    noteDraft.value = p?.note || ''
  },
  { immediate: true },
)
</script>

<template>
  <div class="card">
    <div class="detail-head">
      <span class="avatar" style="width: 40px; height: 40px; font-size: 14px">{{ initials(post.author_name) }}</span>
      <div style="min-width: 0; flex: 1">
        <div style="font-weight: 600">{{ post.author_name || 'Ẩn danh' }}</div>
        <div style="font-size: 12px; color: var(--text-dim)">
          <template v-if="post.source_type === 'page'">📄 {{ post.source_name || post.source_fb_id }}</template>
          <template v-else>{{ post.group_name || post.source_name }}</template>
          · {{ fmtDate(post.posted_at) }}
        </div>
      </div>
      <button class="btn ghost sm" title="Đóng" @click="emit('close')">✕</button>
    </div>

    <div class="meta" style="margin-top: 10px">
      <span class="tag">#{{ post.id }}</span>
      <span class="tag" :class="post.is_read ? 'ok' : 'info'">{{ post.is_read ? 'đã đọc' : 'chưa đọc' }}</span>
      <span v-if="post.fb_post_id" class="tag">fb {{ post.fb_post_id }}</span>
      <span>👍 {{ fmtNum(post.like_count) }}</span>
      <span>💬 {{ fmtNum(post.comment_count) }}</span>
      <span>↗ {{ fmtNum(post.share_count) }}</span>
    </div>

    <h2 v-if="post.title" style="margin-top: 12px">{{ post.title }}</h2>
    <div class="content">{{ post.content || '(không có nội dung)' }}</div>

    <div v-if="post.media?.length" class="gallery" style="margin-bottom: 12px">
      <img
        v-for="m in post.media"
        :key="m.id"
        :src="mediaUrl(m.url)"
        :title="m.kind"
        loading="lazy"
        alt=""
        @click="lightbox = mediaUrl(m.url)"
      />
    </div>

    <div class="toolbar" style="margin-bottom: 0">
      <a v-if="post.permalink" :href="post.permalink" target="_blank" rel="noopener" class="btn sm">Mở trên Facebook ↗</a>
      <button v-if="post.permalink" class="btn ghost sm" @click="emit('copy', post.permalink)">Copy link</button>
      <button class="btn sm" @click="emit('toggle-read', post, $event)">
        {{ post.is_read ? 'Đánh dấu chưa đọc' : 'Đánh dấu đã đọc' }}
      </button>
      <button class="btn danger sm" @click="emit('delete', post)">Xoá bài</button>
    </div>

    <div style="margin-top: 16px">
      <div class="field">
        <span>Ghi chú nội bộ</span>
        <textarea v-model="noteDraft" class="textarea" rows="3" placeholder="VD: đã liên hệ, giá tốt, bỏ qua…"></textarea>
      </div>
      <div style="display: flex; justify-content: flex-end; margin-top: 8px">
        <button class="btn primary sm" :disabled="noteDraft === (post.note || '')" @click="emit('save-note', post, noteDraft)">
          Lưu ghi chú
        </button>
      </div>
    </div>

    <div v-if="post.scraped_at" style="margin-top: 12px; font-size: 12px; color: var(--text-dim)">
      Quét lúc {{ fmtDate(post.scraped_at) }}<template v-if="post.updated_by"> · sửa bởi {{ post.updated_by }}</template>
    </div>

    <div v-if="lightbox" class="lightbox" @click="lightbox = null">
      <img :src="lightbox" alt="" />
    </div>
  </div>
</template>
