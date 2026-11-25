<template>
  <div class="page">
    <div class="toolbar">
      <el-radio-group v-model="q.status" @change="search">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button v-for="(v, k) in CASE_STATUS" :key="k" :value="k">{{ v.text }}</el-radio-button>
      </el-radio-group>
      <el-select v-model="q.decision" placeholder="决策结论" clearable style="width: 110px" @change="search">
        <el-option label="拒绝" value="REJECT" /><el-option label="人审" value="REVIEW" />
      </el-select>
      <el-input v-model="q.user_id" placeholder="账号 ID" clearable style="width: 160px" @change="search" />
    </div>
    <el-card>
      <el-table :data="rows" v-loading="loading" @row-click="(r: any) => $router.push('/cases/' + r.case_no)" style="cursor: pointer">
        <el-table-column label="案件号" prop="case_no" width="170" class-name="mono" />
        <el-table-column label="创建时间" width="170"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        <el-table-column label="事件" width="100"><template #default="{ row }">{{ EVENT_TYPES[row.event_type] }}</template></el-table-column>
        <el-table-column label="账号" prop="user_id" />
        <el-table-column label="决策" width="90">
          <template #default="{ row }"><el-tag :type="DECISION_TAG[row.decision].type">{{ DECISION_TAG[row.decision].text }}</el-tag></template>
        </el-table-column>
        <el-table-column label="风险分" prop="score" width="80" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><el-tag :type="CASE_STATUS[row.status].type" effect="plain">{{ CASE_STATUS[row.status].text }}</el-tag></template>
        </el-table-column>
        <el-table-column label="审核人" prop="assignee" width="110" />
        <el-table-column label="结论" width="90">
          <template #default="{ row }">
            <span v-if="row.conclusion" :class="row.conclusion === 'fraud' ? 'bad' : 'ok'">{{ row.conclusion === 'fraud' ? '欺诈' : '正常' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination style="margin-top: 12px" layout="total, prev, pager, next" :total="total" :page-size="20"
                     v-model:current-page="q.page" @current-change="load" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { get } from '@/api/http'
import { CASE_STATUS, DECISION_TAG, EVENT_TYPES, fmtTime } from '@/api/dict'

const q = reactive({ status: 'PENDING', decision: '', user_id: '', page: 1 })
const rows = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
async function load() {
  loading.value = true
  try {
    const p: any = { page: q.page, size: 20 }
    for (const k of ['status', 'decision', 'user_id'] as const) if (q[k]) p[k] = q[k]
    const r = await get('/cases', p)
    rows.value = r.items
    total.value = r.total
  } finally { loading.value = false }
}
function search() { q.page = 1; load() }
onMounted(load)
</script>