<template>
  <div class="page" v-loading="loading">
    <template v-if="d">
      <el-page-header @back="$router.push('/cases')" style="margin-bottom: 12px">
        <template #content>
          <span class="mono">{{ d.case.case_no }}</span>
          <el-tag :type="CASE_STATUS[d.case.status].type" style="margin-left: 8px">{{ CASE_STATUS[d.case.status].text }}</el-tag>
          <el-tag :type="DECISION_TAG[d.case.decision].type" style="margin-left: 4px">{{ DECISION_TAG[d.case.decision].text }} · {{ d.case.score }} 分</el-tag>
        </template>
        <template #extra>
          <template v-if="auth.can('auditor')">
            <el-button v-if="d.case.status === 'PENDING'" type="primary" @click="act('claim')">认领</el-button>
            <el-button v-if="d.case.status === 'DISPOSED'" @click="act('archive')">归档</el-button>
          </template>
          <span v-else class="muted">只有审核员可以处置案件</span>
        </template>
      </el-page-header>

      <div class="grid" style="grid-template-columns: 1.1fr 1fr">
        <div>
          <el-card header="账号画像">
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="账号">{{ d.case.user_id }}</el-descriptions-item>
              <el-descriptions-item label="事件">{{ EVENT_TYPES[d.case.event_type] }}</el-descriptions-item>
              <el-descriptions-item label="设备">{{ d.event?.device_id }}</el-descriptions-item>
              <el-descriptions-item label="IP">{{ d.event?.ip }}</el-descriptions-item>
              <el-descriptions-item label="手机号">{{ d.event?.phone }}</el-descriptions-item>
              <template v-if="d.case.event_type === 'after_sale_apply'">
                <el-descriptions-item label="退款金额">{{ d.event?.payload?.refund_amount ?? '-' }}</el-descriptions-item>
                <el-descriptions-item label="订单号"><span class="mono">{{ d.event?.order_id || '-' }}</span></el-descriptions-item>
                <el-descriptions-item label="退款原因" :span="2">{{ REFUND_REASONS[d.event?.payload?.refund_reason] || d.event?.payload?.refund_reason || '-' }}</el-descriptions-item>
              </template>
              <el-descriptions-item v-else label="金额">{{ d.event?.amount }}</el-descriptions-item>
              <el-descriptions-item label="地址" :span="2">{{ d.event?.address }}</el-descriptions-item>
              <el-descriptions-item label="历史决策" :span="2">
                <el-tag v-for="(n, k) in d.profile.decision_distribution" :key="k" :type="DECISION_TAG[k]?.type" style="margin-right: 6px">
                  {{ DECISION_TAG[k]?.text }} {{ n }}
                </el-tag>
                <el-link type="primary" @click="$router.push({ path: '/decisions', query: { user_id: d.case.user_id } })">查看全部</el-link>
              </el-descriptions-item>
              <el-descriptions-item label="图谱关联账号" :span="2">
                <span :class="d.profile.related_accounts.length >= 3 ? 'bad' : ''">{{ d.profile.related_accounts.length }} 个</span>
                <span class="muted mono"> {{ d.profile.related_accounts.slice(0, 12).join('、') }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="历史案件" :span="2">
                <el-link v-for="c in d.profile.history_cases" :key="c.case_no" type="primary" style="margin-right: 8px"
                         @click="$router.push('/cases/' + c.case_no)">{{ c.case_no }}</el-link>
                <span v-if="!d.profile.history_cases.length" class="muted">无</span>
              </el-descriptions-item>
            </el-descriptions>
          </el-card>
          <el-card header="决策回溯（命中规则 · 条件实际值 · 特征快照）" style="margin-top: 12px">
            <DecisionTrace :t="trace" />
          </el-card>
        </div>
        <div>
          <el-card v-if="d.case.status === 'IN_REVIEW' && auth.can('auditor')" header="处置">
            <el-form :model="form" label-width="70px">
              <el-form-item label="结论">
                <el-radio-group v-model="form.conclusion">
                  <el-radio value="fraud">确认欺诈</el-radio><el-radio value="normal">误报，正常用户</el-radio>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="动作">
                <el-checkbox-group v-model="form.actions">
                  <el-checkbox v-for="(v, k) in ACTIONS" :key="k" :value="k">{{ v }}</el-checkbox>
                </el-checkbox-group>
              </el-form-item>
              <el-form-item label="备注"><el-input v-model="form.note" type="textarea" :rows="3" /></el-form-item>
              <el-button type="danger" @click="dispose">提交处置</el-button>
              <span class="muted" style="margin-left: 8px">拉黑/封禁会立即写入名单，下一笔事件生效</span>
            </el-form>
          </el-card>
          <el-card v-if="d.case.conclusion" header="处置结果" style="margin-bottom: 12px">
            <p>结论：<b :class="d.case.conclusion === 'fraud' ? 'bad' : 'ok'">{{ d.case.conclusion === 'fraud' ? '确认欺诈' : '正常用户' }}</b></p>
            <p>动作：<el-tag v-for="a in d.case.actions" :key="a" size="small" style="margin-right: 4px">{{ ACTIONS[a] || a }}</el-tag></p>
            <p v-if="d.case.note">备注：{{ d.case.note }}</p>
          </el-card>
          <el-card header="RAG 审核辅助（规则解释 · 相似案例 · 处置建议）" style="margin-top: 12px">
            <el-button :loading="assisting" icon="MagicStick" @click="assist">生成审核建议</el-button>
            <template v-if="advice">
              <el-tag size="small" style="margin-left: 8px">{{ advice.mode === 'llm' ? '大模型生成' : '检索模式' }}</el-tag>
              <div class="advice">{{ advice.answer }}</div>
              <div class="muted" style="margin-top: 6px">引用：</div>
              <el-collapse>
                <el-collapse-item v-for="r in advice.references" :key="r.doc_code" :title="`[${r.doc_code}] ${r.title}`">
                  <div style="white-space: pre-wrap">{{ r.content }}</div>
                </el-collapse-item>
              </el-collapse>
            </template>
          </el-card>
          <el-card header="关联图谱（2 跳）" style="margin-top: 12px">
            <RelationGraph :data="d.graph" :height="420" @account="(u: string) => $router.push({ path: '/graph', query: { user: u } })" />
          </el-card>
          <el-card header="审计时间线" style="margin-top: 12px">
            <el-timeline>
              <el-timeline-item v-for="a in d.audit" :key="a.id" :timestamp="fmtTime(a.created_at)">
                <b>{{ AUDIT_CN[a.action] || a.action }}</b> · {{ a.actor }}
                <div class="muted mono" style="font-size: 12px">{{ a.hash.slice(0, 16) }}…</div>
              </el-timeline-item>
            </el-timeline>
          </el-card>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { get, post } from '@/api/http'
import { useAuth } from '@/stores/auth'
import { ACTIONS, CASE_STATUS, DECISION_TAG, EVENT_TYPES, REFUND_REASONS, fmtTime } from '@/api/dict'
import DecisionTrace from '@/components/DecisionTrace.vue'
import RelationGraph from '@/components/RelationGraph.vue'

const AUDIT_CN: Record<string, string> = {
  'case.create': '自动建案', 'case.claim': '认领', 'case.dispose': '处置', 'case.archive': '归档',
}
const route = useRoute()
const auth = useAuth()
const d = ref<any>(null)
const loading = ref(false)
const form = reactive({ conclusion: 'fraud', actions: ['block_order'] as string[], note: '' })
const advice = ref<any>(null)
const assisting = ref(false)
const trace = computed(() => d.value && { decision: d.value.decision, case_no: d.value.case.case_no, explain: d.value.explain || explainOf(d.value.decision), whatif: d.value.whatif })

function explainOf(dec: any) {
  if (!dec) return ''
  const h = dec.hits.map((x: any) => `${x.code}(+${x.score})`).join(' + ')
  return `风险分 ${dec.score}：${h || '无规则命中'}`
}
async function load() {
  loading.value = true
  try { d.value = await get(`/cases/${route.params.no}`) } finally { loading.value = false }
}
async function act(op: string) {
  await post(`/cases/${route.params.no}/${op}`)
  ElMessage.success('操作成功')
  load()
}
async function dispose() {
  const r = await post(`/cases/${route.params.no}/dispose`, form)
  ElMessage.success(r.namelist_added?.length ? `已处置，写入名单：${r.namelist_added.join('；')}` : '已处置')
  load()
}
async function assist() {
  assisting.value = true
  try { advice.value = await post(`/cases/${route.params.no}/assist`, undefined, { timeout: 120000 }) } finally { assisting.value = false }
}
watch(() => route.params.no, (v) => { if (v) { advice.value = null; load() } })
onMounted(load)
</script>

<style scoped>
.advice { white-space: pre-wrap; background: #f6f8fb; border-radius: 6px; padding: 10px; margin-top: 10px; line-height: 1.7; }
</style>