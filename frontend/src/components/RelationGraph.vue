<template>
  <div>
    <Chart v-if="nodes.length" :option="option" :height="height" @node-click="onClick" />
    <el-empty v-else description="暂无关联数据" :image-size="60" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import Chart from './Chart.vue'

// 后端返回 {nodes:[{id,label,label_cn,value,center}], edges:[{source,target,type}]}
const props = withDefaults(defineProps<{ data: any; height?: number }>(), { height: 420 })
const emit = defineEmits<{ (e: 'account', id: string): void }>()

const CATS = ['Account', 'Device', 'IP', 'Phone', 'Address']
const CAT_CN: Record<string, string> = { Account: '账号', Device: '设备', IP: 'IP', Phone: '手机号', Address: '地址' }
const COLORS = ['#5470c6', '#ee6666', '#fac858', '#91cc75', '#73c0de']

const nodes = computed(() => props.data?.nodes || [])
const option = computed(() => ({
  color: COLORS,
  tooltip: { formatter: (p: any) => (p.dataType === 'node' ? `${CAT_CN[p.data.label] || p.data.label}：${p.data.value}` : p.data.type) },
  legend: { data: CATS.map((c) => CAT_CN[c]), bottom: 0 },
  series: [{
    type: 'graph', layout: 'force', roam: true, draggable: true,
    force: { repulsion: 220, edgeLength: [50, 110] },
    categories: CATS.map((c) => ({ name: CAT_CN[c] })),
    label: { show: true, position: 'right', fontSize: 11, formatter: (p: any) => short(p.data.value) },
    data: nodes.value.map((n: any) => ({
      ...n, name: n.id, category: Math.max(0, CATS.indexOf(n.label)),
      symbolSize: n.center ? 34 : n.label === 'Account' ? 20 : 14,
      itemStyle: n.center ? { borderColor: '#000', borderWidth: 2 } : undefined,
    })),
    links: (props.data?.edges || []).map((e: any) => ({ ...e, lineStyle: { opacity: 0.6 } })),
  }],
}))
const short = (s: string) => (s && s.length > 12 ? s.slice(0, 12) + '…' : s)
function onClick(d: any) {
  if (d?.label === 'Account') emit('account', d.value)
}
</script>