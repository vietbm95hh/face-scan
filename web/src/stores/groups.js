import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api'

export const useGroupsStore = defineStore('groups', () => {
  const groups = ref([])
  const loading = ref(false)
  const error = ref('')
  const health = ref(null)

  async function fetchGroups(force = false) {
    if (groups.value.length && !force) return groups.value
    loading.value = true
    error.value = ''
    try {
      groups.value = await api.groups()
    } catch (e) {
      error.value = e.message
    } finally {
      loading.value = false
    }
    return groups.value
  }

  async function checkHealth() {
    try {
      health.value = await api.health()
      health.value.ok = true
    } catch (e) {
      health.value = { ok: false, error: e.message }
    }
    return health.value
  }

  function groupName(id) {
    return groups.value.find((g) => g.id === id)?.name || `Group #${id}`
  }

  return { groups, loading, error, health, fetchGroups, checkHealth, groupName }
})
