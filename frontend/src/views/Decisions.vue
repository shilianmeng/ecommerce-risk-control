<template>
  <div class="page">
    <div class="toolbar">
      <el-select v-model="q.decision" placeholder="结论" clearable style="width: 110px" @change="search">
        <el-option v-for="(v, k) in DECISION_TAG" :key="k" :label="v.text" :value="k" />
      </el-select>
      <el-select v-model="q.event_type" placeholder="事件类型" clearable style="width: 130px" @change="search">
        <el-option v-for="(v, k) in EVENT_TYPES" :key="k" :label="v" :value="k" />
      </el-select>
      <el-input v-model="q.user_id" placeholder="账号 ID" clearable style="width: 160px" @change="search" />
      <el-button type="primary" icon="Search" @click="search">查询</el-button>
    </div>
    <el-card>
      <el-table :data="rows" v-loading="loading" @row-click="open" style="cursor: pointer">
        <el-table-column label="ID" prop="id" width="80" />
        <el-table-column label="事件时间" width="170"><template #default="{ row }">{{ fmtTime(row.event_time) }}</template></el-table-column>
        <el-table-column label="事件" width="100"><template #default="{ row }">{{ EVENT_TYPES[row.event_type] }}</template></el-table-column>
        <el-table-column label="账号" prop="user_id" width="110" />
        <el-table-column label="结论" width="90">
          <template #default="{ row }"><el-tag :type="DECISION_TAG[row.decision].type">{{ DECISION_TAG[row.decision].text }}</el-tag></template>
        </el-table-column>
        <el-table-column label="风险分" prop="score" width="80" />
        <el-table-column label="命中规则">
          <template #default="{ row }">
            <el-tag v-for="c in row.hit_codes" :key="c" size="small" style="margin-right: 4px">{{ c }}</el-tag>
            <span v-if="row.decided_by !== 'rules'" class="muted">{{ row.decided_by === 'blacklist' ? '黑名单' : '白名单' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时(ms)" prop="latency_ms" width="90" />
      </el-table>
      <el-pagination style="margin-top: 12px" layout="total, prev, pager, next" :total="total" :page-size="20"
                     v-model:current-page="q.page" @current-change="load" />
    </el-card>
    <el-drawer v-model="show" title="决策回溯" size="720px"><DecisionTrace :t="trace" /></el-drawer>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { get } from '@/api/http'
import { DECISION_TAG, EVENT_TYPES, fmtTime } from '@/api/dict'
import DecisionTrace from '@/components/DecisionTrace.vue'

const route = useRoute()
const q = reactive({ decision: '', event_type: '', user_id: (route.query.user_id as string) || '', page: 1 })
const rows = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const show = ref(false)
const trace = ref<any>(null)

async function load() {
  loading.value = true
  try {
    const params: any = { page: q.page, size: 20 }
    for (const k of ['decision', 'event_type', 'user_id'] as const) if (q[k]) params[k] = q[k]
    const r = await get('/decisions', params)
    rows.value = r.items
    total.value = r.total
  } finally { loading.value = false }
}
function search() { q.page = 1; load() }
async function open(row: any) {
  trace.value = await get(`/decisions/${row.id}`)
  show.value = true
}
onMounted(load)
</script>