<template>
  <div class="page">
    <el-card header="仿真测试：在线上规则基础上追加 / 覆盖一条候选规则，回放历史事件（不影响线上）">
      <el-form :model="draft" label-width="80px">
        <div class="grid g4" style="gap: 0 16px">
          <el-form-item label="编号"><el-input v-model="draft.code" placeholder="同编号=覆盖" /></el-form-item>
          <el-form-item label="名称"><el-input v-model="draft.name" /></el-form-item>
          <el-form-item label="分值"><el-input-number v-model="draft.score" :min="-100" :max="100" /></el-form-item>
          <el-form-item label="回放上限"><el-input-number v-model="limit" :min="1" :max="20000" :step="500" /></el-form-item>
        </div>
        <el-form-item label="适用事件">
          <el-checkbox-group v-model="draft.event_types">
            <el-checkbox v-for="(v, k) in EVENT_TYPES" :key="k" :value="k">{{ v }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="表达式"><el-input v-model="draft.expression" type="textarea" :rows="2" class="mono" /></el-form-item>
        <el-form-item label="时间范围">
          <el-date-picker v-model="range" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ss" start-placeholder="开始" end-placeholder="结束" />
          <el-checkbox v-model="useDraft" style="margin-left: 16px">带上候选规则（不勾 = 只回放线上规则）</el-checkbox>
          <el-button type="primary" icon="VideoPlay" :loading="running" style="margin-left: 16px" @click="run">开始仿真</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="r">
      <div class="grid g4" style="margin-top: 12px">
        <el-card class="kpi"><div class="l">回放事件</div><div class="v">{{ r.total }}</div></el-card>
        <el-card class="kpi"><div class="l">结论变化 / 新增拦截</div><div class="v warn">{{ r.changed }} / {{ r.newly_flagged }}</div></el-card>
        <el-card class="kpi">
          <div class="l">已标注欺诈召回</div>
          <div class="v ok">{{ pct(r.labeled.recall) }}</div>
          <div class="muted">{{ r.labeled.fraud_caught }} / {{ r.labeled.fraud_total }}</div>
        </el-card>
        <el-card class="kpi">
          <div class="l">已标注正常误伤率</div>
          <div class="v bad">{{ pct(r.labeled.false_hit_rate) }}</div>
          <div class="muted">{{ r.labeled.false_hits }} / {{ r.labeled.normal_total }}</div>
        </el-card>
      </div>
      <div class="grid g2" style="margin-top: 12px">
        <el-card header="线上 vs 仿真 决策分布"><Chart :option="distOpt" :height="260" /></el-card>
        <el-card header="仿真规则命中">
          <el-table :data="r.rule_hits" size="small" max-height="260">
            <el-table-column label="规则" prop="code" /><el-table-column label="命中次数" prop="count" />
          </el-table>
        </el-card>
      </div>
      <el-card header="结论发生变化的样本" style="margin-top: 12px">
        <el-table :data="r.samples" size="small" max-height="420">
          <el-table-column label="事件时间" width="170"><template #default="{ row }">{{ fmtTime(row.event_time) }}</template></el-table-column>
          <el-table-column label="事件" width="90"><template #default="{ row }">{{ EVENT_TYPES[row.event_type] }}</template></el-table-column>
          <el-table-column label="账号" prop="user_id" width="120" />
          <el-table-column label="线上" width="120">
            <template #default="{ row }"><el-tag :type="DECISION_TAG[row.old.decision].type" size="small">{{ DECISION_TAG[row.old.decision].text }} {{ row.old.score }}</el-tag></template>
          </el-table-column>
          <el-table-column label="仿真" width="120">
            <template #default="{ row }"><el-tag :type="DECISION_TAG[row.new.decision].type" size="small">{{ DECISION_TAG[row.new.decision].text }} {{ row.new.score }}</el-tag></template>
          </el-table-column>
          <el-table-column label="人工标注" width="90">
            <template #default="{ row }"><span v-if="row.label" :class="row.label === 'fraud' ? 'bad' : 'ok'">{{ row.label === 'fraud' ? '欺诈' : '正常' }}</span></template>
          </el-table-column>
          <el-table-column label="仿真命中">
            <template #default="{ row }"><el-tag v-for="h in row.hits" :key="h.code" size="small" style="margin: 1px">{{ h.code }}+{{ h.score }}</el-tag></template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { post } from '@/api/http'
import { DECISION_TAG, EVENT_TYPES, fmtTime } from '@/api/dict'
import Chart from '@/components/Chart.vue'

const draft = reactive({
  code: 'R-101', name: '同设备领券多账号（候选）', event_types: ['coupon_receive'] as string[],
  expression: 'device_coupon_count_24h >= 3 AND is_new_account == true', score: 40,
})
const limit = ref(2000)
const range = ref<string[] | null>(null)
const useDraft = ref(true)
const running = ref(false)
const r = ref<any>(null)
const pct = (v: number | null) => (v == null ? '—' : (v * 100).toFixed(1) + '%')

async function run() {
  running.value = true
  try {
    r.value = await post('/simulate', {
      draft: useDraft.value ? draft : null, limit: limit.value,
      start: range.value?.[0] || null, end: range.value?.[1] || null,
    })
  } finally { running.value = false }
}
const distOpt = computed(() => {
  const ks = ['PASS', 'REVIEW', 'REJECT']
  return {
    tooltip: { trigger: 'axis' }, legend: {}, grid: { left: 40, right: 16, bottom: 30 },
    xAxis: { type: 'category', data: ks.map((k) => DECISION_TAG[k].text) }, yAxis: { type: 'value' },
    series: [
      { name: '线上', type: 'bar', data: ks.map((k) => r.value.old_distribution[k] || 0), itemStyle: { color: '#909399' } },
      { name: '仿真', type: 'bar', data: ks.map((k) => r.value.new_distribution[k] || 0), itemStyle: { color: '#409eff' } },
    ],
  }
})
</script>