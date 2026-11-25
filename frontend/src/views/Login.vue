<template>
  <div class="wrap">
    <el-card class="box">
      <h2>🛡️ 电商风控系统</h2>
      <p class="muted">实时特征 · 规则引擎 · 关联图谱 · 案件闭环</p>
      <el-form :model="form" @submit.prevent="submit">
        <el-form-item><el-input v-model="form.username" placeholder="账号" prefix-icon="User" /></el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码" prefix-icon="Lock" show-password />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" style="width: 100%">登录</el-button>
      </el-form>
      <div class="demo">
        演示账号（密码均为 123456）：
        <el-link v-for="u in demo" :key="u[0]" type="primary" @click="form.username = u[0]; form.password = '123456'">
          {{ u[0] }}（{{ u[1] }}）
        </el-link>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '@/stores/auth'

const form = reactive({ username: 'admin', password: '' })
const loading = ref(false)
const router = useRouter()
const demo = [['admin', '管理员'], ['strategist', '策略师'], ['auditor', '审核员']]
async function submit() {
  loading.value = true
  try {
    await useAuth().login(form.username, form.password)
    router.push('/')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.wrap { height: 100%; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #001529, #1d3a6b); }
.box { width: 380px; padding: 8px 12px; }
h2 { margin: 4px 0; }
.demo { margin-top: 16px; font-size: 12px; color: #8a919f; display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
</style>