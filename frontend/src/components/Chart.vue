<template><div ref="el" :style="{ height: height + 'px', width: '100%' }" /></template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'

const props = withDefaults(defineProps<{ option: any; height?: number }>(), { height: 300 })
const emit = defineEmits<{ (e: 'node-click', data: any): void }>()
const el = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null
const resize = () => chart?.resize()

onMounted(() => {
  chart = echarts.init(el.value!)
  chart.setOption(props.option)
  chart.on('click', (p: any) => emit('node-click', p.data))
  window.addEventListener('resize', resize)
})
watch(() => props.option, (o) => chart?.setOption(o, true), { deep: true })
onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
})
</script>