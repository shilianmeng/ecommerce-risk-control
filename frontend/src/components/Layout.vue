<template>
  <el-container style="height: 100%">
    <el-aside width="200px" class="aside">
      <div class="logo">🛡️ 电商风控系统</div>
      <el-menu :default-active="active" router background-color="#001529" text-color="#c0c4cc" active-text-color="#fff">
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon><span>{{ m.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span class="title">{{ route.meta.title }}</span>
        <span>
          <el-tag size="small" type="info" style="margin-right: 8px">{{ auth.user?.role_cn }}</el-tag>
          {{ auth.user?.name }}
          <el-button link type="primary" style="margin-left: 12px" @click="logout">退出</el-button>
        </span>
      </el-header>
      <el-main style="padding: 0"><router-view /></el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const menus = router.options.routes
  .find((r) => r.path === '/')!
  .children!.filter((c) => !c.meta?.hidden)
  .map((c) => ({ path: '/' + c.path, title: c.meta!.title as string, icon: c.meta!.icon as string }))
const active = computed(() => '/' + route.path.split('/')[1])
function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.aside { background: #001529; }
.logo { color: #fff; font-size: 17px; font-weight: 600; padding: 18px 16px; }
.el-menu { border-right: none; }
.header { background: #fff; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #ebeef5; }
.title { font-size: 16px; font-weight: 600; }
</style>