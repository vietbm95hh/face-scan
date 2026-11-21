import { defineStore } from 'pinia'
import { ref } from 'vue'

let seq = 0

export const useUiStore = defineStore('ui', () => {
  const toasts = ref([])
  /** Hộp thoại xác nhận hiện tại: { title, message, danger, resolve } */
  const dialog = ref(null)

  function toast(message, type = 'info', timeout = 3500) {
    const id = ++seq
    toasts.value.push({ id, message, type })
    setTimeout(() => dismiss(id), timeout)
  }

  function dismiss(id) {
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }

  function confirm({ title = 'Xác nhận', message = '', okText = 'Đồng ý', danger = false } = {}) {
    return new Promise((resolve) => {
      dialog.value = { title, message, okText, danger, resolve }
    })
  }

  function answerDialog(ok) {
    const d = dialog.value
    dialog.value = null
    d?.resolve(ok)
  }

  return { toasts, toast, dismiss, dialog, confirm, answerDialog }
})
