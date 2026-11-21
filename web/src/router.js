import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/posts' },
  { path: '/posts', name: 'posts', component: () => import('./views/PostsView.vue'), meta: { title: 'Bài viết' } },
  { path: '/groups', name: 'groups', component: () => import('./views/GroupsView.vue'), meta: { title: 'Nhóm' } },
  { path: '/scan', name: 'scan', component: () => import('./views/ScanView.vue'), meta: { title: 'Quét dữ liệu' } },
  { path: '/verify', name: 'verify', component: () => import('./views/VerifyView.vue'), meta: { title: 'Đối chiếu' } },
  { path: '/dashboard', name: 'dashboard', component: () => import('./views/DashboardView.vue'), meta: { title: 'Thống kê' } },
  { path: '/:pathMatch(.*)*', redirect: '/posts' },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
