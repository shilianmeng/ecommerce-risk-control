<template>
  <div class="page" v-loading="loading">
    <div class="toolbar">
      <el-radio-group v-model="days" @change="load">
        <el-radio-button :value="1">近 1 天</el-radio-button>
        <el-radio-button :value="3">近 3 天</el-radio-button>
        <el-radio-button :value="7">近 7 天</el-radio-button>
        <el-radio-button :value="30">近 30 天</el-radio-button>
      </el-radio-group>
      <span class="muted" v-if="d">图谱：{{ d.graph_backend }} · 规则版本 {{ d.rule_version }}</span>
      <el-button icon="Refresh" @click="load" style="margin-left: auto">刷新</el-button>
    </div>
    <template v-if="d">
      <div class="grid g4">
        <el-card class="kpi"><div class="l">决策总量</div><div class="v">{{ d.total }}</div></el-card>
        <el-card class="kpi"><div class="l">拒绝率</div><div class="v bad">{{ pct(d.reject_rate) }}</div></el-card>
        <el-card class="kpi"><div class="l">拦截率（拒绝 + 人审）</div><div class="v warn">{{ pct(d.intercept_rate) }}</div></el-card>
        <el-card class="kpi">
          <div class="l">决策耗时 平均 / P95</div><div class="v">{{ d.latency_ms.avg }} / {{ d.latency_ms.p95 }} ms</div>
        </el-card>
      </div>
      <div class="grid g2" style="margin-top: 12px">
        <el-card header="决策趋势"><Chart v-if="d.total" :option="trendOpt" /><el-empty v-else :image-size="80" description="该时间段内没有决策记录" /></el-card>
        <el-card header="风险分布"><Chart v-if="d.total" :option="distOpt" /><el-empty v-else :image-size="80" description="该时间段内没有决策记录" /></el-card>
        <el-card header="风险分值分布"><Chart v-if="d.total" :option="histOpt" /><el-empty v-else :image-size="80" description="该时间段内没有决策记录" /></el-card>
        <el-card header="规则命中排行 Top10"><Chart v-if="d.total" :option="rankOpt" /><el-empty v-else :image-size="80" description="该时间段内没有决策记录" /></el-card>
        <el-card header="各事件类型决策"><Chart v-if="d.total" :option="typeOpt" /><el-empty v-else :image-size="80" description="该时间段内没有决策记录" /></el-card>
        <el-card header="案件状态">
          <div class="grid g4" style="padding: 30px 0">
            <div v-for="(v, k) in CASE_STATUS" :key="k" class="kpi" style="text-align: center">
              <div class="l">{{ v.text }}</div><div class="v">{{ d.cases[k] || 0 }}</div>
            </div>
          </div>
          <el-button type="primary" plain style="width: 100%" @click="$router.push('/cases')">进入案件审核</el-button>
        </el-card>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Chart from '@/components/Chart.vue'
import { get } from '@/api/http'
import { CASE_STATUS, EVENT_TYPES } from '@/api/dict'

const days = ref(7)
const d = ref<any>(null)
const loading = ref(false)
const C = { PASS: '#18a058', REVIEW: '#e6a23c', REJECT: '#d03050' }
const CN = { PASS: '通过', REVIEW: '人审', REJECT: '拒绝' }
const pct = (v: number) => (v * 100).toFixed(1) + '%'

async function load() {
  loading.value = true
  try { d.value = await get('/dashboard', { days: days.value }) } finally { loading.value = false }
}
onMounted(load)

const stackSeries = (rows: any[]) =>
  (['PASS', 'REVIEW', 'REJECT'] as const).map((k) => ({
    name: CN[k], type: 'bar', stack: 't', itemStyle: { color: C[k] }, data: rows.map((r) => r[k] || 0),
  }))
const trendOpt = computed(() => ({
  tooltip: { trigger: 'axis' }, legend: {}, grid: { left: 40, right: 16, bottom: 30 },
  xAxis: { type: 'category', data: d.value.trend.map((t: any) => t.time) }, yAxis: { type: 'value' },
  series: stackSeries(d.value.trend),
}))
const distOpt = computed(() => ({
  tooltip: { trigger: 'item' }, legend: { bottom: 0 },
  series: [{
    type: 'pie', radius: ['45%', '70%'], label: { formatter: '{b}\n{c}（{d}%）' },
    data: (['PASS', 'REVIEW', 'REJECT'] as const).map((k) => ({ name: CN[k], value: d.value.distribution[k], itemStyle: { color: C[k] } })),
  }],
}))
const histOpt = computed(() => ({
  tooltip: { trigger: 'axis' }, grid: { left: 40, right: 16, bottom: 30 },
  xAxis: { type: 'category', data: d.value.score_histogram.map((b: any) => b.range) }, yAxis: { type: 'log', logBase: 10, min: 1 },
  series: [{
    type: 'bar', data: d.value.score_histogram.map((b: any, i: number) => ({
      value: b.count || null, itemStyle: { color: i >= 8 ? C.REJECT : i >= 6 ? C.REVIEW : C.PASS },
    })),
  }],
}))
const rankOpt = computed(() => {
  const r = [...d.value.rule_ranking].reverse()
  return {
    tooltip: {}, grid: { left: 150, right: 30, bottom: 20, top: 10 },
    xAxis: { type: 'value' }, yAxis: { type: 'category', data: r.map((x: any) => `${x.code} ${x.name}`) },
    series: [{ type: 'bar', data: r.map((x: any) => x.count), itemStyle: { color: '#5470c6' }, label: { show: true, position: 'right' } }],
  }
})
const typeOpt = computed(() => {
  const keys = Object.keys(d.value.by_event_type)
  const rows = keys.map((k) => d.value.by_event_type[k])
  return {
    tooltip: { trigger: 'axis' }, legend: {}, grid: { left: 40, right: 16, bottom: 30 },
    xAxis: { type: 'category', data: keys.map((k) => EVENT_TYPES[k] || k) }, yAxis: { type: 'value' },
    series: stackSeries(rows),
  }
})
</script>