<template>
  <div v-if="t">
    <el-alert :title="t.explain" :type="alertType" :closable="false" show-icon style="margin-bottom: 12px" />
    <el-descriptions :column="3" border size="small">
      <el-descriptions-item label="结论">
        <el-tag :type="DECISION_TAG[dec.decision].type">{{ DECISION_TAG[dec.decision].text }}</el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="风险分"><b>{{ dec.score }}</b></el-descriptions-item>
      <el-descriptions-item label="决策依据">{{ BY[dec.decided_by] }}</el-descriptions-item>
      <el-descriptions-item label="事件">{{ EVENT_TYPES[dec.event_type] }}</el-descriptions-item>
      <el-descriptions-item label="账号">{{ dec.user_id }}</el-descriptions-item>
      <el-descriptions-item label="事件时间">{{ fmtTime(dec.event_time) }}</el-descriptions-item>
      <el-descriptions-item label="规则版本"><span class="mono">{{ dec.rule_version }}</span></el-descriptions-item>
      <el-descriptions-item label="耗时">{{ dec.latency_ms }} ms</el-descriptions-item>
      <el-descriptions-item label="案件">
        <el-link v-if="t.case_no" type="primary" @click="$router.push('/cases/' + t.case_no)">{{ t.case_no }}</el-link>
        <span v-else class="muted">未建案</span>
      </el-descriptions-item>
      <el-descriptions-item label="幂等键" :span="3"><span class="mono">{{ dec.idem_key }}</span></el-descriptions-item>
    </el-descriptions>

    <h4>命中规则与单项分值</h4>
    <el-empty v-if="!dec.hits.length && !dec.list_hits.length" description="未命中任何规则" :image-size="50" />
    <el-card v-for="h in dec.hits" :key="h.code" shadow="never" class="hit">
      <div class="hit-head">
        <span><el-tag size="small">{{ h.code }}</el-tag> <b>{{ h.name }}</b></span>
        <el-tag type="danger" effect="dark">+{{ h.score }}</el-tag>
      </div>
      <div class="mono muted" style="margin: 6px 0">{{ h.expression }}</div>
      <div v-if="wf(h.code)?.minimal_change" class="cf">
        <el-tag size="small" type="warning" effect="plain">最小改动</el-tag> {{ wf(h.code).minimal_change.text }}
      </div>
      <div v-else-if="wf(h.code)?.matched && !wf(h.code)?.critical?.length" class="cf muted">
        多个分支同时成立，单独改任何一个条件都不会让这条规则失效
      </div>
      <el-table v-if="h.conditions.length" :data="h.conditions" size="small">
        <el-table-column label="条件" prop="condition" class-name="mono" />
        <el-table-column label="实际值" width="140">
          <template #default="{ row }"><span class="mono">{{ row.actual ?? 'null' }}</span></template>
        </el-table-column>
        <el-table-column label="关键" width="70">
          <template #default="{ row }">
            <el-tooltip v-if="isCritical(h.code, row)" content="单独翻转这个条件，规则结果就会变" placement="top">
              <el-tag size="small" type="warning">关键</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="满足" width="70">
          <template #default="{ row }">
            <el-icon :class="row.matched ? 'ok' : 'bad'"><component :is="row.matched ? 'CircleCheck' : 'CircleClose'" /></el-icon>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
    <el-card v-for="(l, i) in dec.list_hits" :key="'l' + i" shadow="never" class="hit">
      <el-tag :type="l.list_type === 'black' ? 'danger' : l.list_type === 'white' ? 'success' : 'warning'">{{ LIST_TYPES[l.list_type] }}</el-tag>
      {{ ENTITY_TYPES[l.entity_type] }} = <span class="mono">{{ l.value }}</span>
      <span class="muted">{{ l.reason }}</span>
    </el-card>

    <template v-if="w?.applicable">
      <h4>反事实：改哪里结论会变</h4>
      <el-alert :title="w.summary" type="info" :closable="false" style="margin-bottom: 8px" />
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="降一档">
          <span v-if="w.margin.down">再少 <b>{{ w.margin.down.need }}</b> 分 → {{ DECISION_TAG[w.margin.down.to].text }}</span>
          <span v-else class="muted">已是最低档</span>
        </el-descriptions-item>
        <el-descriptions-item label="升一档">
          <span v-if="w.margin.up">再多 <b>{{ w.margin.up.need }}</b> 分 → {{ DECISION_TAG[w.margin.up.to].text }}</span>
          <span v-else class="muted">已是最高档</span>
        </el-descriptions-item>
      </el-descriptions>
      <el-table v-if="w.rules.length" :data="w.rules" size="small" style="margin-top: 8px">
        <el-table-column label="去掉这条规则" width="200">
          <template #default="{ row }"><el-tag size="small">{{ row.code }}</el-tag> <span class="muted">+{{ row.score }}</span></template>
        </el-table-column>
        <el-table-column label="分数">
          <template #default="{ row }">{{ w.score }} → <b>{{ row.without.score }}</b></template>
        </el-table-column>
        <el-table-column label="结论" width="160">
          <template #default="{ row }">
            <el-tag size="small" :type="DECISION_TAG[row.without.decision].type">{{ DECISION_TAG[row.without.decision].text }}</el-tag>
            <span v-if="row.without.changes" class="bad"> 会变</span>
            <span v-else class="muted"> 不变</span>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <h4>特征快照（决策当时的值）</h4>
    <el-table :data="featRows" size="small" max-height="320" border>
      <el-table-column label="特征" prop="k" class-name="mono" width="230" />
      <el-table-column label="值" class-name="mono">
        <template #default="{ row }">{{ row.v ?? 'null' }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { DECISION_TAG, ENTITY_TYPES, EVENT_TYPES, LIST_TYPES, fmtTime } from '@/api/dict'

const props = defineProps<{ t: any }>()
const BY: Record<string, string> = { rules: '规则打分', whitelist: '白名单', blacklist: '黑名单' }
const dec = computed(() => props.t?.decision)
const alertType = computed(() => ({ PASS: 'success', REVIEW: 'warning', REJECT: 'error' } as any)[dec.value?.decision])
const w = computed(() => props.t?.whatif)
const wf = (code: string) => w.value?.rules?.find((r: any) => r.code === code)
// 老决策落库时条件追踪里还没有 cid，按条件原文对齐
const isCritical = (code: string, row: any) =>
  !!wf(code)?.conditions?.find((c: any) => (row.cid != null ? c.cid === row.cid : c.condition === row.condition))?.critical
const featRows = computed(() => Object.entries(dec.value?.features || {}).map(([k, v]) => ({ k, v })))
</script>

<style scoped>
.hit { margin-bottom: 8px; }
.hit-head { display: flex; justify-content: space-between; align-items: center; }
h4 { margin: 16px 0 8px; }
.cf { margin: 4px 0 8px; font-size: 13px; }
</style>