import { createRouter, createWebHistory } from 'vue-router'
import Layout from '@/components/Layout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('@/views/Login.vue'), meta: { public: true } },
    {
      path: '/',
      component: Layout,
      redirect: '/dashboard',
      children: [
        { path: 'dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '态势大盘', icon: 'DataAnalysis' } },
        { path: 'simulator', component: () => import('@/views/EventSimulator.vue'), meta: { title: '事件接入', icon: 'Promotion' } },
        { path: 'decisions', component: () => import('@/views/Decisions.vue'), meta: { title: '决策回溯', icon: 'List' } },
        { path: 'cases', component: () => import('@/views/Cases.vue'), meta: { title: '案件审核', icon: 'Stamp' } },
        { path: 'cases/:no', component: () => import('@/views/CaseDetail.vue'), meta: { title: '案件详情', hidden: true } },
        { path: 'graph', component: () => import('@/views/GraphGangs.vue'), meta: { title: '关联图谱', icon: 'Share' } },
        { path: 'rules', component: () => import('@/views/Rules.vue'), meta: { title: '规则管理', icon: 'SetUp' } },
        { path: 'simulation', component: () => import('@/views/Simulation.vue'), meta: { title: '规则仿真', icon: 'Odometer' } },
        { path: 'namelists', component: () => import('@/views/NameLists.vue'), meta: { title: '名单管理', icon: 'Tickets' } },
        { path: 'audit', component: () => import('@/views/Audit.vue'), meta: { title: '审计日志', icon: 'Lock' } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach((to) => {
  if (!to.meta.public && !localStorage.getItem('risk_token')) return '/login'
})

export default router