<template>
  <div class="page">
    <div class="toolbar">
      <el-select v-model="tt" placeholder="对象类型" clearable style="width: 130px" @change="search">
        <el-option label="案件" value="case" /><el-option label="规则" value="rule" /><el-option label="名单" value="namelist" />
      </el-select>
      <el-button type="primary" icon="Lock" :loading="verifying" @click="verify">校验哈希链完整性</el-button>
      <el-tag v-if="vr" :type="vr.ok ? 'success' : 'danger'" size="large">
        {{ vr.ok ? `完整：已校验 ${vr.checked} 条，未发现篡改` : `链断裂：第 ${vr.broken_at} 条（${vr.reason || '哈希不一致'}）` }}
      </el-tag>
    </div>
    <el-card>
      <el-table :data="rows" v-loading="loading" size="small">
        <el-table-column label="#" prop="id" width="70" />
        <el-table-column label="时间" width="170"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作人" prop="actor" width="110" />
        <el-table-column label="动作" prop="action" width="150" />
        <el-table-column label="对象" width="200"><template #default="{ row }">{{ row.target_type }} / {{ row.target_id }}</template></el-table-column>
        <el-table-column label="详情" show-overflow-tooltip><template #default="{ row }"><span class="mono">{{ JSON.stringify(row.detail) }}</span></template></el-table-column>
        <el-table-column label="哈希" width="160"><template #default="{ row }"><span class="mono muted">{{ row.hash.slice(0, 16) }}</span></template></el-table-column>
      </el-table>
      <el-pagination style="margin-top: 12px" layout="total, prev, pager, next" :total="total" :page-size="50"
                     v-model:current-page="page" @current-change="load" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { get } from '@/api/http'
import { fmtTime } from '@/api/dict'

const tt = ref('')
const page = ref(1)
const rows = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const verifying = ref(false)
const vr = ref<any>(null)
async function load() {
  loading.value = true
  try {
    const r = await get('/audit', { page: page.value, size: 50, ...(tt.value ? { target_type: tt.value } : {}) })
    rows.value = r.items
    total.value = r.total
  } finally { loading.value = false }
}
function search() { page.value = 1; load() }
async function verify() {
  verifying.value = true
  try { vr.value = await get('/audit/verify') } finally { verifying.value = false }
}
onMounted(load)
</script>